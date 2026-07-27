#!/usr/bin/env python3
"""
Regal Irvine Spectrum -> IMAX 70mm "The Odyssey" seat-opening watcher
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta

import requests

# ------------------------- CONFIG -------------------------

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
CINEMA_ID = "1010"
MOVIE_KEYWORD = "odyssey"
FORMAT_KEYWORDS = ["70mm", "imax"]
DAYS_AHEAD = 21
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "odyssey_state.json")
DEBUG = False

THEATRE_PAGE = "https://www.regmovies.com/theatres/regal-edwards-irvine-spectrum-1010"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": THEATRE_PAGE,
    "Origin": "https://www.regmovies.com",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def warm_up_session():
    try:
        SESSION.get(THEATRE_PAGE, timeout=20)
    except Exception as e:
        print(f"[warmup] failed: {e}")


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
    url = (
        "https://www.regmovies.com/api/getShowtimes"
        f"?theatres={CINEMA_ID}&date={date_str}&hoCode=&ignoreCache=false&moviesOnly=false"
    )
    try:
        resp = SESSION.get(url, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[fetch] {date_str}: request failed ({e})")
        return None


def looks_like_odyssey(title):
    return MOVIE_KEYWORD in (title or "").lower()


def matches_70mm_imax(performance, movie):
    blob = json.dumps(performance).lower() + " " + json.dumps(movie).lower()
    return all(k in blob for k in [f.lower() for f in FORMAT_KEYWORDS])


def find_availability_signal(performance):
    signals = {}
    for k, v in performance.items():
        lk = k.lower()
        if "sold" in lk or "seat" in lk or "avail" in lk:
            signals[k] = v

    available_guess = None
    for k, v in signals.items():
        lk = k.lower()
        if "sold" in lk:
            if isinstance(v, bool):
                available_guess = not v
        elif "seat" in lk or "avail" in lk:
            if isinstance(v, (int, float)):
                available_guess = v > 0
            elif isinstance(v, bool):
                available_guess = v

    return signals, available_guess


def scan():
    warm_up_session()

    today = datetime.now()
    dates = [(today + timedelta(days=i)).strftime("%m-%d-%Y") for i in range(DAYS_AHEAD)]

    found = {}

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
