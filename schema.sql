-- Run this once in the Supabase SQL editor (Project -> SQL Editor -> New query) to set up the
-- database. Safe to re-run: uses IF NOT EXISTS / OR REPLACE where possible, but if you already
-- have these tables from a previous run, drop them first or skip the `create table` statements.

create table if not exists players (
  player_id text primary key,        -- "espn-<ESPN athlete id>", e.g. "espn-5041935" (name-school
                                     -- slug only if ESPN has no profile link for the player)
  name text not null,
  school text,
  conference text,
  "position" text,
  class text,
  height text,
  weight text,
  record text,
  espn_player_id text,
  gp numeric,
  "min" numeric,
  ppg numeric,
  rpg numeric,
  apg numeric,
  spg numeric,
  bpg numeric,
  topg numeric,
  fg_pct numeric,
  three_pct numeric,
  ft_pct numeric,
  last_updated timestamptz default now()
);

alter table players add column if not exists espn_player_id text;

-- Advanced metrics (computed by the scraper from the box score cache below). The four rates
-- are percentages (e.g. 12.3 = 12.3%); ft_rate is the ratio FTA / FGA (e.g. 0.536).
alter table players add column if not exists orb_pct numeric;
alter table players add column if not exists drb_pct numeric;
alter table players add column if not exists stl_pct numeric;
alter table players add column if not exists blk_pct numeric;
alter table players add column if not exists ft_rate numeric;

-- Box score cache: one row per team per completed game, and one per player per game. Only the
-- scraper reads/writes these (service_role); they exist so team + opponent season totals can
-- be summed without re-fetching every game every day.
create table if not exists game_team_stats (
  event_id text not null,
  team_id text not null,
  opp_team_id text,
  season integer not null,
  game_minutes numeric,    -- 40 + 5 per OT
  fga numeric,
  fg3a numeric,
  fta numeric,
  orb numeric,
  drb numeric,
  tov numeric,
  primary key (event_id, team_id)
);

create table if not exists game_player_stats (
  event_id text not null,
  espn_player_id text not null,
  team_id text not null,
  season integer not null,
  "min" numeric,
  orb numeric,
  drb numeric,
  stl numeric,
  blk numeric,
  fga numeric,
  fta numeric,
  primary key (event_id, espn_player_id)
);

-- Full box score lines (for the "vs High/Mid Major" split). parse_version lets the scraper
-- re-fetch games cached before a field was added.
alter table game_team_stats add column if not exists parse_version integer;
alter table game_player_stats add column if not exists pts numeric;
alter table game_player_stats add column if not exists reb numeric;
alter table game_player_stats add column if not exists ast numeric;
alter table game_player_stats add column if not exists tov numeric;
alter table game_player_stats add column if not exists fgm numeric;
alter table game_player_stats add column if not exists fg3m numeric;
alter table game_player_stats add column if not exists fg3a numeric;
alter table game_player_stats add column if not exists ftm numeric;

-- Per-player stat lines over a subset of games, computed by the scraper from the box score
-- cache. split = 'vs_hm': games vs ACC/Big Ten/Big East/Big 12/SEC/Pac-12/Mountain West/
-- A-10/American/MVC opponents (by conference membership the season the game was played).
create table if not exists player_splits (
  player_id text references players(player_id) on delete cascade,
  split text not null,
  gp numeric,
  "min" numeric,
  ppg numeric,
  rpg numeric,
  apg numeric,
  spg numeric,
  bpg numeric,
  topg numeric,
  fg_pct numeric,
  three_pct numeric,
  ft_pct numeric,
  orb_pct numeric,
  drb_pct numeric,
  stl_pct numeric,
  blk_pct numeric,
  ft_rate numeric,
  last_updated timestamptz default now(),
  primary key (player_id, split)
);

-- JUCO: NJCAA Division I/II players live in the same players table (so scouting reports work
-- the same way), marked level = 'JUCO'. Everything from the ESPN scraper is 'D1'. JUCO rows
-- come from scraper/scrape_njcaa.py: player_id "njcaa-<NJCAA person id>", conference holds the
-- NJCAA region ("Region 17"), juco_division is 'D1'/'D2', and stats_school is the school the
-- 2025-26 stats are from when that differs from his 2026-27 school.
alter table players add column if not exists level text not null default 'D1';
alter table players add column if not exists juco_division text;
alter table players add column if not exists hometown text;
alter table players add column if not exists high_school text;
alter table players add column if not exists stats_school text;
create index if not exists players_level_idx on players(level);

-- Scout-added players (level = 'ADDED'): anyone not on an ESPN or NJCAA roster - high
-- schoolers, NAIA, international, a JUCO whose school hasn't posted a roster - added from
-- the report form so they can be scouted. player_id "added-<uuid>". added_level is what the
-- scout called him ('High School', 'JUCO', 'D2/D3/NAIA', 'International', 'Other'); added_by
-- is the scout's name. Neither scraper touches these rows.
alter table players add column if not exists added_level text;
alter table players add column if not exists added_by text;

-- Season totals + shooting volume, summed from the box score cache by the scraper (on both
-- players and player_splits). tot_* are season totals of the per-game columns; fgm/fga etc.
-- are total makes/attempts; *_pg are attempts per game (box-score games, so they line up
-- with the attempt totals even where ESPN's own GP differs by a missing box score).
alter table players add column if not exists tot_min numeric;
alter table players add column if not exists tot_pts numeric;
alter table players add column if not exists tot_reb numeric;
alter table players add column if not exists tot_ast numeric;
alter table players add column if not exists tot_stl numeric;
alter table players add column if not exists tot_blk numeric;
alter table players add column if not exists tot_tov numeric;
alter table players add column if not exists fgm numeric;
alter table players add column if not exists fga numeric;
alter table players add column if not exists fg3m numeric;
alter table players add column if not exists fg3a numeric;
alter table players add column if not exists ftm numeric;
alter table players add column if not exists fta numeric;
alter table players add column if not exists fga_pg numeric;
alter table players add column if not exists fg3a_pg numeric;
alter table players add column if not exists fta_pg numeric;

alter table player_splits add column if not exists tot_min numeric;
alter table player_splits add column if not exists tot_pts numeric;
alter table player_splits add column if not exists tot_reb numeric;
alter table player_splits add column if not exists tot_ast numeric;
alter table player_splits add column if not exists tot_stl numeric;
alter table player_splits add column if not exists tot_blk numeric;
alter table player_splits add column if not exists tot_tov numeric;
alter table player_splits add column if not exists fgm numeric;
alter table player_splits add column if not exists fga numeric;
alter table player_splits add column if not exists fg3m numeric;
alter table player_splits add column if not exists fg3a numeric;
alter table player_splits add column if not exists ftm numeric;
alter table player_splits add column if not exists fta numeric;
alter table player_splits add column if not exists fga_pg numeric;
alter table player_splits add column if not exists fg3a_pg numeric;
alter table player_splits add column if not exists fta_pg numeric;

create table if not exists reports (
  id uuid primary key default gen_random_uuid(),
  player_id text references players(player_id) on delete cascade,
  scout_name text not null,
  date_watched date not null,
  context text,
  overall_grade numeric,
  strengths text,
  weaknesses text,
  projection text,
  confidence_level numeric,      -- 1-5, how many/how well the scout saw this player
  would_recommend text,          -- 'Yes' | 'No' | 'Maybe'
  notes text,
  submitted_at timestamptz default now()
);

alter table reports add column if not exists confidence_level numeric;
alter table reports add column if not exists would_recommend text;

-- Nelson's report questions (2026-09). These replaced the grade/projection/strengths/weaknesses/
-- confidence/recommend/notes fields on the form; those columns stay so older reports still show.
-- NOTE: parents/agent/finances are sensitive - reports are still publicly readable (DEMO MODE
-- below) until the LOCKDOWN policy is applied.
alter table reports add column if not exists parents text;
alter table reports add column if not exists aau_coach text;
alter table reports add column if not exists agent text;
alter table reports add column if not exists finances text;
alter table reports add column if not exists success_reasons text;
alter table reports add column if not exists failure_reasons text;
alter table reports add column if not exists live_game_notes text;

create table if not exists career_stats (
  id uuid primary key default gen_random_uuid(),
  player_id text references players(player_id) on delete cascade,
  espn_player_id text,
  season_year integer,
  season_display text,
  school text,
  gp numeric,
  "min" numeric,
  ppg numeric,
  rpg numeric,
  apg numeric,
  spg numeric,
  bpg numeric,
  topg numeric,
  fg_pct numeric,
  three_pct numeric,
  ft_pct numeric,
  unique (player_id, season_year, school)
);

create index if not exists reports_player_id_idx on reports(player_id);
create index if not exists players_school_idx on players(school);
create index if not exists players_conference_idx on players(conference);
create index if not exists career_stats_player_id_idx on career_stats(player_id);
create index if not exists game_team_stats_season_idx on game_team_stats(season);
create index if not exists game_player_stats_season_idx on game_player_stats(season);

alter table players enable row level security;
alter table reports enable row level security;
alter table career_stats enable row level security;
-- RLS on with no policies at all = the public site can't read or write these; only the
-- scraper's service_role key (which bypasses RLS) touches them.
alter table game_team_stats enable row level security;
alter table game_player_stats enable row level security;
alter table player_splits enable row level security;

drop policy if exists "player_splits readable by anyone" on player_splits;
create policy "player_splits readable by anyone" on player_splits
  for select using (true);

drop policy if exists "players readable by anyone" on players;
create policy "players readable by anyone" on players
  for select using (true);

drop policy if exists "career_stats readable by anyone" on career_stats;
create policy "career_stats readable by anyone" on career_stats
  for select using (true);

-- DEMO MODE (current live state): reports are publicly readable, same as players, so the
-- report-viewing gate in docs/player.html doesn't require a login yet
-- (docs/config.js: REQUIRE_LOGIN_FOR_REPORTS = false). Anyone with the link can already read
-- reports today - this policy isn't providing real access control in this state.
drop policy if exists "reports readable by anyone" on reports;
drop policy if exists "reports readable by authenticated users" on reports;
create policy "reports readable by anyone" on reports
  for select using (true);

-- LOCKDOWN MODE (not yet applied): once the client's Supabase Auth login exists and
-- config.js's REQUIRE_LOGIN_FOR_REPORTS flips to true, swap the policy above for this one so
-- only a signed-in account can read reports - scouts keep submitting blind via the public
-- insert policy below, but can no longer read back anyone's reports.
--   drop policy if exists "reports readable by anyone" on reports;
--   create policy "reports readable by authenticated users" on reports
--     for select using (auth.role() = 'authenticated');

drop policy if exists "anyone can submit a report" on reports;
create policy "anyone can submit a report" on reports
  for insert with check (true);

-- The public site may add one kind of player row: a scout-added player. The check pins it to
-- level 'ADDED' with an "added-" id and no ESPN id, so it can't create or overwrite (no update
-- policy) a D1/JUCO row, and stat columns stay empty.
drop policy if exists "scouts can add unlisted players" on players;
create policy "scouts can add unlisted players" on players
  for insert with check (
    level = 'ADDED'
    and player_id like 'added-%'
    and espn_player_id is null
    and gp is null
    and ppg is null
  );

-- Otherwise no insert/update/delete policy on `players` or `career_stats` for the public
-- (anon) role, and no delete policy on `reports` either. Both `players` and
-- `career_stats` are written only by the daily scrapers using the service_role key, which
-- bypasses RLS entirely. Scouts can only ever add new reports (and scout-added players),
-- never delete existing ones (or edit any player/career data) from the public site -
-- except editing reports, which the "Report editing" section below allows (DEMO MODE).

-- ---------- Report editing (2026-10) ----------

alter table reports add column if not exists updated_at timestamptz;

-- Stamped by the database on every edit (not trusted from the page).
create or replace function touch_report_updated_at() returns trigger
language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end $$;

drop trigger if exists reports_touch_updated_at on reports;
create trigger reports_touch_updated_at before update on reports
  for each row execute function touch_report_updated_at();

-- DEMO MODE (current live state, while Nelson is the only user): anyone with the link can
-- edit any report, same as the public read policy. Goes away with the LOCKDOWN step once
-- scout logins exist - see the LOCKDOWN note on the reports policies in schema.sql.
drop policy if exists "reports editable by anyone" on reports;
drop policy if exists "reports editable by authenticated users" on reports;
create policy "reports editable by anyone" on reports
  for update using (true) with check (true);

-- LOCKDOWN MODE (not yet applied): only signed-in accounts may edit.
--   drop policy if exists "reports editable by anyone" on reports;
--   create policy "reports editable by authenticated users" on reports
--     for update using (auth.role() = 'authenticated') with check (auth.role() = 'authenticated');

-- ---------- Player flags (2026-10) ----------

create table if not exists flag_types (
  id uuid primary key default gen_random_uuid(),
  label text not null,
  color text not null check (color ~ '^#[0-9a-fA-F]{6}$'),
  created_at timestamptz default now()
);

-- No foreign key to players on purpose: a player whose row is replaced (e.g. at the season
-- rollover) shouldn't silently lose his flag - an orphaned flag just doesn't show, and can be
-- moved to the new player_id.
create table if not exists player_flags (
  player_id text primary key,
  flag_id uuid not null references flag_types(id) on delete cascade,
  updated_at timestamptz default now()
);

alter table flag_types enable row level security;
alter table player_flags enable row level security;

-- DEMO MODE (current live state, while Nelson is the only user): anyone with the link can
-- read and change flags, like reports. LOCKDOWN (once logins exist): swap "using (true)" /
-- "with check (true)" for auth.role() = 'authenticated' - or per-user flags, Nelson's call.
drop policy if exists "flag_types open to anyone" on flag_types;
create policy "flag_types open to anyone" on flag_types
  for all using (true) with check (true);

drop policy if exists "player_flags open to anyone" on player_flags;
create policy "player_flags open to anyone" on player_flags
  for all using (true) with check (true);
