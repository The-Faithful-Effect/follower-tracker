"""
The Faithful Effect — Instagram Follower Tracker
--------------------------------------------------
Pulls public follower/following/post counts for a list of Instagram handles
using instaloader. Appends results to a long-format CSV so you build up
a daily time series per contestant.

Two modes:
- ANONYMOUS (default): no login, lowest ban risk, but Instagram's bot
  detection increasingly blocks anonymous requests for some accounts
  (often business/creator-type profiles — brand deals, verified, high
  traffic). This is the account itself being flagged, not the script.
- AUTHENTICATED (--login-user): uses a saved instaloader session for a
  DEDICATED account (never your personal one — see README for why).
  Dramatically more reliable, since Instagram treats logged-in browser
  traffic very differently from anonymous requests.

Usage:
    python track_followers.py --handles data/handles.csv --out data/follower_history.csv
    python track_followers.py --handles data/handles.csv --out data/follower_history.csv --login-user your_burner_account

handles.csv format (one row per contestant):
    contestant_name,instagram_handle,season,cast_list
    Boston Rob,bostonrob,4,current
    ...

Design choices (matching project conventions):
- Modular / single-purpose script (does one thing: pull + append counts)
- Partial rows on error rather than skipping a contestant entirely
  (records the error but still writes a row with what it could get,
  so gaps are visible in the CSV rather than silently missing)
- Long-format output: one row per (date, contestant) — easy to plot
  a follower trajectory per person or facet by archetype later
- Randomized delay between requests to reduce rate-limit / block risk
- Local JSON cache of each day's raw profile dict, so a partial failure
  mid-run doesn't force you to re-hit every profile you already fetched
"""

import argparse
import concurrent.futures
import csv
import html
import json
import random
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

import instaloader
import requests


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MIN_DELAY_SECONDS = 8   # polite floor between profile fetches
MAX_DELAY_SECONDS = 20  # ceiling — randomized to look less bot-like
MAX_RETRIES = 3         # transient Instagram errors get retried before giving up
RETRY_BACKOFF_SECONDS = 30  # base wait before a retry; grows each attempt

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "data" / ".cache"
BROKEN_ACCOUNTS_FILE = SCRIPT_DIR / "data" / "known_broken_accounts.json"
DEBUG_DIR = SCRIPT_DIR / "data" / ".debug_html"
SESSION_DIR = SCRIPT_DIR / "data" / ".sessions"


@dataclass
class FollowerRecord:
    date: str
    contestant_name: str
    instagram_handle: str
    season: str
    cast_list: str
    followers: object
    following: object
    post_count: object
    is_private: object
    full_name: object
    biography: object
    error: str


def _parse_abbreviated_count(text: str):
    """Turn '237K', '1,766', '3.4M' into an int. Returns None if unparseable."""
    text = text.strip().replace(",", "")
    match = re.match(r"^([\d.]+)([KMB]?)$", text, re.IGNORECASE)
    if not match:
        return None
    number, suffix = match.groups()
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[suffix.upper()]
    try:
        return int(float(number) * multiplier)
    except ValueError:
        return None


def _extract_counts_from_page_html(page: str, handle: str, source_label: str) -> dict:
    """
    Shared parser: given a full HTML page (from requests OR a rendered
    Selenium page_source), pull follower/following/post counts out of the
    meta description tag. Used by both fallback tiers so the parsing logic
    only lives in one place.
    """
    desc_match = (
        re.search(r'<meta\s+property="og:description"\s+content="([^"]+)"', page)
        or re.search(r'<meta\s+name="description"\s+content="([^"]+)"', page)
        or re.search(r'<meta\s+content="([^"]+)"\s+name="description"', page)
    )
    title_match = (
        re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', page)
        or re.search(r'<title>([^<]+)</title>', page)
    )

    if not desc_match:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        debug_path = DEBUG_DIR / f"{handle}_{source_label}.html"
        debug_path.write_text(page, encoding="utf-8")
        raise ValueError(
            f"[{source_label}] could not find a description meta tag on the page "
            f"— raw HTML saved to {debug_path} for inspection"
        )

    description = html.unescape(desc_match.group(1))
    # Typical format: "237K Followers, 1,766 Following, 322 Posts - See
    # Instagram photos and videos from Cirie Fields (@cirie_fields)"
    counts_match = re.search(
        r"([\d.,]+[KMB]?)\s+Followers?,\s+([\d.,]+[KMB]?)\s+Following,\s+([\d.,]+[KMB]?)\s+Posts?",
        description, re.IGNORECASE,
    )
    if not counts_match:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        debug_path = DEBUG_DIR / f"{handle}_{source_label}.html"
        debug_path.write_text(page, encoding="utf-8")
        raise ValueError(
            f"[{source_label}] found a description tag but couldn't parse counts from it: "
            f"{description!r} — raw HTML saved to {debug_path} for inspection"
        )

    followers_raw, following_raw, posts_raw = counts_match.groups()
    full_name = None
    if title_match:
        full_name = html.unescape(title_match.group(1)).split(" (@")[0].strip()

    return {
        "followers": _parse_abbreviated_count(followers_raw),
        "following": _parse_abbreviated_count(following_raw),
        "post_count": _parse_abbreviated_count(posts_raw),
        "is_private": None,  # not reliably present in the meta tag
        "full_name": full_name,
        "biography": None,  # not present in the meta description
        "error": f"used_{source_label}_fallback (counts may be abbreviated/rounded)",
    }


def fetch_via_html_fallback(handle: str) -> dict:
    """
    TIER 2 fallback. Plain HTTP request to the profile page. Works when
    Instagram serves the normal server-rendered page, but for requests that
    get fingerprinted as non-browser traffic, Instagram sometimes serves an
    empty JS app shell instead — in which case this raises and TIER 3
    (Selenium) is worth trying, since a real rendered browser is far less
    likely to get flagged this way.
    """
    url = f"https://www.instagram.com/{handle}/?hl=en"
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
    resp.raise_for_status()
    page = resp.text

    # Detect the common ways Instagram blocks a non-browser request instead
    # of serving the real profile page, so the error message says WHY it
    # failed rather than just "couldn't find the tag".
    final_url = resp.url
    if "accounts/login" in final_url or "Log in" in page[:2000]:
        raise ValueError(f"blocked: redirected to login wall (final url: {final_url})")
    if "Sorry, this page isn't available" in page:
        raise ValueError("blocked: Instagram returned its 'page not available' interstitial")
    if "Restricted profile" in page or "cookie" in final_url.lower():
        raise ValueError(f"blocked: possible cookie-consent wall (final url: {final_url})")

    return _extract_counts_from_page_html(page, handle, source_label="html")


# ---------------------------------------------------------------------------
# TIER 3 fallback — Selenium (real rendered browser)
# ---------------------------------------------------------------------------
# Only imported if actually used, so people running anonymous-only don't
# need selenium/undetected_chromedriver installed at all.

def _detect_local_chrome_major_version() -> int:
    """
    Reads the actually-installed Chrome version so we can tell
    undetected_chromedriver exactly which version to target, instead of
    letting it auto-detect and potentially grab a driver built for a
    newer Chrome than what's actually installed (a common mismatch).
    """
    import subprocess

    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",  # macOS
        "google-chrome",  # Linux (e.g. GitHub Actions runners)
        "google-chrome-stable",
    ]
    for path in candidates:
        try:
            output = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=10
            ).stdout.strip()
            # Output looks like "Google Chrome 151.0.7922.108"
            match = re.search(r"(\d+)\.\d+\.\d+\.\d+", output)
            if match:
                return int(match.group(1))
        except (FileNotFoundError, subprocess.SubprocessError):
            continue
    return None  # couldn't detect — let undetected_chromedriver guess


def fetch_via_selenium_fallback(handle: str) -> dict:
    """
    Last-resort fallback for accounts where BOTH the JSON API and a plain
    HTTP request fail — typically because Instagram is serving an empty
    JS app shell to non-browser-looking traffic instead of the real page.
    A real rendered (headless) browser is treated like normal traffic, so
    it should receive the actual page content. Slower (~5-15s/profile) —
    reserved for accounts that need it, not run by default on everyone.

    Reuses the same undetected_chromedriver setup already proven out in
    the Fandom wiki scraper.
    """
    try:
        import undetected_chromedriver as uc
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
    except ImportError:
        raise RuntimeError(
            "selenium/undetected_chromedriver not installed. Run: "
            "pip install selenium undetected-chromedriver"
        )

    options = uc.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1280,1696")
    options.add_argument("--lang=en-US")
    # CI runners (like GitHub Actions) often have restricted /dev/shm and
    # sandboxing that can cause headless Chrome to hang or crash silently
    # without these flags, even though the same code runs fine locally.
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")

    chrome_version = _detect_local_chrome_major_version()
    driver_kwargs = {"options": options}
    if chrome_version:
        driver_kwargs["version_main"] = chrome_version

    # uc.Chrome() startup itself has no built-in timeout — on CI runners
    # this is the more likely hang point (chromedriver waiting indefinitely
    # to bind to the browser process), not page loading. Force a hard cap
    # here so a stuck startup fails in ~25s instead of running forever.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(uc.Chrome, **driver_kwargs)
        try:
            driver = future.result(timeout=25)
        except concurrent.futures.TimeoutError:
            raise RuntimeError(
                "Chrome driver startup hung for 25s+ — likely a CI environment "
                "issue (headless Chrome failing to launch). Not retrying "
                "further within this fallback."
            )

    try:
        driver.set_page_load_timeout(30)
        driver.get(f"https://www.instagram.com/{handle}/?hl=en")
        # Wait for the page to actually finish loading rather than grabbing
        # source immediately — Instagram's content can populate a beat
        # after initial load.
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(2)  # small buffer for any late client-side rendering
        page = driver.page_source
    finally:
        driver.quit()

    return _extract_counts_from_page_html(page, handle, source_label="selenium")


def load_broken_accounts() -> set:
    if BROKEN_ACCOUNTS_FILE.exists():
        return set(json.loads(BROKEN_ACCOUNTS_FILE.read_text()))
    return set()


def save_broken_accounts(handles: set):
    BROKEN_ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    BROKEN_ACCOUNTS_FILE.write_text(json.dumps(sorted(handles), indent=2))


# ---------------------------------------------------------------------------
# Core fetch logic
# ---------------------------------------------------------------------------

def build_loader(login_user: str = None) -> instaloader.Instaloader:
    """
    Anonymous by default. If login_user is given, loads a previously saved
    session for that account instead — see README for how to create one.
    Never enters a password here; sessions must be created ahead of time
    via the `instaloader --login=<user>` command so credentials never
    touch this script or its logs.
    """
    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
        max_connection_attempts=1,  # let OUR retry/fallback chain handle
                                     # rate limits, not instaloader's own
                                     # internal backoff — its default can
                                     # sleep 10+ minutes per account before
                                     # ever returning control to this script
    )

    if login_user:
        session_path = SESSION_DIR / f"session-{login_user}"
        if not session_path.exists():
            print(
                f"\nNo saved session found for '{login_user}' at {session_path}.\n"
                f"Create one first (run this once, interactively, from Terminal):\n\n"
                f"    instaloader --login={login_user} --sessionfile=\"{session_path}\"\n\n"
                f"It'll prompt for the password (and 2FA code if enabled) once, "
                f"then save a reusable session file. This script never asks for "
                f"or stores the password itself.\n"
            )
            sys.exit(1)
        try:
            L.load_session_from_file(login_user, filename=str(session_path))
            print(f"Loaded authenticated session for @{login_user}.")
        except Exception as e:
            print(f"Failed to load session for '{login_user}': {e}")
            print("The session may have expired — recreate it with the instaloader --login command above.")
            sys.exit(1)

    return L


def resolve_via_fallback_chain(handle: str) -> tuple[dict, str]:
    """
    Tries TIER 2 (plain HTTP) then TIER 3 (Selenium) in order. Returns
    (data_dict, status_label) where status_label describes which tier
    succeeded, or 'failed' if both did.
    """
    try:
        data = fetch_via_html_fallback(handle)
        return data, "html"
    except Exception as html_err:
        print(f"html fallback failed, trying rendered-browser fallback...", end=" ")
        try:
            data = fetch_via_selenium_fallback(handle)
            return data, "selenium"
        except Exception as selenium_err:
            return {
                "followers": None, "following": None, "post_count": None,
                "is_private": None, "full_name": None, "biography": None,
                "error": f"html_fallback failed: {html_err} | selenium_fallback failed: {selenium_err}",
            }, "failed"


def fetch_one(L: instaloader.Instaloader, handle: str, cache_path: Path) -> dict:
    """
    Fetch a single profile's public counts. Caches the raw result to disk
    so a crash later in the run doesn't lose work already done today.
    """
    if cache_path.exists():
        return json.loads(cache_path.read_text())

    profile = instaloader.Profile.from_username(L.context, handle)
    result = {
        "followers": profile.followers,
        "following": profile.followees,
        "post_count": profile.mediacount,
        "is_private": profile.is_private,
        "full_name": profile.full_name,
        "biography": profile.biography,
        "error": "",
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(result))
    return result


def run(handles_csv: Path, out_csv: Path, login_user: str = None):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    L = build_loader(login_user=login_user)
    broken_accounts = load_broken_accounts()
    newly_broken = set()

    rows: list[FollowerRecord] = []

    with handles_csv.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        contestants = list(reader)

    print(f"Loaded {len(contestants)} contestants from {handles_csv}")
    if broken_accounts:
        print(f"({len(broken_accounts)} handles known to need the HTML fallback — skipping straight there for those)")

    for i, row in enumerate(contestants, start=1):
        name = row.get("contestant_name", "").strip()
        handle = (row.get("instagram_handle") or "").strip().lstrip("@")
        season = row.get("season", "")
        cast_list = row.get("cast_list", "")

        if not handle:
            rows.append(FollowerRecord(
                date=today, contestant_name=name, instagram_handle="",
                season=season, cast_list=cast_list,
                followers=None, following=None, post_count=None,
                is_private=None, full_name=None, biography=None,
                error="no_handle_provided",
            ))
            continue

        cache_path = CACHE_DIR / today / f"{handle}.json"
        print(f"[{i}/{len(contestants)}] {name} (@{handle})...", end=" ")

        # Known-broken accounts skip the JSON API entirely — no point
        # spending retries on a call that fails 100% of the time for them.
        if handle in broken_accounts:
            data, status = resolve_via_fallback_chain(handle)
            print(f"ok (via {status} fallback, known-broken account)" if status != "failed" else f"ALL FALLBACKS FAILED: {data['error']}")
            rows.append(FollowerRecord(
                date=today, contestant_name=name, instagram_handle=handle,
                season=season, cast_list=cast_list,
                followers=data.get("followers"), following=data.get("following"),
                post_count=data.get("post_count"), is_private=data.get("is_private"),
                full_name=data.get("full_name"), biography=data.get("biography"),
                error=data.get("error", ""),
            ))
            if i < len(contestants):
                time.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))
            continue

        data = None
        attempt = 1
        while attempt <= MAX_RETRIES:
            try:
                data = fetch_one(L, handle, cache_path)
                print("ok" if not data.get("error") else f"cached error: {data['error']}")
                break  # success — stop retrying
            except instaloader.exceptions.ProfileNotExistsException:
                # Permanent — the handle is wrong/doesn't exist. Retrying won't help.
                data = {"followers": None, "following": None, "post_count": None,
                         "is_private": None, "full_name": None, "biography": None,
                         "error": "profile_not_found"}
                print("PROFILE NOT FOUND")
                break
            except instaloader.exceptions.ConnectionException as e:
                # Genuinely transient — rate limiting, network blips. Worth
                # retrying with backoff.
                if attempt == MAX_RETRIES:
                    print(f"JSON API failed after {MAX_RETRIES} attempts (connection issue), trying fallbacks...", end=" ")
                    data, status = resolve_via_fallback_chain(handle)
                    if status != "failed":
                        print(f"ok (via {status} fallback)")
                    else:
                        data["error"] = f"connection_error (after {MAX_RETRIES} attempts): {e} | {data['error']}"
                        print(f"ALL FALLBACKS FAILED: {data['error']}")
                else:
                    wait = RETRY_BACKOFF_SECONDS * attempt
                    print(f"transient error, retrying in {wait}s ({attempt}/{MAX_RETRIES})...", end=" ")
                    time.sleep(wait)
                attempt += 1
            except Exception as e:
                # Not a connection issue — this is the deterministic
                # schema-error / bot-detection pattern. Retrying the JSON
                # call won't fix it, so go straight to the fallback chain
                # and remember this handle so future runs skip the JSON
                # call entirely.
                print(f"JSON API broken for this account, trying fallbacks...", end=" ")
                newly_broken.add(handle)
                data, status = resolve_via_fallback_chain(handle)
                if status != "failed":
                    print(f"ok (via {status} fallback, marked as known-broken)")
                else:
                    data["error"] = f"unexpected: {e} | {data['error']}"
                    print(f"ALL FALLBACKS FAILED: {data['error']}")
                break

        rows.append(FollowerRecord(
            date=today, contestant_name=name, instagram_handle=handle,
            season=season, cast_list=cast_list,
            followers=data.get("followers"), following=data.get("following"),
            post_count=data.get("post_count"), is_private=data.get("is_private"),
            full_name=data.get("full_name"), biography=data.get("biography"),
            error=data.get("error", ""),
        ))

        # polite randomized delay — skip after the very last one
        if i < len(contestants):
            time.sleep(random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))

    if newly_broken:
        broken_accounts |= newly_broken
        save_broken_accounts(broken_accounts)
        print(f"\nAdded {len(newly_broken)} newly-discovered broken account(s) to {BROKEN_ACCOUNTS_FILE.name}: "
              f"{', '.join(sorted(newly_broken))}")

    append_rows(out_csv, rows)
    print(f"Done. Wrote {len(rows)} rows to {out_csv} for {today}.")


def append_rows(out_csv: Path, rows: list[FollowerRecord]):
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    file_exists = out_csv.exists()
    with out_csv.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()))
        if not file_exists:
            writer.writeheader()
        for r in rows:
            writer.writerow(asdict(r))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Track Instagram follower counts over time.")
    parser.add_argument("--handles", type=Path, default=SCRIPT_DIR / "data" / "handles.csv",
                         help="CSV of contestants + instagram_handle column")
    parser.add_argument("--out", type=Path, default=SCRIPT_DIR / "data" / "follower_history.csv",
                         help="Long-format output CSV (appended to daily)")
    parser.add_argument("--login-user", type=str, default=None,
                         help="Instagram username of a DEDICATED (non-personal) account with a "
                              "saved session, for accounts anonymous mode can't reach. See README.")
    args = parser.parse_args()
    run(args.handles, args.out, login_user=args.login_user)


if __name__ == "__main__":
    main()
