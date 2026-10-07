// Shared helpers used by index.html, player.html, and report.html.
// Relies on the Supabase JS UMD build (loaded via <script> tag before this file) and on
// window.SUPABASE_URL / window.SUPABASE_ANON_KEY from config.js.

const supabaseClient = window.supabase.createClient(
  window.SUPABASE_URL,
  window.SUPABASE_ANON_KEY
);

// Supabase/PostgREST caps a single response at 1000 rows regardless of how many match, so
// big tables come back in pages. The first page also asks for the total row count; the rest
// are then fetched all at once rather than one after another. `buildQuery` must return a
// fresh query each call, with a stable order (ending in a unique column) so the pages don't
// overlap or skip rows.
async function fetchAllPages(buildQuery) {
  const pageSize = 1000;
  const first = await buildQuery({ count: "exact" }).range(0, pageSize - 1);
  if (first.error) throw first.error;
  const total = first.count ?? (first.data || []).length;
  const rest = [];
  for (let from = pageSize; from < total; from += pageSize) {
    rest.push(buildQuery().range(from, from + pageSize - 1));
  }
  const pages = await Promise.all(rest);
  for (const page of pages) if (page.error) throw page.error;
  return [first, ...pages].flatMap((page) => page.data || []);
}

async function fetchAllPlayers() {
  return fetchAllPages((opts) =>
    supabaseClient
      .from("players")
      .select("*", opts)
      .order("name", { ascending: true })
      .order("player_id", { ascending: true })
  );
}

async function fetchPlayer(playerId) {
  const { data, error } = await supabaseClient
    .from("players")
    .select("*")
    .eq("player_id", playerId)
    .maybeSingle();
  if (error) throw error;
  return data;
}

// Stat lines over a subset of games (e.g. split = "vs_hm"), keyed by player_id.
async function fetchAllSplits(split) {
  const rows = await fetchAllPages((opts) =>
    supabaseClient
      .from("player_splits")
      .select("*", opts)
      .eq("split", split)
      .order("player_id", { ascending: true })
  );
  const byPlayer = {};
  for (const row of rows) byPlayer[row.player_id] = row;
  return byPlayer;
}

async function fetchSplit(playerId, split) {
  const { data, error } = await supabaseClient
    .from("player_splits")
    .select("*")
    .eq("player_id", playerId)
    .eq("split", split)
    .maybeSingle();
  if (error) throw error;
  return data;
}

// Every stat column a player_splits row can stand in for on the players row.
const SPLIT_STAT_KEYS = [
  "gp", "min", "ppg", "rpg", "apg", "spg", "bpg", "topg", "fg_pct", "three_pct", "ft_pct",
  "orb_pct", "drb_pct", "stl_pct", "blk_pct", "ft_rate",
  "tot_min", "tot_pts", "tot_reb", "tot_ast", "tot_stl", "tot_blk", "tot_tov",
  "fgm", "fga", "fg3m", "fg3a", "ftm", "fta", "fga_pg", "fg3a_pg", "fta_pg",
];

// A copy of player `p` with its stat columns swapped for `split`'s (null where the player
// has no games in that split), leaving name/school/etc. untouched.
function withSplitStats(p, split) {
  const out = { ...p };
  for (const k of SPLIT_STAT_KEYS) out[k] = split ? split[k] : null;
  return out;
}

async function fetchReports(playerId) {
  const { data, error } = await supabaseClient
    .from("reports")
    .select("*")
    .eq("player_id", playerId)
    .order("date_watched", { ascending: false });
  if (error) throw error;
  return data || [];
}

async function fetchCareerStats(playerId) {
  const { data, error } = await supabaseClient
    .from("career_stats")
    .select("*")
    .eq("player_id", playerId)
    .order("season_year", { ascending: false });
  if (error) throw error;
  return data || [];
}

async function fetchReportCounts() {
  // Just the player_id column, paginated - lets index.html show a report count per player
  // without pulling every report's full content.
  const all = await fetchAllPages((opts) =>
    supabaseClient.from("reports").select("player_id", opts).order("id", { ascending: true })
  );
  const counts = {};
  for (const r of all) {
    counts[r.player_id] = (counts[r.player_id] || 0) + 1;
  }
  return counts;
}

async function submitReport(report) {
  const { error } = await supabaseClient.from("reports").insert(report);
  if (error) throw error;
}

async function getSession() {
  const { data, error } = await supabaseClient.auth.getSession();
  if (error) throw error;
  return data.session;
}

async function signOut() {
  await supabaseClient.auth.signOut();
}

// Adds a Log in / Log out link to a page's nav (id="auth-nav-slot") based on session state.
async function renderAuthNav() {
  const slot = document.getElementById("auth-nav-slot");
  if (!slot) return;
  const session = await getSession();
  if (session) {
    slot.innerHTML = `<a href="#" id="logout-link">Log out</a>`;
    document.getElementById("logout-link").addEventListener("click", async (e) => {
      e.preventDefault();
      await signOut();
      window.location.reload();
    });
  } else {
    slot.innerHTML = `<a href="login.html">Log in</a>`;
  }
}

function fmtStat(v, decimals = 1) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (Number.isNaN(n)) return "—";
  return n.toFixed(decimals);
}

// Whole-number counts with thousands separators (season totals: 1,274).
function fmtCount(v) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (Number.isNaN(n)) return "—";
  return Math.round(n).toLocaleString("en-US");
}

// "211-521" makes-attempts, or "—" if either is missing.
function fmtMadeAtt(made, att) {
  if (made === null || made === undefined || att === null || att === undefined) return "—";
  return `${fmtCount(made)}-${fmtCount(att)}`;
}

function fmtPct(v) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (Number.isNaN(n)) return "—";
  return `${n.toFixed(1)}%`;
}

// ESPN's full conference names are what's stored (the scraper matches on them); these are
// display-only abbreviations so the player table fits on a laptop screen.
const SHORT_CONFERENCE = {
  "America East Conference": "America East",
  "American Conference": "American",
  "Atlantic 10 Conference": "A-10",
  "Atlantic Coast Conference": "ACC",
  "Atlantic Sun Conference": "ASUN",
  "Big 12 Conference": "Big 12",
  "Big East Conference": "Big East",
  "Big Sky Conference": "Big Sky",
  "Big South Conference": "Big South",
  "Big Ten Conference": "Big Ten",
  "Big West Conference": "Big West",
  "Coastal Athletic Association": "CAA",
  "Conference USA": "CUSA",
  "Horizon League": "Horizon",
  "Ivy League": "Ivy",
  "Metro Conference": "MAAC",
  "Mid-American Conference": "MAC",
  "Mid-Eastern Athletic Conference": "MEAC",
  "Missouri Valley Conference": "MVC",
  "Mountain West Conference": "MWC",
  "Northeast Conference": "NEC",
  "Ohio Valley Conference": "OVC",
  "Pac-12 Conference": "Pac-12",
  "Patriot League": "Patriot",
  "Southeastern Conference": "SEC",
  "Southern Conference": "SoCon",
  "Southland Conference": "Southland",
  "Southwestern Athletic Conference": "SWAC",
  "Summit League": "Summit",
  "Sun Belt Conference": "Sun Belt",
  "United Athletic Conference": "UAC",
  "West Coast Conference": "WCC",
};

// Unknown names (ESPN adds or renames a conference) fall back to dropping a trailing
// "Conference", so they're still short-ish rather than blank.
function shortConference(name) {
  if (!name) return "";
  return SHORT_CONFERENCE[name] || name.replace(/\s+Conference$/, "");
}

function playerLink(playerId, hmOnly = false) {
  return `player.html?id=${encodeURIComponent(playerId)}${hmOnly ? "&hm=1" : ""}`;
}

function qs(name) {
  return new URLSearchParams(window.location.search).get(name);
}

function escapeHtml(s) {
  if (s === null || s === undefined) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
