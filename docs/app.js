// Shared helpers used by index.html, player.html, and report.html.
// Relies on the Supabase JS UMD build (loaded via <script> tag before this file) and on
// window.SUPABASE_URL / window.SUPABASE_ANON_KEY from config.js.

const supabaseClient = window.supabase.createClient(
  window.SUPABASE_URL,
  window.SUPABASE_ANON_KEY
);

async function fetchAllPlayers() {
  // Supabase/PostgREST caps a single response at 1000 rows regardless of how many match,
  // so page through with .range() until a page comes back short of the page size.
  const pageSize = 1000;
  let all = [];
  let from = 0;
  for (;;) {
    const { data, error } = await supabaseClient
      .from("players")
      .select("*")
      .order("name", { ascending: true })
      .range(from, from + pageSize - 1);
    if (error) throw error;
    all = all.concat(data || []);
    if (!data || data.length < pageSize) break;
    from += pageSize;
  }
  return all;
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

function fmtPct(v) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (Number.isNaN(n)) return "—";
  return `${n.toFixed(1)}%`;
}

function playerLink(playerId) {
  return `player.html?id=${encodeURIComponent(playerId)}`;
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
