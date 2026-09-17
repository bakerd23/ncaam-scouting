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
  notes text,
  submitted_at timestamptz default now()
);

create index if not exists reports_player_id_idx on reports(player_id);
create index if not exists players_school_idx on players(school);
create index if not exists players_conference_idx on players(conference);

alter table players enable row level security;
alter table reports enable row level security;

drop policy if exists "players readable by anyone" on players;
create policy "players readable by anyone" on players
  for select using (true);

drop policy if exists "reports readable by anyone" on reports;
create policy "reports readable by anyone" on reports
  for select using (true);

drop policy if exists "anyone can submit a report" on reports;
create policy "anyone can submit a report" on reports
  for insert with check (true);

-- Deliberately no insert/update/delete policy on `players` for the public (anon) role, and no
-- update/delete policy on `reports` either. Player rows are written only by the daily scraper
-- using the service_role key, which bypasses RLS entirely. Scouts can only ever add new reports,
-- never edit or delete existing ones (or other players' data) from the public site.
