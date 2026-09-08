import json
import random
import re
import subprocess
import sys
import time
import urllib.request
import winsound
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

VERSION = "1.0.4"
UPDATE_REPO = "benfoster231/ProlificWatcher"

STUDIES_URL = "https://app.prolific.com/studies"

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent

STORAGE_STATE_PATH = APP_DIR / "storage_state.json"
LOG_PATH = APP_DIR / "watcher.log"


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config():
    config_path = APP_DIR / "config.json"
    if not config_path.exists():
        print(f"config.json not found next to the program at {config_path}")
        print("Copy config.json into this folder and edit it, then run again.")
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def alert(cfg):
    if cfg.get("sound_alert", True):
        for _ in range(3):
            winsound.Beep(1200, 200)
            time.sleep(0.05)


def dismiss_cookie_banner(page):
    try:
        page.get_by_role("button", name=re.compile("I decline|That's ok", re.I)).first.click(timeout=2000)
    except PWTimeout:
        pass
    except Exception:
        pass


def studies_page_ready(page):
    # The SPA can briefly sit at /studies before its client-side auth guard
    # redirects to auth.prolific.com, so a URL check right after goto() is a
    # race. Waiting for real page content (present only once actually logged
    # in and hydrated) is the reliable signal. Use the persistent left-nav
    # "Studies" link rather than something list-view-specific like the
    # Filters button — if there's already a pending reservation, Prolific may
    # land you straight on that study's detail page instead of the bare
    # list, which still has the nav but not a Filters button.
    try:
        page.locator('a[href="/studies"]').first.wait_for(state="visible", timeout=4000)
        return True
    except PWTimeout:
        return False


def ensure_logged_in(page):
    page.goto(STUDIES_URL, wait_until="domcontentloaded")
    dismiss_cookie_banner(page)
    if not studies_page_ready(page):
        log("Not logged in. Log in to Prolific in the opened Chrome window.")
        log("Waiting for login to complete (checking every 3s, up to 10 minutes)...")
        for _ in range(200):
            page.goto(STUDIES_URL, wait_until="domcontentloaded")
            dismiss_cookie_banner(page)
            if studies_page_ready(page):
                break
            time.sleep(3)
        else:
            log("Timed out waiting for login. Exiting.")
            sys.exit(1)
    log("Logged in.")


def parse_money(text, pattern):
    m = re.search(pattern, text)
    return float(m.group(1)) if m else None


def parse_minutes(text):
    m = re.search(r"(\d+)\s*mins?", text)
    return int(m.group(1)) if m else None


def card_matches_filters(card_text, cfg):
    reward = parse_money(card_text, r"£(\d+(?:\.\d+)?)\s*•")
    hourly = parse_money(card_text, r"£(\d+(?:\.\d+)?)\s*/\s*hr")
    duration = parse_minutes(card_text)

    if cfg["min_reward_gbp"] and (reward is None or reward < cfg["min_reward_gbp"]):
        return False
    if cfg["min_hourly_gbp"] and (hourly is None or hourly < cfg["min_hourly_gbp"]):
        return False
    if cfg["max_duration_minutes"] and duration is not None and duration > cfg["max_duration_minutes"]:
        return False

    lower = card_text.lower()
    includes = [k.lower() for k in cfg.get("keywords_include", [])]
    excludes = [k.lower() for k in cfg.get("keywords_exclude", [])]
    if includes and not any(k in lower for k in includes):
        return False
    if any(k in lower for k in excludes):
        return False
    return True


def get_study_cards(page):
    # Study card titles are the only <a href="#"> elements on the page (nav links
    # use real paths, everything else is a <button>) so this isolates them cleanly.
    links = page.locator('a[href="#"]')
    count = links.count()
    cards = []
    for i in range(count):
        link = links.nth(i)
        title = link.inner_text().strip()
        if not title:
            continue
        try:
            block_text = link.locator(
                "xpath=ancestor::*[self::div or self::li][1]"
            ).inner_text()
        except Exception:
            block_text = title
        cards.append({"title": title, "link": link, "text": block_text})
    return cards


TAKE_PART_PATTERN = re.compile(
    r"take part|start (this )?study|start now|join (this )?study|begin (this )?study",
    re.I,
)


RETURN_TO_STUDIES_PATTERN = re.compile(
    r"back to studies|return to studies|back to prolific|return to prolific",
    re.I,
)


def is_on_studies_list(page):
    # Must be exactly /studies (query string aside), not a sub-path like
    # /studies/<id> — the per-study "ready to start" page is a prefix match
    # on STUDIES_URL but is NOT the list, and must never be auto-reloaded.
    return urlparse(page.url).path.rstrip("/") == "/studies"


def find_return_to_studies_control(page):
    for role in ("link", "button"):
        loc = page.get_by_role(role, name=RETURN_TO_STUDIES_PATTERN)
        try:
            if loc.count() > 0:
                return loc.first
        except Exception:
            continue
    return None


def try_take_part(page, card, cfg):
    title = card["title"]
    if cfg.get("dry_run"):
        log(f"[DRY RUN] Would take part in: {title}")
        return True
    try:
        card["link"].click(timeout=5000)
        take_part_btn = page.get_by_role("button", name=TAKE_PART_PATTERN).first
        take_part_btn.wait_for(state="visible", timeout=5000)
        btn_text = take_part_btn.inner_text().strip()
        if not take_part_btn.is_enabled():
            log(f"Button '{btn_text}' disabled (full/ineligible?) for: {title}")
            return False
        take_part_btn.click(timeout=5000)
        log(f"CLICKED '{btn_text}' for: {title}")
        return True
    except PWTimeout:
        log(f"No matching take-part button found (or timed out) for: {title}. "
            f"If Prolific changed the button wording, update TAKE_PART_PATTERN in watcher.py.")
        return False
    except Exception as e:
        log(f"Error taking part in '{title}': {e}")
        return False


def parse_version(v):
    return tuple(int(x) for x in v.split("."))


def apply_update(new_exe_path):
    # A running exe can't overwrite its own file on Windows, so hand off to a
    # tiny detached helper: it waits for this process to fully exit (freeing
    # the file lock), swaps the new exe into place, relaunches, then deletes
    # itself. This process exits immediately after spawning it.
    #
    # Delays use "ping -n" rather than "timeout" — timeout needs a real
    # console to read from and silently aborts the whole script without one,
    # which matters because CREATE_NO_WINDOW still allocates a (hidden)
    # console but a fully detached helper might not.
    current_exe = Path(sys.executable)
    helper_script = APP_DIR / "_update_helper.bat"
    helper_script.write_text(
        "@echo off\r\n"
        "ping -n 3 127.0.0.1 >nul\r\n"
        ":retry\r\n"
        f'move /y "{new_exe_path}" "{current_exe}" >nul 2>&1\r\n'
        "if errorlevel 1 (\r\n"
        "    ping -n 2 127.0.0.1 >nul\r\n"
        "    goto retry\r\n"
        ")\r\n"
        f'start "" "{current_exe}"\r\n'
        'del "%~f0"\r\n'
    )
    # CREATE_BREAKAWAY_FROM_JOB matters if this exe itself was launched
    # inside a Windows Job Object (some launchers/sandboxes do this) that
    # kills all descendants when this process exits — without it, the
    # helper could get killed mid-swap. Not every job allows breakaway, so
    # fall back to plain CREATE_NO_WINDOW if the flag itself is rejected.
    try:
        subprocess.Popen(
            ["cmd", "/c", str(helper_script)],
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_BREAKAWAY_FROM_JOB,
            close_fds=True,
        )
    except OSError:
        subprocess.Popen(
            ["cmd", "/c", str(helper_script)],
            creationflags=subprocess.CREATE_NO_WINDOW,
            close_fds=True,
        )
    log("Update downloaded — restarting with the new version...")
    sys.exit(0)


def check_for_update():
    if not getattr(sys, "frozen", False):
        return  # only the packaged exe can usefully self-update
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.load(resp)
        latest_tag = data.get("tag_name", "").lstrip("v")
        if not latest_tag or parse_version(latest_tag) <= parse_version(VERSION):
            return
        asset = next(
            (a for a in data.get("assets", []) if a["name"] == "ProlificWatcher.exe"),
            None,
        )
        if not asset:
            return
        log(f"New version {latest_tag} available (you have {VERSION}) — downloading update...")
        new_exe_path = APP_DIR / "ProlificWatcher.exe.new"
        urllib.request.urlretrieve(asset["browser_download_url"], new_exe_path)
        apply_update(new_exe_path)
    except SystemExit:
        raise
    except Exception as e:
        log(f"Update check failed, continuing with current version: {e}")


def run():
    log(f"ProlificWatcher v{VERSION}")
    check_for_update()

    cfg = load_config()
    # title -> "taken" (successfully clicked, or dry-run) | "filtered" (doesn't
    # match config) | "notified" (auto_click off, already alerted once).
    # Anything NOT in here — including a title that failed (full/disabled/
    # timeout) — gets retried on the next cycle, since availability changes.
    study_status = {}
    alerted_titles = set()
    backoff = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel=cfg.get("chrome_channel", "chrome"),
            headless=False,
        )
        has_saved_session = STORAGE_STATE_PATH.exists()
        context = browser.new_context(
            storage_state=str(STORAGE_STATE_PATH) if has_saved_session else None,
            viewport={"width": 1400, "height": 900},
        )
        page = context.new_page()
        try:
            ensure_logged_in(page)
            context.storage_state(path=str(STORAGE_STATE_PATH))

            log("Watching for new studies. Press Ctrl+C to stop.")
            cycles_since_save = 0
            away_from_studies = False
            while True:
                try:
                    if not is_on_studies_list(page):
                        if not away_from_studies:
                            log("You're on a study page — pausing refreshing so it "
                                "doesn't disrupt it. Watching for it to finish...")
                            away_from_studies = True

                        return_ctrl = find_return_to_studies_control(page)
                        if return_ctrl is not None:
                            log("Study finished/cancelled — returning to the studies page.")
                            page.goto(STUDIES_URL, wait_until="domcontentloaded")
                            dismiss_cookie_banner(page)
                            away_from_studies = False
                        # else: still mid-study, do nothing this cycle — no reload,
                        # no interaction, so it can't disturb whatever's on screen.

                    else:
                        if away_from_studies:
                            log("Back on the studies page — resuming normal watching.")
                            away_from_studies = False

                        page.reload(wait_until="domcontentloaded")
                        dismiss_cookie_banner(page)
                        page.wait_for_timeout(400)

                        cards = get_study_cards(page)
                        pending = [c for c in cards if study_status.get(c["title"]) is None]

                        for card in pending:
                            title = card["title"]
                            matches = card_matches_filters(card["text"], cfg)
                            if not matches:
                                log(f"STUDY AVAILABLE: {title} (filtered out)")
                                study_status[title] = "filtered"
                                continue

                            if title not in alerted_titles:
                                log(f"STUDY AVAILABLE: {title}")
                                alert(cfg)
                                alerted_titles.add(title)

                            if cfg.get("auto_click", True):
                                if try_take_part(page, card, cfg):
                                    study_status[title] = "taken"
                                # else: left unset on purpose — retry next cycle,
                                # it may have just been full/disabled momentarily.
                            else:
                                study_status[title] = "notified"

                    backoff = 0

                    cycles_since_save += 1
                    if cycles_since_save >= 50:
                        context.storage_state(path=str(STORAGE_STATE_PATH))
                        cycles_since_save = 0

                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    backoff = min(cfg.get("max_backoff_seconds", 60), max(5, backoff * 2 or 5))
                    log(f"Error during poll cycle: {e}. Backing off {backoff}s.")
                    time.sleep(backoff)
                    continue

                sleep_for = random.uniform(cfg["poll_min_seconds"], cfg["poll_max_seconds"])
                time.sleep(sleep_for)
        finally:
            try:
                context.storage_state(path=str(STORAGE_STATE_PATH))
            except Exception:
                pass
            context.close()
            browser.close()


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log("Stopped by user.")
