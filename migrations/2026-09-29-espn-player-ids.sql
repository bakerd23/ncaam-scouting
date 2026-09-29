-- One-time migration: re-key players from name+school slugs ("colby-duggan-charleston-cougars")
-- to ESPN's permanent athlete ID ("espn-5174947"), so reports and career history follow a
-- player across transfers and ESPN name changes. Also collapses the duplicate rows that
-- appeared while ESPN was part-way through rolling rosters over to 2026-27, and drops
-- leftover rows ESPN no longer lists.
--
-- Run once in the Supabase SQL editor. Safe to re-run (every step skips work already done).

begin;

-- 1. One espn-keyed row per player, copied from whichever old row has the most stats (ties go
--    to the most recently scraped). The next scraper run overwrites these fields anyway.
insert into players (
  player_id, name, school, conference, "position", class, height, weight, record,
  espn_player_id, gp, "min", ppg, rpg, apg, spg, bpg, topg, fg_pct, three_pct, ft_pct,
  orb_pct, drb_pct, stl_pct, blk_pct, ft_rate, last_updated
)
select distinct on (espn_player_id)
  'espn-' || espn_player_id, name, school, conference, "position", class, height, weight, record,
  espn_player_id, gp, "min", ppg, rpg, apg, spg, bpg, topg, fg_pct, three_pct, ft_pct,
  orb_pct, drb_pct, stl_pct, blk_pct, ft_rate, last_updated
from players
where espn_player_id is not null and player_id not like 'espn-%'
order by espn_player_id, (gp is null), gp desc, last_updated desc
on conflict (player_id) do nothing;

-- 2. Scouting reports follow the player to the new ID.
update reports r
set player_id = 'espn-' || p.espn_player_id
from players p
where r.player_id = p.player_id
  and p.espn_player_id is not null
  and p.player_id not like 'espn-%';

-- 3. Career history too (one copy per season + school, since duplicate rows had copies).
insert into career_stats (
  player_id, espn_player_id, season_year, season_display, school,
  gp, "min", ppg, rpg, apg, spg, bpg, topg, fg_pct, three_pct, ft_pct
)
select distinct on ('espn-' || p.espn_player_id, c.season_year, c.school)
  'espn-' || p.espn_player_id, p.espn_player_id, c.season_year, c.season_display, c.school,
  c.gp, c."min", c.ppg, c.rpg, c.apg, c.spg, c.bpg, c.topg, c.fg_pct, c.three_pct, c.ft_pct
from career_stats c
join players p on p.player_id = c.player_id
where p.espn_player_id is not null and p.player_id not like 'espn-%'
order by 'espn-' || p.espn_player_id, c.season_year, c.school
on conflict (player_id, season_year, school) do nothing;

-- 4. Remove the old slug-keyed rows (their split lines and career copies cascade with them;
--    reports were already moved in step 2).
delete from players
where espn_player_id is not null and player_id not like 'espn-%';

-- 5. Drop players the latest scraper run didn't find anywhere on ESPN (renamed, graduated,
--    left D1) - anything last seen more than a day before the newest row. Never a player with
--    reports. From here on the scraper prunes these itself after 7 days.
delete from players
where last_updated < (select max(last_updated) from players) - interval '1 day'
  and player_id not in (select player_id from reports where player_id is not null);

commit;
