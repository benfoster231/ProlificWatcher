# ProlificWatcher

Watches https://app.prolific.com/studies and clicks "Take part in this study"
on any study that matches your filters — whatever's already listed the moment
you start it, plus anything new that appears afterward — using your real
logged-in Chrome session.

## How it works

- Drives an actual Chrome window (via Playwright), not a hidden/headless bot —
  a separate Chrome session from your everyday browsing, so it doesn't touch
  your normal profile.
- First run opens Chrome to the Prolific login page; log in manually once.
  The session is then saved to `storage_state.json` next to the program, so
  future runs start already logged in. Delete that file to force a fresh
  login (e.g. if the session expires).
- Every 5-9 seconds (randomized, configurable) it reloads the studies page and
  checks every currently-listed study against your filters in `config.json`
  — including on the very first check after startup, not just ones that
  appear later. A study that doesn't match your filters, or that you've
  already successfully taken, is only logged/attempted once. But a study
  that matched and *failed* (full, disabled, or a timing hiccup) is retried
  on the next cycle rather than given up on — availability can change, e.g.
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
