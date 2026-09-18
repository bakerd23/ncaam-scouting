// Public Supabase project config. The anon key is safe to expose here — it can only do what
// the RLS policies in schema.sql allow (read players/reports, insert reports). Filled in once
// the Supabase project exists.
window.SUPABASE_URL = "https://sokbqukhzdnwwmzerodp.supabase.co";
window.SUPABASE_ANON_KEY = "sb_publishable_uEExsDNR_8Nl2pr8o-YzYQ_YQkziU8J";

// Demo mode: reports show to everyone, no login needed. The reports table's RLS policy is
// still the public-read one at this point (the authenticated-only policy in schema.sql hasn't
// been applied yet), so this flag isn't hiding anything the database wouldn't otherwise show -
// it just skips the login prompt in the UI. Once the client's login is set up and the RLS
// policy is switched to authenticated-only, flip this to true.
window.REQUIRE_LOGIN_FOR_REPORTS = false;
