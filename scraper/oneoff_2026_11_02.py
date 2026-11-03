"""One-time cleanup (2026-11-02), run once from Actions and then removed:
- move Kuai Deng's report from the scout-added duplicate to his real NJCAA row, fix its
  date (2027-10-09 -> 2026-10-09), and delete the duplicate
- delete two Mercer players no longer on ESPN's roster (stale 2025-26 stats)
"""

import os

from supabase import create_client

REPORT_ID = "beca8834-a425-4650-89dc-14b065e42a6e"
ADDED_ID = "added-137dc42a-574b-4ae1-8c1d-296f63056b00"
REAL_ID = "njcaa-422450529256343250"
MERCER_IDS = ["espn-5176784", "espn-5243236"]

c = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

real = c.table("players").select("player_id,name,school").eq("player_id", REAL_ID).execute().data
assert real and real[0]["name"] == "Kuai Deng", real
report = c.table("reports").select("id,player_id,date_watched").eq("id", REPORT_ID).execute().data
assert report and report[0]["player_id"] in (ADDED_ID, REAL_ID), report

print(
    "report:",
    c.table("reports")
    .update({"player_id": REAL_ID, "date_watched": "2026-10-09"})
    .eq("id", REPORT_ID)
    .execute()
    .data,
)

flags = c.table("player_flags").select("*").eq("player_id", ADDED_ID).execute().data
assert not flags, flags
left = c.table("reports").select("id").eq("player_id", ADDED_ID).execute().data
assert not left, left
print("deleted added:", c.table("players").delete().eq("player_id", ADDED_ID).eq("level", "ADDED").execute().data)

mercer_reports = c.table("reports").select("id").in_("player_id", MERCER_IDS).execute().data
assert not mercer_reports, mercer_reports
print(
    "deleted mercer:",
    [
        (r["player_id"], r["name"])
        for r in c.table("players").delete().in_("player_id", MERCER_IDS).eq("level", "D1").execute().data
    ],
)
