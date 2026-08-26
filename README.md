# The Faithful Effect — Follower Tracker

Daily Instagram follower snapshots for Traitors contestants, using
`instaloader` in anonymous (no-login) mode. Outputs a long-format CSV
you can plot, join to your Fame Framework scores, or compare pre/post-show.

## 1. Local test run (do this first)

```bash
cd follower_tracker
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install instaloader requests

python track_followers.py --handles data/handles.csv --out data/follower_history.csv
```

- The core dependencies above cover tiers 1 and 2 of the fallback chain
  (see section 5). If any handles need tier 3 (rendered-browser
  fallback), also run:
  ```bash
  pip install selenium undetected-chromedriver
  ```
  This isn't required for a first run — the script will only complain
  about a missing selenium install if a specific account actually needs
  that tier, so it's fine to add it later if you never hit that case.

- `data/handles.csv` is currently pre-filled with 3 Season 4 anchors
  (Boston Rob, Dr. Will, Cirie) so you can validate the pipeline before
  pointing it at real New Blood data — matches the proxy-testing approach
  you used for the confessional pipeline.
- Each run appends a new day's rows to `data/follower_history.csv`.
  Run it twice on different days locally and you'll see the long-format
  shape build up: one row per (date, contestant).
- A per-day cache lives in `data/.cache/<date>/<handle>.json` — if the
  script errors out partway through, re-running the same day won't
  re-hit profiles it already successfully fetched.

## 2. Switch to the New Blood cast at reveal

1. Copy `data/handles_new_blood_TEMPLATE.csv` → `data/handles.csv`
   (or point `--handles` at the new file).
2. Fill in one row per contestant: `contestant_name, instagram_handle,
   season, cast_list`. Use `cast_list = new_blood` so it's easy to filter
   later once you're combining with Seasons 1–4 history.
3. Run once manually to confirm every handle resolves — Instagram
   handles from wiki pages / press releases are sometimes stale, so
   check the `error` column for `profile_not_found` rows and fix by hand.
4. This is your baseline snapshot — the one worth capturing cleanly the
   moment the cast is officially revealed, since it establishes the
   "before the show aired" fame level for each player.

## 3. Automate with GitHub Actions

1. Push this folder to a GitHub repo (public or private both work; no
   Instagram login/secrets are needed since the loader runs anonymous).
2. The workflow at `.github/workflows/track_followers.yml` is already
   wired to run daily at 13:00 UTC and commit the updated CSV back to
   the repo automatically.
3. To trigger a run manually (e.g., right at cast reveal, don't wait
   for the schedule): go to the repo's **Actions** tab → **Daily
   Follower Tracker** → **Run workflow**.
4. To change the time it runs, edit the `cron` line — cron times are
   always UTC.

## 4. Optional: authenticated mode for accounts anonymous mode can't reach

Some accounts (often business/creator profiles — brand deals, verified,
high-traffic) get blocked by Instagram's bot detection in anonymous mode
even though nothing is wrong with your script or your handle spelling.
This is increasingly common for exactly the kind of contestants you most
care about — established names with agency-managed accounts — so it's
worth having this ready rather than discovering it account by account.

**The tradeoff, stated plainly:** logging in gets far more reliable
access, but it puts *that account* at some risk of a temporary
restriction if Instagram decides the request pattern looks automated,
even with polite delays. **Never use your personal Instagram account for
this.** Create a dedicated account with no personal content or
connections you'd be upset to lose, used for nothing else.

**One-time setup:**
1. Create the burner account in the Instagram app and verify it normally.
   A fresh account with zero activity is *more* likely to get flagged,
   not less — browse and follow a few things normally for a few days
   before relying on it here.
2. From Terminal, in the `follower_tracker` folder:
   ```bash
   mkdir -p data/.sessions
   instaloader --login=your_burner_username --sessionfile="data/.sessions/session-your_burner_username"
   ```
   Enter the password (and 2FA code if enabled) when prompted — this
   happens once. The tracking script itself never asks for or stores
   the password.
3. Confirm it worked: `ls data/.sessions/`

**Running with authentication:**
```bash
python track_followers.py --handles data/handles.csv --out data/follower_history.csv --login-user your_burner_username
```
Anonymous mode stays the default — this only activates when you pass
`--login-user`. Session files are excluded from git (`data/.sessions/`
is in `.gitignore`) since they grant real account access — never commit
them.

**For GitHub Actions with a logged-in account:** this needs more care
than the anonymous workflow — the session has to be provided as an
encrypted repo secret rather than committed, and Instagram is more
likely to flag repeated automated logins from a shared GitHub IP than a
real login on your own machine. Worth validating it works locally first,
then setting up the automated version as a deliberate next step rather
than folding it into today's launch.

## 5. Notes / gotchas

- **Rate limiting:** the script waits a randomized 8–20 seconds between
  profiles. For ~90 Season 1–4 contestants that's roughly 15–25 minutes
  per run; for a New Blood cast of ~20-something it'll be much faster.
  If Instagram starts blocking anonymous requests from GitHub's shared
  IP ranges, the usual fix is switching the Action to run less often
  (e.g., every 2 days) rather than adding a login (logins carry real
  ban risk for the account you use).
- **Three-tier fallback for blocked accounts:** some accounts (often
  business/creator profiles — brand deals, verified, high-traffic) get
  fingerprinted by Instagram as non-browser traffic and served an empty
  JS app shell instead of real page content. This is unrelated to
  whether the handle is correct — it's Instagram's detection, not a bug
  in your data. The script handles it in tiers, each one only
  attempted if the one before it fails:
  1. **JSON API** (fast, works for most accounts) — genuine connection/
     rate-limit errors get retried 3x with backoff here before moving on.
  2. **Plain HTTP fallback** — scrapes follower/following/post counts
     from the profile page's meta tag directly. Works when Instagram
     serves the normal page but the JSON endpoint itself is broken for
     that account.
  3. **Rendered-browser fallback (Selenium)** — for accounts where even
     tier 2 gets served the empty JS shell. A real rendered browser is
     treated like normal traffic, so it gets the actual page. Slower
     (~5-15s per profile) and requires `pip install selenium
     undetected-chromedriver` — only kicks in when tiers 1 and 2 both fail.
  - The first time a handle needs tier 2 or 3, it's recorded in
    `data/known_broken_accounts.json`. Every run after that skips the
    doomed JSON call for that handle entirely and starts at tier 2 —
    this file is meant to be committed to the repo (it's not in
    `.gitignore`) so the list builds up over time instead of
    re-discovering the same accounts every day.
  - Fallback rows are flagged in the `error` column
    (`used_html_fallback` or `used_selenium_fallback`) and may show
    abbreviated counts (e.g. `237000` parsed from "237K") instead of
    an exact number — worth a spot-check against the profile directly
    if precision matters for a specific contestant.
  - If a handle needs tier 3 regularly, `data/.debug_html/` will have
    saved copies of what each tier actually received — useful if a
    future run starts failing again and you want to see why.
- **Private profiles:** you'll still get `followers`/`following`/
  `post_count` for private accounts (those are public-facing numbers),
  but posts themselves aren't accessible — that's fine, this tracker
  only needs the counts.
- **Column meaning:**
  - `followers` / `following` / `post_count` — current public counts
  - `is_private` — True/False
  - `error` — empty string on success; otherwise a short reason
    (`profile_not_found`, `connection_error: ...`, etc.) so failed
    rows are visible in the CSV rather than silently dropped
- **Merging into your broader dataset:** `contestant_name` here should
  match the naming convention in your Fame Framework roster so a join
  is a one-liner in pandas later.
