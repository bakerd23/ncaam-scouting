-- Run this once in the Supabase SQL editor (Project -> SQL Editor -> New query) to set up the
-- database. Safe to re-run: uses IF NOT EXISTS / OR REPLACE where possible, but if you already
-- have these tables from a previous run, drop them first or skip the `create table` statements.

create table if not exists players (
  player_id text primary key,        -- slug: name-school, e.g. "cooper-flagg-duke"
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
-- are percentages (e.g. 12.3 = 12.3%); ft_rate is FTA per 100 FGA.
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

-- Deliberately no insert/update/delete policy on `players` or `career_stats` for the public
-- (anon) role, and no update/delete policy on `reports` either. Both `players` and
-- `career_stats` are written only by the daily scraper using the service_role key, which
-- bypasses RLS entirely. Scouts can only ever add new reports, never edit or delete existing
-- ones (or any player/career data) from the public site.
