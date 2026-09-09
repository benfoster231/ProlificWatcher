# ProlificWatcher

Watches https://app.prolific.com/studies and clicks "Take part in this study"
on any study that matches your filters — whatever's already listed the moment
you start it, plus anything new that appears afterward — using your real
logged-in Chrome session.

## First-run setup and live commands

The first time `config.json` doesn't exist yet, it asks two questions right
in the console: a minimum reward per study, and whether to include studies
that require a camera. Answers are saved and never asked again.

While it's running, type these in the black console window and press Enter:

| Command | Effect |
|---|---|
| `min <amount>` | Change the minimum reward, e.g. `min 2.50` |
| `camera include` | Allow studies that require a camera |
| `camera exclude` | Skip studies that require a camera |
| `status` | Show current settings |
| `help` | List commands |

Changes take effect immediately and are saved to `config.json`, so they
persist across restarts too.

The reward minimum matches whatever currency symbol (£ or $) the study
itself shows — it does not convert between currencies, so `min 5` means "at
least 5 in whatever currency that study displays," not "5 in one specific
currency." Studies on the same account can be priced in either.

## Diagnostic reporting

When something goes wrong (a click fails, the take-part button isn't found,
login times out, the update check errors, etc.), the app automatically sends
a short report to a public `ntfy.sh` topic
(`prolificwatcher-diag-bf231-9k2x7q`) — just the log line text, the version
number, and a random per-install ID (not tied to identity). No credentials,
no page content, nothing beyond what's already written to the local
`watcher.log`. This lets issues get diagnosed without needing someone to
manually copy their log file. View the feed at
`https://ntfy.sh/prolificwatcher-diag-bf231-9k2x7q`.

## Auto-update

The .exe checks `https://github.com/benfoster231/ProlificWatcher/releases`
for a newer version every time it starts. If one exists, it downloads it and
replaces itself automatically, then relaunches — you (or your friend) never
need to manually re-download after a change.

To ship a new version: bump `VERSION` in `watcher.py`, rebuild
(`build_exe.bat`), then:
```
git add -A && git commit -m "..." && git push
gh release create vX.Y.Z dist/ProlificWatcher.exe --title "vX.Y.Z" --notes "..."
```
Every copy of the exe already out there picks it up next time it's launched.

## How it works

- Drives an actual Chrome window (via Playwright), not a hidden/headless bot —
  a separate Chrome session from your everyday browsing, so it doesn't touch
  your normal profile.
- First run opens Chrome to the Prolific login page; log in manually once.
  The session is then saved to `storage_state.json` next to the program, so
  future runs start already logged in. Delete that file to force a fresh
  login (e.g. if the session expires).
- Every 5-9 seconds (randomized, configurable) it reloads the studies page
  and checks everything currently listed against your filters in
  `config.json` — including on the very first check after startup, not just
  ones that appear later. A "Still watching — N studies currently listed"
  line gets written once a minute so there's a record it was alive even
  during a quiet stretch, but only to `watcher.log` — it's left out of the
  console so the visible window doesn't fill up with routine noise.
  If the browser ever closes unexpectedly (a crash, Chrome getting killed,
  etc.), it's detected automatically and a fresh browser is relaunched and
  logged back in within a few seconds — no manual restart needed. If that
  relaunch attempt itself fails (can happen if Chrome hasn't fully released
  its resources yet right after crashing), it keeps retrying with backoff
  until it succeeds, rather than giving up after one try and being stuck
  hitting a dead page forever — a real bug in an earlier version that
  looked exactly like "stopped clicking studies" from the outside.
  A study that doesn't match your filters is logged as `SKIPPED: <title> —
  <reason>`, with the actual reward/hourly/duration it found and the
  threshold it missed by — so you can see exactly what's getting passed
  over, not just that something was. A study you've already successfully
  taken is only logged once. But a study that matched and *failed* (full,
  disabled, or a timing hiccup) is retried on the next cycle rather than
  given up on — availability can change, e.g.
  someone else's reservation expires and frees a slot back up.
  One caveat: this keys studies by their title text, which Prolific doesn't
  guarantee is unique — two genuinely different postings with an identical
  title would be treated as the same study, so the second wouldn't be
  re-attempted after the first is taken. In practice this is uncommon.
- Everything is logged to `watcher.log` next to the program.
- **If you're actually doing a study**, the tab has navigated away from the
  studies list (often to a different site entirely for the survey itself).
  The watcher notices this and stops reloading/scanning completely — it
  won't touch that page or risk disrupting your progress. It just checks,
  without reloading, for a "back to studies" / "return to studies" control,
  which is what shows up once you finish or cancel. The moment that appears,
  it navigates back to the studies list and normal watching resumes.

## Running it

**From source (no build needed):**
```
pip install -r requirements.txt
python -m playwright install chrome
python watcher.py
```

**As a standalone .exe:**
```
build_exe.bat
```
This produces `dist\ProlificWatcher.exe` with `dist\config.json` next to it.
Run the exe directly after that — no Python needed on the machine you copy
it to, but Chrome itself must still be installed there.

## config.json

| Field | Meaning |
|---|---|
| `poll_min_seconds` / `poll_max_seconds` | Randomized reload interval range. Lower = faster but more load and more bot-like. Don't go below ~3s. |
| `auto_click` | `true` clicks "Take part" automatically. `false` just alerts you (sound + log) and lets you click manually. |
| `dry_run` | `true` logs what it *would* click without actually clicking — use this to sanity-check filters before trusting it. |
| `min_reward_gbp` / `min_hourly_gbp` | Skip studies paying below these. `0` = no minimum. |
| `max_duration_minutes` | Skip studies longer than this. `0` = no maximum. |
| `keywords_include` | If non-empty, only take studies whose card text contains at least one of these (case-insensitive). |
| `keywords_exclude` | Skip studies whose card text contains any of these. |
| `sound_alert` | Beep when a new matching study is found. |
| `chrome_channel` | Leave as `"chrome"` to use your real installed Chrome. |

Edit `config.json`, save, and the next run picks it up (no rebuild needed,
even for the .exe — it reads config.json from disk next to it).

The button it clicks is matched by wording, not by exact text — `TAKE_PART_PATTERN`
near the top of `watcher.py` matches "take part", "start (this) study", "start now",
"join (this) study", and "begin (this) study" (case-insensitive). Every click is
logged with the exact button text it matched, so you can verify it's clicking the
right thing. If `watcher.log` ever shows "No matching take-part button found" for a
study you know is joinable, Prolific used different wording — add it to that regex
and restart (for the .exe, you'd need to rebuild).

## Things worth knowing before you rely on this

- **This detects new studies from the DOM of the page you'd see anyway.**
  It doesn't reverse-engineer Prolific's private API, so it's not fragile to
  auth/token changes, but it *is* fragile to Prolific redesigning the studies
  page — if `watcher.log` stops seeing any studies, the page layout likely
  changed and the selector logic (`get_study_cards` / `card_matches_filters`
  in `watcher.py`) needs a small update.
- **Poll interval is a real tradeoff, not just an ISP question.** The
  bottleneck isn't your connection, it's how often you hit Prolific's
  servers and how patterned that traffic looks. The default 5-9s jittered
  range is a reasonable middle ground; going much faster buys you little
  (most of the speed win over manual refreshing already happens at this
  range) while making the traffic pattern more obviously non-human.
- **Prolific does watch for this behavior.** Rapid repeat reservation clicks
  and abnormally frequent refreshing are patterns their systems flag, and
  flagged accounts can have study access temporarily restricted. That's
  independent of what the participant terms say in writing — it's how
  their detection actually behaves in practice. Keep the interval sane and
  don't run multiple instances against the same account.
- **Test with `dry_run: true` first.** Confirm in `watcher.log` that it's
  correctly identifying new studies and that your reward/duration filters
  are parsing as expected, before letting it click for real.
- Closing the Chrome window it opened stops the watcher (the script exits
  when the browser context closes). Ctrl+C in the console also stops it
  cleanly.
