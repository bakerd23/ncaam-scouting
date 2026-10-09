"""
Daily NJCAA (junior college) men's basketball scraper - Division I and II only.

Upserts one players row per player on a posted NJCAA 2026-27 roster, marked level = 'JUCO',
so JUCO players can be scouted exactly like D1 players (reports hang off players.player_id).
Schools that haven't posted a 2026-27 roster yet simply have no players until they do; this
picks them up on the next nightly run.

Everything comes from the public API behind njcaa.org (an Ardor Sports Hub site), via curl
like the ESPN JSON calls:
  - tenant/stats (2026-27, no players): every NJCAA school with its division, region and
    team-season ID
  - team/roster: one call per D1/D2 school for its 2026-27 roster
  - tenant/stats (2025-26, with players): last season's full stat lines, paged 250 at a time

Last season's stats are attached to returning players. The 2026-27 rosters use a different
person ID than the 2025-26 archive, so the match is on normalized name: same school first,
then any NJCAA school if exactly one 2025-26 player has that name and exactly one roster
player is claiming it (stats_school then records where the stats are from). A stat line is
never given to two players. Incoming freshmen have no stats, which is expected.

No advanced rebound/steal/block rates here - those need per-game team and opponent totals,
and NJCAA's archive has no box scores. FT Rate only needs the player's own numbers, so it's
included, with the same 20-FGA minimum as D1.

Env vars required: SUPABASE_URL, SUPABASE_SERVICE_KEY (same as scrape_espn.py).
"""

import json
import os
import re
import subprocess
import sys
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from supabase import create_client

from scrape_espn import MIN_FGA_FOR_FT_RATE, execute, prune_stale_players

NJCAA_TENANT_ID = "404933495157163011"
ROSTER_SEASON = "2026-27"
STATS_SEASON = "2025-26"
DIVISIONS = {"d1": "D1", "d2": "D2"}

API = "https://0wtr7r0cl7.execute-api.us-east-2.amazonaws.com/prod"
SCHOOLS_URL = (
    API + "/tenant/stats?tenant_id={tenant}&season={season}&sport=mbkb"
    "&include_players=false&include_games=false"
)
ROSTER_URL = (
    API + "/team/roster?tenant_id={tenant}&team_season_id={team}&sport=mbkb"
    "&season={season}&limit=1000"
)
STATS_URL = (
    API + "/tenant/stats?tenant_id={tenant}&season={season}&sport=mbkb"
    "&include_players=true&include_games=false&player_offset={offset}"
)
STATS_PAGE_SIZE = 250

# Some schools' "2026-27" rosters on NJCAA are really last season's roster carried over -
# 100% the same names, sophomores who've already left for D1 included. JUCO rosters normally
# turn over half or more each year (2026-10-08: 70 of 104 posted rosters had under 50% of
# last year's team; 28 had 75-100%). A roster where at least this share played for the
# same school last season is treated as not posted yet, and drops back in automatically
# once the school puts up its real one.
STALE_ROSTER_SHARE = 0.75
WORKERS = 6

POSITIONS = {
    "g": "G", "guard": "G", "pg": "PG", "sg": "SG", "wing": "W", "w": "W",
    "f": "F", "forward": "F", "sf": "SF", "pf": "PF", "c": "C", "center": "C",
}
CLASSES = {"FR": "FR", "SO": "SO", "JR": "JR", "SR": "SR"}


def njcaa_get(url):
    """GET JSON from the NJCAA API. Its responses are gzip-encoded regardless of what the
    client asks for, so curl needs --compressed."""
    result = subprocess.run(
        ["curl", "-sS", "--compressed", "--max-time", "60", "-w", "\n%{http_code}", url],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl exit {result.returncode} for {url}: {result.stderr!r}")
    body, _, status = result.stdout.decode("utf-8", errors="replace").rpartition("\n")
    if int(status) >= 400:
        raise RuntimeError(f"HTTP {status} for {url}")
    return json.loads(body)


def norm_name(name):
    """'Ja'Kobi Smith Jr.' -> 'jakobismith', for matching names across the two ID systems."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", s)
    return re.sub(r"[^a-z]", "", s)


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def get_schools():
    data = njcaa_get(SCHOOLS_URL.format(tenant=NJCAA_TENANT_ID, season=ROSTER_SEASON))
    return [
        s
        for s in data.get("schools", [])
        if s.get("gender") == "mens" and s.get("division") in DIVISIONS
    ]


def get_roster(school):
    try:
        data = njcaa_get(
            ROSTER_URL.format(tenant=NJCAA_TENANT_ID, team=school["teamId"], season=ROSTER_SEASON)
        )
        return school, data.get("roster") or []
    except Exception as e:
        print(f"    roster error ({school.get('school')}): {e}")
        return school, None


def get_last_season_stats():
    """Every 2025-26 NJCAA player stat line (all divisions, so a D3-to-D1 move still
    matches)."""
    first = njcaa_get(STATS_URL.format(tenant=NJCAA_TENANT_ID, season=STATS_SEASON, offset=0))
    total = first.get("playerTotal") or 0
    offsets = range(STATS_PAGE_SIZE, total, STATS_PAGE_SIZE)
    with ThreadPoolExecutor(WORKERS) as pool:
        pages = list(
            pool.map(
                lambda o: njcaa_get(
                    STATS_URL.format(tenant=NJCAA_TENANT_ID, season=STATS_SEASON, offset=o)
                ).get("players")
                or [],
                offsets,
            )
        )
    players = (first.get("players") or []) + [p for page in pages for p in page]
    if len(players) < total:
        print(f"  warning: got {len(players)} of {total} 2025-26 stat lines")
    return players


def stat_line(stats):
    """players-table stat columns from an NJCAA season stat line (totals -> per game)."""
    gp = _int(stats.get("gp")) or 0
    if not gp:
        return {}
    t = {k: _int(stats.get(k)) or 0 for k in (
        "min", "pts", "treb", "ast", "stl", "blk", "to",
        "fgm", "fga", "fgm3", "fga3", "ftm", "fta",
    )}

    def pg(v):
        return round(v / gp, 1)

    def pct(made, att):
        return round(100 * made / att, 1) if att else None

    return {
        "gp": gp,
        "min": pg(t["min"]) if t["min"] else None,
        "ppg": pg(t["pts"]),
        "rpg": pg(t["treb"]),
        "apg": pg(t["ast"]),
        "spg": pg(t["stl"]),
        "bpg": pg(t["blk"]),
        "topg": pg(t["to"]),
        "fg_pct": pct(t["fgm"], t["fga"]),
        "three_pct": pct(t["fgm3"], t["fga3"]),
        "ft_pct": pct(t["ftm"], t["fta"]),
        "ft_rate": round(t["fta"] / t["fga"], 3) if t["fga"] >= MIN_FGA_FOR_FT_RATE else None,
        "tot_min": t["min"] or None,
        "tot_pts": t["pts"],
        "tot_reb": t["treb"],
        "tot_ast": t["ast"],
        "tot_stl": t["stl"],
        "tot_blk": t["blk"],
        "tot_tov": t["to"],
        "fgm": t["fgm"],
        "fga": t["fga"],
        "fg3m": t["fgm3"],
        "fg3a": t["fga3"],
        "ftm": t["ftm"],
        "fta": t["fta"],
        "fga_pg": pg(t["fga"]),
        "fg3a_pg": pg(t["fga3"]),
        "fta_pg": pg(t["fta"]),
    }


STAT_KEYS = tuple(stat_line({"gp": 1}).keys())


def split_stale_rosters(rosters, last_season):
    """(current, stale): stale rosters are mostly last season's team at the same school."""
    played_here = {(norm_name(p.get("displayName")), p.get("schoolTenantId")) for p in last_season}
    current, stale = [], []
    for school, roster in rosters:
        returning = sum(
            1 for a in roster if (norm_name(a.get("name")), school["tenantId"]) in played_here
        )
        share = returning / len(roster) if roster else 0
        (stale if share >= STALE_ROSTER_SHARE else current).append((school, roster))
    return current, stale


def delete_players(client, player_ids):
    """Removes JUCO players right away (no 7-day wait) - for schools whose posted roster is
    really last year's. A player with scouting reports is kept."""
    ids = list(player_ids)
    with_reports = set()
    for i in range(0, len(ids), 200):
        with_reports |= {
            r["player_id"]
            for r in execute(
                client.table("reports").select("player_id").in_("player_id", ids[i : i + 200])
            ).data
        }
    to_delete = [pid for pid in ids if pid not in with_reports]
    deleted = 0
    for i in range(0, len(to_delete), 200):
        deleted += len(
            execute(
                client.table("players")
                .delete()
                .eq("level", "JUCO")
                .in_("player_id", to_delete[i : i + 200])
            ).data
        )
    return deleted, len(with_reports)


def build_rows(rosters, last_season):
    by_school = {}
    by_name = defaultdict(list)
    for p in last_season:
        key = norm_name(p.get("displayName"))
        by_school[(key, p.get("schoolTenantId"))] = p
        by_name[key].append(p)

    # Pass 1: same-name-same-school matches claim their stat line outright. Pass 2: a
    # name-only match (player changed schools) only counts if nobody else claimed that line
    # and he's the only roster player with that name - two Malcolm Currys on 2026-27 rosters
    # and one 2025-26 Malcolm Curry means we can't tell which one it is.
    entries = [
        (school, a)
        for school, roster in rosters
        for a in roster
        if (a.get("personId") or a.get("playerId")) and a.get("name")
    ]
    claimed = {}
    for school, a in entries:
        prev = by_school.get((norm_name(a["name"]), school["tenantId"]))
        if prev:
            claimed[prev["personId"]] = a
    roster_name_count = defaultdict(int)
    for _, a in entries:
        roster_name_count[norm_name(a["name"])] += 1

    now = datetime.now(timezone.utc).isoformat()
    rows, matched = [], 0
    for school, a in entries:
        person_id = a.get("personId") or a.get("playerId")
        key = norm_name(a["name"])
        prev = by_school.get((key, school["tenantId"]))
        if (
            prev is None
            and len(by_name.get(key, [])) == 1
            and roster_name_count[key] == 1
            and by_name[key][0]["personId"] not in claimed
        ):
            prev = by_name[key][0]
        stats = stat_line(prev.get("stats") or {}) if prev else {}
        matched += bool(stats)
        position = (a.get("position") or "").strip().lower()
        year = (a.get("classYear") or a.get("year") or "").upper()
        row = {
            "player_id": f"njcaa-{person_id}",
            "name": a["name"].strip(),
            "school": school.get("school") or a.get("schoolName"),
            "conference": school.get("regionName"),
            "level": "JUCO",
            "juco_division": DIVISIONS[school["division"]],
            # Combined positions come through as "g_f" -> "G/F".
            "position": POSITIONS.get(position, position.replace("_", "/").upper() or None),
            "class": CLASSES.get(year),
            "height": a.get("height") or None,
            "weight": a.get("weight") or None,
            "hometown": a.get("hometown") or None,
            "high_school": a.get("highSchool") or None,
            "record": None,
            "stats_school": (
                prev.get("schoolDisplayName")
                if stats and prev.get("schoolTenantId") != school["tenantId"]
                else None
            ),
            "last_updated": now,
        }
        row.update({k: stats.get(k) for k in STAT_KEYS})
        rows.append(row)
    return rows, matched


def main():
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not supabase_url or not supabase_key:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY env vars are required.")
        sys.exit(1)
    client = create_client(supabase_url, supabase_key)

    schools = get_schools()
    print(f"NJCAA D1/D2 men's basketball schools: {len(schools)}")
    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(get_roster, schools))
    rosters = [(s, r) for s, r in results if r]
    failed = sum(1 for _, r in results if r is None)
    print(f"{len(rosters)} schools have a {ROSTER_SEASON} roster posted ({failed} fetch errors).")

    last_season = get_last_season_stats()
    print(f"{len(last_season)} {STATS_SEASON} stat lines.")

    rosters, stale = split_stale_rosters(rosters, last_season)
    print(
        f"Skipping {len(stale)} rosters that are mostly last season's team (not really "
        f"{ROSTER_SEASON} yet): " + ", ".join(sorted(s["school"] for s, _ in stale))
    )

    rows, matched = build_rows(rosters, last_season)
    # One row per player, in case a school lists someone twice.
    rows = list({r["player_id"]: r for r in rows}.values())
    print(f"{len(rows)} JUCO players, {matched} with {STATS_SEASON} stats.")

    for i in range(0, len(rows), 500):
        execute(client.table("players").upsert(rows[i : i + 500], on_conflict="player_id"))

    current_ids = {r["player_id"] for r in rows}
    stale_ids = {
        f"njcaa-{a.get('personId') or a.get('playerId')}"
        for _, roster in stale
        for a in roster
        if a.get("personId") or a.get("playerId")
    } - current_ids
    if stale_ids:
        deleted, kept = delete_players(client, stale_ids)
        print(f"Removed {deleted} players from last-season rosters ({kept} kept for their reports).")

    # Only prune when every roster call worked - otherwise a school whose roster just failed
    # to load would start aging out (the 7-day grace period also covers this).
    if not failed:
        pruned = prune_stale_players(client, {r["player_id"] for r in rows}, level="JUCO")
        print(f"Pruned {pruned} JUCO players no longer on any roster.")
    print("Done.")


if __name__ == "__main__":
    main()
