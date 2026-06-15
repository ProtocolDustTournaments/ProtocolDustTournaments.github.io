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

# --- Elo tuning parameters ---
ELO_START_RATING = 1200
ELO_K_FACTOR = 512
# How many *matches* (not tournaments) a player must have played before
# their rating is shown at full confidence. Below this threshold their
# displayed rating is linearly damped back toward ELO_START_RATING.
ELO_FULL_CONFIDENCE_MATCHES = 10
ELO_FULL_CONFIDENCE_PARTICIPANTS = 16
# How much rating a known player loses for skipping a tournament.
# This is applied as a phantom loss against a virtual opponent rated
# at start_rating, scaled by tournament size the same way real matches are.
ELO_ABSENCE_PENALTY = 0.25  # fraction of scaled_k lost per skipped tournament


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


def fetch_participants(tournament_id):
    try:
        data = api_get(f"/tournaments/{tournament_id}/participants")
    except Exception as e:
        print(f"Warning: could not fetch participants for tournament {tournament_id}: {e}", file=sys.stderr)
        return []
    return [item["participant"] for item in data]


def get_winner(tournament_id):
    """Return {'name': ..., 'avatar_url': ...} for the rank-1 participant, or None."""
    participants = fetch_participants(tournament_id)
    winner = next((p for p in participants if p.get("final_rank") == 1), None)
    if not winner:
        return None

    name = (
        winner.get("display_name")
        or winner.get("name")
        or winner.get("challonge_username")
        or winner.get("username")
        or "Unknown"
    )

    avatar_url = None
    if winner.get("attached_participatable_portrait_url"):
        avatar_url = winner["attached_participatable_portrait_url"]
    elif winner.get("email_hash"):
        avatar_url = f"https://www.gravatar.com/avatar/{winner['email_hash']}?d=mp&s=64"

    return {"name": name, "avatar_url": avatar_url}


def fetch_matches(tournament_id):
    try:
        data = api_get(f"/tournaments/{tournament_id}/matches")
    except Exception as e:
        print(f"Warning: could not fetch matches for tournament {tournament_id}: {e}", file=sys.stderr)
        return []
    return [item["match"] for item in data]


def player_key(p):
    """A stable identity key for a participant, preferring Challonge username."""
    cu = (p.get("challonge_username") or p.get("username") or "").strip().lower()
    if cu:
        return f"user:{cu}"
    name = (p.get("display_name") or p.get("name") or "").strip().lower()
    return f"name:{name}"


def player_display_name(p):
    return p.get("display_name") or p.get("name") or p.get("challonge_username") or p.get("username") or "Unknown"


def compute_elo(
    completed_tournaments_with_data,
    start_rating=ELO_START_RATING,
    k=ELO_K_FACTOR,
    full_confidence_matches=ELO_FULL_CONFIDENCE_MATCHES,
    full_confidence_participants=ELO_FULL_CONFIDENCE_PARTICIPANTS,
    absence_penalty=ELO_ABSENCE_PENALTY,
):
    """
    completed_tournaments_with_data: list of dicts with keys:
        - start_at: ISO date string (for ordering)
        - participants: list of participant dicts
        - matches: list of match dicts (with winner_id, loser_id, state)

    Returns a list of dicts:
        {name, rating, raw_rating, confidence,
         tournaments_played, matches_played, wins, losses}

    Dampening is based on *matches played* (not tournaments entered) so
    that a player who entered many events but rarely had decisive matches
    is still treated as low-confidence. Confidence scales linearly from 0
    to 1 over the first `full_confidence_matches` completed matches, after
    which the displayed rating equals the raw Elo rating.

    Each match's K factor is also scaled by tournament size relative to
    `full_confidence_participants`. A 4-person tournament scales K down;
    a 32-person tournament scales K up proportionally.

    Players who skip a tournament receive a phantom-loss penalty scaled
    by `absence_penalty` and tournament size, so staying active matters.
    """
    ratings = {}
    names = {}
    wins = {}
    losses = {}
    tournaments_played = {}
    matches_played = {}  # decisive matches (complete state with a winner/loser)
    tournaments_skipped = {}

    # Process tournaments in chronological order
    ordered = sorted(completed_tournaments_with_data, key=lambda t: t["start_at"] or "")

    for t in ordered:
        # Map participant id -> player_key
        id_to_key = {}
        for p in t["participants"]:
            key = player_key(p)
            id_to_key[p["id"]] = key
            names[key] = player_display_name(p)
            ratings.setdefault(key, start_rating)
            wins.setdefault(key, 0)
            losses.setdefault(key, 0)
            matches_played.setdefault(key, 0)
            tournaments_skipped.setdefault(key, 0)
            tournaments_played[key] = tournaments_played.get(key, 0) + 1

        # Scale K by tournament size: more players = higher stakes.
        # At full_confidence_participants the multiplier is exactly 1.0.
        num_participants = len(t["participants"])
        size_scale = num_participants / full_confidence_participants if full_confidence_participants > 0 else 1.0
        scaled_k = k * size_scale

        # Process matches in id order as a stable approximation of chronological order
        match_list = sorted(t["matches"], key=lambda m: m.get("id") or 0)

        for m in match_list:
            if m.get("state") != "complete":
                continue
            winner_id = m.get("winner_id")
            loser_id = m.get("loser_id")
            if not winner_id or not loser_id:
                continue
            if winner_id not in id_to_key or loser_id not in id_to_key:
                continue

            wk = id_to_key[winner_id]
            lk = id_to_key[loser_id]

            r_winner = ratings[wk]
            r_loser = ratings[lk]

            expected_winner = 1 / (1 + 10 ** ((r_loser - r_winner) / 400))
            expected_loser = 1 - expected_winner

            ratings[wk] = r_winner + scaled_k * (1 - expected_winner)
            ratings[lk] = r_loser + scaled_k * (0 - expected_loser)

            wins[wk] += 1
            losses[lk] += 1
            matches_played[wk] += 1
            matches_played[lk] += 1

        # Absence decay: players who exist but skipped this tournament
        # lose rating as if they took a phantom loss against a start_rating
        # opponent, scaled by tournament size.
        if absence_penalty > 0:
            for key in list(ratings.keys()):
                if key not in id_to_key.values():
                    r = ratings[key]
                    expected = 1 / (1 + 10 ** ((start_rating - r) / 400))
                    ratings[key] = r + scaled_k * absence_penalty * (0 - expected)
                    tournaments_skipped[key] = tournaments_skipped.get(key, 0) + 1

    leaderboard = []
    for key, raw_rating in ratings.items():
        played = matches_played.get(key, 0)

        # Confidence: 0.0 → 1.0 based on matches with decisive outcomes.
        # A new player with 0 matches stays pinned at start_rating.
        # At full_confidence_matches they earn their full raw Elo.
        confidence = min(played / full_confidence_matches, 1.0) if full_confidence_matches > 0 else 1.0
        displayed_rating = start_rating + (raw_rating - start_rating) * confidence

        leaderboard.append({
            "name": names[key],
            "rating": round(displayed_rating),
            "raw_rating": round(raw_rating),
            "confidence": round(confidence, 3),  # expose so frontends can show a "?" badge
            "tournaments_played": tournaments_played.get(key, 0),
            "matches_played": played,
            "wins": wins[key],
            "losses": losses[key],
            "tournaments_skipped": tournaments_skipped.get(key, 0),
        })

    leaderboard.sort(key=lambda r: r["rating"], reverse=True)
    return leaderboard


def to_record(t):
    subdomain = t.get("subdomain")
    url_part = t.get("url")
    if subdomain:
        full_url = f"https://{subdomain}.challonge.com/{url_part}"
    else:
        full_url = f"https://challonge.com/{url_part}"

    record = {
        "_id": t["id"],
        "name": t.get("name"),
        "state": t.get("state"),
        "participants_count": t.get("participants_count"),
        "start_at": t.get("start_at"),
        "url": url_part,
        "full_challonge_url": full_url,
        "game_name": t.get("game_name"),
    }

    if t.get("state") == "complete":
        winner = get_winner(t["id"])
        if winner:
            record["winner"] = winner

    return record


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

    # Sort: in-progress first, then upcoming (earliest start first), then
    # completed (most recent first).
    others = [r for r in results if r["state"] != "complete"]
    complete = [r for r in results if r["state"] == "complete"]
    others.sort(key=lambda r: (state_order.get(r["state"], 3), r["start_at"] or ""))
    complete.sort(key=lambda r: r["start_at"] or "", reverse=True)
    results = others + complete

    output = {
        "game": GAME_NAME,
        "source_accounts": CHALLONGE_USERNAMES,
        "last_updated": datetime.utcnow().isoformat() + "Z",
        "tournaments": [{k: v for k, v in r.items() if k != "_id"} for r in results],
    }

    with open("tournaments.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {len(results)} tournament(s) to tournaments.json")

    # --- Elo leaderboard ---
    completed_data = []
    for r in complete:
        # Find the original tournament dict to get its id
        tid = r.get("_id")
        if tid is None:
            continue
        participants = fetch_participants(tid)
        matches = fetch_matches(tid)
        completed_data.append({
            "start_at": r["start_at"],
            "participants": participants,
            "matches": matches,
        })

    leaderboard = compute_elo(
        completed_data,
        start_rating=ELO_START_RATING,
        k=ELO_K_FACTOR,
        full_confidence_matches=ELO_FULL_CONFIDENCE_MATCHES,
        full_confidence_participants=ELO_FULL_CONFIDENCE_PARTICIPANTS,
        absence_penalty=ELO_ABSENCE_PENALTY,
    )

    # Hide players with no recorded wins or losses (e.g. never had a
    # decisive match counted)
    leaderboard = [p for p in leaderboard if not (p["wins"] == 0 and p["losses"] == 0)]

    leaderboard_output = {
        "game": GAME_NAME,
        "last_updated": datetime.utcnow().isoformat() + "Z",
        "starting_rating": ELO_START_RATING,
        "k_factor": ELO_K_FACTOR,
        "full_confidence_matches": ELO_FULL_CONFIDENCE_MATCHES,
        "full_confidence_participants": ELO_FULL_CONFIDENCE_PARTICIPANTS,
        "absence_penalty": ELO_ABSENCE_PENALTY,
        "players": leaderboard,
    }

    with open("leaderboard.json", "w") as f:
        json.dump(leaderboard_output, f, indent=2)

    print(f"Wrote {len(leaderboard)} player(s) to leaderboard.json")


if __name__ == "__main__":
    main()
