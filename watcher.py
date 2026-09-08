import json
import random
import re
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import winsound
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

VERSION = "1.0.9"
UPDATE_REPO = "benfoster231/ProlificWatcher"

# Where automatic error/issue diagnostics get sent (see report() below) —
# a free pub/sub topic, not a secret; disclosed to users in START_HERE.txt.
DIAG_TOPIC = "prolificwatcher-diag-bf231-9k2x7q"

STUDIES_URL = "https://app.prolific.com/studies"

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent

STORAGE_STATE_PATH = APP_DIR / "storage_state.json"
LOG_PATH = APP_DIR / "watcher.log"
INSTALL_ID_PATH = APP_DIR / "install_id.txt"


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_install_id():
    # A random label (not tied to identity) so reports from the same
    # install can be told apart from each other in the diagnostics feed.
    if INSTALL_ID_PATH.exists():
        return INSTALL_ID_PATH.read_text().strip()
    new_id = uuid.uuid4().hex[:8]
    INSTALL_ID_PATH.write_text(new_id)
    return new_id


def report(msg):
    # Logs locally as usual, and best-effort sends a short diagnostic report
    # off-machine so issues can be debugged without needing someone to
    # manually copy their log file. Disclosed in START_HERE.txt. Never
    # includes credentials or page content — just the same short status
    # lines already written to watcher.log locally.
    log(msg)
    try:
        body = f"[{get_install_id()}] v{VERSION} — {msg}"
        req = urllib.request.Request(
            f"https://ntfy.sh/{DIAG_TOPIC}",
            data=body.encode("utf-8"),
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # diagnostics are best-effort — never let this break the app


DEFAULT_CONFIG = {
    "poll_min_seconds": 5,
    "poll_max_seconds": 9,
    "auto_click": True,
    "dry_run": False,
    "min_reward_gbp": 0.0,
    "min_hourly_gbp": 0.0,
    "max_duration_minutes": 0,
    "exclude_camera_studies": False,
    "keywords_include": [],
    "keywords_exclude": [],
    "sound_alert": True,
    "chrome_channel": "chrome",
    "max_backoff_seconds": 60,
}


def save_config(cfg):
    config_path = APP_DIR / "config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def prompt_first_run_setup():
    # Asked once, ever — the answers get saved to config.json, and from then
    # on load_config() just reads the file like normal. Falls back to
    # defaults silently if stdin isn't interactive (e.g. under automation).
    print("First-time setup — press Enter to accept the default in [brackets].")
    cfg = dict(DEFAULT_CONFIG)
    try:
        raw = input("Minimum reward per study, in your account's currency (0 = no minimum) [0]: ").strip()
        if raw:
            cfg["min_reward_gbp"] = float(raw)
    except (ValueError, EOFError, OSError):
        pass
    try:
        raw = input("Include studies that require a camera? (y/n) [y]: ").strip().lower()
        if raw.startswith("n"):
            cfg["exclude_camera_studies"] = True
    except (EOFError, OSError):
        pass
    return cfg


def load_config():
    # A single downloaded .exe should be able to run on its own — create a
    # sensible default config.json next to it rather than requiring the
    # user to separately source one.
    config_path = APP_DIR / "config.json"
    if not config_path.exists():
        cfg = prompt_first_run_setup()
        save_config(cfg)
        print(f"Saved your choices to {config_path}.")
        print("Edit that file in Notepad any time to change other filters, "
              "or use the min/camera commands below while this is running.")
        return cfg
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


CONSOLE_HELP = (
    "Commands (type one and press Enter):\n"
    "  min <amount>     - set minimum reward per study (0 = no minimum), e.g: min 2.50\n"
    "  camera include   - allow studies that require a camera\n"
    "  camera exclude   - skip studies that require a camera\n"
    "  status           - show current settings\n"
    "  help             - show this list\n"
)


def console_command_loop(cfg, cfg_lock):
    # Runs in a background thread so it can't block the watch loop. Lets
    # someone change the two settings they're most likely to want to tweak
    # on the fly, without editing config.json and restarting.
    print(CONSOLE_HELP)
    while True:
        try:
            line = input().strip()
        except (EOFError, OSError):
            return
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()
        if cmd == "min" and len(parts) > 1:
            try:
                amount = float(parts[1])
            except ValueError:
                print("Couldn't parse that amount — example: min 2.50")
                continue
            with cfg_lock:
                cfg["min_reward_gbp"] = amount
                save_config(cfg)
            log(f"Minimum reward set to {amount}.")
        elif cmd == "camera" and len(parts) > 1 and parts[1].lower() in ("include", "exclude"):
            with cfg_lock:
                cfg["exclude_camera_studies"] = parts[1].lower() == "exclude"
                save_config(cfg)
            log(f"Camera-required studies: {'excluded' if cfg['exclude_camera_studies'] else 'included'}.")
        elif cmd == "status":
            with cfg_lock:
                log(f"min_reward={cfg.get('min_reward_gbp')} "
                    f"camera_excluded={cfg.get('exclude_camera_studies')} "
                    f"auto_click={cfg.get('auto_click')} dry_run={cfg.get('dry_run')}")
        elif cmd == "help":
            print(CONSOLE_HELP)
        else:
            print("Unknown command. Type 'help' for the list.")


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
            time.sleep(3)
            # Passively check the page as-is — do NOT navigate/reload here.
            # The user may be actively typing their email/password on the
            # login form right now; forcing a goto() would wipe the form and
            # bounce them back to a fresh login page mid-entry. Auth0 itself
            # redirects back to the app once login actually succeeds.
            if studies_page_ready(page):
                break
        else:
            report("Timed out waiting for login. Exiting.")
            sys.exit(1)
        dismiss_cookie_banner(page)
    log("Logged in.")


def parse_money(text, pattern):
    m = re.search(pattern, text)
    return float(m.group(1)) if m else None


def parse_minutes(text):
    m = re.search(r"(\d+)\s*mins?", text)
    return int(m.group(1)) if m else None


def card_matches_filters(card_text, cfg):
    # Prolific shows £ or $ depending on the study/researcher, so match
    # either — a £-only pattern would silently stop filtering $ studies.
    reward = parse_money(card_text, r"[£$](\d+(?:\.\d+)?)\s*•")
    hourly = parse_money(card_text, r"[£$](\d+(?:\.\d+)?)\s*/\s*hr")
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


def study_requires_camera(page):
    # The study detail page lists a "You will also need:" section (Audio /
    # Camera / Microphone) — confirmed by inspecting a real study. Scoped to
    # a short window right after that heading so a study whose description
    # merely mentions "camera" in passing doesn't false-positive.
    try:
        text = page.locator("main").inner_text()
    except Exception:
        try:
            text = page.inner_text("body")
        except Exception:
            return False
    if "You will also need:" not in text:
        return False
    snippet = text.split("You will also need:", 1)[1][:200]
    return "camera" in snippet.lower()


def try_take_part(page, card, cfg):
    # Returns "taken" (clicked, or dry-run got this far), "camera_excluded"
    # (skip permanently, user doesn't want camera studies), or "failed"
    # (transient — full/disabled/timeout, worth retrying next cycle).
    title = card["title"]
    try:
        card["link"].click(timeout=5000)
        page.wait_for_timeout(300)
        if cfg.get("exclude_camera_studies") and study_requires_camera(page):
            log(f"Skipping (requires camera): {title}")
            return "camera_excluded"
        take_part_btn = page.get_by_role("button", name=TAKE_PART_PATTERN).first
        take_part_btn.wait_for(state="visible", timeout=5000)
        btn_text = take_part_btn.inner_text().strip()
        if not take_part_btn.is_enabled():
            report(f"Button '{btn_text}' disabled (full/ineligible?) for: {title}")
            return "failed"
        if cfg.get("dry_run"):
            log(f"[DRY RUN] Would click '{btn_text}' for: {title}")
            return "taken"
        take_part_btn.click(timeout=5000)
        log(f"CLICKED '{btn_text}' for: {title}")
        return "taken"
    except PWTimeout:
        report(f"No matching take-part button found (or timed out) for: {title}. "
               f"If Prolific changed the button wording, update TAKE_PART_PATTERN in watcher.py.")
        return "failed"
    except Exception as e:
        report(f"Error taking part in '{title}': {e}")
        return "failed"


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
        report(f"Update check failed, continuing with current version: {e}")


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

            cfg_lock = threading.Lock()
            threading.Thread(target=console_command_loop, args=(cfg, cfg_lock), daemon=True).start()

            log("Watching for new studies. Press Ctrl+C to stop.")
            cycles_since_save = 0
            cycles_since_refresh = 0
            last_heartbeat = time.time()
            away_from_studies = False
            while True:
                try:
                    with cfg_lock:
                        cfg_snapshot = dict(cfg)

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

                        # Prolific's own UI says studies appear live without a
                        # manual refresh — reloading every cycle was almost
                        # certainly what caused the recurring browser crashes.
                        # Just re-scan the live DOM instead, with an occasional
                        # full reload as a safety net against silent staleness.
                        cycles_since_refresh += 1
                        if cycles_since_refresh >= 150:
                            page.reload(wait_until="domcontentloaded")
                            dismiss_cookie_banner(page)
                            page.wait_for_timeout(400)
                            cycles_since_refresh = 0

                        cards = get_study_cards(page)
                        pending = [c for c in cards if study_status.get(c["title"]) is None]

                        if time.time() - last_heartbeat >= 60:
                            log(f"Still watching — {len(cards)} studies currently listed, "
                                f"nothing new to act on right now.")
                            last_heartbeat = time.time()

                        for card in pending:
                            title = card["title"]
                            matches = card_matches_filters(card["text"], cfg_snapshot)
                            if not matches:
                                log(f"STUDY AVAILABLE: {title} (filtered out)")
                                study_status[title] = "filtered"
                                continue

                            if title not in alerted_titles:
                                log(f"STUDY AVAILABLE: {title}")
                                alert(cfg_snapshot)
                                alerted_titles.add(title)

                            if cfg_snapshot.get("auto_click", True):
                                result = try_take_part(page, card, cfg_snapshot)
                                if result in ("taken", "camera_excluded"):
                                    study_status[title] = "taken" if result == "taken" else "filtered"
                                # "failed" -> left unset on purpose — retry next
                                # cycle, it may have just been full/disabled
                                # momentarily.
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
                    was_already_failing = backoff > 0
                    backoff = min(cfg.get("max_backoff_seconds", 60), max(5, backoff * 2 or 5))
                    msg = f"Error during poll cycle: {e}. Backing off {backoff}s."
                    # Only report the first failure in a streak remotely —
                    # repeated retries of the same underlying issue would
                    # otherwise flood the diagnostics feed.
                    (log if was_already_failing else report)(msg)
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
