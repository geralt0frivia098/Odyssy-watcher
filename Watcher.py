#!/usr/bin/env python3
"""
Regal Irvine Spectrum -> IMAX 70mm "The Odyssey" seat-opening watcher
----------------------------------------------------------------------
Polls Regal's own showtimes API (the same one regmovies.com uses) for
Regal Irvine Spectrum, looks for IMAX 70mm showings of "The Odyssey",
and pushes a notification to your phone via ntfy.sh when:

  1. A brand-new IMAX 70mm showtime for the movie appears, or
  2. A showtime that was sold out now shows availability again.

SETUP
-----
1. pip install requests
2. Pick a private ntfy topic name (long/random, e.g. "odyssey-irvine-x7q2p")
   and set NTFY_TOPIC below.
3. In the ntfy app (iOS): tap "+", subscribe to that exact topic name.
4. Run this script on a schedule (cron / Task Scheduler / launchd), e.g.
   every 15 minutes:

     */15 * * * * /usr/bin/python3 /path/to/regal_odyssey_watcher.py >> /path/to/watcher.log 2>&1

   Don't poll much more often than that -- Regal's site sits behind
   Cloudflare and aggressive polling risks getting temporarily blocked.

NOTES / CAVEATS
----------------
- Regal's API isn't officially public and its JSON field names aren't
  documented anywhere, so this script is written defensively: it scans
  performance records for ANY field that looks like it relates to sold
  out status or seat counts, rather than hardcoding one exact key name.
  The first time you run it with DEBUG=True, it will print the raw
  fields it sees for a matching performance so you can confirm it's
  reading the right thing.
- If Regal ever returns a Cloudflare challenge page instead of JSON,
  the script will just log an error and skip that run -- it won't
  crash your cron job.
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta

import requests

# ------------------------- CONFIG -------------------------

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
CINEMA_ID = "1010"  # Regal Irvine Spectrum (Edwards Irvine Spectrum ScreenX/IMAX/RPX/VIP)
MOVIE_KEYWORD = "odyssey"
FORMAT_KEYWORDS = ["70mm", "imax"]  # a matching performance/attributes must mention these
DAYS_AHEAD = 21  # how many days out to check
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "odyssey_state.json")
DEBUG = False  # set True once to sanity-check the raw fields Regal returns

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.regmovies.com/theatres/regal-edwards-irvine-spectrum-1010",
}

# ------------------------------------------------------------


def ntfy(title, message, priority="high", tags="ticket"):
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=15,
        )
    except Exception as e:
        print(f"[ntfy] failed to send notification: {e}")


def fetch_showtimes(date_str):
    """date_str format: MM-DD-YYYY"""
    url = (
        "https://www.regmovies.com/api/getShowtimes"
        f"?theatres={CINEMA_ID}&date={date_str}&hoCode=&ignoreCache=false&moviesOnly=false"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[fetch] {date_str}: request failed ({e})")
        return None


def looks_like_odyssey(title):
    return MOVIE_KEYWORD in (title or "").lower()


def matches_70mm_imax(performance, movie):
    """
    Check the performance (and, as a fallback, the parent movie record) for
    any text that indicates IMAX 70mm. Regal's Vista-based API usually
    tucks this into an "Attributes" list on the performance.
    """
    blob = json.dumps(performance).lower() + " " + json.dumps(movie).lower()
    return all(k in blob for k in [f.lower() for f in FORMAT_KEYWORDS])


def find_availability_signal(performance):
    """
    Defensive scan: look for any key whose name suggests sold-out status
    or seat counts, since the exact schema isn't documented. Returns a
    dict of {key: value} for anything that looks relevant, plus a best
    guess "available" boolean if it can figure one out.
    """
    signals = {}
    for k, v in performance.items():
        lk = k.lower()
        if "sold" in lk or "seat" in lk or "avail" in lk:
            signals[k] = v

    available_guess = None
    for k, v in signals.items():
        lk = k.lower()
        if "sold" in lk:
            # SoldOut True -> not available
            if isinstance(v, bool):
                available_guess = not v
        elif "seat" in lk or "avail" in lk:
            if isinstance(v, (int, float)):
                available_guess = v > 0
            elif isinstance(v, bool):
                available_guess = v

    return signals, available_guess


def scan():
    today = datetime.now()
    dates = [(today + timedelta(days=i)).strftime("%m-%d-%Y") for i in range(DAYS_AHEAD)]

    found = {}  # performance_id -> details dict

    for date_str in dates:
        data = fetch_showtimes(date_str)
        if not data:
            continue

        shows = data.get("shows") or []
        if not shows:
            continue
        movies = shows[0].get("Film", [])

        for movie in movies:
            title = movie.get("Title", "")
            if not looks_like_odyssey(title):
                continue

            for perf in movie.get("Performances", []):
                if not matches_70mm_imax(perf, movie):
                    continue

                perf_id = perf.get("PerformanceId") or perf.get("id") or f"{date_str}-{perf.get('CalendarShowTime')}"
                signals, available_guess = find_availability_signal(perf)
                show_time = perf.get("CalendarShowTime", f"{date_str}T??:??")

                found[perf_id] = {
                    "date": date_str,
                    "time": show_time,
                    "title": title,
                    "available": available_guess,
                    "signals": signals,
                }

                if DEBUG:
                    print(f"[debug] {title} {show_time} raw performance fields:")
                    print(json.dumps(perf, indent=2))

    return found


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def main():
    old_state = load_state()
    new_state = scan()

    if not new_state and not old_state:
        print("No IMAX 70mm 'The Odyssey' showtimes found yet at Regal Irvine Spectrum.")

    for perf_id, info in new_state.items():
        old_info = old_state.get(perf_id)

        if old_info is None:
            # brand new showtime appeared
            msg = f"{info['title']} - IMAX 70mm\n{info['date']} {info['time']}"
            print(f"[new] {msg}")
            ntfy(
                "New IMAX 70mm Odyssey showtime!",
                msg + "\nRegal Irvine Spectrum - book now.",
                tags="clapper",
            )
        else:
            was_avail = old_info.get("available")
            now_avail = info.get("available")
            if was_avail is False and now_avail is True:
                msg = f"{info['title']} - IMAX 70mm\n{info['date']} {info['time']}"
                print(f"[opened] {msg}")
                ntfy(
                    "Seats just opened up!",
                    msg + "\nRegal Irvine Spectrum - go grab one.",
                    tags="rotating_light",
                )

    save_state(new_state)


if __name__ == "__main__":
    if not NTFY_TOPIC:
        print("NTFY_TOPIC is not set. Add it as a GitHub Secret named NTFY_TOPIC.")
        sys.exit(1)
    main()
