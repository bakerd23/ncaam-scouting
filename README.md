# College Basketball Scouting Database

Daily-refreshing D1 men's basketball player database with a custom scouting-report form.
Scouts anywhere can open the site and log a report on any player; the report attaches
directly to that player's profile alongside their auto-scraped season stats.

## How it works

- **`scraper/scrape_espn.py`** pulls every D1 team from ESPN's standings API, then scrapes
  each team's roster + season-stats pages and upserts one row per player into the Supabase
  `players` table. Runs daily via `.github/workflows/scrape.yml` (GitHub Actions cron, 10am
  UTC / 5am ET), and can be triggered manually from the Actions tab.
- **`docs/`** is a static dashboard (no build step) hosted on GitHub Pages (Pages only serves
  from `/` or `/docs`, hence the folder name):
  - `index.html` — searchable/filterable/sortable list of every player
  - `player.html` — one player's stats + full scouting report history
  - `report.html` — the form scouts fill out; writes directly to Supabase
- **Supabase** (Postgres) holds two tables: `players` (scraper-owned) and `reports`
  (scout-submitted). Row-level security means the public site can read everything and
  insert reports, but can never write to `players` or edit/delete existing reports — see
  `schema.sql` for the exact policies.

## One-time setup

1. **Create a Supabase project** at supabase.com (free tier is plenty).
   - In the SQL Editor, paste and run `schema.sql` to create the tables + policies.
   - In Project Settings -> API, grab the **Project URL**, the **anon public** key, and the
     **service_role** key.

2. **Fill in `docs/config.js`** with the Project URL and anon key (safe to commit — RLS is
   what actually restricts access, not secrecy of this key):
   ```js
   window.SUPABASE_URL = "https://xxxxx.supabase.co";
   window.SUPABASE_ANON_KEY = "eyJ...";
   ```

3. **Add GitHub repo secrets** (Settings -> Secrets and variables -> Actions):
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_KEY` (the service_role key — keep this one secret, never put it in
     `docs/`)

4. **Enable GitHub Pages** (Settings -> Pages) serving from the `docs/` folder on `master`.

5. **Run the workflow once manually** (Actions tab -> Daily Player Stats Update -> Run
   workflow) to populate the `players` table before sharing the site.

## Local testing

```bash
# Test the scraper against a handful of teams before letting it run against all of D1:
cd scraper
pip install -r requirements.txt
TEAM_LIMIT=3 SUPABASE_URL=... SUPABASE_SERVICE_KEY=... python scrape_espn.py

# Preview the site locally:
cd ../docs
python -m http.server 8000
# open http://localhost:8000
```
