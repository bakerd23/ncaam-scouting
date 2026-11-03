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
  - tenant/stats (2026-27, with players): this season's stat lines, keyed by the same player
    ID as the rosters, paged 250 at a time. Blank until a player has played.
  - tenant/stats (2025-26, with players): last season's archive, written to career_stats so
    it shows under Career History (like D1's past seasons from ESPN)

The 2025-26 archive uses a different person ID than 2026-27, so last season is matched on
normalized name: same school first, then any NJCAA school if exactly one 2025-26 player has
that name and exactly one roster player is claiming it. A stat line is never given to two
players.

No advanced rebound/steal/block rates here - those need per-game team and opponent totals,
and NJCAA has no box scores in this feed. FT Rate only needs the player's own numbers, so
it's included, with the same 20-FGA minimum as D1.

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
STATS_SEASON = "2026-27"
LAST_SEASON = "2025-26"
LAST_SEASON_YEAR = 2026  # career_stats.season_year, ESPN-style (2026 = 2025-26)
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


def get_stat_pages(season):
    """Every page of tenant/stats with players for `season` (all NJCAA divisions)."""
    first = njcaa_get(STATS_URL.format(tenant=NJCAA_TENANT_ID, season=season, offset=0))
    total = first.get("playerTotal") or 0
    offsets = range(STATS_PAGE_SIZE, total, STATS_PAGE_SIZE)
    with ThreadPoolExecutor(WORKERS) as pool:
        pages = list(
            pool.map(
                lambda o: njcaa_get(
                    STATS_URL.format(tenant=NJCAA_TENANT_ID, season=season, offset=o)
                ),
                offsets,
            )
        )
    return [first] + pages, total


def get_last_season_stats():
    """Every 2025-26 NJCAA player stat line (all divisions, so a D3-to-D1 move still
    matches). The archive lists players flat, with schoolTenantId/schoolDisplayName."""
    pages, total = get_stat_pages(LAST_SEASON)
    players = [p for page in pages for p in page.get("players") or []]
    if len(players) < total:
        print(f"  warning: got {len(players)} of {total} {LAST_SEASON} stat lines")
    return players


def get_current_stats():
    """{roster player ID: stat line} for this season. The live feed nests players under
    schools. A line claiming more games than the school has played is ignored - a guard
    against last season's numbers showing before a team's first game, as ESPN does."""
    pages, total = get_stat_pages(STATS_SEASON)
    stats, seen = {}, 0
    for page in pages:
        for school in page.get("schools") or []:
            team_gp = _int((school.get("totals") or {}).get("gp")) or 0
            for p in school.get("players") or []:
                seen += 1
                line = p.get("stats") or {}
                gp = _int(line.get("gp")) or 0
                if p.get("playerId") and 0 < gp <= team_gp:
                    stats[str(p["playerId"])] = line
    if seen < total:
        print(f"  warning: got {seen} of {total} {STATS_SEASON} player lines")
    return stats


def stat_line(stats):
    """players-table stat columns from an NJCAA season stat line (totals -> per game)."""
    gp = _int(stats.get("gp")) or 0
    if not gp:
        return {}
    t = {k: _int(stats.get(k)) or 0 for k in (
        "min", "pts", "treb", "ast", "stl", "blk", "to", "fgm", "fga", "ftm", "fta",
    )}
    # The 2025-26 archive spells threes fgm3/fga3; the live 2026-27 feed uses fg3m/fg3a.
    t["fgm3"] = _int(stats.get("fgm3", stats.get("fg3m"))) or 0
    t["fga3"] = _int(stats.get("fga3", stats.get("fg3a"))) or 0

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
CAREER_KEYS = (
    "gp", "min", "ppg", "rpg", "apg", "spg", "bpg", "topg", "fg_pct", "three_pct", "ft_pct",
)


def build_rows(rosters, current_stats, last_season):
    """(players rows, career_stats rows for last season, count with this season's stats)."""
    by_school = {}
    by_name = defaultdict(list)
    for p in last_season:
        key = norm_name(p.get("displayName"))
        by_school[(key, p.get("schoolTenantId"))] = p
        by_name[key].append(p)

    # Pass 1: same-name-same-school matches claim their 2025-26 line outright. Pass 2: a
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
    rows, career, matched = [], [], 0
    for school, a in entries:
        person_id = a.get("personId") or a.get("playerId")
        player_id = f"njcaa-{person_id}"
        key = norm_name(a["name"])
        prev = by_school.get((key, school["tenantId"]))
        if (
            prev is None
            and len(by_name.get(key, [])) == 1
            and roster_name_count[key] == 1
            and by_name[key][0]["personId"] not in claimed
        ):
            prev = by_name[key][0]
        last = stat_line(prev.get("stats") or {}) if prev else {}
        if last:
            career.append({
                "player_id": player_id,
                "season_year": LAST_SEASON_YEAR,
                "season_display": LAST_SEASON,
                "school": prev.get("schoolDisplayName") or "",
                **{k: last[k] for k in CAREER_KEYS},
            })
        stats = stat_line(current_stats.get(str(person_id)) or {})
        matched += bool(stats)
        position = (a.get("position") or "").strip().lower()
        year = (a.get("classYear") or a.get("year") or "").upper()
        row = {
            "player_id": player_id,
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
            # This season's stats come from his current school's feed.
            "stats_school": None,
            "last_updated": now,
        }
        row.update({k: stats.get(k) for k in STAT_KEYS})
        rows.append(row)
    return rows, career, matched


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

    current_stats = get_current_stats()
    print(f"{len(current_stats)} NJCAA players with {STATS_SEASON} stats so far.")
    last_season = get_last_season_stats()
    print(f"{len(last_season)} {LAST_SEASON} stat lines (for Career History).")

    rows, career, matched = build_rows(rosters, current_stats, last_season)
    # One row per player, in case a school lists someone twice.
    rows = list({r["player_id"]: r for r in rows}.values())
    career = list({(c["player_id"], c["school"]): c for c in career}.values())
    print(
        f"{len(rows)} JUCO players, {matched} with {STATS_SEASON} stats, "
        f"{len(career)} with a {LAST_SEASON} season in Career History."
    )

    for i in range(0, len(rows), 500):
        execute(client.table("players").upsert(rows[i : i + 500], on_conflict="player_id"))
    for i in range(0, len(career), 500):
        execute(
            client.table("career_stats").upsert(
                career[i : i + 500], on_conflict="player_id,season_year,school"
            )
        )

    # Only prune when every roster call worked - otherwise a school whose roster just failed
    # to load would start aging out (the 7-day grace period also covers this).
    if not failed:
        pruned = prune_stale_players(client, {r["player_id"] for r in rows}, level="JUCO")
        print(f"Pruned {pruned} JUCO players no longer on any roster.")
    print("Done.")


if __name__ == "__main__":
    main()
