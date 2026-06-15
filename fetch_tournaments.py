#!/usr/bin/env python3
"""
fetch_tournaments.py

Fetches all tournaments belonging to a Challonge organizer account
(subdomain/username) via the v1 API, filters them to those matching
GAME_NAME, and writes the result to tournaments.json for use by
index.html.

Usage:
    CHALLONGE_API_KEY=your_key_here python fetch_tournaments.py

Get an API key from: https://challonge.com/settings/developer
"""

import os
import json
import sys
import urllib.request
import urllib.parse
from datetime import datetime

API_BASE = "https://api.challonge.com/v1"
GAME_NAME = "DUST: Virtual Combat"

# Challonge usernames/subdomains whose tournaments we scan.
CHALLONGE_USERNAMES = [
    "Cubeking",
    # "anotherusername",
]


def api_get(path, params=None):
    api_key = os.environ.get("CHALLONGE_API_KEY")
    if not api_key:
        sys.exit("Error: CHALLONGE_API_KEY environment variable not set.")

    params = params or {}
    params["api_key"] = api_key
    query = urllib.parse.urlencode(params)
    url = f"{API_BASE}{path}.json?{query}"

    req = urllib.request.Request(url, headers={"User-Agent": "dust-vc-tournament-site"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def fetch_for_user(username):
    try:
        # No subdomain param: returns tournaments owned by the
        # account that the API key belongs to.
        data = api_get("/tournaments")
    except Exception as e:
        print(f"Warning: could not fetch tournaments for '{username}': {e}", file=sys.stderr)
        return []
    return [item["tournament"] for item in data]


def matches_game(t):
    game = (t.get("game_name") or "").strip().lower()
    return game == GAME_NAME.strip().lower()


def to_record(t):
    subdomain = t.get("subdomain")
    url_part = t.get("url")
    if subdomain:
        full_url = f"https://{subdomain}.challonge.com/{url_part}"
    else:
        full_url = f"https://challonge.com/{url_part}"

    return {
        "name": t.get("name"),
        "state": t.get("state"),
        "participants_count": t.get("participants_count"),
        "start_at": t.get("start_at"),
        "url": url_part,
        "full_challonge_url": full_url,
        "game_name": t.get("game_name"),
    }


def main():
    results = []
    seen_ids = set()

    for username in CHALLONGE_USERNAMES:
        for t in fetch_for_user(username):
            if matches_game(t) and t["id"] not in seen_ids:
                results.append(to_record(t))
                seen_ids.add(t["id"])

    if not results:
        # Fallback: if nothing matched game_name exactly (e.g. it wasn't
        # set on some tournaments), include all tournaments from these
        # accounts so nothing gets silently dropped.
        for username in CHALLONGE_USERNAMES:
            for t in fetch_for_user(username):
                if t["id"] not in seen_ids:
                    results.append(to_record(t))
                    seen_ids.add(t["id"])

    state_order = {"underway": 0, "pending": 1, "complete": 2}
    results.sort(key=lambda r: (state_order.get(r["state"], 3), r["start_at"] or ""))

    output = {
        "game": GAME_NAME,
        "source_accounts": CHALLONGE_USERNAMES,
        "last_updated": datetime.utcnow().isoformat() + "Z",
        "tournaments": results,
    }

    with open("tournaments.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {len(results)} tournament(s) to tournaments.json")


if __name__ == "__main__":
    main()
