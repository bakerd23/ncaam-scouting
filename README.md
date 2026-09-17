# College Basketball Scouting Database

Daily-refreshing D1 men's basketball player database with a custom scouting-report form.
Scouts anywhere can open the site and log a report on any player; the report attaches
directly to that player's profile alongside their auto-scraped season stats.

## How it works

- **`scraper/scrape_espn.py`** pulls every D1 team from ESPN's standings page, then scrapes
  each team's roster + season-stats pages and upserts one row per player into the Supabase
  `players` table. Runs daily via `.github/workflows/scrape.yml` (GitHub Actions cron, 10am
  UTC / 5am ET), and can be triggered manually from the Actions tab.
  - Fetches go through a real headless Chromium browser (Playwright), not a plain HTTP
    client. ESPN's standings/team pages sit behind AWS WAF Bot Control, which serves a
    JS challenge (empty HTTP 202) to non-browser clients from cloud/CI IP ranges — confirmed
    against both `requests` and `curl` with full browser-style headers. A real browser solves
    that challenge transparently, the same as it would for any ordinary visitor.
- **`docs/`** is a static dashboard (no build step) hosted on GitHub Pages (Pages only serves
  from `/` or `/docs`, hence the folder name):
  - `index.html` — searchable/filterable/sortable list of every player
  - `player.html` — one player's stats + full scouting report history
  - `report.html` — the form scouts fill out; writes directly to Supabase
- **Supabase** (Postgres) holds two tables: `players` (scraper-owned) and `reports`
  (scout-submitted). Row-level security means the public site can read everything and
  insert reports, but can never write to `players` or edit/delete existing reports — see
  `schema.sql` for the exact policies.

## Current live setup

- Repo: https://github.com/bakerd23/ncaam-scouting (public — GitHub Pages on a private repo
  needs GitHub Pro/Team, so this stays public; there's no access control on the link itself)
- Site: https://bakerd23.github.io/ncaam-scouting/
- Supabase project already created and wired up (`docs/config.js` has the URL + publishable
  key; `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` repo secrets are set for the scraper)
- `players` table is populated: 5,667 players across all 364 D1 teams (first full run
  2026-09-17)
- Daily cron runs at 10am UTC / 5am ET; trigger manually anytime from the Actions tab

## One-time setup (for a fresh clone / new project)

1. **Create a Supabase project** at supabase.com (free tier is plenty).
   - In the SQL Editor, paste and run `schema.sql` to create the tables + policies.
   - In Project Settings -> API, grab the **Project URL**, the **publishable/anon** key, and
     the **secret/service_role** key.

2. **Fill in `docs/config.js`** with the Project URL and publishable key (safe to commit —
   RLS is what actually restricts access, not secrecy of this key):
   ```js
   window.SUPABASE_URL = "https://xxxxx.supabase.co";
   window.SUPABASE_ANON_KEY = "sb_publishable_...";
   ```

3. **Add GitHub repo secrets** (Settings -> Secrets and variables -> Actions):
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_KEY` (the secret/service_role key — keep this one secret, never put
     it in `docs/`)

4. **Enable GitHub Pages** (Settings -> Pages) serving from the `docs/` folder on `master`.

5. **Run the workflow once manually** (Actions tab -> Daily Player Stats Update -> Run
   workflow) to populate the `players` table before sharing the site.

## Local testing

```bash
# Test the scraper against a handful of teams before letting it run against all of D1:
cd scraper
pip install -r requirements.txt
playwright install chromium
TEAM_LIMIT=3 SUPABASE_URL=... SUPABASE_SERVICE_KEY=... python scrape_espn.py

# Preview the site locally:
cd ../docs
python -m http.server 8000
# open http://localhost:8000
```
