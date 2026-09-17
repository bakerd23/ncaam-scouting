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

Env vars required:
  SUPABASE_URL
  SUPABASE_SERVICE_KEY   (service_role key - bypasses RLS, never expose to the browser)

Optional:
  TEAM_LIMIT   - if set, only scrape the first N teams (useful for a quick local test run)
"""

import os
import re
import sys
import time
from datetime import datetime, timezone
from io import StringIO

import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from supabase import create_client

STANDINGS_URL = "https://www.espn.com/mens-college-basketball/standings"
ROSTER_URL = "https://www.espn.com/mens-college-basketball/team/roster/_/id/{team_id}"
STATS_URL = "https://www.espn.com/mens-college-basketball/team/stats/_/id/{team_id}"

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


def fetch_html(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    page.wait_for_selector("table", timeout=PAGE_TIMEOUT_MS)
    return page.content()


def slugify(s):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").strip().lower())
    return s.strip("-") or "unknown"


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


def scrape_roster(page, team_id):
    """Returns {player_name: {position, class, height, weight}}."""
    info = {}
    url = ROSTER_URL.format(team_id=team_id)
    try:
        html = fetch_html(page, url)
        tables = pd.read_html(StringIO(html))
        if not tables:
            return info
        df = tables[0]
        for _, row in df.iterrows():
            raw_name = str(row.get("Name", "")).strip()
            name = strip_jersey_number(raw_name)
            if not name or name.lower() == "nan":
                continue
            info[name] = {
                "position": str(row.get("POS", "")).strip() or None,
                "class": str(row.get("Class", "")).strip() or None,
                "height": str(row.get("HT", "")).strip() or None,
                "weight": str(row.get("WT", "")).strip() or None,
            }
    except Exception as e:
        print(f"    roster error ({team_id}): {e}")
    return info


def scrape_stats(page, team_id):
    """Returns {player_name: {gp, min, ppg, rpg, apg, spg, bpg, topg, fg_pct, ft_pct, three_pct}}."""
    info = {}
    url = STATS_URL.format(team_id=team_id)
    try:
        html = fetch_html(page, url)
        tables = pd.read_html(StringIO(html))
        if len(tables) < 2:
            return info
        names_df, stats_df = tables[0], tables[1]
        n = min(len(names_df), len(stats_df))
        for i in range(n):
            raw_name = str(names_df.iloc[i, 0]).strip()
            if not raw_name or raw_name.lower() in ("nan", "total"):
                continue
            name = strip_position_suffix(raw_name)
            row = stats_df.iloc[i]
            stat_row = {}
            for espn_col, our_col in STAT_COLUMN_MAP.items():
                if espn_col in stats_df.columns:
                    stat_row[our_col] = _num(row.get(espn_col))
            info[name] = stat_row
    except Exception as e:
        print(f"    stats error ({team_id}): {e}")
    return info


def scrape_team(page, team):
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
        player_id = f"{slugify(name)}-{slugify(team_name)}"
        rows.append(
            {
                "player_id": player_id,
                "name": name,
                "school": team_name,
                "conference": conference,
                "position": r_info.get("position"),
                "class": r_info.get("class"),
                "height": r_info.get("height"),
                "weight": r_info.get("weight"),
                "record": record,
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
            }
        )
    return rows


def upsert_players(client, rows):
    if not rows:
        return
    # Supabase/PostgREST upsert batches work fine in the low thousands; chunk defensively.
    chunk_size = 500
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i : i + chunk_size]
        client.table("players").upsert(chunk, on_conflict="player_id").execute()


def main():
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not supabase_url or not supabase_key:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY env vars are required.")
        sys.exit(1)

    client = create_client(supabase_url, supabase_key)

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

        team_limit = os.environ.get("TEAM_LIMIT", "").strip()
        if team_limit:
            teams = teams[: int(team_limit)]
            print(f"TEAM_LIMIT set, only scraping first {len(teams)} teams.")

        total_players = 0
        for i, team in enumerate(teams, start=1):
            print(f"[{i}/{len(teams)}] {team['name']} ({team['conference']})")
            rows = scrape_team(page, team)
            upsert_players(client, rows)
            total_players += len(rows)
            time.sleep(SLEEP_BETWEEN_TEAMS)

        browser.close()

    print(f"\nDone. Upserted stats for {total_players} players across {len(teams)} teams.")


if __name__ == "__main__":
    main()
