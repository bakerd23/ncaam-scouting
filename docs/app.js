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

// Players are D1 (ESPN), JUCO (NJCAA D1/D2) or ADDED (added by a scout from the report
// form). Rows from before the level column existed count as D1.
function playerLevel(p) {
  return p.level || "D1";
}

// Display-only: JUCO school names run long ("Shelton State Community College"), which
// pushes the player table off a laptop screen. Full name stays in the data and on hover.
function shortSchool(name) {
  return (name || "")
    .replace(/\bCommunity & Technical College\b/g, "CTC")
    .replace(/\bCommunity College\b/g, "CC")
    .replace(/\bJunior College\b/g, "JC")
    .replace(/\bTechnical College\b/g, "Tech")
    .replace(/\bState College\b/g, "State");
}

// Line under the school: "D1 · Region 17" for JUCO, what the scout called him ("High
// School") for scout-added players, the short conference name for D1.
function conferenceLabel(p) {
  const lvl = playerLevel(p);
  if (lvl === "JUCO") return [p.juco_division, p.conference].filter(Boolean).join(" · ");
  if (lvl === "ADDED") return p.added_level || "Added by scout";
  return shortConference(p.conference);
}

// " · JUCO" / " · Added" tag after a school name in pickers; nothing for D1.
function levelTag(p) {
  const lvl = playerLevel(p);
  return lvl === "JUCO" ? " · JUCO" : lvl === "ADDED" ? " · Added" : "";
}

// Adds a player who isn't on any ESPN/NJCAA roster. The database only accepts rows shaped
// like this from the public site (level ADDED, "added-" id, no stats) - see schema.sql.
function newPlayerRow(fields) {
  return {
    ...fields,
    player_id: `added-${crypto.randomUUID()}`,
    level: "ADDED",
  };
}

// ---------- Offline outbox ----------
// A report submitted with no connection is saved in this browser's localStorage and sent
// later - automatically when the connection comes back, or whenever any page of the site
// is opened online. It only lives on that device/browser until it's sent.
const OUTBOX_KEY = "reportOutbox";

function readOutbox() {
  try {
    return JSON.parse(localStorage.getItem(OUTBOX_KEY) || "[]");
  } catch (e) {
    return [];
  }
}

// Throws if the browser won't store it (private mode, storage blocked) - callers handle that.
function writeOutbox(items) {
  localStorage.setItem(OUTBOX_KEY, JSON.stringify(items));
}

function queueReport(item) {
  writeOutbox([...readOutbox(), item]);
}

function isNetworkError(err) {
  if (!navigator.onLine) return true;
  const m = String((err && (err.message || err)) || "");
  return /failed to fetch|networkerror|load failed|network request failed/i.test(m);
}

function isDuplicate(err) {
  return !!err && (err.code === "23505" || /duplicate key/i.test(err.message || ""));
}

// Sends one report, creating its new player first if it has one. `item` carries the report
// and player ids up front, so a retry after a send that half-finished (or finished but the
// reply never arrived) hits "duplicate key" and counts as done instead of saving it twice.
async function sendReport(item) {
  if (item.newPlayer) {
    const { error } = await supabaseClient.from("players").insert(item.newPlayer);
    if (error && !isDuplicate(error)) throw error;
  }
  const { error } = await supabaseClient.from("reports").insert(item.report);
  if (error && !isDuplicate(error)) throw error;
}

let outboxFlushing = false;

async function flushOutbox() {
  if (outboxFlushing || !navigator.onLine) return;
  outboxFlushing = true;
  try {
    for (const item of readOutbox()) {
      try {
        await sendReport(item);
        writeOutbox(readOutbox().filter((x) => x.report.id !== item.report.id));
      } catch (err) {
        if (isNetworkError(err)) break; // still offline - try again later
        // Rejected for some other reason: keep it (never silently drop a report) and say why.
        writeOutbox(
          readOutbox().map((x) =>
            x.report.id === item.report.id ? { ...x, lastError: err.message || String(err) } : x
          )
        );
      }
    }
  } catch (e) {
    console.error(e);
  } finally {
    outboxFlushing = false;
    renderOutboxBanner();
  }
}

// Small bar at the bottom of every page while reports are waiting on this device.
function renderOutboxBanner() {
  const items = readOutbox();
  let bar = document.getElementById("outbox-banner");
  if (!items.length) {
    if (bar) bar.remove();
    return;
  }
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "outbox-banner";
    bar.className = "outbox-banner";
    document.body.appendChild(bar);
  }
  const failed = items.filter((x) => x.lastError);
  const n = items.length;
  bar.innerHTML = `
    <span>${n} report${n === 1 ? "" : "s"} saved on this device, waiting to send${
      navigator.onLine ? "" : " (no connection)"
    }.${failed.length ? ` ${failed.length} couldn't be sent: ${escapeHtml(failed[0].lastError)}` : ""}</span>
    <button type="button" class="btn secondary" id="outbox-send">Send now</button>`;
  bar.querySelector("#outbox-send").addEventListener("click", flushOutbox);
}

window.addEventListener("online", flushOutbox);
window.addEventListener("offline", renderOutboxBanner);
// Mobile browsers don't always fire "online", so also retry every minute while anything's queued.
setInterval(() => {
  if (readOutbox().length) flushOutbox();
}, 60000);

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

renderOutboxBanner();
flushOutbox();
