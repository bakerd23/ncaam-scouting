"""
Daily D1 men's college basketball scraper.

Pulls every D1 team from ESPN's standings page (which also gives conference + record),
then scrapes each team's roster + season stats pages, and upserts one row per player
into the Supabase `players` table. Scouting `reports` are a separate table this script
never touches, so daily stat refreshes never disturb submitted scouting reports.

Fetches go through a real headless Chromium browser (Playwright), not a plain HTTP
client. ESPN's standings/team pages sit behind AWS WAF Bot Control, which serves a
JS proof-of-work challenge (HTTP 202, no real content) to non-browser clients -
confirmed against both `requests` and bare `curl` with full browser-style headers,
both from GitHub Actions runners specifically. A real browser executes that challenge
transparently, the same way it would for any ordinary site visitor.

Every player's real ESPN athlete ID is recovered from the roster/stats pages' own
profile links (pandas.read_html drops them, so a parallel BeautifulSoup pass pulls the
href instead) - no separate ID-lookup request needed, and it covers players who've
already left the roster too, since the stats page still lists them for games already
played even after the roster page drops them.

Career history (the career_stats upsert) uses a separate ESPN JSON endpoint
(site.web.api.espn.com's athlete stats API) keyed on that ID. It isn't behind the WAF
challenge above, but it does block Python's `requests` specifically (every call 403'd in
a full production run) while a bare `curl` with no custom headers - not even a spoofed
User-Agent - gets through cleanly. So this also shells out to curl, just without the
browser-header dressing the WAF-protected pages need.

Career history only runs when INCLUDE_CAREER_STATS is set (see below) - past seasons
never change, so there's no reason to re-fetch 5,000+ players' full histories on every
daily run. Re-run manually with it enabled whenever ESPN rolls over to a new season.

Advanced metrics (ORB%, DRB%, STL%, BLK%, FT Rate) need team and *opponent* season totals,
which no ESPN page or endpoint exposes directly. So every completed game's box score (both
teams' lines plus every player's line, from the same curl-friendly JSON API as career
history) is cached in the game_team_stats / game_player_stats tables - a game is only ever
fetched once - and season totals are summed from that cache on each run. ADVANCED_SEASON
pins which season this covers.

Env vars required:
  SUPABASE_URL
  SUPABASE_SERVICE_KEY   (service_role key - bypasses RLS, never expose to the browser)

Optional:
  TEAM_LIMIT             - if set, only scrape the first N teams (quick local test run)
  INCLUDE_CAREER_STATS   - if "1"/"true"/"yes", also fetch/upsert career history
"""

import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from io import StringIO

import httpx
import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from supabase import create_client

STANDINGS_URL = "https://www.espn.com/mens-college-basketball/standings"
ROSTER_URL = "https://www.espn.com/mens-college-basketball/team/roster/_/id/{team_id}"
STATS_URL = "https://www.espn.com/mens-college-basketball/team/stats/_/id/{team_id}"

CAREER_STATS_URL = (
    "https://site.web.api.espn.com/apis/common/v3/sports/basketball/mens-college-basketball"
    "/athletes/{espn_id}/stats"
)

# Season the advanced metrics are computed for, as ESPN's end year (2026 = 2025-26). ESPN
# has rolled its site over to the 2026-27 preseason, but rosters are still last year's, so
# this stays pinned to 2025-26 until the new rosters are in - then bump to 2027.
ADVANCED_SEASON = 2026

SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball"
    "/scoreboard?dates={date}&groups=50&limit=500"
)
SUMMARY_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball"
    "/summary?event={event_id}"
)
STANDINGS_API_URL = (
    "https://site.api.espn.com/apis/v2/sports/basketball/mens-college-basketball"
    "/standings?season={season}"
)
BOX_SCORE_WORKERS = 8
# Bump whenever parse_box_score starts storing new fields: games cached under an older
# version get re-fetched on the next run so every cached game has the full set.
BOX_SCORE_VERSION = 2

# "vs High/Mid Major Only": games against teams in these conferences, by ESPN's name for the
# conference and by membership *in the season the game was played* (so e.g. Gonzaga counts
# in 2026-27 via the Pac-12, but not in 2025-26 when it was in the WCC).
HIGH_MID_MAJOR_CONFERENCES = {
    "Atlantic Coast Conference",
    "Big Ten Conference",
    "Big East Conference",
    "Big 12 Conference",
    "Southeastern Conference",
    "Pac-12 Conference",
    "Mountain West Conference",
    "Atlantic 10 Conference",
    "American Conference",
    "Missouri Valley Conference",
}
# Opponent possessions = FGA - ORB + TOV + 0.475*FTA (KenPom's college FTA coefficient).
POSS_FTA_COEF = 0.475
# Total minutes a player needs before his advanced rates are shown. The rates estimate how many
# rebounds/possessions were available while he was on the floor by spreading the team's
# totals evenly over the game - fine over hundreds of minutes, but 1 offensive board in a
# 1-minute cameo comes out as ~100% ORB% (and a split can top 100%). Below the cutoff the
# rates are left blank, like Sports-Reference/KenPom leave them off leaderboards. The split
# gets a lower bar since it's a subset of the season's games.
MIN_MINUTES_FOR_ADVANCED = 100
MIN_MINUTES_FOR_ADVANCED_SPLIT = 50

JSON_REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_PLAYERS = 0.1

STALE_AFTER_DAYS = 7

PAGE_TIMEOUT_MS = 30000
SLEEP_BETWEEN_TEAMS = 0.3

STAT_COLUMN_MAP = {
    "GP": "gp",
    "MIN": "min",
    "PTS": "ppg",
    "REB": "rpg",
    "AST": "apg",
    "STL": "spg",
    "BLK": "bpg",
    "TO": "topg",
    "FG%": "fg_pct",
    "FT%": "ft_pct",
    "3P%": "three_pct",
}

CAREER_STAT_LABEL_MAP = {
    "GP": "gp",
    "MIN": "min",
    "PTS": "ppg",
    "REB": "rpg",
    "AST": "apg",
    "STL": "spg",
    "BLK": "bpg",
    "TO": "topg",
    "FG%": "fg_pct",
    "FT%": "ft_pct",
    "3P%": "three_pct",
}


SUPABASE_ATTEMPTS = 4


def execute(query):
    """query.execute(), retried with backoff on dropped connections. Supabase occasionally
    closes the client's HTTP/2 connection out from under it (seen right after a schema
    change), and without this one dropped request kills the whole nightly run. Every write
    here is an idempotent upsert/delete, so a retry is always safe."""
    for attempt in range(SUPABASE_ATTEMPTS):
        try:
            return query.execute()
        except httpx.TransportError as e:
            if attempt == SUPABASE_ATTEMPTS - 1:
                raise
            print(f"    Supabase connection error, retrying: {e!r}")
            time.sleep(2**attempt)


def fetch_html(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    page.wait_for_selector("table", timeout=PAGE_TIMEOUT_MS)
    return page.content()


def slugify(s):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").strip().lower())
    return s.strip("-") or "unknown"


def curl_get_json(url, timeout=JSON_REQUEST_TIMEOUT):
    result = subprocess.run(
        ["curl", "-sS", "--max-time", str(timeout), "-w", "\n%{http_code}", url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl exit {result.returncode} for {url}: {result.stderr.strip()}")
    body, _, status = result.stdout.rpartition("\n")
    if int(status) >= 400:
        raise RuntimeError(f"HTTP {status} for {url}")
    return json.loads(body)


def _num(v):
    try:
        if v is None:
            return None
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def get_teams(page):
    """Returns a list of dicts: {team_id, name, conference, record}."""
    html = fetch_html(page, STANDINGS_URL)
    soup = BeautifulSoup(html, "html.parser")

    conf_names = [t.get_text(strip=True) for t in soup.select("div.Table__Title")]
    tables = soup.find_all("table")

    teams = []
    for i, conf_name in enumerate(conf_names):
        names_table = tables[2 * i] if 2 * i < len(tables) else None
        stats_table = tables[2 * i + 1] if 2 * i + 1 < len(tables) else None
        if names_table is None or stats_table is None:
            continue

        team_entries = []
        for tr in names_table.find_all("tr"):
            link = tr.select_one("span.hide-mobile a") or tr.select_one("a.AnchorLink")
            if not link:
                continue
            href = link.get("href", "")
            m = re.search(r"/id/(\d+)/", href)
            if not m:
                continue
            team_entries.append((m.group(1), link.get_text(strip=True)))

        stat_rows = stats_table.find_all("tr")
        # First 2 rows are grouped/sub headers ("Conference/Overall/Polls", "W-L/GB/PCT/...").
        data_rows = stat_rows[2:]

        n = min(len(team_entries), len(data_rows))
        for j in range(n):
            team_id, name = team_entries[j]
            cells = [c.get_text(strip=True) for c in data_rows[j].find_all(["td", "th"])]
            record = cells[3] if len(cells) > 3 else ""  # "Overall W-L" column
            teams.append(
                {
                    "team_id": team_id,
                    "name": name,
                    "conference": conf_name,
                    "record": record,
                }
            )
    return teams


def strip_jersey_number(raw_name):
    return re.sub(r"\d+$", "", raw_name).strip()


def strip_position_suffix(raw_name):
    return re.sub(r"\s+[A-Z]{1,2}$", "", raw_name).strip()


def extract_player_ids(html, table_index):
    """Returns a list of espn_player_id (or None) for each *data* row (rows with a <td>,
    i.e. excluding the header row) of the given table, in document order. Both the roster
    and stats pages link every player's name to their ESPN profile
    (".../player/_/id/<id>/<slug>"), which pandas.read_html quietly throws away - this
    walks the same table with BeautifulSoup to recover it. Aligns 1:1 with the
    corresponding pandas.read_html dataframe's rows since both see the same row order.
    """
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if table_index >= len(tables):
        return []
    ids = []
    for row in tables[table_index].find_all("tr"):
        if not row.find("td"):
            continue  # header row (uses <th>, not <td>)
        a = row.find("a", href=re.compile(r"/player/"))
        m = re.search(r"/id/(\d+)/", a["href"]) if a else None
        ids.append(m.group(1) if m else None)
    return ids


def scrape_roster(page, team_id):
    """Returns {player_name: {position, class, height, weight, espn_player_id}}."""
    info = {}
    url = ROSTER_URL.format(team_id=team_id)
    try:
        html = fetch_html(page, url)
        tables = pd.read_html(StringIO(html))
        if not tables:
            return info
        df = tables[0]
        ids = extract_player_ids(html, 0)
        for i, (_, row) in enumerate(df.iterrows()):
            raw_name = str(row.get("Name", "")).strip()
            name = strip_jersey_number(raw_name)
            if not name or name.lower() == "nan":
                continue
            info[name] = {
                "position": str(row.get("POS", "")).strip() or None,
                "class": str(row.get("Class", "")).strip() or None,
                "height": str(row.get("HT", "")).strip() or None,
                "weight": str(row.get("WT", "")).strip() or None,
                "espn_player_id": ids[i] if i < len(ids) else None,
            }
    except Exception as e:
        print(f"    roster error ({team_id}): {e}")
    return info


def scrape_stats(page, team_id):
    """Returns {player_name: {gp, min, ppg, ..., position, espn_player_id}}.

    `position` and `espn_player_id` here are a fallback source for players who've since
    left the roster (transferred, etc.) - ESPN keeps their already-played games on this
    stats page even after dropping them from the roster page, and this is the only page
    that still identifies them at all once that happens.
    """
    info = {}
    url = STATS_URL.format(team_id=team_id)
    try:
        html = fetch_html(page, url)
        tables = pd.read_html(StringIO(html))
        if len(tables) < 2:
            return info
        names_df, stats_df = tables[0], tables[1]
        ids = extract_player_ids(html, 0)
        n = min(len(names_df), len(stats_df))
        for i in range(n):
            raw_name = str(names_df.iloc[i, 0]).strip()
            if not raw_name or raw_name.lower() in ("nan", "total"):
                continue
            name = strip_position_suffix(raw_name)
            position = raw_name[len(name) :].strip() or None
            row = stats_df.iloc[i]
            stat_row = {"position": position, "espn_player_id": ids[i] if i < len(ids) else None}
            for espn_col, our_col in STAT_COLUMN_MAP.items():
                if espn_col in stats_df.columns:
                    stat_row[our_col] = _num(row.get(espn_col))
            info[name] = stat_row
    except Exception as e:
        print(f"    stats error ({team_id}): {e}")
    return info


def get_career_stats(espn_id):
    """Returns a list of season dicts (season_year, season_display, school, gp, ppg, ...)."""
    try:
        data = curl_get_json(CAREER_STATS_URL.format(espn_id=espn_id))

        avg_cat = next(
            (c for c in data.get("categories", []) if c.get("name") == "averages"), None
        )
        if not avg_cat:
            return []

        labels = avg_cat.get("labels", [])
        school_by_team_id = {
            info.get("id"): info.get("location") or info.get("displayName")
            for info in data.get("teams", {}).values()
            if info.get("id")
        }

        seasons = []
        for row in avg_cat.get("statistics", []):
            season = row.get("season", {})
            team_id = str(row.get("teamId", ""))
            stat_map = dict(zip(labels, row.get("stats", [])))
            seasons.append(
                {
                    "season_year": season.get("year"),
                    "season_display": season.get("displayName"),
                    "school": school_by_team_id.get(team_id, ""),
                    **{
                        our_col: _num(stat_map.get(espn_label))
                        for espn_label, our_col in CAREER_STAT_LABEL_MAP.items()
                    },
                }
            )
        return seasons
    except Exception as e:
        print(f"    career stats error ({espn_id}): {e}")
        return []


def list_completed_games(season):
    """Returns the set of ESPN event IDs for every completed regular/postseason D1 game in
    `season`. The scoreboard only accepts one date per request, so this walks Nov 1 through
    mid-April (or today, if the season's still going)."""
    day = date(season - 1, 11, 1)
    last = min(date(season, 4, 15), date.today())
    event_ids = set()
    while day <= last:
        try:
            data = curl_get_json(SCOREBOARD_URL.format(date=day.strftime("%Y%m%d")))
            for ev in data.get("events", []):
                s = ev.get("season", {})
                completed = ev["competitions"][0]["status"]["type"].get("completed")
                if s.get("year") == season and s.get("type") in (2, 3) and completed:
                    event_ids.add(ev["id"])
        except Exception as e:
            print(f"    scoreboard error ({day}): {e}")
        day += timedelta(days=1)
    return event_ids


def _made_attempted(v):
    """'22-52' -> (22.0, 52.0)."""
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", str(v or ""))
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)


def parse_box_score(event_id, season, summary):
    """Returns (team_rows, player_rows) for one game's summary JSON, or None if the box
    score doesn't have both teams' lines (some low-major games never get one)."""
    box = summary.get("boxscore", {})
    teams = box.get("teams", [])
    if len(teams) != 2:
        return None

    competitors = summary.get("header", {}).get("competitions", [{}])[0].get("competitors", [])
    periods = max((len(c.get("linescores", [])) for c in competitors), default=2)
    game_minutes = 40 + 5 * max(0, periods - 2)  # two 20-min halves + 5-min OTs

    team_rows = []
    for t in teams:
        stats = {s["name"]: s.get("displayValue") for s in t.get("statistics", [])}
        _, fga = _made_attempted(stats.get("fieldGoalsMade-fieldGoalsAttempted"))
        _, fg3a = _made_attempted(
            stats.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted")
        )
        _, fta = _made_attempted(stats.get("freeThrowsMade-freeThrowsAttempted"))
        team_rows.append(
            {
                "event_id": event_id,
                "team_id": str(t["team"]["id"]),
                "season": season,
                "game_minutes": game_minutes,
                "fga": fga,
                "fg3a": fg3a,
                "fta": fta,
                "orb": _num(stats.get("offensiveRebounds")),
                "drb": _num(stats.get("defensiveRebounds")),
                "tov": _num(stats.get("totalTurnovers", stats.get("turnovers"))),
                "parse_version": BOX_SCORE_VERSION,
            }
        )
    if any(r["fga"] is None or r["orb"] is None for r in team_rows):
        return None
    team_rows[0]["opp_team_id"] = team_rows[1]["team_id"]
    team_rows[1]["opp_team_id"] = team_rows[0]["team_id"]

    player_rows = []
    for team_block in box.get("players", []):
        team_id = str(team_block["team"]["id"])
        for stat_group in team_block.get("statistics", []):
            names = stat_group.get("names", [])
            for a in stat_group.get("athletes", []):
                if a.get("didNotPlay") or not a.get("stats"):
                    continue
                line = dict(zip(names, a["stats"]))
                minutes = _num(line.get("MIN"))
                if not minutes:
                    continue
                fgm, fga = _made_attempted(line.get("FG"))
                fg3m, fg3a = _made_attempted(line.get("3PT"))
                ftm, fta = _made_attempted(line.get("FT"))
                player_rows.append(
                    {
                        "event_id": event_id,
                        "espn_player_id": str(a["athlete"]["id"]),
                        "team_id": team_id,
                        "season": season,
                        "min": minutes,
                        "pts": _num(line.get("PTS")),
                        "reb": _num(line.get("REB")),
                        "ast": _num(line.get("AST")),
                        "tov": _num(line.get("TO")),
                        "orb": _num(line.get("OREB")),
                        "drb": _num(line.get("DREB")),
                        "stl": _num(line.get("STL")),
                        "blk": _num(line.get("BLK")),
                        "fgm": fgm,
                        "fga": fga,
                        "fg3m": fg3m,
                        "fg3a": fg3a,
                        "ftm": ftm,
                        "fta": fta,
                    }
                )
    return team_rows, player_rows


def select_all(client, table, columns, season, **eq):
    """Every row of `table` for `season` (plus any extra column=value filters), paging past
    PostgREST's 1000-row response cap."""
    page_size = 1000
    rows, start = [], 0
    while True:
        query = client.table(table).select(columns).eq("season", season)
        for col, val in eq.items():
            query = query.eq(col, val)
        data = execute(query.range(start, start + page_size - 1)).data
        rows.extend(data)
        if len(data) < page_size:
            return rows
        start += page_size


def _fetch_box_score(event_id, season):
    try:
        return parse_box_score(event_id, season, curl_get_json(SUMMARY_URL.format(event_id=event_id)))
    except Exception as e:
        print(f"    box score error ({event_id}): {e}")
        return None


def update_box_score_cache(client, season):
    """Fetches and stores box scores for every completed game not already cached."""
    cached = {
        r["event_id"]
        for r in select_all(
            client, "game_team_stats", "event_id", season, parse_version=BOX_SCORE_VERSION
        )
    }
    new_ids = sorted(list_completed_games(season) - cached)
    print(f"  {len(cached) // 2} games cached, {len(new_ids)} new to fetch.")

    chunk_size = 200
    for i in range(0, len(new_ids), chunk_size):
        chunk = new_ids[i : i + chunk_size]
        with ThreadPoolExecutor(max_workers=BOX_SCORE_WORKERS) as pool:
            results = [r for r in pool.map(lambda eid: _fetch_box_score(eid, season), chunk) if r]
        team_rows = [row for t, _ in results for row in t]
        player_rows = [row for _, p in results for row in p]
        # Players first: game_team_stats is what marks a game as cached, so if this dies
        # partway the game just gets re-fetched (and re-upserted idempotently) next run.
        for j in range(0, len(player_rows), 1000):
            execute(
                client.table("game_player_stats").upsert(
                    player_rows[j : j + 1000], on_conflict="event_id,espn_player_id"
                )
            )
        if team_rows:
            execute(
                client.table("game_team_stats").upsert(team_rows, on_conflict="event_id,team_id")
            )
        print(f"    fetched {min(i + chunk_size, len(new_ids))}/{len(new_ids)}")


PLAYER_BOX_KEYS = (
    "min", "pts", "reb", "ast", "tov", "orb", "drb", "stl", "blk",
    "fgm", "fga", "fg3m", "fg3a", "ftm", "fta",
)


def _standings_entries(season):
    """(conference name, ESPN team dict) for every D1 team in `season`."""
    data = curl_get_json(STANDINGS_API_URL.format(season=season))
    return [
        (conf.get("name"), e["team"])
        for conf in data.get("children", [])
        for e in conf.get("standings", {}).get("entries", [])
    ]


def get_conferences(season):
    """{espn_team_id: conference name} for every D1 team in `season`."""
    return {str(team["id"]): conf for conf, team in _standings_entries(season)}


def get_school_names(season):
    """{espn_team_id: school name without the mascot} - ESPN's `location`, e.g. "Duke" for
    "Duke Blue Devils", "Miami (OH)" for "Miami (OH) RedHawks". Unique across D1."""
    return {
        str(team["id"]): team["location"]
        for _, team in _standings_entries(season)
        if team.get("location")
    }


def compute_player_stats(team_games, player_games, include_game=None, min_minutes=0):
    """Season stat lines per espn_player_id, summed from cached box scores.

    `include_game(event_id, team_id, opp_team_id)` limits which games count - applied to the
    team/opponent totals and the player's own games alike, so a split's rate stats are
    measured against only that split's possessions and rebound chances.

    Per-game stats plus the advanced rates, using the standard Sports-Reference/KenPom
    definitions, where team minutes / 5 is just the sum of game lengths (so OT counts):
      ORB% = 100 * ORB * (TmMin/5) / (MIN * (Tm ORB + Opp DRB))
      DRB% = 100 * DRB * (TmMin/5) / (MIN * (Tm DRB + Opp ORB))
      STL% = 100 * STL * (TmMin/5) / (MIN * Opp Poss)
      BLK% = 100 * BLK * (TmMin/5) / (MIN * Opp 2PA)
      FT Rate = FTA / FGA   (a ratio, e.g. 0.536 - not x100, same as Sports-Reference)
    The advanced rates are None for players under `min_minutes` total minutes.
    """
    by_event = defaultdict(list)
    for g in team_games:
        by_event[g["event_id"]].append(g)

    opp_of = {}
    team = defaultdict(lambda: defaultdict(float))
    for event_id, games in by_event.items():
        if len(games) != 2:
            continue
        for own, opp in ((games[0], games[1]), (games[1], games[0])):
            opp_of[(event_id, own["team_id"])] = opp["team_id"]
            if include_game and not include_game(event_id, own["team_id"], opp["team_id"]):
                continue
            t = team[own["team_id"]]
            t["minutes"] += own["game_minutes"]
            t["orb"] += own["orb"] or 0
            t["drb"] += own["drb"] or 0
            t["opp_orb"] += opp["orb"] or 0
            t["opp_drb"] += opp["drb"] or 0
            t["opp_2pa"] += (opp["fga"] or 0) - (opp["fg3a"] or 0)
            t["opp_poss"] += (
                (opp["fga"] or 0)
                - (opp["orb"] or 0)
                + (opp["tov"] or 0)
                + POSS_FTA_COEF * (opp["fta"] or 0)
            )

    player = defaultdict(lambda: defaultdict(float))
    for g in player_games:
        opp_team_id = opp_of.get((g["event_id"], g["team_id"]))
        if opp_team_id is None:
            continue
        if include_game and not include_game(g["event_id"], g["team_id"], opp_team_id):
            continue
        p = player[(g["espn_player_id"], g["team_id"])]
        p["gp"] += 1
        for k in PLAYER_BOX_KEYS:
            p[k] += g[k] or 0

    def rate(stat, tm_min, p_min, denom):
        if not p_min or not denom:
            return None
        return round(100 * stat * tm_min / (p_min * denom), 1)

    def per_game(total, gp):
        return round(total / gp, 1) if gp else None

    def pct(made, att):
        return round(100 * made / att, 1) if att else None

    # A player who switched teams mid-season gets measured against the team he played the
    # most minutes for - the players table only holds one school per player anyway.
    best = {}
    for (pid, team_id), p in player.items():
        if pid not in best or p["min"] > best[pid][1]["min"]:
            best[pid] = (team_id, p)

    out = {}
    for pid, (team_id, p) in best.items():
        t = team.get(team_id)
        if not t:
            continue
        gp = p["gp"]
        qualified = p["min"] >= min_minutes
        out[pid] = {
            "gp": gp,
            "min": per_game(p["min"], gp),
            "ppg": per_game(p["pts"], gp),
            "rpg": per_game(p["reb"], gp),
            "apg": per_game(p["ast"], gp),
            "spg": per_game(p["stl"], gp),
            "bpg": per_game(p["blk"], gp),
            "topg": per_game(p["tov"], gp),
            "fg_pct": pct(p["fgm"], p["fga"]),
            "three_pct": pct(p["fg3m"], p["fg3a"]),
            "ft_pct": pct(p["ftm"], p["fta"]),
        }
        advanced = {
            "orb_pct": rate(p["orb"], t["minutes"], p["min"], t["orb"] + t["opp_drb"]),
            "drb_pct": rate(p["drb"], t["minutes"], p["min"], t["drb"] + t["opp_orb"]),
            "stl_pct": rate(p["stl"], t["minutes"], p["min"], t["opp_poss"]),
            "blk_pct": rate(p["blk"], t["minutes"], p["min"], t["opp_2pa"]),
            "ft_rate": round(p["fta"] / p["fga"], 3) if p["fga"] else None,
        }
        out[pid].update(advanced if qualified else dict.fromkeys(advanced))
    return out


def get_advanced_stats(client, season):
    """Refreshes the box score cache, then returns
    {"all": {espn_player_id: stats}, "vs_hm": {espn_player_id: stats}}."""
    update_box_score_cache(client, season)
    team_games = select_all(
        client,
        "game_team_stats",
        "event_id,team_id,game_minutes,fga,fg3a,fta,orb,drb,tov",
        season,
    )
    player_games = select_all(
        client,
        "game_player_stats",
        "event_id,espn_player_id,team_id," + ",".join(PLAYER_BOX_KEYS),
        season,
    )
    conferences = get_conferences(season)
    hm_team_ids = {tid for tid, conf in conferences.items() if conf in HIGH_MID_MAJOR_CONFERENCES}
    print(f"  {len(hm_team_ids)} high/mid-major teams in {season}.")
    return {
        "all": compute_player_stats(
            team_games, player_games, min_minutes=MIN_MINUTES_FOR_ADVANCED
        ),
        "vs_hm": compute_player_stats(
            team_games,
            player_games,
            lambda _e, _t, opp: opp in hm_team_ids,
            min_minutes=MIN_MINUTES_FOR_ADVANCED_SPLIT,
        ),
    }


ADVANCED_STAT_KEYS = ("orb_pct", "drb_pct", "stl_pct", "blk_pct", "ft_rate")


def player_id_for(espn_player_id, name, team_name):
    """ESPN's athlete ID is permanent across transfers and name changes, so it's the key -
    reports and career history stay attached to the player, not to a name+school combo.
    The name+school slug is only a fallback for the rare player with no ESPN profile link."""
    if espn_player_id:
        return f"espn-{espn_player_id}"
    return f"{slugify(name)}-{slugify(team_name)}"


def scrape_team(page, team):
    """Returns one candidate row per name found on this team's roster and/or stats page.
    The same player can come back from more than one team (see merge_player_rows)."""
    team_id, team_name, conference, record = (
        team["team_id"],
        team["name"],
        team["conference"],
        team["record"],
    )
    roster = scrape_roster(page, team_id)
    stats = scrape_stats(page, team_id)

    all_names = set(roster.keys()) | set(stats.keys())
    now = datetime.now(timezone.utc).isoformat()

    rows = []
    for name in all_names:
        r_info = roster.get(name, {})
        s_info = stats.get(name, {})
        espn_player_id = r_info.get("espn_player_id") or s_info.get("espn_player_id")
        rows.append(
            {
                "player_id": player_id_for(espn_player_id, name, team_name),
                "name": name,
                "school": team_name,
                "conference": conference,
                "position": r_info.get("position") or s_info.get("position"),
                "class": r_info.get("class"),
                "height": r_info.get("height"),
                "weight": r_info.get("weight"),
                "record": record,
                "espn_player_id": espn_player_id,
                "gp": s_info.get("gp"),
                "min": s_info.get("min"),
                "ppg": s_info.get("ppg"),
                "rpg": s_info.get("rpg"),
                "apg": s_info.get("apg"),
                "spg": s_info.get("spg"),
                "bpg": s_info.get("bpg"),
                "topg": s_info.get("topg"),
                "fg_pct": s_info.get("fg_pct"),
                "three_pct": s_info.get("three_pct"),
                "ft_pct": s_info.get("ft_pct"),
                "last_updated": now,
                "_on_stats_page": bool(s_info),
            }
        )
    return rows


ROSTER_FIELDS = ("position", "class", "height", "weight")


def merge_player_rows(candidates):
    """Collapses candidate rows to one per player_id.

    During the offseason ESPN rolls team rosters over to next season one team at a time,
    while every team's stats page stays on last season until games start. So a transfer
    shows up on his new team's roster (no stats) *and* his old team's stats page, and a
    player ESPN renamed shows up under both names on one team. Both share an ESPN ID.

    The row from a stats page wins - that's the school the season's stats are from - and
    any roster fields it's missing (class, height...) are filled in from the other rows.
    If he's on more than one stats page (a mid-season transfer), the one where he played
    the most minutes wins.
    """
    by_id = defaultdict(list)
    for row in candidates:
        by_id[row["player_id"]].append(row)

    merged = []
    for rows in by_id.values():
        rows.sort(
            key=lambda r: (r["_on_stats_page"], (r["gp"] or 0) * (r["min"] or 0)),
            reverse=True,
        )
        best = dict(rows[0])
        for other in rows[1:]:
            for field in ROSTER_FIELDS:
                if not best.get(field) and other.get(field):
                    best[field] = other[field]
        del best["_on_stats_page"]
        merged.append(best)
    return merged


def attach_advanced(rows, advanced):
    """Adds the advanced columns to each player row and returns the vs-High/Mid-Major split
    rows. `advanced` is None when the box-score step failed this run - then the advanced
    keys are left off entirely so the upsert keeps yesterday's values instead of nulling
    them out."""
    split_rows = []
    if advanced is None:
        return split_rows
    for row in rows:
        espn_player_id = row["espn_player_id"]
        adv = advanced["all"].get(espn_player_id, {})
        row.update({k: adv.get(k) for k in ADVANCED_STAT_KEYS})
        hm = advanced["vs_hm"].get(espn_player_id)
        if hm:
            split_rows.append(
                {
                    "player_id": row["player_id"],
                    "split": "vs_hm",
                    **hm,
                    "last_updated": row["last_updated"],
                }
            )
    return split_rows


def upsert_players(client, rows):
    if not rows:
        return
    # Supabase/PostgREST upsert batches work fine in the low thousands; chunk defensively.
    chunk_size = 500
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i : i + chunk_size]
        execute(client.table("players").upsert(chunk, on_conflict="player_id"))


def replace_player_splits(client, player_ids, split_rows):
    """Upserts this run's split rows and deletes any old split line for a player in
    `player_ids` who no longer has one."""
    for i in range(0, len(split_rows), 500):
        execute(
            client.table("player_splits").upsert(
                split_rows[i : i + 500], on_conflict="player_id,split"
            )
        )
    has_split = {r["player_id"] for r in split_rows}
    without = [pid for pid in player_ids if pid not in has_split]
    # Chunked so the id list stays well under URL-length limits.
    for i in range(0, len(without), 200):
        execute(client.table("player_splits").delete().in_("player_id", without[i : i + 200]))


def prune_stale_players(client, seen_ids):
    """Deletes players ESPN hasn't listed on any roster/stats page for STALE_AFTER_DAYS -
    graduated, left D1, or a leftover row from a rename. The grace period means one failed
    page load can't wipe out a team (deleting a player also deletes his career history).
    A player with scouting reports is never deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=STALE_AFTER_DAYS)).isoformat()
    stale, start = [], 0
    while True:
        page = execute(
            client.table("players")
            .select("player_id")
            .lt("last_updated", cutoff)
            .order("player_id")
            .range(start, start + 999)
        ).data
        stale.extend(r["player_id"] for r in page if r["player_id"] not in seen_ids)
        if len(page) < 1000:
            break
        start += 1000
    if not stale:
        return 0
    with_reports = set()
    for i in range(0, len(stale), 200):
        with_reports |= {
            r["player_id"]
            for r in execute(
                client.table("reports").select("player_id").in_("player_id", stale[i : i + 200])
            ).data
        }
    to_delete = [pid for pid in stale if pid not in with_reports]
    for i in range(0, len(to_delete), 200):
        execute(client.table("players").delete().in_("player_id", to_delete[i : i + 200]))
    if with_reports:
        print(f"  Kept {len(with_reports)} stale players because they have scouting reports.")
    return len(to_delete)


def upsert_career_stats(client, rows):
    if not rows:
        return
    execute(
        client.table("career_stats").upsert(rows, on_conflict="player_id,season_year,school")
    )


def scrape_and_upsert_career_stats(client, roster_rows):
    for row in roster_rows:
        espn_id = row.get("espn_player_id")
        if not espn_id:
            continue
        seasons = get_career_stats(espn_id)
        if seasons:
            career_rows = [
                {"player_id": row["player_id"], "espn_player_id": espn_id, **s}
                for s in seasons
            ]
            # Replace rather than just upsert: school is part of the row's key, so if the
            # school's spelling ever changes (e.g. the switch to mascot-free names) an upsert
            # alone would leave every past season listed twice.
            execute(client.table("career_stats").delete().eq("player_id", row["player_id"]))
            upsert_career_stats(client, career_rows)
        time.sleep(SLEEP_BETWEEN_PLAYERS)


def main():
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not supabase_url or not supabase_key:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY env vars are required.")
        sys.exit(1)

    client = create_client(supabase_url, supabase_key)

    print(f"Updating box score cache + advanced metrics for season {ADVANCED_SEASON}...")
    try:
        advanced = get_advanced_stats(client, ADVANCED_SEASON)
        print(
            f"Computed advanced metrics for {len(advanced['all'])} players "
            f"({len(advanced['vs_hm'])} with games vs high/mid majors)."
        )
    except Exception as e:
        advanced = None
        print(f"Advanced metrics failed, keeping existing values this run: {e}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            )
        )

        print("Fetching D1 team list from ESPN standings...")
        teams = get_teams(page)
        print(f"Found {len(teams)} teams.")
        try:
            school_names = get_school_names(ADVANCED_SEASON)
        except Exception as e:
            school_names = {}
            print(f"Couldn't fetch mascot-free school names, keeping ESPN's full names: {e}")
        for team in teams:
            team["name"] = school_names.get(team["team_id"], team["name"])

        team_limit = os.environ.get("TEAM_LIMIT", "").strip()
        if team_limit:
            teams = teams[: int(team_limit)]
            print(f"TEAM_LIMIT set, only scraping first {len(teams)} teams.")

        include_career_stats = os.environ.get("INCLUDE_CAREER_STATS", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        if include_career_stats:
            print("INCLUDE_CAREER_STATS set - also fetching career history (slower).")
        else:
            print("Skipping career history this run (INCLUDE_CAREER_STATS not set).")

        candidates = []
        for i, team in enumerate(teams, start=1):
            print(f"[{i}/{len(teams)}] {team['name']} ({team['conference']})")
            candidates.extend(scrape_team(page, team))
            time.sleep(SLEEP_BETWEEN_TEAMS)

        browser.close()

    # Everything's collected before anything's written, since one player can come back from
    # two teams (see merge_player_rows) and only the full set can say which row wins.
    rows = merge_player_rows(candidates)
    print(f"\nMerged {len(candidates)} roster/stats entries into {len(rows)} players.")
    split_rows = attach_advanced(rows, advanced)
    upsert_players(client, rows)
    if advanced is not None:
        replace_player_splits(client, [r["player_id"] for r in rows], split_rows)
    if include_career_stats:
        scrape_and_upsert_career_stats(client, rows)
    if not team_limit:
        pruned = prune_stale_players(client, {r["player_id"] for r in rows})
        print(f"Pruned {pruned} players not seen on ESPN in {STALE_AFTER_DAYS}+ days.")

    print(f"Done. Upserted stats for {len(rows)} players across {len(teams)} teams.")


if __name__ == "__main__":
    main()
