const CHAIN = document.documentElement.dataset.chain || "sol";
const PAIR_CACHE_MAX = 3;

function withChain(url) {
  const u = new URL(url, location.origin);
  if (!u.searchParams.has("chain")) u.searchParams.set("chain", CHAIN);
  return u.pathname + u.search;
}

function xHandle(value) {
  return String(value || "").replace(/^@/, "");
}

function looksLikeContract(value) {
  const raw = String(value || "").trim();
  if (raw.length >= 10 && raw.toLowerCase().startsWith("0x")) return true;
  return raw.length >= 32 && raw.length <= 44 && /^[1-9A-HJ-NP-Za-km-z]+$/.test(raw);
}

function tapeTicker(t) {
  const sym = String(t?.symbol || "").trim();
  if (sym && !looksLikeContract(sym)) return sym;
  const name = String(t?.name || "").trim();
  if (name && !looksLikeContract(name)) return name;
  return "—";
}

function tapeSub(t) {
  const ticker = tapeTicker(t);
  const name = String(t?.name || "").trim();
  if (name && name !== ticker && !looksLikeContract(name)) return name;
  const mint = String(t?.mint || "").trim();
  return mint || "";
}

function fmtCompact(n) {
  const v = Number(n || 0);
  if (!v) return "";
  if (v >= 1e6) return `${(v / 1e6).toFixed(v >= 10e6 ? 0 : 1).replace(/\.0$/, "")}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(v >= 10e3 ? 0 : 1).replace(/\.0$/, "")}k`;
  return String(Math.round(v));
}

/** Paper-safe desk toast + optional browser Notification. Not a trade signal. */
const _earlyDiffNotified = new Set();
function maybeEarlyDiffDesktopNotify(alerts) {
  const rows = Array.isArray(alerts) ? alerts : [];
  for (const a of rows.slice(0, 3)) {
    const key = `${a.mint || ""}:${a.at || ""}`;
    if (!key || _earlyDiffNotified.has(key)) continue;
    _earlyDiffNotified.add(key);
    const title = `Early-diff closer-look · ${a.symbol || "mint"}`;
    const body = a.message || `crossed ${a.threshold ?? 0.55} (score ${a.divergence_score ?? "—"}) · not a trade`;
    try {
      if (typeof Notification !== "undefined" && Notification.permission === "granted") {
        new Notification(title, { body, tag: `ediff-${a.mint || "x"}` });
      } else if (typeof Notification !== "undefined" && Notification.permission === "default") {
        Notification.requestPermission().then((perm) => {
          if (perm === "granted") new Notification(title, { body, tag: `ediff-${a.mint || "x"}` });
        }).catch(() => {});
      }
    } catch (_) { /* ignore */ }
    console.info("[early-diff alert]", title, body);
  }
}

function xProfileLine(t) {
  if (t.claimed_brand_x) return "";
  const bits = [];
  const age = Number(t.twitter_age_days || 0);
  const fol = Number(t.twitter_followers || 0);
  const tw = Number(t.twitter_tweets || 0);
  if (age > 0) bits.push(`${Math.round(age)}d`);
  if (fol > 0) bits.push(`${fmtCompact(fol)} fol`);
  if (tw > 0) bits.push(`${fmtCompact(tw)} tw`);
  if (t.twitter_bio_match) bits.push("bio matches");
  const ghAge = Number(t.github_age_days || 0);
  if (t.github_url && ghAge >= 30) bits.push(`gh ${Math.round(ghAge)}d`);
  if (!bits.length) return "";
  return `<span class="x-profile">${esc(bits.join(" · "))}</span>`;
}

function xAccountLinks(t) {
  const tokenX = xHandle(t.twitter_handle);
  const devX = xHandle(t.dev_handle);
  if (t.claimed_brand_x && tokenX) {
    return `<span class="x-brand">Claimed @${esc(tokenX)}</span>`;
  }
  const same = tokenX && devX && tokenX.toLowerCase() === devX.toLowerCase();
  const token = tokenX && !same
    ? `<a class="x-token" href="https://x.com/${esc(tokenX)}" target="_blank" rel="noreferrer">X @${esc(tokenX)}</a>`
    : "";
  const dev = t.has_dev_x && devX
    ? `<a class="x-dev" href="https://x.com/${esc(devX)}" target="_blank" rel="noreferrer">Dev @${esc(devX)}</a>`
    : "";
  return `${token}${dev}${xProfileLine(t)}`;
}

const rowsEl = document.getElementById("rows");
const detailEl = document.getElementById("detail");
const metricsEl = document.getElementById("metrics");
const chartHead = document.getElementById("chart-head");
const pairHost = document.getElementById("pair-host");
const pairSkel = document.getElementById("pair-skel");
const pairFallback = document.getElementById("pair-fallback");
const pairDex = document.getElementById("pair-dex");
const minScore = document.getElementById("min-score");
const minLabel = document.getElementById("min-score-label");
const liveOnly = document.getElementById("live-only");
const sortSel = document.getElementById("sort");
const searchEl = document.getElementById("search");
const deskMeta = document.getElementById("desk-meta");
const statusEl = document.getElementById("status");
const statusText = document.getElementById("status-text");

let selected = null;
let lastItems = [];
let hotItems = [];
let bloomItems = [];
let watchItems = [];
let walletItems = [];
let walletMeta = { wallets: 0, gold: 0, parked: 0 };
let paperItems = [];
let paperMeta = { open: 0, closed: 0, wins: 0, avg: null, total: null, rule: "", scorecard: null };
let pickItems = [];
let pickMeta = { day: "", cap: 6, cap_per_chain: 3, taken: 0, locked: false, queued: 0, open: 0, closed: 0, skipped: 0, wins: 0, avg: null, rule: "", summary: null };
let reviewData = { day: "", picked: [], queued: [], skipped: [], closed: [], metrics: null, loadedAt: 0 };
let runnersRetroData = null;
let yearWinnersData = null;
let missCohortData = null;
let offlineSprintData = null;
let fomoTrendingData = null;
let fomoTradersData = null;
let fomoAlertsFlowData = null;
let vetoRetroData = null;
let sanityLoopData = null;
let dayDeltaData = null;
let earlyDiffData = null;
let productionGateData = null;
let reportData = { text: "", risk: null, totals: null, loadedAt: 0 };
let learnRows = [];
let ticketItems = [];
let boardData = { calibration: null, weekly: null, artifacts: null, firstSight: null, live: null, scorecard: null, loadedAt: 0 };
let honest = { weekly: null, loadedAt: 0, counts: null };
let healthState = {};
let deskTab = "picks";
let lastDetail = null;
let pairCache = new Map();
let pairHangTimer = 0;
let stripCache = new Map();
const tapeStrip = document.getElementById("tape-strip");
const boardPane = document.getElementById("board-pane");
const learnPane = document.getElementById("learn-pane");
const fomoPane = document.getElementById("fomo-pane");

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" }[c]));
}
function pct(n) { return `${Math.round((n || 0) * 1000) / 10}%`; }
// Model cards: a missing metric is a dash, never 0%.
function pctOrDash(n) { return n == null || Number.isNaN(Number(n)) ? "—" : `${Math.round(Number(n) * 100)}%`; }
function liveMultiple(t) {
  const t0 = Number(t.t0_mcap || 0);
  const last = Number(t.last_mcap || 0);
  if (t0 > 0 && last > 0) return last / t0;
  return last > 0 ? Number(t.multiple || 0) : 0;
}
function liveMcap(t) { return Number(t.last_mcap || 0); }
function fmtMcap(n) {
  const v = Number(n || 0);
  if (!v) return "—";
  if (v >= 1e9) return `$${(v / 1e9).toFixed(v >= 10e9 ? 0 : 1)}B`;
  if (v >= 1e6) return `$${(v / 1e6).toFixed(v >= 10e6 ? 1 : 2).replace(/\.0$/, "")}M`;
  if (v >= 1e3) return `$${(v / 1e3).toFixed(v >= 10e3 ? 0 : 1).replace(/\.0$/, "")}k`;
  return `$${Math.round(v).toLocaleString()}`;
}
function ageOf(t) {
  // Sol leftovers (live ARMY): pair created_at can be a year older
  // than migrated_at / first_seen. Prefer the earlier honest clock.
  const stamps = [t.created_at, t.migrated_at, t.first_seen_at]
    .map((raw) => (raw ? new Date(raw).getTime() : 0))
    .filter((ms) => ms > Date.parse("2021-01-01T00:00:00Z"));
  if (!stamps.length) return null;
  const ms = Date.now() - Math.min(...stamps);
  return ms > 0 ? ms / 60000 : null;
}
function fmtAge(mins) {
  if (mins == null) return "—";
  if (mins < 90) return `${Math.round(mins)}m`;
  if (mins < 48 * 60) return `${(mins / 60).toFixed(1)}h`;
  return `${(mins / 1440).toFixed(1)}d`;
}

function paperScoreAvgs(meta) {
  const sc = meta.scorecard || {};
  const tw = sc.this_window || {};
  const lc = sc.leftover_clock || {};
  const nr = sc.no_run || {};
  const bits = [];
  const by = sc.this_window_by_line || {};
  const hiEv = by.hi || {};
  const watchEv = by.watch || {};
  if (tw.avg_return_pct != null) bits.push(`this-window avg ${tw.avg_return_pct}%`);
  if (hiEv.n && hiEv.avg_return_pct != null) bits.push(`buy-line EV ${hiEv.avg_return_pct}%`);
  if (watchEv.n && watchEv.avg_return_pct != null) bits.push(`watch EV ${watchEv.avg_return_pct}%`);
  const sh = sc.shadow_late || {};
  if (sh.n && sh.avg_return_pct != null) bits.push(`shadow-late EV ${sh.avg_return_pct}%`);
  if (lc.avg_return_pct != null) bits.push(`leftover-clock avg ${lc.avg_return_pct}%`);
  if (nr.avg_return_pct != null) bits.push(`no-run avg ${nr.avg_return_pct}%`);
  const v1 = sc.paper_v1 || {};
  if (v1.cap) {
    const today = v1.today || {};
    const taken = today.taken || 0;
    bits.push(`paperV1 ${taken}/${v1.cap}${today.locked ? " locked" : ""}`);
  }
  if (!bits.length && meta.avg != null) bits.push(`avg ${meta.avg}%`);
  return bits.length ? ` · ${bits.join(" · ")}` : "";
}

function leftoverChip(t) {
  if (!t.leftover_window) return "";
  return ` <span class="leftover-chip" title="12–24h leftover on RH Hunt. Paper stays 12h. Sort is still 2× then Live, not leftover.">leftover</span>`;
}

function huntAgeChip(t) {
  const age = fmtAge(ageOf(t));
  if (t.leftover_window) {
    return `<span class="num leftover" title="12–24h leftover on RH Hunt. Paper stays 12h. Sort is still 2× then Live, not leftover.">${age}</span>`;
  }
  return `<span class="num">${age}</span>`;
}
function scoreClass(s) { return s >= 55 ? "high" : s >= 35 ? "mid" : "low"; }

// Desk lines belong to the scorer that wrote the Entry, not to the chain.
// Legacy blend: 70 / 90. First-sight (calibrated p of a sellable 2× in 24h):
// Sol 10 / 14 (watch / buy on the honest plateau), RH 25 / 30. The API
// sends the lines on the weekly board, the first-sight card and every card.
const LEGACY_LINES = { scorer: "legacy", lo: 0.7, hi: 0.9, thin: 0.2 };
// First-sight lines are per chain (mirrors launchfinder/desk_lines.py); the
// server sends desk_lines on every card, these are only the offline fallback.
const FIRST_SIGHT_LINES = CHAIN === "rh" || CHAIN === "robinhood"
  ? { scorer: "first_sight", lo: 0.25, hi: 0.3, thin: 0.05 }
  : { scorer: "first_sight", lo: 0.1, hi: 0.14, thin: 0.05 };
function chainLines() {
  return honest.weekly?.lines || boardData.firstSight?.desk_lines || boardData.calibration?.lines || LEGACY_LINES;
}
function cardLines(t) {
  if (t?.desk_lines) return t.desk_lines;
  if (t?.scorer === "first_sight") return FIRST_SIGHT_LINES;
  if (t?.scorer === "legacy") return LEGACY_LINES;
  return chainLines();
}
function linePct(v) { return Math.round(Number(v || 0) * 100); }
function entryClass(entryPct, lines) {
  const L = lines || chainLines();
  return entryPct >= L.hi * 100 ? "high" : entryPct >= L.lo * 100 ? "mid" : "low";
}
function lineName(lines, slot) {
  const L = lines || chainLines();
  if (L.scorer === "first_sight") {
    if (slot === "hi") return L.hi >= 0.5 ? "even odds" : L.hi === 0.3 ? "3-in-10 buy line" : "buy line";
    return L.lo === 0.3 ? "3-in-10" : "watch line";
  }
  return slot === "hi" ? "90 line" : "70 line";
}
function entryTierTitle(entryPct, lines) {
  const L = lines || chainLines();
  const scale = L.scorer === "first_sight" ? "first-sight p(2× in 24h)" : "legacy blend";
  if (entryPct >= L.hi * 100) return `above the ${lineName(L, "hi")} (≥${linePct(L.hi)}) · ${scale}`;
  if (entryPct >= L.lo * 100) return `above the ${lineName(L, "lo")} (≥${linePct(L.lo)}) · ${scale}`;
  return `under the desk lines (${linePct(L.lo)} / ${linePct(L.hi)}) · ${scale}`;
}
// Piecewise map onto the legacy 0.70 / 0.90 scale (mirrors desk_lines.legacy_equivalent).
// The pill stays the calibrated first-sight p (19). 90-eq is a label, never the score.
function legacyEquivalent(p, lines) {
  const L = lines || chainLines();
  p = Math.min(1, Math.max(0, Number(p || 0)));
  if (!L || L.scorer === "legacy" || !(Number(L.hi) > Number(L.lo)) || !(Number(L.lo) > 0)) return p;
  const loOut = 0.7;
  const hiOut = 0.9;
  let eq;
  if (p <= L.lo) eq = (p / L.lo) * loOut;
  else if (p <= L.hi) eq = loOut + ((p - L.lo) / (L.hi - L.lo)) * (hiOut - loOut);
  else eq = hiOut + ((p - L.hi) / (1 - L.hi)) * (1 - hiOut);
  return Math.round(eq * 10000) / 10000;
}
function entryLineLabel(entryPct, lines) {
  const L = lines || chainLines();
  if (!(entryPct > 0) || L.scorer !== "first_sight") return "";
  if (entryPct >= L.hi * 100) return "buy";
  if (entryPct >= L.lo * 100) return "watch";
  return "";
}
function entryEqNote(entryPct, lines) {
  const L = lines || chainLines();
  if (L.scorer !== "first_sight" || !(entryPct > 0)) return "";
  return `90-eq ${Math.round(legacyEquivalent(entryPct / 100, L) * 100)}`;
}
function entryTitle(entryPct, lines, frozen) {
  const L = lines || chainLines();
  const bits = [entryPct ? `Entry ${entryPct.toFixed(0)}` : "Entry —"];
  if (frozen) bits.push("frozen at first sight");
  bits.push(entryTierTitle(entryPct, L));
  const eq = entryEqNote(entryPct, L);
  if (eq) bits.push(eq);
  return bits.join(" · ");
}
function entryPillText(entryPct, lines) {
  if (!entryPct) return "—";
  const line = entryLineLabel(entryPct, lines);
  return line ? `${entryPct.toFixed(0)} ${line}` : entryPct.toFixed(0);
}
function entryCell(t, frozen) {
  const entry = entryPct(t);
  const L = cardLines(t);
  return `<span class="sc-pill ${entryClass(entry, L)}" title="${esc(entryTitle(entry, L, frozen))}">${entryPillText(entry, L)}</span>`;
}
function entryDetailLine(t, entry) {
  const L = cardLines(t);
  const line = entryLineLabel(entry, L);
  const eq = entryEqNote(entry, L);
  const bits = [`Entry ${entry ? entry.toFixed(0) : "—"}%`];
  if (line) bits.push(eq ? `${line} line (${eq})` : `${line} line`);
  else if (eq) bits.push(eq);
  return bits.join(" · ");
}
function multipleClass(x) { return x >= 1 ? "up" : x > 0 ? "down" : ""; }

async function j(url, opts = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), opts.timeoutMs || 10000);
  try {
    const r = await fetch(url, { ...opts, signal: ctrl.signal });
    if (!r.ok) throw new Error(await r.text());
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

function walletExplorer(owner) {
  return CHAIN === "robinhood"
    ? `https://robinhoodchain.blockscout.com/address/${esc(owner)}`
    : `https://solscan.io/account/${esc(owner)}`;
}

function embedUrl(dexLink) {
  if (!dexLink) return "";
  const u = new URL(dexLink, "https://dexscreener.com");
  u.searchParams.set("embed", "1");
  u.searchParams.set("theme", "dark");
  u.searchParams.set("trades", "0");
  u.searchParams.set("info", "0");
  u.searchParams.set("tabs", "0");
  u.searchParams.set("loadChartSettings", "0");
  u.searchParams.set("chartLeftToolbar", "0");
  return u.toString();
}

function dumpedOffAth(t) {
  return (t?.risk_flags || []).some((flag) => String(flag).includes("Dumped off ATH"));
}

function liveConviction(t) {
  if (t.watch_only) {
    if (t.preview_p != null && t.preview_p !== "") return Number(t.preview_p) * 100;
    return Number(t.score || 0);
  }
  // Live ETAC: Dex dumped, Hunt conviction_p stayed 90 and Doing well
  // score stayed 92 because the tape row overwrote the honest token card.
  if (!dumpedOffAth(t)) {
    if (t.conviction_p != null && t.conviction_p !== "") return Number(t.conviction_p) * 100;
    if (t.promise_p != null && t.promise_p !== "") return Number(t.promise_p) * 100;
    return null;
  }
  if (t.score != null && t.score !== "") return Number(t.score);
  return null;
}
function entryPct(t) {
  const p = Number(t.entry_p != null ? t.entry_p : t.p_good || 0);
  return p > 1 ? p : p * 100;
}
function paperPnl(t) {
  if (t.return_pct != null && t.return_pct !== "") return Number(t.return_pct);
  const x = Number(t.multiple || 0);
  if (x <= 0) return null;
  // Open mark: full bag under 2×; half banked at 2×, rest marked.
  if (x >= 2) return (0.5 * 1 + 0.5 * (x - 1)) * 100;
  return (x - 1) * 100;
}
function fmtPnl(n) {
  if (n == null || Number.isNaN(n)) return "—";
  const v = Number(n);
  return `${v > 0 ? "+" : ""}${v.toFixed(1)}%`;
}

function lineTotals(weekly, line) {
  // Sum a desk line across the honest weekly board: n, resolved, hit2x.
  const out = { n: 0, resolved: 0, hit2x: 0, hit5x: 0 };
  for (const wk of weekly?.weeks || []) {
    const cell = (wk.lines || {})[line];
    if (!cell) continue;
    out.n += Number(cell.n || 0);
    out.resolved += Number(cell.resolved || 0);
    out.hit2x += Number(cell.hit2x || 0);
    out.hit5x += Number(cell.hit5x || 0);
  }
  return out;
}

function fmtRate(hit, n) {
  if (!n) return "—";
  return `${Math.round((hit / n) * 100)}%`;
}

function rateClass(hit, n, line) {
  if (!n || n < 8) return "";
  const rate = hit / n;
  return rate >= line ? "good" : rate < line * 0.6 ? "bad" : "warn";
}

function renderMetrics(s) {
  const cells = [];
  const taken = pickMeta.taken || 0;
  const cap = pickMeta.cap || 6;
  const per = pickMeta.cap_per_chain || 3;
  const locked = pickMeta.locked ? " locked" : "";
  cells.push([
    "Picks today",
    `${taken}/${cap}`,
    taken > 0 ? "good" : "",
    `paperV1 short list · ${per}/chain/day (${cap} total) · ${pickMeta.locked ? "locked for today" : "open until 23:00 UTC"}${locked}. Paper only — no auto-buy.`,
  ]);
  cells.push([
    "Short P&L",
    pickMeta.avg != null ? `${pickMeta.avg}%` : "—",
    "",
    `Closed paperV1 avg on this chain · ${pickMeta.wins || 0} wins · ${pickMeta.closed || 0} closed · ${pickMeta.open || 0} open · ${pickMeta.queued || 0} queued`,
  ]);
  cells.push(["Hunt flood", lastItems.length || "—", "", "Sensor tape — training ocean, not the buy list"]);
  const L = chainLines();
  const lhi = lineTotals(honest.weekly, "hi");
  cells.push([
    `${linePct(L.hi)}+ → 2×`,
    lhi.resolved ? `${fmtRate(lhi.hit2x, lhi.resolved)}<small>/${lhi.resolved}</small>` : "—",
    rateClass(lhi.hit2x, lhi.resolved, L.hi),
    `Wide-book calibration (honest board). Short-list success is paperV1 P&L, not this rate.`,
  ]);
  const rev = healthState.image_rev || "";
  if (rev) cells.push(["Image", rev.replace(/^stack-/, ""), "", `Running IMAGE_REV ${rev}${healthState.github ? " · GitHub token on" : " · GitHub token off"}`]);
  if (honest.counts) {
    cells.push(["Ledger", `${fmtCompact(honest.counts.decisions) || 0}<small>dec</small>`, "", `${honest.counts.decisions} decisions · ${honest.counts.paper_fills} fills · ${honest.counts.tickets} tickets, append-only`]);
  }
  metricsEl.innerHTML = cells
    .map(([k, v, cls, title]) => `<div class="metric ${cls}" title="${esc(title)}"><span>${k}</span><b>${v}</b></div>`)
    .join("");
}

function filtered(items) {
  const q = (searchEl.value || "").trim().toLowerCase();
  const min = Number(minScore.value || 0);
  const noMin = deskTab === "wallets" || deskTab === "paper" || deskTab === "tickets" || deskTab === "board" || deskTab === "picks" || deskTab === "learn" || deskTab === "fomo";
  return items.filter((t) => {
    if (!noMin && Number(t.score || 0) < min) return false;
    if (!q) return true;
    if (t.owner || t.wallet) {
      return [t.owner, t.wallet, t.best_symbol, t.grade].some((x) => String(x || "").toLowerCase().includes(q));
    }
    if (t.learn_only) {
      return [t.symbol, t.name, t.status, t.thesis].some((x) => String(x || "").toLowerCase().includes(q));
    }
    const tags = (t.tags || []).join(" ");
    return [t.symbol, t.name, t.mint, t.launchpad, t.veto, t.status, tags].some((x) => String(x || "").toLowerCase().includes(q));
  });
}

function sorted(items) {
  if (deskTab === "board" || deskTab === "learn" || deskTab === "fomo") return items;
  const copy = items.slice();
  const key = sortSel.value;
  copy.sort((a, b) => {
    if (key === "thesis" || (deskTab === "picks" && key === "newest")) {
      return (Number(b.thesis_score || 0) - Number(a.thesis_score || 0))
        || (Number(b.live_p || b.entry_p || 0) - Number(a.live_p || a.entry_p || 0));
    }
    if (key === "score") return (liveConviction(b) || 0) - (liveConviction(a) || 0);
    if (key === "entry") return entryPct(b) - entryPct(a);
    if (key === "mcap") return liveMcap(b) - liveMcap(a);
    if (key === "multiple") return liveMultiple(b) - liveMultiple(a);
    const ta = new Date(a.migrated_at || a.first_seen_at || a.created_at || a.opened_at || 0).getTime();
    const tb = new Date(b.migrated_at || b.first_seen_at || b.created_at || b.opened_at || 0).getTime();
    return tb - ta;
  });
  return copy;
}

function activeItems() {
  if (deskTab === "picks") return pickItems;
  if (deskTab === "learn") return learnRows;
  if (deskTab === "hot") return hotItems;
  if (deskTab === "bloom") return bloomItems;
  if (deskTab === "watch") return watchItems;
  if (deskTab === "wallets") return walletItems;
  if (deskTab === "paper") return paperItems;
  if (deskTab === "tickets") return ticketItems;
  if (deskTab === "board") return boardRows();
  if (deskTab === "fomo") return fomoTabRows();
  return lastItems;
}

const TAPE_REFRESH_MS = 12000;

function renderTapeHead() {
  const head = document.querySelector(".tape-head");
  if (!head) return;
  const cols = head.querySelectorAll("span");
  if (cols.length < 6) return;
  if (deskTab === "picks") {
    cols[1].textContent = "State";
    cols[2].textContent = "Thesis";
    cols[3].textContent = "×";
    cols[4].textContent = "Entry";
    cols[5].textContent = "P&L";
  } else if (deskTab === "learn") {
    cols[1].textContent = "Pulse";
    cols[2].textContent = "Age";
    cols[3].textContent = "n";
    cols[4].textContent = "Signal";
    cols[5].textContent = "OK";
  } else if (deskTab === "watch") {
    cols[1].textContent = "Age";
    cols[2].textContent = "Mcap";
    cols[3].textContent = "Curve";
    cols[4].textContent = "—";
    cols[5].textContent = "Prev";
  } else if (deskTab === "wallets") {
    cols[1].textContent = "Early";
    cols[2].textContent = "FOMO";
    cols[3].textContent = "Best";
    cols[4].textContent = "×";
    cols[5].textContent = "Score";
  } else if (deskTab === "paper") {
    cols[1].textContent = "State";
    cols[2].textContent = "Entry";
    cols[3].textContent = "×";
    cols[4].textContent = "Score";
    cols[5].textContent = "P&L";
  } else if (deskTab === "tickets") {
    cols[1].textContent = "State";
    cols[2].textContent = "Size";
    cols[3].textContent = "×";
    cols[4].textContent = "Entry";
    cols[5].textContent = "P&L";
  } else if (deskTab === "board") {
    cols[1].textContent = "n";
    cols[2].textContent = "Pred";
    cols[3].textContent = "2×";
    cols[4].textContent = "5×";
    cols[5].textContent = "Dead";
  } else if (deskTab === "fomo") {
    cols[1].textContent = "Status";
    cols[2].textContent = "Mcap";
    cols[3].textContent = "Hunt";
    cols[4].textContent = "Desk";
    cols[5].textContent = "×";
  } else {
    cols[1].textContent = "Age";
    cols[2].textContent = "Mcap";
    cols[3].textContent = "×";
    cols[4].textContent = "Entry";
    cols[5].textContent = "Live";
  }
}

// Oversight desk: short list, learn loop, flood sensor, honest board.
// Hot/Bloom/Wide paper/Watch/Tickets/Wallets stay off the primary nav.
const TAB_LABELS = {
  picks: "Picks",
  learn: "Learn",
  live: "Hunt",
  fomo: "FOMO",
  board: "Calibrate",
};
const LEGACY_TAB_REDIRECT = {
  hot: "live",
  bloom: "live",
  watch: "live",
  paper: "picks",
  tickets: "picks",
  wallets: "learn",
};

function thesisTagsHtml(tags) {
  return (tags || []).map((t) => `<span class="v1-tag">${esc(t)}</span>`).join("");
}

function pickStateClass(status) {
  if (status === "open") return "open";
  if (status === "queued") return "held";
  if (status === "skipped") return "skipped";
  if (status === "closed") return "confirmed";
  return "held";
}

function ticketStateLabel(tk) {
  if (tk.status === "skipped") return "skipped";
  if (tk.status === "confirmed") return tk.fill_status === "closed" ? "closed" : "confirmed";
  if (tk.fill_status === "closed") return "closed";
  return "shadow";
}

function renderTape() {
  const items = sorted(filtered(activeItems()));
  const label = TAB_LABELS[deskTab] || "Hunt";
  if (deskTab === "picks") {
    deskMeta.textContent = `${label} · paperV1 · ${pickMeta.taken || 0}/${pickMeta.cap || 6} today (${pickMeta.cap_per_chain || 3}/chain)${pickMeta.locked ? " · locked" : " · open to 23:00 UTC"} · ${pickMeta.open || 0} open · ${pickMeta.queued || 0} queued · signal rank · paper only`;
  } else if (deskTab === "learn") {
    const stale = loopStale(healthState);
    deskMeta.textContent = `${label} · loop oversight · ${items.length} edges${stale.length ? ` · stale: ${stale.join(", ")}` : " · loops fresh"}${healthState.image_rev ? ` · ${healthState.image_rev}` : ""}`;
  } else if (deskTab === "wallets") {
    deskMeta.textContent = `${label} · ${items.length}${walletMeta.gold ? ` · ${walletMeta.gold} gold` : ""}${walletMeta.parked ? ` · ${walletMeta.parked} parked` : ""}`;
  } else if (deskTab === "paper") {
    deskMeta.textContent = `${label} · training ocean · ≥${linePct(chainLines().hi)} fills · half at 2× · ${paperMeta.open} open · ${paperMeta.closed} closed${paperMeta.vetoed ? ` · ${paperMeta.vetoed} vetoed` : ""}${paperMeta.pending ? ` · ${paperMeta.pending} held` : ""}${paperScoreAvgs(paperMeta)}`;
  } else if (deskTab === "tickets") {
    const shadow = ticketItems.filter((t) => t.status === "shadow").length;
    const confirmed = ticketItems.filter((t) => t.status === "confirmed").length;
    deskMeta.textContent = `${label} · ${items.length} · ${shadow} shadow · ${confirmed} confirmed · nothing executes`;
  } else if (deskTab === "board") {
    const cal = boardData.calibration;
    deskMeta.textContent = cal ? `${label} · ${cal.resolved ?? items.length} resolved · ${cal.open ?? 0} open · frozen Entry vs ${cal.horizon_hours || 24}h forward · backfill excluded` : `${label} · loading…`;
  } else if (deskTab === "live") {
    deskMeta.textContent = `${label} · flood sensor · ${items.length} · not the buy list`;
  } else if (deskTab === "fomo") {
    const fr = fomoFreshness(fomoTrendingData || {});
    deskMeta.textContent = `${label} · ${items.length} on board · ${fr.fresh ? "mirror fresh" : "STALE"} · cross-check only · paper only`;
  } else {
    deskMeta.textContent = `${label} · ${items.length}`;
  }
  renderTapeHead();
  const keepScroll = rowsEl.scrollTop;
  if (deskTab === "board") {
    renderBoardRows(items);
    rowsEl.scrollTop = keepScroll;
    return;
  }
  if (deskTab === "fomo") {
    rowsEl.innerHTML = items.map((row) => {
      const mult = row.multiple != null && Number(row.multiple) > 0 ? Number(row.multiple) : 0;
      const mcap = row.mcap_usd != null ? fmtMcap(row.mcap_usd) : (row.last_mcap ? fmtMcap(row.last_mcap) : "—");
      const sym = row.symbol || row.name || "?";
      return `<button type="button" class="tape-row${row.mint === selected ? " on" : ""}" data-fomo-mint="${esc(row.mint || "")}">
        <span class="tok"><span class="ph">${esc(String(sym).slice(0, 2).toUpperCase())}</span><span><b>${esc(sym)}</b><small>#${row.rank != null ? esc(String(row.rank)) : "—"} · ${fomoMintShort(row.mint)}</small></span></span>
        <span class="state ${fomoStatusClass(row.status)}">${esc(row.bucket || row.status || "?")}</span>
        <span class="num">${esc(mcap)}</span>
        <span class="num">${row.on_hunt ? "✓" : "—"}</span>
        <span class="num">${row.on_desk ? "✓" : "—"}</span>
        <span class="num ${multipleClass(mult)}">${mult ? mult.toFixed(1) + "×" : "—"}</span>
      </button>`;
    }).join("") || `<p class="tape-note">No FOMO trending snapshot yet. The worker captures hourly — check back after the next audit.</p>`;
    rowsEl.querySelectorAll("[data-fomo-mint]").forEach((btn) => {
      btn.onclick = () => {
        selected = btn.dataset.fomoMint;
        renderTape();
        renderFomoPane();
        renderFomoDetail(selected);
      };
    });
    rowsEl.scrollTop = keepScroll;
    return;
  }
  if (deskTab === "learn") {
    rowsEl.innerHTML = items.map((row) => {
      const ok = row.ok === true ? "ok" : row.ok === false ? "bad" : "warn";
      return `<button type="button" class="tape-row${row.mint === selected ? " on" : ""}" data-learn="${esc(row.mint)}">
        <span class="tok"><span class="ph">${esc(row.glyph || "?")}</span><span><b>${esc(row.symbol)}</b><small>${esc(row.name || "")}</small></span></span>
        <span class="state ${ok === "ok" ? "open" : ok === "bad" ? "skipped" : "held"}">${esc(row.pulse || "—")}</span>
        <span class="num">${esc(row.age || "—")}</span>
        <span class="num">${row.n != null ? row.n : "—"}</span>
        <span class="num">${esc(row.signal || "—")}</span>
        <span class="sc-pill ${ok === "ok" ? "high" : ok === "bad" ? "veto" : "mid"}">${ok === "ok" ? "ok" : ok === "bad" ? "!" : "?"}</span>
      </button>`;
    }).join("") || `<p class="tape-note">Loading the learning loop…</p>`;
    rowsEl.querySelectorAll("[data-learn]").forEach((btn) => {
      btn.onclick = () => {
        selected = btn.dataset.learn;
        renderTape();
        renderLearnDetail(btn.dataset.learn);
      };
    });
    rowsEl.scrollTop = keepScroll;
    return;
  }
  if (deskTab === "picks") {
    rowsEl.innerHTML = items.map((t) => {
      const x = Number(t.multiple || 0);
      const pnl = t.return_pct != null ? Number(t.return_pct) : (t.status === "open" || t.status === "queued" ? paperPnl(t) : null);
      const initials = String(tapeTicker(t) || "?").slice(0, 2).toUpperCase();
      const img = t.image_url
        ? `<img src="${esc(t.image_url)}" alt="" onerror="this.style.visibility='hidden'" />`
        : `<span class="ph">${esc(initials)}</span>`;
      const state = t.status || "—";
      const thesis = t.thesis_score != null ? Number(t.thesis_score).toFixed(2) : "—";
      const tags = thesisTagsHtml(t.tags);
      const entry = Number(t.entry_p || 0) * 100;
      const sub = `${tags || `<span class="muted">no thesis tags</span>`} · ${esc(tapeSub(t) || t.exit_reason || "")}`;
      return `<button type="button" class="tape-row${t.mint === selected ? " on" : ""}" data-mint="${esc(t.mint)}">
        <span class="tok">${img}<span><b>${esc(tapeTicker(t))}</b><small>${sub}</small></span></span>
        <span class="state ${pickStateClass(state)}">${esc(state)}</span>
        <span class="num" title="Frozen thesis rank">${thesis}</span>
        <span class="num ${multipleClass(x)}">${x ? x.toFixed(2) + "×" : "—"}</span>
        <span class="sc-pill ${scoreClass(entry)}">${entry ? entry.toFixed(0) : "—"}</span>
        <span class="sc-pill ${pnl != null && pnl >= 0 ? "high" : pnl != null ? "low" : "none"}">${pnl != null ? fmtPnl(pnl) : "—"}</span>
      </button>`;
    }).join("") || `<p class="tape-note">No paperV1 picks today yet. Qualify: Sol buy line + Live ≥ 50, RH Entry ≥ 40. Soft thesis needs hard tags (GitHub / dev / CTO) — meme alone does not pass. Rank by cohort signal, then Live/Entry. Cap ${pickMeta.cap_per_chain || 3}/chain/day (${pickMeta.cap || 6} total). Locks 23:00 UTC. <b>Paper only — nothing buys.</b></p>`;
    rowsEl.querySelectorAll("[data-mint]").forEach((btn) => {
      btn.onclick = () => openDetail(btn.dataset.mint);
    });
    rowsEl.scrollTop = keepScroll;
    return;
  }
  if (deskTab === "tickets") {
    rowsEl.innerHTML = items.map((tk) => {
      const x = Number(tk.multiple || 0);
      const entry = Number(tk.entry_p || 0) * 100;
      const state = ticketStateLabel(tk);
      const initials = String(tk.symbol || "?").slice(0, 2).toUpperCase();
      const pnl = tk.return_pct != null ? Number(tk.return_pct) : (x > 0 ? (x - 1) * 100 : null);
      return `<button type="button" class="tape-row${tk.mint === selected ? " on" : ""}" data-mint="${esc(tk.mint)}" data-ticket="${tk.id}">
        <span class="tok"><span class="ph">${esc(initials)}</span><span><b>${esc(tk.symbol || "—")}</b><small>${esc(fmtAge(tk.created_at ? (Date.now() - new Date(tk.created_at).getTime()) / 60000 : null))} ago · ${fmtMcap(tk.entry_mcap)} · ${esc((tk.flags || [])[0] || (tk.reasons || [])[0] || "")}</small></span></span>
        <span class="state ${state}">${state}</span>
        <span class="num">${tk.size_usd ? "$" + Math.round(tk.size_usd) : "—"}</span>
        <span class="num ${multipleClass(x)}">${x ? x.toFixed(2) + "×" : "—"}</span>
        <span class="sc-pill ${scoreClass(entry)}">${entry ? entry.toFixed(0) : "—"}</span>
        <span class="sc-pill ${pnl != null && pnl >= 0 ? "high" : "low"}">${fmtPnl(pnl)}</span>
      </button>`;
    }).join("") || `<p class="tape-note">No shadow tickets yet. A ticket is written when a this-window card above the ${linePct(chainLines().hi)} line (${lineName(chainLines(), "hi")}) clears the gate: size as a slice of the pool, slippage from the book, stop / take / ride levels and the reasons. <b>Nothing executes</b> — confirm or skip here records the desk's answer for the forward P&amp;L report.</p>`;
    rowsEl.querySelectorAll("[data-mint]").forEach((btn) => {
      btn.onclick = () => openDetail(btn.dataset.mint);
    });
    rowsEl.scrollTop = keepScroll;
    return;
  }
  if (deskTab === "wallets") {
    rowsEl.innerHTML = items.map((w) => {
      const owner = w.owner || w.wallet || "";
      const score = Number(w.score || 0);
      const early = `${w.n_sized || 0}/${w.n_early || 0}`;
      const wins = Number(w.n_wins || w.n_fomo_wins || 0);
      const x = Number(w.best_multiple || 0);
      const short = owner.length > 10 ? `${owner.slice(0, 4)}…${owner.slice(-4)}` : owner;
      return `<button type="button" class="tape-row${owner === selected ? " on" : ""}" data-wallet="${esc(owner)}">
        <span class="tok"><span class="ph">${esc(String(w.grade || "?").slice(0, 1))}</span><span><b>${esc(short || "—")}</b><small>${w.gold ? "gold · " : ""}${w.parked ? "parked · " : ""}${esc(w.grade || "—")} · ${w.n_sized || 0} sized</small></span></span>
        <span class="num">${early}</span>
        <span class="num">${wins}${w.n_rugs ? `/−${w.n_rugs}` : ""}</span>
        <span class="num">${esc(w.best_symbol || "—")}</span>
        <span class="num ${multipleClass(x)}">${x ? x.toFixed(1) + "×" : "—"}</span>
        <span class="sc-pill ${scoreClass(score)}">${score ? score.toFixed(0) : "—"}</span>
      </button>`;
    }).join("") || `<p class="muted" style="padding:1rem">No stored Early or FOMO maps yet. First-hour buyers and repeat wallets on confirmed 5× books — stored maps only, not a watchlist.</p>`;
    rowsEl.querySelectorAll("[data-wallet]").forEach((btn) => {
      btn.onclick = () => openWallet(btn.dataset.wallet);
    });
    rowsEl.scrollTop = keepScroll;
    return;
  }
  if (deskTab === "paper") {
    rowsEl.innerHTML = items.map((t) => {
      const x = Number(t.multiple || 0);
      const pnl = paperPnl(t);
      const initials = String(tapeTicker(t) || "?").slice(0, 2).toUpperCase();
      const img = t.image_url
        ? `<img src="${esc(t.image_url)}" alt="" onerror="this.style.visibility='hidden'" />`
        : `<span class="ph">${esc(initials)}</span>`;
      const state = t.paper_veto ? "veto" : t.paper_wait ? "held" : t.paper_open ? "open" : "out";
      const sub = t.paper_veto ? esc(t.veto || "vetoed") : `${state === "open" ? "holding" : state === "held" ? "held" : "closed"} · ${esc(tapeSub(t))}`;
      return `<button type="button" class="tape-row${t.mint === selected ? " on" : ""}" data-mint="${esc(t.mint)}">
        <span class="tok">${img}<span><b>${esc(tapeTicker(t))}</b><small>${sub}</small></span></span>
        <span class="state ${state}">${state}</span>
        <span class="num">${fmtMcap(t.entry_mcap || t.t0_mcap)}</span>
        <span class="num ${multipleClass(x)}">${t.paper_veto ? "—" : x ? x.toFixed(2) + "×" : "—"}</span>
        ${entryCell(t, false)}
        ${t.paper_veto ? `<span class="sc-pill veto" title="${esc(t.veto || "")}">veto</span>` : `<span class="sc-pill ${pnl != null && pnl >= 0 ? "high" : "low"}">${fmtPnl(pnl)}</span>`}
      </button>`;
    }).join("") || `<p class="tape-note">No this-window fills above the ${linePct(chainLines().hi)} line yet. The next graduation at Entry ≥ ${linePct(chainLines().hi)} (${lineName(chainLines(), "hi")}) fills now if the book is liquid and not vetoed, then half comes off at 2×. Fills are written once to the ledger and never re-derived. <b>Simulated</b> — no live orders.</p>`;
    rowsEl.querySelectorAll("[data-mint]").forEach((btn) => {
      btn.onclick = () => openDetail(btn.dataset.mint);
    });
    rowsEl.scrollTop = keepScroll;
    return;
  }
  rowsEl.innerHTML = items.map((t) => {
    const x = liveMultiple(t);
    const live = liveConviction(t);
    const initials = String(tapeTicker(t) || "?").slice(0, 2).toUpperCase();
    const img = t.image_url
      ? `<img src="${esc(t.image_url)}" alt="" onerror="this.style.visibility='hidden'" />`
      : `<span class="ph">${esc(initials)}</span>`;
    const mid = t.watch_only
      ? (() => {
        const prog = Number(t.progress || 0);
        const pct = prog ? Math.round(prog <= 1 ? prog * 100 : prog) : 0;
        return `<span class="num">${pct ? pct + "%" : "—"}</span>
      <span class="sc-pill">—</span>
      <span class="sc-pill ${scoreClass(live)}" title="Watch preview — not entry">${live ? live.toFixed(0) : "—"}</span>`;
      })()
      : `<span class="num ${multipleClass(x)}">${x ? x.toFixed(2) + "×" : "—"}</span>
      ${entryCell(t, true)}
      ${livePill(t, live)}`;
    const dev = xHandle(t.dev_handle);
    return `<button type="button" class="tape-row${t.mint === selected ? " on" : ""}${t.has_dev_x || dev ? " has-dev-x" : ""}" data-mint="${esc(t.mint)}">
      <span class="tok">${img}<span><b class="${t.has_dev_x || dev ? "has-dev-x" : ""}">${esc(tapeTicker(t))}</b><small>${esc(tapeSub(t))}${dev ? ` · <span class="dev-x-handle">@${esc(dev)}</span>` : ""}${leftoverChip(t)}</small></span></span>
      ${huntAgeChip(t)}
      <span class="num">${fmtMcap(liveMcap(t))}</span>
      ${mid}
    </button>`;
  }).join("") || `<p class="tape-note">${emptyTapeNote()}</p>`;
  rowsEl.querySelectorAll("[data-mint]").forEach((btn) => {
    btn.onclick = () => openDetail(btn.dataset.mint);
    btn.onpointerenter = () => {
      const row = tapeRow(btn.dataset.mint) || activeItems().find((item) => item.mint === btn.dataset.mint);
      if (row) prefetchPair(row);
    };
  });
  rowsEl.scrollTop = keepScroll;
}

function liveCapped(t) {
  const cap = Number(t?.live_cap || 0);
  if (!cap) return false;
  const live = liveConviction(t) / 100;
  return live >= cap - 0.005;
}

function livePill(t, live) {
  if (live == null || Number.isNaN(Number(live))) {
    return `<span class="sc-pill none" title="No live print yet. Live lands on the first Dex last after migrate.">—</span>`;
  }
  const n = Number(live);
  if (liveCapped(t)) {
    const cap = Math.round(Number(t.live_cap) * 100);
    return `<span class="sc-pill ${scoreClass(n)} capped" title="Capped at ${cap}: thin-watch book (Entry under ${linePct(cardLines(t).thin)} or a thin Watch preview). The tape can be up but the cap holds until holders and liquidity confirm.">${n.toFixed(0)}</span>`;
  }
  const dumped = dumpedOffAth(t);
  const title = n === 0
    ? "Live 0 — collapsed, dead pool, or dumped tape. A missing print is —, not 0."
    : dumped ? "Dumped off ATH — Live reads the token card, not the tape" : "Live conviction from the current tape · refreshes every minute";
  return `<span class="sc-pill ${scoreClass(n)}${n === 0 ? " none" : ""}" title="${title}">${n.toFixed(0)}</span>`;
}

function emptyTapeNote() {
  if (deskTab === "picks") return "No paperV1 picks today. The short list stays empty until a name clears the qualify floor (score/Live or hard-tag thesis).";
  if (deskTab === "learn") return "Learning loop edges will appear once health and scorecard load.";
  if (deskTab === "fomo") return "FOMO trending board empty or still loading from /api/fomo-trending.";
  if (deskTab === "hot") return "Nothing is doing well right now. A card lands here at a liquidity-confirmed 2× that is still holding.";
  if (deskTab === "bloom") return "No late bloomers. Bloom lists a live tape that looks better than its frozen Entry.";
  if (deskTab === "watch") return "No curves near graduation on this scan.";
  if (liveOnly.checked) return "No names in this window. Untick <b>This window</b> to see the last 7 days.";
  return "No names on this tape.";
}

// ---------------------------------------------------------------------------
// Picks + Learn: short list and learning-loop oversight
// ---------------------------------------------------------------------------

function mapPickRow(row) {
  const thesis = row.thesis || {};
  const tags = row.tags || thesis.tags || [];
  return {
    ...row,
    name: row.name || row.symbol || "",
    p_good: row.entry_p,
    score: Number(row.entry_p || 0) * 100,
    t0_mcap: row.entry_mcap,
    last_mcap: row.last_mcap || row.entry_mcap,
    thesis_score: row.thesis_score != null ? row.thesis_score : thesis.score,
    tags,
    thesis_detail: thesis,
    paper_only: true,
    paper_open: row.status === "open",
    paper_wait: row.status === "queued",
    first_seen_at: row.opened_at,
    created_at: row.opened_at,
  };
}

function applyPickMeta(book) {
  const summary = book.summary || {};
  const today = summary.today || {};
  pickMeta = {
    day: book.day || today.day || "",
    cap: book.cap || summary.cap || 6,
    cap_per_chain: book.cap_per_chain || summary.cap_per_chain || 3,
    taken: today.taken || 0,
    taken_chain: today.taken_chain != null ? today.taken_chain : (today.taken || 0),
    locked: Boolean(today.locked),
    queued: summary.queued || today.queued || 0,
    open: summary.open || 0,
    closed: summary.closed || 0,
    skipped: summary.skipped || 0,
    wins: summary.wins || 0,
    avg: summary.avg_return_pct,
    rule: summary.rule || "",
    summary,
  };
}

function loopAgeLabel(info) {
  if (!info) return "—";
  const age = info.age_s != null ? Number(info.age_s) : (info.at ? (Date.now() - new Date(info.at).getTime()) / 1000 : null);
  if (age == null || Number.isNaN(age)) return "—";
  if (age < 90) return `${Math.round(age)}s`;
  if (age < 3600) return `${Math.round(age / 60)}m`;
  return `${(age / 3600).toFixed(1)}h`;
}

function buildLearnRows() {
  const loops = healthState.loops || {};
  const hunt = loops.hunt_tape || loops.hunt || null;
  const paper = loops.paper || loops.paper_sync || null;
  const fit = loops.batch_fit || loops.fit || null;
  const liveFit = loops.live_fit || null;
  const stale = new Set(loopStale(healthState));
  const v1 = pickMeta.summary || {};
  const today = v1.today || {};
  const psc = (boardData.scorecard || {}).scorecard || {};
  const tw = psc.this_window || {};
  const fs = boardData.firstSight;
  const fsArt = fs && ((fs.artifacts || []).find((a) => a.promoted) || (fs.artifacts || [])[0]);
  const live = boardData.live;
  const liveArt = live && ((live.artifacts || []).find((a) => a.promoted) || (live.artifacts || [])[0]);
  const cal = boardData.calibration;
  const gate = productionGateData || {};
  const gateBars = gate.bars || [];
  const gateGreen = gate.cleared != null ? gate.cleared : gateBars.filter((b) => b.color === "green").length;
  const rows = [
    {
      mint: "learn-gate",
      glyph: "PG",
      symbol: "Prod gate",
      name: (() => {
        if (!productionGateData) return "FORWARD production bars · paper only";
        return gate.ready ? "All six bars green · still paper" : `${gateGreen}/${gate.total || 6} bars green · paper only`;
      })(),
      pulse: productionGateData ? (gate.ready ? "ready" : "watch") : "?",
      age: healthState.image_rev || "—",
      n: gate.total || 6,
      signal: (() => {
        const risk = gateBars.find((b) => b.key === "risk");
        const thesis = gateBars.find((b) => b.key === "thesis_coverage");
        const cov = thesis && thesis.value != null ? Math.round(Number(thesis.value) * 100) + "%" : "—";
        return `thesis ${cov} · arm ${risk && risk.extra && risk.extra.armed ? "ON" : "off"}`;
      })(),
      ok: productionGateData ? gate.ready === true || gateGreen >= 1 : null,
      learn_only: true,
      thesis: "Operator trust: thesis, sample, hit2× vs wide-hi, IMAGE_REV, daily ritual, armed=false. No trading surface.",
    },
    {
      mint: "learn-ingest",
      glyph: "IN",
      symbol: "Ingest",
      name: "Hunt flood → sensor / training ocean",
      pulse: hunt ? "live" : "?",
      age: loopAgeLabel(hunt),
      n: lastItems.length || null,
      signal: heliusCapped(healthState) ? "helius capped" : (healthState.bitquery ? "bitquery" : "tape"),
      ok: hunt ? !stale.has("hunt tape") && !stale.has("hunt_tape") : null,
      learn_only: true,
      thesis: "Worker Hunt tape must keep heartbeating. This is the flood sensor — not the buy list.",
    },
    {
      mint: "learn-freeze",
      glyph: "FZ",
      symbol: "Freeze",
      name: "Entry + thesis features locked at first sight",
      pulse: honest.counts ? "ledger" : "?",
      age: "—",
      n: honest.counts?.decisions ?? null,
      signal: healthState.github ? "github on" : "github off",
      ok: Boolean(honest.counts?.decisions),
      learn_only: true,
      thesis: "Decisions freeze Entry and thesis columns (GitHub / real project / CTO / name). Never re-score from mutable Research.",
    },
    {
      mint: "learn-shortlist",
      glyph: "V1",
      symbol: "Short list",
      name: (() => {
        const d = dayDeltaData || {};
        const t = d.today || {};
        const cov = t.thesis_coverage != null ? Math.round(Number(t.thesis_coverage) * 100) + "% hard-tag on opens" : null;
        const base = `paperV1 · ${today.taken || 0}/${pickMeta.cap || 6} today (${pickMeta.cap_per_chain || 3}/chain)`;
        return cov ? `${base} · ${cov}` : base;
      })(),
      pulse: today.locked ? "locked" : "open",
      age: pickMeta.day || today.day || "—",
      n: today.taken || 0,
      signal: (() => {
        const d = dayDeltaData || {};
        const delta = d.delta || {};
        const hit = delta.hit2x_rate != null ? `Δhit2 ${(Number(delta.hit2x_rate) * 100).toFixed(0)}pp` : `${pickMeta.open || 0} open · ${pickMeta.queued || 0} q`;
        return hit;
      })(),
      ok: true,
      learn_only: true,
      thesis: pickMeta.rule || "3/chain/day. Signal rank, hard-tag thesis soft path. Paper only.",
    },
    {
      mint: "learn-ritual",
      glyph: "DY",
      symbol: "Daily ritual",
      name: (() => {
        const d = dayDeltaData || {};
        const t = d.today || {};
        if (!dayDeltaData) return "Yesterday → today short-list delta";
        const cov = t.thesis_coverage != null ? Math.round(Number(t.thesis_coverage) * 100) + "%" : "—";
        return `hard-tag opens ${cov} · closed ${t.closed_n ?? 0} · hit2 ${t.hit2x_n ?? 0}`;
      })(),
      pulse: dayDeltaData ? "daily" : "?",
      age: (dayDeltaData && dayDeltaData.today && dayDeltaData.today.day) || "—",
      n: (dayDeltaData && dayDeltaData.today) ? dayDeltaData.today.picked_n : null,
      signal: (dayDeltaData && dayDeltaData.rank_policy) || "—",
      ok: dayDeltaData ? ((dayDeltaData.today && dayDeltaData.today.silence) ? false : true) : null,
      learn_only: true,
      thesis: "Operator ritual: thesis coverage, skip reasons, hit2× vs yesterday, rank policy. Paper only.",
    },
    {
      mint: "learn-fomo",
      glyph: "FO",
      symbol: "FOMO trend",
      name: (() => {
        const san = (fomoTrendingData && fomoTrendingData.sanity) || {};
        const board = (fomoTrendingData && fomoTrendingData.counts && fomoTrendingData.counts.board) || (fomoTrendingData && fomoTrendingData.items && fomoTrendingData.items.length) || 0;
        const seen = san.seen_rate != null ? Math.round(Number(san.seen_rate) * 100) + "%" : "—";
        const stale = fomoTrendingData.board_stale || san.board_stale;
        const ageH = fomoTrendingData.capture_age_hours ?? san.capture_age_hours;
        const staleBit = stale ? ` · STALE mirror ${ageH != null ? ageH + "h" : "?"}` : "";
        return `FOMO trending · ${board} board · seen ${seen}${staleBit}`;
      })(),
      pulse: fomoTrendingData ? "hourly" : "?",
      age: (fomoTrendingData && fomoTrendingData.last_audit && fomoTrendingData.last_audit.at) ? String(fomoTrendingData.last_audit.at).slice(11, 16) + "Z" : "—",
      n: (fomoTrendingData && fomoTrendingData.sanity && fomoTrendingData.sanity.miss_n) ?? null,
      signal: (() => {
        const san = (fomoTrendingData && fomoTrendingData.sanity) || {};
        if (fomoTrendingData.board_stale || san.board_stale) {
          const ageH = fomoTrendingData.capture_age_hours ?? san.capture_age_hours;
          return `mirror stale ${ageH != null ? ageH + "h" : "?"}`;
        }
        return `hijack ${san.veto_hijack_n ?? 0} · miss ${san.miss_n ?? 0}`;
      })(),
      ok: fomoTrendingData ? (
        !(fomoTrendingData.board_stale || (fomoTrendingData.sanity && fomoTrendingData.sanity.board_stale)) &&
        ((fomoTrendingData.sanity && fomoTrendingData.sanity.miss_n) || 0) === 0
      ) : null,
      learn_only: true,
      thesis: "FOMO Tokens→Trending mirror must be fresh (≤15m capture). Stale API ≠ app board — fail loud, not green.",
    },
    {
      mint: "learn-fomo-traders",
      glyph: "FT",
      symbol: "FOMO traders",
      name: (() => {
        const card = fomoTradersData || {};
        const top = (card.traders || [])[0];
        const n = (card.traders || []).length;
        const min = card.min_buys != null ? card.min_buys : 5;
        if (!fomoTradersData) return `/ws/alerts trader scorecard · Learn only`;
        if (!top) return `No ranked traders yet (need ≥${min} buys)`;
        return `${n} ranked · #1 ${top.trader || top.user_id || "?"} · score ${top.rank_score}`;
      })(),
      pulse: fomoTradersData ? "flow" : "?",
      age: (fomoTradersData && fomoTradersData.since) ? String(fomoTradersData.since).slice(0, 10) : "—",
      n: (fomoTradersData && fomoTradersData.traders) ? fomoTradersData.traders.length : null,
      signal: (() => {
        const top = (fomoAlertsFlowData && fomoAlertsFlowData.top_traders) || [];
        if (top.length) {
          const t = top[0];
          return `flow #1 ${t.trader || t.user_id || "?"}`;
        }
        const row = (fomoTradersData && fomoTradersData.traders && fomoTradersData.traders[0]) || null;
        return row ? `lead ${Math.round(Number(row.lead_rate || 0) * 100)}%` : "—";
      })(),
      ok: fomoTradersData ? ((fomoTradersData.traders || []).length > 0) : null,
      learn_only: true,
      thesis: "Keyed FOMO /ws/alerts buys ranked by lead quality. Paper-only Learn — never arms or opens fills.",
    },
    {
      mint: "learn-sanity",
      glyph: "SN",
      symbol: "Sanity loop",
      name: (() => {
        const s = sanityLoopData || {};
        const n = (s.improve || []).length;
        const fail = (s.checks || []).filter((c) => c.status === "fail").length;
        if (!sanityLoopData) return "Improve + reality checks";
        return s.hard_ok ? `hard ok · ${n} next` : `hard fail×${fail} · gate experiments`;
      })(),
      pulse: sanityLoopData ? "hourly" : "?",
      age: (sanityLoopData && sanityLoopData.at) ? String(sanityLoopData.at).slice(11, 16) + "Z" : "—",
      n: (sanityLoopData && sanityLoopData.improve) ? sanityLoopData.improve.length : null,
      signal: (() => {
        const s = sanityLoopData || {};
        const act = ((s.improve || [])[0] || {}).action || "—";
        return String(act).replace(/_/g, " ").slice(0, 22);
      })(),
      ok: sanityLoopData ? Boolean(sanityLoopData.hard_ok) : null,
      learn_only: true,
      thesis: "Continuous improve gated by FOMO door + veto-retro + paper EV. Never auto-buys a hard veto.",
    },
    {
      mint: "learn-veto",
      glyph: "VT",
      symbol: "Veto health",
      name: (() => {
        const v = vetoRetroData || {};
        const hj = (v.needles || []).find((n) => n.veto === "hijack");
        if (!hj) return "Historical hard/soft veto scorecard";
        const held = hj.peak5_held_sellable_rate != null ? Math.round(Number(hj.peak5_held_sellable_rate) * 100) + "% held" : "—";
        return `hijack peak5 dust ${hj.peak5_then_dust ?? "—"}/${hj.hit5_t0 ?? "—"} · ${held}`;
      })(),
      pulse: vetoRetroData ? "ledger" : "?",
      age: (vetoRetroData && vetoRetroData.at) ? String(vetoRetroData.at).slice(11, 16) + "Z" : "—",
      n: (vetoRetroData && vetoRetroData.n_gate_veto) ?? null,
      signal: (() => {
        const ver = (vetoRetroData && vetoRetroData.verdict) || {};
        return ver.hijack_keep ? "hijack keep" : (ver.watch && ver.watch.length ? "watch" : "—");
      })(),
      ok: vetoRetroData ? Boolean((vetoRetroData.verdict || {}).hijack_keep) : null,
      learn_only: true,
      thesis: "Full-history gate veto health. Keep hard refuses when peaks dust; soft/late stay shadow-only. Not a buy list.",
    },
    {
      mint: "learn-early-diff",
      glyph: "ED",
      symbol: "Early diff",
      name: (() => {
        const e = earlyDiffData || {};
        const proof = e.cohort_proof || {};
        if (!earlyDiffData) return "SI feature-divergence closer-look";
        const sep = proof.separation_ok ? "cohort sep ok" : "cohort thin";
        return `top ${e.top_n ?? 0}/${e.n_scanned ?? 0} · ${sep}`;
      })(),
      pulse: earlyDiffData ? "shadow" : "?",
      age: (earlyDiffData && earlyDiffData.at) ? String(earlyDiffData.at).slice(11, 16) + "Z" : "—",
      n: (earlyDiffData && earlyDiffData.top_n) ?? null,
      signal: (() => {
        const top = ((earlyDiffData || {}).items || [])[0];
        return top ? `${top.symbol || "?"} ${(Number(top.divergence_score) || 0).toFixed(2)}` : "—";
      })(),
      ok: earlyDiffData ? Boolean((earlyDiffData.cohort_proof || {}).separation_ok) : null,
      learn_only: true,
      thesis: "SI cohort divergence ranker. Rising book + top10 band + vol-with-direction. Serial = prior never block. Not a buy list.",
    },
    {
      mint: "learn-outcomes",
      glyph: "OUT",
      symbol: "Outcomes",
      name: "Honest sellable prints → paper scorecard",
      pulse: paper ? "sync" : (tw.n ? "scored" : "?"),
      age: loopAgeLabel(paper),
      n: tw.n ?? pickMeta.closed ?? null,
      signal: tw.avg_return_pct != null ? `tw ${tw.avg_return_pct}%` : (pickMeta.avg != null ? `v1 ${pickMeta.avg}%` : "—"),
      ok: paper ? !stale.has("paper") && !stale.has("paper_sync") : (tw.n != null ? true : null),
      learn_only: true,
      thesis: "This-window vs leftover-clock vs no-run. Short-list P&L is the promotion bar.",
    },
    {
      mint: "learn-fit",
      glyph: "FIT",
      symbol: "Model fit",
      name: "First-sight / live / batch promotion",
      pulse: fit || liveFit ? "hourly" : "?",
      age: loopAgeLabel(fit || liveFit),
      n: fsArt?.n_valid ?? liveArt?.n_valid ?? cal?.resolved ?? null,
      signal: fsArt ? `fs v${fsArt.version}${fsArt.promoted ? " ·" : ""}` : (liveArt ? `live v${liveArt.version}` : "no fit"),
      ok: Boolean(fsArt?.promoted || liveArt?.promoted || fsArt || liveArt),
      learn_only: true,
      thesis: "Fits train on frozen ledger rows. Promote only when validation beats the incumbent.",
    },
  ];
  learnRows = rows;
  return rows;
}

function gateValueText(bar) {
  if (!bar) return "—";
  if (bar.key === "thesis_coverage" && bar.value != null) return Math.round(Number(bar.value) * 100) + "%";
  if (bar.key === "sample") return String(bar.value ?? 0);
  if (bar.key === "hit_quality" && bar.value != null) return Math.round(Number(bar.value) * 100) + "%";
  if (bar.value == null || bar.value === "") return "—";
  return String(bar.value);
}

function renderGateCard(gate) {
  if (!gate || !(gate.bars || []).length) {
    return `<h4>Production gate</h4>
      <p class="board-sub">Loading FORWARD bars from existing paper / risk / health APIs…</p>
      <div class="gate-card"><p class="board-sub">Paper only · armed stays false · no trading surface.</p></div>`;
  }
  const bars = gate.bars || [];
  const rows = bars.map((b) => {
    const pct = b.progress != null ? Math.round(Number(b.progress) * 100) : (b.color === "green" ? 100 : b.color === "amber" ? 50 : 18);
    return `<div class="gate-bar ${esc(b.color || "amber")}" title="${esc(b.detail || b.target || "")}">
      <span class="gate-label">${esc(b.label || b.key)}</span>
      <span class="gate-track"><span class="gate-fill" style="width:${pct}%"></span></span>
      <span class="gate-value">${esc(gateValueText(b))}</span>
    </div>`;
  }).join("");
  const headline = gate.ready
    ? "All six bars green — still paper until Dave arms."
    : (gate.summary || `${gate.cleared || 0}/${gate.total || 6} bars green · paper only.`);
  return `<h4>Production gate · ${gate.cleared || 0}/${gate.total || 6}</h4>
    <p class="board-sub">${esc(headline)}</p>
    <div class="gate-card">
      <div class="gate-bars">${rows}</div>
      <p class="board-sub">${esc(gate.note || "Read-only. No arm flip.")}</p>
    </div>`;
}

function reviewRowHtml(row) {
  const tags = thesisTagsHtml(row.tags);
  const peak = row.peak_multiple != null ? `${Number(row.peak_multiple).toFixed(2)}× peak` : "";
  const ret = row.return_pct != null ? fmtPnl(row.return_pct) : "";
  const skip = row.skip_reason || (row.shadow ? "near-miss" : "");
  const status = skip || row.status || "";
  return `<button type="button" class="v1-row" data-mint="${esc(row.mint || "")}" title="${esc(row.why || skip || "")}">
    <b>${esc(row.symbol || row.mint || "?")}</b>
    <span class="muted">${esc(status)}</span>
    <span class="muted">${esc(row.why || "")}</span>
    ${tags}
    ${peak ? `<span class="muted">${esc(peak)}</span>` : ""}
    ${ret ? `<span class="${Number(row.return_pct) >= 0 ? "up" : "down"}">${esc(ret)}</span>` : ""}
  </button>`;
}

function fomoTraderDeskRowHtml(row, opts = {}) {
  const ranked = row.ranked !== false && row.rank != null;
  const hit = row.hit2x_rate != null ? Math.round(Number(row.hit2x_rate) * 100) + "%" : "—";
  const dump = row.dump_rate != null ? Math.round(Number(row.dump_rate) * 100) + "%" : "—";
  const overlap = row.desk_overlap || {};
  const hunt = overlap.on_hunt != null ? Math.round(Number(overlap.on_hunt) * 100) + "%" : "—";
  const paper = overlap.on_paper_v1 != null ? Math.round(Number(overlap.on_paper_v1) * 100) + "%" : "—";
  const skip = overlap.paper_v1_skipped != null ? Math.round(Number(overlap.paper_v1_skipped) * 100) + "%" : "—";
  const leadPct = row.lead_rate != null ? Math.round(Number(row.lead_rate) * 100) + "%" : "—";
  const score = row.rank_score != null ? Number(row.rank_score).toFixed(2) : "—";
  const rank = ranked ? `#${row.rank}` : "—";
  const who = row.trader || row.user_id || row.trader_key || "?";
  const detailId = row.user_id ? `uid:${row.user_id}` : (row.trader_key || who);
  const cls = opts.compact ? "v1-row compact" : "v1-row";
  return `<button type="button" class="${cls}" data-fomo-trader="${esc(detailId)}" title="${esc(row.trader_key || "")}">
    <b>${esc(rank)} ${esc(who)}</b>
    <span class="muted">buys ${row.n_buys ?? 0}</span>
    <span class="muted">lead ${row.lead_early ?? 0} (${esc(leadPct)})</span>
    <span class="muted">2× ${esc(hit)}</span>
    <span class="muted">dump ${esc(dump)}</span>
    <span class="muted">H ${esc(hunt)} · P ${esc(paper)} · skip ${esc(skip)}</span>
    <span class="muted">clust ${row.cluster_penalty != null ? Number(row.cluster_penalty).toFixed(2) : "—"}</span>
    <span class="${Number(row.rank_score) >= 0 ? "up" : ""}">${esc(score)}</span>
  </button>`;
}

function fomoTradersScorecardHtml(limit = 8) {
  const card = fomoTradersData;
  if (!card) return `<p class="muted">Loading FOMO trader scorecard…</p>`;
  const traders = card.traders || [];
  const min = card.min_buys != null ? card.min_buys : 5;
  const below = card.below_min_buys || [];
  if (!traders.length && !below.length) {
    return `<p class="board-sub">No qualifying buys in window yet. Needs buys ≥ $${esc(String(card.min_usd || "—"))} on /ws/alerts.</p>`;
  }
  let html = `<p class="board-sub">Ranked when n_buys ≥ ${min}. Learn only — paper_only, arm off, no FOMO→buys.</p>`;
  if (traders.length) {
    html += `<div class="v1-list">${traders.slice(0, limit).map((r) => fomoTraderDeskRowHtml(r, { compact: true })).join("")}</div>`;
  } else {
    html += `<p class="board-sub">No traders meet the ${min}-buy floor yet. ${below.length} below threshold — open <b>FOMO traders</b> in the loop list.</p>`;
  }
  const flowTop = (fomoAlertsFlowData && fomoAlertsFlowData.top_traders) || [];
  if (flowTop.length) {
    html += `<p class="board-sub">Today flow top · ${flowTop.slice(0, 3).map((t) => esc(`${t.trader || t.user_id || "?"} (${t.rank_score})`)).join(" · ")}</p>`;
  }
  return html;
}

function bindFomoTraderRows(root) {
  if (!root) return;
  root.querySelectorAll("[data-fomo-trader]").forEach((btn) => {
    btn.onclick = () => {
      const id = btn.dataset.fomoTrader;
      if (!id) return;
      selected = "learn-fomo-traders";
      renderLearnDetail("learn-fomo-traders", id);
      renderTape();
    };
  });
}

function renderLearnPane() {
  if (!learnPane) return;
  const chainLabel = CHAIN === "robinhood" ? "Robinhood" : "Solana";
  chartHead.innerHTML = `<div class="picked"><span class="ph">L</span><div>
    <h2>Learn loop <small class="muted">${esc(chainLabel)}</small></h2>
    <div class="meta"><span>oversight</span><span>1–5 picks / day</span><span>paper only</span><span>${esc(healthState.image_rev || "image?")}</span></div>
  </div></div>`;
  const v1 = pickMeta.summary || {};
  const today = v1.today || {};
  const metrics = reviewData.metrics || {};
  const psc = (boardData.scorecard || {}).scorecard || {};
  const tw = psc.this_window || {};
  const stale = loopStale(healthState);
  const loops = healthState.loops || {};
  const risk = (healthState.risk || reportData.risk || {});
  const riskLine = risk.kill_switch
    ? "kill switch ON"
    : risk.armed
      ? "LIVE ARMED"
      : "paper only · arm off";
  const byChain = reviewData.by_chain || today.by_chain || {};
  const per = reviewData.cap_per_chain || pickMeta.cap_per_chain || 3;
  const takenChain = today.taken_chain != null ? today.taken_chain : (reviewData.taken_chain != null ? reviewData.taken_chain : (today.taken || reviewData.taken || 0));
  const room = reviewData.room != null ? reviewData.room : Math.max(0, per - takenChain);
  let html = renderGateCard(productionGateData);
  html += `<h4>Daily short-list review · ${esc(reviewData.day || today.day || "today")}</h4>
    <p class="board-sub">${esc(v1.rule || pickMeta.rule || "Paper 3/chain/day. Hard-tag thesis soft path · signal rank.")}</p>
    <div class="model-card">
      <div><span>This chain</span><b>${takenChain}/${per}<small class="muted"> · room ${room} · total ${(today.taken || reviewData.taken || 0)}/${pickMeta.cap || 6} · ${today.locked || reviewData.locked ? "locked" : "open → 23:00 UTC"}</small></b></div>
      <div><span>Short-list 2×</span><b>${metrics.hit2x != null ? Math.round(Number(metrics.hit2x) * 100) + "%" : "—"}<small class="muted"> · ${metrics.hit2x_n || 0}/${metrics.closed || 0}</small></b></div>
      <div><span>Short-list 5×</span><b>${metrics.hit5x != null ? Math.round(Number(metrics.hit5x) * 100) + "%" : "—"}<small class="muted"> · ${metrics.hit5x_n || 0}</small></b></div>
      <div><span>Risk</span><b class="${risk.kill_switch || risk.armed ? "down" : "up"}">${esc(riskLine)}</b></div>
    </div>
    <div class="model-card">
      <div><span>Sol spent</span><b>${byChain.sol ?? 0}<small class="muted"> · q ${byChain.queued_sol ?? 0} · lane ${per}</small></b></div>
      <div><span>RH spent</span><b>${byChain.robinhood ?? 0}<small class="muted"> · q ${byChain.queued_robinhood ?? 0} · lane ${per}</small></b></div>
      <div><span>Cap</span><b>${per}+${per}/day<small class="muted"> · independent lanes</small></b></div>
      <div><span>Offline</span><b class="up">signal adjust</b></div>
    </div>`;
  if (reviewData.silence) {
    html += `<p class="board-sub down">${esc(reviewData.silence)}</p>`;
  }
  if (reportData.text) {
    html += `<p class="board-sub">${esc(reportData.text)}</p>`;
  }
  const ftEarly = fomoTrendingData;
  if (ftEarly && (ftEarly.items || ftEarly.counts)) {
    const san = ftEarly.sanity || {};
    const counts = ftEarly.counts || {};
    const buckets = ftEarly.by_bucket || {};
    const seenPct = san.seen_rate != null ? Math.round(Number(san.seen_rate) * 100) + "%" : "—";
    const mirrorStale = ftEarly.board_stale || san.board_stale;
    html += `<h4>FOMO trending · hourly sanity</h4>
      ${mirrorStale ? `<p class="board-sub down"><b>Mirror stale</b> — FOMO API capture ≈${esc(String(ftEarly.capture_age_hours ?? san.capture_age_hours ?? "?"))}h old. App Tokens→Trending may differ; misses are not door bugs until capture is fresh.</p>` : ""}
      <p class="board-sub">${esc(san.note || "Not a buy list. miss = door bug; veto_hijack = seen but filtered.")}</p>
      <div class="model-card">
        <div><span>Board</span><b>${counts.board ?? (ftEarly.items || []).length}<small class="muted"> · ${esc(ftEarly.sanity_board || ftEarly.source || "?")}</small></b></div>
        <div><span>Seen</span><b>${seenPct}<small class="muted"> · miss ${san.miss_n ?? counts.miss ?? 0}</small></b></div>
        <div><span>Hijack veto</span><b>${san.veto_hijack_n ?? buckets.veto_hijack ?? 0}</b></div>
        <div><span>Short list</span><b>${san.short_list_n ?? buckets.short_list ?? 0}</b></div>
      </div>`;
    const noHunt = ftEarly.fomo_trend_no_hunt || {};
    if (noHunt.n != null || noHunt.n_desk_no_hunt != null || (noHunt.autopsies || []).length) {
      const whyLine = Object.entries(noHunt.by_why || {}).map(([k, v]) => `${k}×${v}`).join(" · ") || "—";
      const nhSeps = (noHunt.separators || []).slice(0, 4).map((s) => {
        const d = Number(s.delta);
        const sign = d > 0 ? "+" : "";
        return `${s.feature} ${sign}${Number.isFinite(d) ? d.toFixed(2) : "?"}`;
      }).join(" · ");
      html += `<p class="board-sub"><b>FOMO trending · not on Hunt</b> — ${esc(noHunt.note || "on_desk/caught with on_hunt=false, plus door misses. Learn only.")} · desk ${noHunt.n_desk_no_hunt ?? 0} · door ${noHunt.n_door_miss ?? 0} · later ≥5× ${noHunt.n_ran ?? noHunt.n_runners ?? 0} · duds ${noHunt.n_duds ?? 0} · n ${noHunt.n_total ?? noHunt.n_joined ?? noHunt.n ?? 0} · open=false</p>
        <p class="board-sub muted">why: ${esc(whyLine)}</p>`;
      if (nhSeps) html += `<p class="board-sub">Separators · ${esc(nhSeps)}</p>`;
      const nhCards = (noHunt.autopsies || []).slice(0, 12);
      if (nhCards.length) {
        html += `<div class="v1-list">${nhCards.map((m) => `<button type="button" class="v1-row" data-mint="${esc(m.mint || "")}">
          <b>${esc(m.symbol || m.mint || "?")}</b>
          <span class="muted">${esc(m.why || "?")}${m.hit5x ? " · ran ≥5×" : ""}</span>
          <span class="muted">${m.mcap != null ? "$" + Number(m.mcap).toLocaleString() : "—"}</span>
        </button>`).join("")}</div>`;
      }
    }
    const missAudit = ftEarly.secondary_miss_audit || {};
    const unionMiss = Number(missAudit.union_miss || 0);
    const hcMiss = Number(missAudit.high_confidence_miss_n || 0);
    if (missAudit.union_board != null || unionMiss > 0) {
      const repairs = missAudit.repairs_attempted ?? 0;
      const repairOk = missAudit.repairs_ok ?? 0;
      const br = missAudit.miss_reason_breakdown || {};
      const brLine = Object.entries(br).map(([k, v]) => `${k}×${v}`).join(" · ") || "—";
      html += `<p class="board-sub"><b>Secondary union miss</b> — ${esc(missAudit.note || "")} · misses ${unionMiss} · HC ${hcMiss} · repairs ${repairOk}/${repairs} (cap ${missAudit.repair_cap ?? 5}). <span class="muted">fomo_mirror unchanged.</span></p>
        <p class="board-sub muted">${esc(missAudit.auto_repair_note || "")} · reasons: ${esc(brLine)}</p>`;
      const topMiss = (missAudit.misses || []).slice(0, 5);
      if (topMiss.length) {
        html += `<div class="v1-list">${topMiss.map((m) => `<button type="button" class="v1-row" data-mint="${esc(m.mint || "")}">
          <b>${esc(m.symbol || m.mint || "?")}</b>
          <span class="muted">${esc((m.sources || []).join("+"))}${m.high_confidence_miss ? " · HC" : ""} · ${esc(m.miss_reason || "?")}${m.repair_attempted ? " · fix" : ""}</span>
          <span class="muted">rank ${m.rank ?? "—"}</span>
        </button>`).join("")}</div>`;
      }
    }
    const grad = ftEarly.graduated_board || {};
    const gmgnB = ftEarly.gmgn_trending_board || {};
    const dexB = ftEarly.dexscreener_trending_board || {};
    const overlap = ftEarly.secondary_overlap || {};
    if (mirrorStale) {
      const liveTags = [];
      if ((grad.items || []).length && grad.board_live) liveTags.push("FOMO graduated");
      if ((gmgnB.items || []).length && gmgnB.board_live) liveTags.push("GMGN trending");
      if ((dexB.items || []).length && dexB.board_live) liveTags.push("DexScreener boosts");
      const liveLine = liveTags.length ? `Live secondary: ${liveTags.join(" · ")}` : "No live secondary boards loaded";
      html += `<p class="board-sub down"><b>Secondary ≠ FOMO trending</b> — ${esc(liveLine)}. Door sanity uses secondary boards when mirror is stale; production gate still keys off FOMO trending capture only.</p>`;
      if ((grad.items || []).length) {
        const gc = grad.counts || {};
        html += `<p class="board-sub"><b>Graduated</b> · ${esc(grad.note || "")} · board ${gc.board ?? grad.items.length} · overlap ${overlap.graduated ?? 0}/${overlap.fomo_trending ?? "?"}</p>`;
      }
      const gmgnWhy = gmgnB.empty_reason || gmgnB.skip_reason || (gmgnB.error ? "error" : "");
      if ((gmgnB.items || []).length || gmgnB.skipped || gmgnWhy) {
        const gc = gmgnB.counts || {};
        const skip = gmgnWhy ? ` · ${esc(gmgnWhy)}` : "";
        html += `<p class="board-sub"><b>GMGN trending</b> · ${esc(gmgnB.note || "")} · board ${gc.board ?? (gmgnB.items || []).length} · overlap ${overlap.gmgn_trending ?? 0}/${overlap.fomo_trending ?? "?"}${skip}</p>`;
      }
      const dexWhy = dexB.empty_reason || dexB.skip_reason || (dexB.error ? "error" : "");
      if ((dexB.items || []).length || dexWhy) {
        const gc = dexB.counts || {};
        html += `<p class="board-sub"><b>DexScreener</b> · ${esc(dexB.note || "")} · board ${gc.board ?? dexB.items.length} · overlap ${overlap.dexscreener_trending ?? 0}/${overlap.fomo_trending ?? "?"}${dexWhy ? ` · ${esc(dexWhy)}` : ""}</p>`;
      }
    }
    html += fomoBoardRowsHtml(12);
    const auditAt = (ftEarly.last_audit && ftEarly.last_audit.at) || "";
    if (auditAt) {
      html += `<p class="board-sub">Last audit · ${esc(String(auditAt).slice(0, 19).replace("T", " "))} UTC · click FOMO trend in the loop list for full board</p>`;
    }
    const rhAlarms = ftEarly.rh_hydrate_alarms || [];
    if (rhAlarms.length) {
      html += `<h4>Hunt mcap hydrate · Learn alarm</h4>
        <p class="board-sub">Desk last_mcap vs FOMO board (zero last, desk too high, or board gap). Dex pair / FDV — not a buy signal.</p>
        <div class="v1-list">${rhAlarms.slice(0, 6).map((a) => `<button type="button" class="v1-row" data-mint="${esc(a.mint || "")}">
          <b>${esc(a.symbol || "?")}</b>
          <span class="muted">${esc(a.reason || "")}</span>
          <span class="muted">last ${a.last_mcap || 0}</span>
          <span class="muted">fomo ${a.fomo_board_mcap != null ? Math.round(Number(a.fomo_board_mcap) / 1000) + "k" : "—"}</span>
        </button>`).join("")}</div>`;
    }
  }
  if (fomoTradersData || fomoAlertsFlowData) {
    const card = fomoTradersData || {};
    html += `<h4>FOMO /ws/alerts · trader scorecard</h4>
      <p class="board-sub">API <code>/api/fomo-alerts/traders</code> · <code>/api/fomo-alerts/flow</code> · <code>/api/fomo-alerts/recent</code>. Select <b>FOMO traders</b> in the loop list for detail.</p>
      <div class="model-card">
        <div><span>Window</span><b>${card.window_days ?? 7}d<small class="muted"> · since ${esc(String(card.since || "—").slice(0, 10))}</small></b></div>
        <div><span>Ranked</span><b>${(card.traders || []).length}<small class="muted"> · min ${card.min_buys ?? 5} buys</small></b></div>
        <div><span>Below floor</span><b>${(card.below_min_buys || []).length}</b></div>
        <div><span>Learn</span><b class="up">paper only</b></div>
      </div>`;
    html += fomoTradersScorecardHtml(6);
  }
  const dd = dayDeltaData;
  {
    const t = (dd && dd.today) || {};
    const y = (dd && dd.yesterday) || {};
    const cov = t.thesis_coverage != null ? Math.round(Number(t.thesis_coverage) * 100) + "%" : "—";
    const ycov = y.thesis_coverage != null ? Math.round(Number(y.thesis_coverage) * 100) + "%" : "—";
    const signalSkips = Object.entries(t.skip_reasons_signal || {}).sort((a, b) => b[1] - a[1]).slice(0, 4)
      .map(([k, v]) => `${k}×${v}`).join(" · ") || "";
    const fam = t.skip_families || {};
    const drown = fam.copycat ? `copycat drown ×${fam.copycat} (excluded)` : "";
    const skips = [signalSkips, drown].filter(Boolean).join(" · ")
      || Object.entries(t.skip_reasons || {}).sort((a, b) => b[1] - a[1]).slice(0, 4)
        .map(([k, v]) => `${k}×${v}`).join(" · ") || "none yet";
    const used = Boolean(t.picked_n || t.skipped_n || t.closed_n || t.shadow_n);
    html += `<h4>Daily ritual · yesterday → today</h4>
      <p class="board-sub">${esc(((dd && dd.ritual) || [])[0] || "Self-improve loop: thesis coverage, skip reasons, hit2× vs yesterday. Paper only.")}</p>
      <div class="model-card">
        <div><span>Ritual</span><b class="${used ? "up" : ""}">${used ? "used today" : (dd ? "idle" : "loading")}</b></div>
        <div><span>Hard-tag on opens</span><b>${esc(cov)}<small class="muted"> · yday ${esc(ycov)}</small></b></div>
        <div><span>Hit2×</span><b>${t.hit2x_n ?? 0}/${t.closed_n ?? 0}<small class="muted"> · Δ ${dd && dd.delta && dd.delta.hit2x_rate != null ? (Number(dd.delta.hit2x_rate) * 100).toFixed(0) + "pp" : "—"}</small></b></div>
        <div><span>Policy</span><b>${esc((dd && dd.rank_policy) || "—")}</b></div>
      </div>
      <p class="board-sub">Signal skips · ${esc(skips)} · yday picked ${y.picked_n ?? 0} / closed ${y.closed_n ?? 0}</p>`;
    const miss = (dd && dd.paper_misses) || {};
    const wh = miss.would_have || {};
    if (miss.n_runners != null || wh.n_hit_runners != null) {
      const prec = wh.precision != null ? Math.round(Number(wh.precision) * 100) + "%" : "—";
      const rec = wh.recall != null ? Math.round(Number(wh.recall) * 100) + "%" : "—";
      html += `<p class="board-sub">Miss autopsies · ${miss.n_runners ?? 0} runners / ${miss.n_duds ?? 0} duds · would-have ${wh.n_hit_runners ?? 0} P ${esc(prec)} R ${esc(rec)} · ${esc(wh.evidence || "thin")} · open=false</p>`;
    }
    const fnh = miss.fomo_trend_no_hunt || {};
    if (fnh.n_joined != null || fnh.n_runners != null) {
      html += `<p class="board-sub">FOMO trending no-Hunt · ${fnh.n_joined ?? 0} joined / ${fnh.n_runners ?? 0} later ≥5× · desk ${fnh.n_desk_no_hunt ?? 0} · door ${fnh.n_door_miss ?? 0} · open=false</p>`;
    }
  }
  const ed = earlyDiffData;
  if (ed && (ed.items || ed.cohort_proof)) {
    const proof = ed.cohort_proof || {};
    const top = (ed.items || []).slice(0, 6);
    const surv = ed.survivors || {};
    html += `<h4>Early diff · SI cohort shadow</h4>
      <p class="board-sub">${esc(ed.note || "Feature-divergence closer-look. Not a buy list. Serial = prior never block.")}</p>
      <div class="model-card">
        <div><span>Scanned</span><b>${ed.n_scanned ?? 0}<small class="muted"> · top ${ed.top_n ?? 0}</small></b></div>
        <div><span>Cohort sep</span><b class="${proof.separation_ok ? "up" : ""}">${proof.separation_ok ? "ok" : "thin"}<small class="muted"> · P ${proof.printer_score_median ?? "—"} / D ${proof.dud_score_median ?? "—"}</small></b></div>
        <div><span>Survivors</span><b>${surv.n_survivors ?? 0}<small class="muted"> · h2/h3/h4</small></b></div>
        <div><span>Alerts</span><b>${(ed.alerts || []).length}<small class="muted"> · thr ${ed.alert_threshold ?? 0.55}</small></b></div>
      </div>`;
    if ((ed.alerts_new || []).length) {
      html += `<p class="board-sub up">New closer-look · ${(ed.alerts_new || []).slice(0, 3).map((a) => esc(a.message || a.symbol || "")).join(" · ")}</p>`;
      maybeEarlyDiffDesktopNotify(ed.alerts_new);
    }
    if (top.length) {
      html += `<p class="board-sub">Closer look · ${top.map((r) => esc(`${r.symbol || "?"} ${r.divergence_score != null ? Number(r.divergence_score).toFixed(2) : "—"}`)).join(" · ")}</p>`;
    }
  }
  const sl = sanityLoopData;
  if (sl && (sl.checks || []).length) {
    const fails = (sl.checks || []).filter((c) => c.status === "fail").length;
    const warns = (sl.checks || []).filter((c) => c.status === "warn").length;
    const next = ((sl.improve || [])[0] || {}).action || "hold_course";
    const appliedN = (sl.applied && sl.applied.applied) ? sl.applied.applied.length : 0;
    html += `<h4>Sanity loop · improve gated</h4>
      <p class="board-sub">${esc(((sl.rules || [])[0]) || "Hard sanity blocks buy-side experiments. Soft/late stay shadow-only.")}</p>
      <div class="model-card">
        <div><span>Hard</span><b class="${sl.hard_ok ? "up" : "down"}">${sl.hard_ok ? "ok" : "fail×" + fails}</b></div>
        <div><span>Warn</span><b>${warns}</b></div>
        <div><span>FOMO miss</span><b>${(sl.fomo && sl.fomo.miss_n) ?? "—"}</b></div>
        <div><span>Next</span><b>${esc(String(next).replace(/_/g, " ").slice(0, 22))}</b></div>
      </div>`;
    if ((sl.improve || []).length) {
      html += `<p class="board-sub">${(sl.improve || []).slice(0, 3).map((a) => esc(`${a.action}: ${a.why || ""}`.slice(0, 90))).join(" · ")}${appliedN ? ` · applied ${appliedN}` : ""}</p>`;
    }
  }
  const vr = vetoRetroData;
  if (vr && (vr.needles || []).length) {
    const ver = vr.verdict || {};
    const hj = (vr.needles || []).find((n) => n.veto === "hijack") || {};
    const soft = (vr.needles || []).filter((n) => n.class === "soft_shadow").slice(0, 3);
    html += `<h4>Veto health · full history</h4>
      <p class="board-sub">${esc(ver.summary || "Hard vetoes stay when peaks dust. Soft/late stay shadow-only. Not a buy list.")}</p>
      <div class="model-card">
        <div><span>Gate vetoes</span><b>${vr.n_gate_veto ?? 0}<small class="muted"> · pass ${vr.n_gate_pass ?? 0}</small></b></div>
        <div><span>Hijack peak5</span><b>${hj.hit5_t0 ?? "—"}<small class="muted"> · dust ${hj.peak5_then_dust ?? "—"}</small></b></div>
        <div><span>Hijack held</span><b>${hj.peak5_held_sellable_rate != null ? Math.round(Number(hj.peak5_held_sellable_rate) * 100) + "%" : "—"}</b></div>
        <div><span>Policy</span><b class="${ver.hijack_keep ? "up" : "down"}">${ver.hijack_keep ? "hijack keep" : "watch"}</b></div>
      </div>`;
    if (soft.length) {
      html += `<p class="board-sub">Soft shadow · ${soft.map((n) => {
        const avg = n.shadow && n.shadow.avg_return_pct != null ? `${n.shadow.avg_return_pct}%` : "—";
        return `${esc(n.veto)} ${avg}`;
      }).join(" · ")}</p>`;
    }
  }
  const shadowRows = reviewData.shadow || [];
  const skipRows = (reviewData.skipped || []).filter((r) => !r.shadow);
  const sections = [
    ["Picked / open", reviewData.picked || []],
    ["Queued", reviewData.queued || []],
    ["Skipped at lock", skipRows],
    ["Shadow near-miss (negatives)", shadowRows],
  ];
  for (const [title, rows] of sections) {
    html += `<h4>${title}${rows.length ? ` · ${rows.length}` : ""}</h4>`;
    if (!rows.length) html += `<p class="board-sub">None yet.</p>`;
    else html += `<div class="v1-list">${rows.slice(0, 12).map(reviewRowHtml).join("")}</div>`;
  }
  html += `<h4>Worker loops</h4><p class="board-sub">A tape loop older than 10 minutes is down. batch_fit is hourly by design.</p><div class="model-card">`;
  const names = Object.keys(loops);
  if (!names.length) html += `<div><span>Loops</span><b>—</b></div>`;
  for (const name of names.slice(0, 8)) {
    const info = loops[name] || {};
    const age = loopAgeLabel(info);
    const bad = name !== "batch_fit" && stale.some((s) => s.replace(/\s/g, "_") === name || s === name.replace("_", " "));
    html += `<div><span>${esc(name.replace(/_/g, " "))}</span><b class="${bad ? "down" : ""}">${esc(age)}${info.note ? `<small class="muted"> · ${esc(String(info.note).slice(0, 40))}</small>` : ""}</b></div>`;
  }
  html += `</div>`;
  html += `<h4>Wide-book context</h4><p class="board-sub">Training ocean only — not the success metric.</p>
    <div class="model-card">
      <div><span>This-window EV</span><b>${tw.avg_return_pct != null ? tw.avg_return_pct + "%" : "—"}<small class="muted"> · ${tw.n || 0}</small></b></div>
      <div><span>Worker</span><b class="${stale.length ? "down" : "up"}">${stale.length ? stale.join(", ") + " stale" : "heartbeats ok"}</b></div>
      <div><span>APIs</span><b>${healthState.github ? "GitHub" : "no GH"}${healthState.bitquery ? " · BQ" : ""}${healthState.alerts ? " · alerts" : " · quiet"}</b></div>
    </div>`;
  const fs = boardData.firstSight;
  const fsArt = fs && ((fs.artifacts || []).find((a) => a.promoted) || (fs.artifacts || [])[0]);
  if (fsArt) {
    html += `<h4>First-sight fit</h4><div class="model-card">
      <div><span>Version</span><b class="${fsArt.promoted ? "up" : ""}">v${fsArt.version}${fsArt.promoted ? " · promoted" : ""}</b></div>
      <div><span>AUC</span><b>${fsArt.auc ?? "—"}</b></div>
      <div><span>Top decile 2×</span><b>${fsArt.precision_top_decile != null ? Math.round(fsArt.precision_top_decile * 100) + "%" : "—"}</b></div>
      <div><span>Brier</span><b>${fsArt.brier ?? "—"}</b></div>
    </div>`;
  }
  const rr = runnersRetroData;
  if (rr && rr.n != null) {
    const rate = rr.would_pass_rate != null ? Math.round(Number(rr.would_pass_rate) * 100) + "%" : "—";
    const bp = rr.by_path || {};
    const bm = rr.by_miss || {};
    const missBits = Object.entries(bm).slice(0, 4).map(([k, v]) => `${k}×${v}`).join(" · ");
    html += `<h4>Runners vs paperV1</h4>
      <p class="board-sub">${esc(rr.note || "Would confirmed 5×+ names have cleared the short list?")}</p>
      <div class="model-card">
        <div><span>Would pass</span><b>${rr.would_pass_v1 ?? 0}/${rr.n}<small class="muted"> · ${esc(rate)}</small></b></div>
        <div><span>Score path</span><b>${bp.score ?? 0}</b></div>
        <div><span>Thesis path</span><b>${bp.thesis ?? 0}</b></div>
        <div><span>Miss</span><b>${bp.miss ?? 0}<small class="muted"> · thin ${rr.thin_entry_features ?? 0} · live ${rr.live_reconstructed ?? 0}</small></b></div>
      </div>`;
    if (missBits) {
      html += `<p class="board-sub">Miss taxonomy · ${esc(missBits)}</p>`;
    }
    if (rr.would_pass_not_booked) {
      html += `<p class="board-sub">Would pass but not booked · ${rr.would_pass_not_booked}</p>`;
    }
    const hits = (rr.items || []).filter((r) => r.would_pass_v1).slice(0, 6);
    const misses = (rr.items || []).filter((r) => !r.would_pass_v1).slice(0, 6);
    if (hits.length) {
      html += `<p class="board-sub">Would pass · ${hits.map((r) => esc(`${r.symbol} ${r.multiple}×`)).join(" · ")}</p>`;
    }
    if (misses.length) {
      html += `<p class="board-sub">Missed · ${misses.map((r) => esc(`${r.symbol} ${r.multiple}×${r.miss_reason ? " · " + r.miss_reason : ""}`)).join(" · ")}</p>`;
    }
  }
  const mc = missCohortData;
  if (mc && (mc.n_winners != null || mc.n_losers != null)) {
    const tw = Object.entries(mc.tag_rate_winners || {}).map(([k, v]) => `${k} ${Math.round(Number(v) * 100)}%`).join(" · ");
    const tl = Object.entries(mc.tag_rate_losers || {}).map(([k, v]) => `${k} ${Math.round(Number(v) * 100)}%`).join(" · ");
    html += `<h4>Same-day miss cohort</h4>
      <p class="board-sub">${esc(mc.note || "Winners vs losers in $10k–$500k entry band.")}</p>
      <div class="model-card">
        <div><span>Winners</span><b>${mc.n_winners ?? 0}</b></div>
        <div><span>Losers</span><b>${mc.n_losers ?? 0}</b></div>
        <div><span>Paper skip</span><b>${mc.paper_skipped ?? 0}<small class="muted"> · shadow ${mc.paper_shadow ?? 0}</small></b></div>
        <div><span>Days</span><b>${(mc.by_day || []).length}</b></div>
      </div>`;
    if (tw) html += `<p class="board-sub">Winner tags · ${esc(tw)}</p>`;
    if (tl) html += `<p class="board-sub">Loser tags · ${esc(tl)}</p>`;
    if (mc.n_copycat_veto_winners != null || (mc.evidence_rows || []).length) {
      html += `<p class="board-sub">Copycat vetoed winners · ${mc.n_copycat_veto_winners ?? (mc.evidence_rows || []).length} · gate_veto=copycat</p>`;
    }
    if (mc.signal_score_avg_winners != null || mc.hard_tag_rate_winners != null) {
      html += `<div class="model-card">
        <div><span>Signal W</span><b>${mc.signal_score_avg_winners != null ? Number(mc.signal_score_avg_winners).toFixed(2) : "—"}</b></div>
        <div><span>Signal L</span><b>${mc.signal_score_avg_losers != null ? Number(mc.signal_score_avg_losers).toFixed(2) : "—"}</b></div>
        <div><span>Hard tags W</span><b>${mc.hard_tag_rate_winners != null ? Math.round(Number(mc.hard_tag_rate_winners) * 100) + "%" : "—"}</b></div>
        <div><span>Hard tags L</span><b>${mc.hard_tag_rate_losers != null ? Math.round(Number(mc.hard_tag_rate_losers) * 100) + "%" : "—"}</b></div>
      </div>`;
    }
    const pm = mc.paper_misses || {};
    if (pm.n_runners != null || (pm.autopsies || []).length) {
      const seps = (pm.separators || []).slice(0, 4).map((s) => {
        const d = Number(s.delta);
        const sign = d > 0 ? "+" : "";
        return `${s.feature} ${sign}${d.toFixed(2)}`;
      }).join(" · ");
      html += `<h4>Paper miss autopsies · 5×+</h4>
        <p class="board-sub">${esc(pm.note || "Every paperV1 taxonomy skip feeds Learn. Book/holders over social.")}</p>
        <div class="model-card">
          <div><span>Runners</span><b>${pm.n_runners ?? 0}<small class="muted"> · joined ${pm.n_joined ?? 0}</small></b></div>
          <div><span>Duds</span><b>${pm.n_duds ?? 0}</b></div>
          <div><span>Side key</span><b>${esc(pm.side_key || "paper_miss_join")}</b></div>
          <div><span>Win</span><b>≥${pm.win_multiple ?? 5}×</b></div>
        </div>`;
      const wh = pm.would_have || {};
      if (wh.n_hit_runners != null || wh.precision != null) {
        const prec = wh.precision != null ? Math.round(Number(wh.precision) * 100) + "%" : "—";
        const rec = wh.recall != null ? Math.round(Number(wh.recall) * 100) + "%" : "—";
        html += `<div class="model-card">
          <div><span>Would-have</span><b>${wh.n_hit_runners ?? 0}/${pm.n_runners ?? 0}</b></div>
          <div><span>Precision</span><b>${esc(prec)}</b></div>
          <div><span>Recall</span><b>${esc(rec)}</b></div>
          <div><span>Open</span><b class="muted">${wh.open ? "on" : "shadow"}</b></div>
        </div>`;
        html += `<p class="board-sub">${esc(wh.note || "early_runner_book would-have. Live 0.50 unchanged. Not a buy.")}</p>`;
      }
      if (pm.n_copycat_suppressed) {
        html += `<p class="board-sub">Copycat suppressed from denominators · ${pm.n_copycat_suppressed}${pm.n_si_suppressed ? ` · SI ${pm.n_si_suppressed}` : ""}</p>`;
      }
      const cv = mc.copycat_veto || pm.copycat_veto || {};
      const ev = mc.evidence_rows || cv.evidence_rows || [];
      if (cv.n_winners != null || ev.length || mc.n_copycat_veto_winners != null) {
        const nWin = cv.n_winners ?? mc.n_copycat_veto_winners ?? ev.length;
        const nWh = cv.n_would_have ?? mc.n_would_have_copycat ?? 0;
        html += `<p class="board-sub">Copycat vetoed winners · ${nWin} · gate_veto=copycat · would-have ${nWh} (shadow)</p>`;
        const first = ev[0];
        if (first) {
          const bits = [
            first.symbol || "www",
            first.label || "FALSE-VETO",
            first.gate_veto || "copycat",
            first.multiple != null ? `${Number(first.multiple)}×` : "",
          ].filter(Boolean).join(" · ");
          html += `<p class="board-sub">Evidence #1 · ${esc(bits)}</p>`;
        }
      }
      const fnh = pm.fomo_trend_no_hunt || {};
      if (fnh.n_joined != null || (fnh.autopsies || []).length || (fnh.runners || []).length) {
        const whyLine = Object.entries(fnh.by_why || {}).map(([k, v]) => `${k}×${v}`).join(" · ") || "—";
        const fnhSeps = (fnh.separators || []).slice(0, 4).map((s) => {
          const d = Number(s.delta);
          const sign = d > 0 ? "+" : "";
          return `${s.feature} ${sign}${Number.isFinite(d) ? d.toFixed(2) : "?"}`;
        }).join(" · ");
        html += `<p class="board-sub">FOMO trending → no Hunt · ${fnh.n_joined ?? 0} joined / ${fnh.n_runners ?? 0} later ≥5× · duds ${fnh.n_duds ?? 0} · ${esc(whyLine)} · open=false</p>`;
        if (fnhSeps) html += `<p class="board-sub">FOMO separators · ${esc(fnhSeps)}</p>`;
      }
      if (seps) html += `<p class="board-sub">Separators · ${esc(seps)}</p>`;
      const cards = (pm.autopsies || []).slice(0, 6);
      if (cards.length) {
        html += `<p class="board-sub">Autopsy · ${cards.map((r) => {
          const book = r.book || {};
          const bits = [
            r.symbol || "?",
            r.multiple != null ? `${Number(r.multiple).toFixed(1)}×` : "",
            r.skip_reason || "",
            book.organic_book ? "organic" : "",
            book.top10_pct != null ? `top10 ${Number(book.top10_pct).toFixed(0)}%` : "",
          ].filter(Boolean).join(" ");
          return esc(bits);
        }).join(" · ")}</p>`;
      }
    }
  }
  const os = offlineSprintData;
  if (os) {
    const rep = os.repair || {};
    html += `<h4>Offline sprint</h4>
      <p class="board-sub">Accelerate without waiting on new launches. Leftover ≥$1M rejected · Live frozen at paper sight · 3+3 caps.</p>
      <div class="model-card">
        <div><span>Thesis repair</span><b>${rep.patched ?? 0}<small class="muted"> / ${rep.scanned ?? 0}</small></b></div>
        <div><span>Weights</span><b>${esc(Object.entries(os.thesis_weights || {}).map(([k, v]) => `${k.split("_")[0]} ${v}`).slice(0, 2).join(" · ") || "default")}</b></div>
        <div><span>Policy</span><b>${esc(os.rank_policy || "signal")}</b></div>
        <div><span>Next</span><b>${esc(((os.next || [])[0] || "—").slice(0, 28))}</b></div>
      </div>`;
  }
  const yw = yearWinnersData;
  if (yw) {
    const lr = yw.ledger_runners || {};
    const curated = yw.curated || [];
    html += `<h4>Incredible returns template</h4>
      <p class="board-sub">${esc(yw.horizon_note || yw.how_to_use || "")}</p>
      <div class="model-card">
        <div><span>Curated</span><b>${curated.length}</b></div>
        <div><span>Ledger 5×+</span><b>${lr.n ?? 0}</b></div>
        <div><span>V1 capture</span><b>${lr.would_pass_rate != null ? Math.round(Number(lr.would_pass_rate) * 100) + "%" : "—"}</b></div>
        <div><span>Tags</span><b>${esc(Object.entries(lr.tag_counts || {}).map(([k, v]) => `${k}×${v}`).join(" ") || "—")}</b></div>
      </div>`;
    if (curated.length) {
      html += `<p class="board-sub">North stars · ${curated.slice(0, 6).map((r) => esc(r.symbol || r.id || "?")).join(" · ")}</p>`;
    }
  }
  learnPane.innerHTML = html;
  bindFomoBoardRows(learnPane);
  bindFomoTraderRows(learnPane);
}

function fomoBoardRowsHtml(limit = 18) {
  const ft = fomoTrendingData;
  if (!ft || !(ft.items || []).length) return `<p class="muted">No FOMO board snapshot yet — worker audits hourly.</p>`;
  return `<div class="v1-list">${(ft.items || []).slice(0, limit).map((row) => {
    const bucket = row.bucket || row.status || "?";
    const mult = row.multiple != null && Number(row.multiple) > 0 ? `${Number(row.multiple).toFixed(1)}×` : "";
    const veto = row.gate_veto ? ` · ${row.gate_veto}` : "";
    return `<button type="button" class="v1-row" data-mint="${esc(row.mint || "")}" title="${esc(bucket)}${esc(veto)}">
      <b>${esc(row.symbol || "?")}</b>
      <span class="muted">${esc(row.chain || "")}</span>
      <span class="muted">#${row.rank != null ? esc(String(row.rank)) : "—"}</span>
      <span class="muted">${esc(bucket)}</span>
      ${mult ? `<span class="muted">${esc(mult)}</span>` : ""}
    </button>`;
  }).join("")}</div>`;
}

function bindFomoBoardRows(root) {
  if (!root) return;
  root.querySelectorAll("[data-mint]").forEach((btn) => {
    btn.onclick = () => {
      if (btn.dataset.mint) {
        setDeskTab("picks");
        openDetail(btn.dataset.mint).catch(() => {});
      }
    };
  });
}

function fomoChainKey(chain) {
  const c = String(chain || "").toLowerCase();
  if (c === "rh" || c === "robinhood") return "robinhood";
  return c || "sol";
}

function fomoTabRows() {
  const ft = fomoTrendingData;
  const raw = (ft && (ft.trending_items || ft.items)) || [];
  const want = CHAIN === "robinhood" ? "robinhood" : "sol";
  return raw.filter((row) => fomoChainKey(row.chain) === want);
}

function fomoMintShort(mint) {
  const m = String(mint || "");
  if (m.length < 12) return m || "—";
  return `${m.slice(0, 4)}…${m.slice(-4)}`;
}

function fomoMintLinks(mint, chain) {
  const m = String(mint || "");
  if (!m) return "";
  const ck = fomoChainKey(chain);
  if (ck === "robinhood" && m.startsWith("0x")) {
    return `<a href="https://robinhoodchain.blockscout.com/token/${esc(m)}" target="_blank" rel="noreferrer">${esc(fomoMintShort(m))}</a>`;
  }
  return `<a href="https://gmgn.ai/sol/token/${esc(m)}" target="_blank" rel="noreferrer">${esc(fomoMintShort(m))}</a>`
    + ` · <a href="https://solscan.io/token/${esc(m)}" target="_blank" rel="noreferrer">scan</a>`;
}

function fomoFreshness(ft) {
  const card = ft || {};
  const staleFlag = card.board_stale === true || card.api_stale_flag === true;
  const ageH = card.capture_age_hours != null ? Number(card.capture_age_hours) : null;
  const snapS = card.snapshot_age_s != null ? Number(card.snapshot_age_s) : null;
  const source = String(card.source || card.api_source || "");
  const live = card.board_live === true || source === "live";
  const sitOut = source === "sit-out" || source === "error";
  let fresh = !staleFlag && live;
  if (!fresh && !staleFlag) {
    if (ageH != null && ageH <= 0.5) fresh = true;
    else if (snapS != null && snapS <= 25 * 60 && !sitOut) fresh = true;
  }
  const ageLabel = ageH != null
    ? `${ageH < 1 ? Math.round(ageH * 60) + "m" : ageH.toFixed(1) + "h"}`
    : (snapS != null ? `${Math.round(snapS / 60)}m snap` : source || "—");
  const note = card.mirror_note || card.sanity?.note || "Cross-check FOMO app trending vs this desk. Sanity only — not a buy list.";
  return { fresh, staleFlag, ageLabel, source, note };
}

function fomoSecondaryFootnote(ft) {
  const bits = [];
  const add = (label, pack) => {
    if (!pack || typeof pack !== "object") return;
    const n = (pack.items || []).length;
    const why = pack.empty_reason || pack.note || "";
    if (n || why) bits.push(`${label}: ${n}${why ? ` (${why})` : ""}`);
  };
  add("graduated", ft.graduated_board);
  add("GMGN", ft.gmgn_trending_board);
  add("Dex", ft.dexscreener_trending_board);
  if (!bits.length) return "";
  return `<p class="board-sub fomo-secondary-note">Secondary boards (footnote only): ${esc(bits.join(" · "))}</p>`;
}

function fomoStatusClass(status) {
  const s = String(status || "").toLowerCase();
  if (s === "caught") return "open";
  if (s === "miss") return "skipped";
  if (s === "leftover" || s === "thin" || s === "no_dex") return "held";
  return "held";
}

function renderFomoPane() {
  if (!fomoPane) return;
  const ft = fomoTrendingData;
  const rows = fomoTabRows();
  if (!ft) {
    fomoPane.innerHTML = `<h4>FOMO trending</h4><p class="muted">Loading FOMO board…</p>`;
    return;
  }
  const fr = fomoFreshness(ft);
  const bannerCls = fr.fresh ? "fresh" : "stale";
  const counts = ft.counts || {};
  const banner = `<div class="fomo-mirror-banner ${bannerCls}" role="status">
    <strong>${fr.fresh ? "Live FOMO mirror fresh" : "Stale FOMO capture — verify in app"}</strong>
    · source ${esc(fr.source || "—")} · capture ${esc(fr.ageLabel)}
    ${fr.staleFlag ? " · board_stale" : ""}
    · board ${counts.board ?? rows.length} · miss ${counts.miss ?? "—"}
    <br><span class="muted">${esc(fr.note)}</span>
  </div>`;
  const table = rows.length
    ? `<table class="fomo-trend-table"><thead><tr>
        <th>#</th><th>Token</th><th>Mint</th><th>Mcap</th><th>Status</th><th>Hunt</th><th>Desk</th><th>×</th>
      </tr></thead><tbody>${rows.map((row) => {
        const mult = row.multiple != null && Number(row.multiple) > 0 ? `${Number(row.multiple).toFixed(1)}×` : "—";
        const mcap = row.mcap_usd != null ? fmtMcap(row.mcap_usd) : (row.last_mcap ? fmtMcap(row.last_mcap) : "—");
        const sym = row.symbol || row.name || "?";
        return `<tr class="fomo-row${row.mint === selected ? " on" : ""}" data-fomo-mint="${esc(row.mint || "")}">
          <td>${row.rank != null ? esc(String(row.rank)) : "—"}</td>
          <td><b>${esc(sym)}</b>${row.name && row.name !== sym ? `<br><span class="muted">${esc(row.name)}</span>` : ""}</td>
          <td class="fomo-mint-cell">${fomoMintLinks(row.mint, row.chain)}</td>
          <td>${esc(mcap)}</td>
          <td><span class="state ${fomoStatusClass(row.status)}">${esc(row.bucket || row.status || "?")}</span></td>
          <td>${row.on_hunt ? "yes" : "—"}</td>
          <td>${row.on_desk ? "yes" : "—"}</td>
          <td class="${multipleClass(Number(row.multiple || 0))}">${esc(mult)}</td>
        </tr>`;
      }).join("")}</tbody></table>`
    : `<p class="muted">No FOMO trending rows for this chain yet. Worker audits hourly; try again after the next capture.</p>`;
  const auditAt = (ft.last_audit && ft.last_audit.at) || ft.captured_at || "";
  fomoPane.innerHTML = `
    <h4>FOMO trending · cross-check</h4>
    <p class="board-sub">Paper-only sanity board. Compare rank and symbol to the FOMO app — not the 1–5 short list.</p>
    ${banner}
    ${table}
    ${auditAt ? `<p class="muted">Last audit · ${esc(String(auditAt).slice(0, 19).replace("T", " "))} UTC</p>` : ""}
    ${fomoSecondaryFootnote(ft)}
  `;
  fomoPane.querySelectorAll("[data-fomo-mint]").forEach((tr) => {
    tr.onclick = () => {
      selected = tr.dataset.fomoMint;
      renderTape();
      renderFomoPane();
      renderFomoDetail(selected);
    };
  });
}

function renderFomoDetail(mint) {
  const row = fomoTabRows().find((r) => r.mint === mint) || fomoTabRows()[0];
  if (!row) {
    detailEl.innerHTML = `<p class="muted">Select a FOMO trending row to compare desk coverage.</p>`;
    return;
  }
  selected = row.mint;
  const mult = row.multiple != null && Number(row.multiple) > 0 ? `${Number(row.multiple).toFixed(2)}×` : "—";
  const veto = row.gate_veto ? `<p class="board-sub">Gate veto · ${esc(row.gate_veto)}</p>` : "";
  detailEl.innerHTML = `
    <div class="card-top">
      <div class="who">
        <span class="ph">FO</span>
        <div>
          <h3>${esc(row.symbol || "?")}</h3>
          <p class="sub">FOMO rank #${row.rank != null ? esc(String(row.rank)) : "—"} · ${esc(row.status || "?")}</p>
        </div>
      </div>
      <div class="score-badge ${row.on_desk ? "high" : row.status === "miss" ? "low" : "mid"}">${row.on_hunt ? "hunt" : row.on_desk ? "desk" : "—"}<small>cover</small></div>
    </div>
    <div class="stat-grid">
      <div><span>Mcap</span><b>${esc(fmtMcap(row.mcap_usd || row.last_mcap || 0))}</b></div>
      <div><span>Multiple</span><b>${esc(mult)}</b></div>
      <div><span>On Hunt</span><b>${row.on_hunt ? "yes" : "no"}</b></div>
      <div><span>On desk</span><b>${row.on_desk ? "yes" : "no"}</b></div>
    </div>
    <p class="section-label">Mint</p>
    <p class="links">${fomoMintLinks(row.mint, row.chain)}</p>
    ${veto}
    ${!row.on_hunt ? `<p class="board-sub">Why not Hunt · ${esc(row.why_not_hunt || "unknown")} · Learn side key fomo_trend_no_hunt · open=false</p>` : ""}
    <p class="section-label">Open on desk</p>
    <p class="muted"><button type="button" class="v1-row" data-fomo-open="${esc(row.mint || "")}">Open research card →</button></p>
  `;
  const openBtn = detailEl.querySelector("[data-fomo-open]");
  if (openBtn) {
    openBtn.onclick = () => openDetail(openBtn.dataset.fomoOpen).catch(() => {});
  }
}

function renderLearnDetail(id, fomoTraderFocus) {
  const row = learnRows.find((r) => r.mint === id) || learnRows[0];
  if (!row) {
    detailEl.innerHTML = `<p class="muted">Select a learning-loop edge.</p>`;
    return;
  }
  selected = row.mint;
  if (row.mint === "learn-gate") {
    const g = productionGateData || {};
    const bars = (g.bars || []).map((b) => {
      const cls = b.color === "green" ? "up" : (b.color === "red" ? "down" : "");
      return `<button type="button" class="v1-row" title="${esc(b.detail || "")}">
        <b><span class="gate-dot ${esc(b.color || "amber")}"></span>${esc(b.label || b.key)}</b>
        <span class="muted">${esc(b.target || "")}</span>
        <span class="${cls}">${esc(gateValueText(b))}</span>
      </button>`;
    }).join("");
    detailEl.innerHTML = `
      <div class="card-top">
        <div class="who">
          <span class="ph">${esc(row.glyph || "PG")}</span>
          <div>
            <h3>${esc(row.symbol)}</h3>
            <p class="sub">${esc(row.name || "")}</p>
          </div>
        </div>
        <div class="score-badge ${g.ready ? "high" : "mid"}">${g.cleared ?? "?"}/${g.total || 6}<small>bars</small></div>
      </div>
      <p class="thesis">${esc(g.summary || row.thesis || "")}</p>
      <p class="section-label">FORWARD bars</p>
      <div class="v1-list">${bars || `<p class="muted">Loading…</p>`}</div>
      <p class="section-label">Mission</p>
      <p class="muted">${esc(g.note || "Read-only operator trust. No arm flip. Paper only.")}</p>
    `;
    return;
  }
  if (row.mint === "learn-fomo") {
    const ft = fomoTrendingData || {};
    const san = ft.sanity || {};
    const counts = ft.counts || {};
    const buckets = ft.by_bucket || {};
    const seenPct = san.seen_rate != null ? Math.round(Number(san.seen_rate) * 100) + "%" : "—";
    const auditAt = (ft.last_audit && ft.last_audit.at) || "";
    detailEl.innerHTML = `
      <div class="card-top">
        <div class="who">
          <span class="ph">${esc(row.glyph || "FO")}</span>
          <div>
            <h3>${esc(row.symbol)}</h3>
            <p class="sub">${esc(row.name || "")}</p>
          </div>
        </div>
        <div class="score-badge ${row.ok === true ? "high" : row.ok === false ? "low" : "mid"}">${row.ok === true ? "ok" : row.ok === false ? "!" : "?"}<small>loop</small></div>
      </div>
      <p class="thesis">${esc(san.note || row.thesis || "")}</p>
      <div class="stat-grid">
        <div><span>Board</span><b>${counts.board ?? (ft.items || []).length}<small class="muted"> · seen ${seenPct}</small></b></div>
        <div><span>Miss</span><b>${san.miss_n ?? counts.miss ?? 0}</b></div>
        <div><span>Hijack</span><b>${san.veto_hijack_n ?? buckets.veto_hijack ?? 0}</b></div>
        <div><span>On desk</span><b>${buckets.on_desk ?? 0}</b></div>
      </div>
      ${(() => {
        const noHunt = ft.fomo_trend_no_hunt || {};
        if (noHunt.n == null && !(noHunt.autopsies || []).length) return "";
        const whyLine = Object.entries(noHunt.by_why || {}).map(([k, v]) => `${k}×${v}`).join(" · ") || "—";
        const seps = (noHunt.separators || []).slice(0, 4).map((s) => {
          const d = Number(s.delta);
          const sign = d > 0 ? "+" : "";
          return `${s.feature} ${sign}${Number.isFinite(d) ? d.toFixed(2) : "?"}`;
        }).join(" · ");
        const cards = (noHunt.autopsies || []).slice(0, 16).map((m) => `<button type="button" class="v1-row" data-mint="${esc(m.mint || "")}">
          <b>${esc(m.symbol || m.mint || "?")}</b>
          <span class="muted">${esc(m.why || "?")}${m.hit5x ? " · ran ≥5×" : ""}</span>
        </button>`).join("");
        return `<p class="section-label">Trending · not on Hunt</p>
          <p class="board-sub">desk ${noHunt.n_desk_no_hunt ?? 0} · door ${noHunt.n_door_miss ?? 0} · later ≥5× ${noHunt.n_ran ?? noHunt.n_runners ?? 0} · duds ${noHunt.n_duds ?? 0} · ${esc(whyLine)} · open=false</p>
          ${seps ? `<p class="board-sub">Separators · ${esc(seps)}</p>` : ""}
          <div class="v1-list">${cards || `<p class="muted">None this capture.</p>`}</div>`;
      })()}
      <p class="section-label">Trending board</p>
      ${fomoBoardRowsHtml(18)}
      ${auditAt ? `<p class="muted">Last audit · ${esc(String(auditAt).slice(0, 19).replace("T", " "))} UTC</p>` : ""}
      ${(ft.rh_hydrate_alarms || []).length ? `<p class="section-label">Mcap hydrate alarm</p>
        <div class="v1-list">${(ft.rh_hydrate_alarms || []).slice(0, 8).map((a) => `<button type="button" class="v1-row" data-mint="${esc(a.mint || "")}">
          <b>${esc(a.symbol || "?")}</b><span class="muted">${esc(a.reason || "")}</span>
          <span class="down">last ${a.last_mcap || 0}</span>
          <span class="muted">fomo ${a.fomo_board_mcap != null ? "$" + Math.round(Number(a.fomo_board_mcap)).toLocaleString() : "—"}</span>
        </button>`).join("")}</div>` : ""}
      <p class="section-label">Mission</p>
      <p class="muted">Sanity only — not a buy list. miss = door bug; veto_hijack = seen but filtered (e/acc class).</p>
      <p class="section-label">Social flow</p>
      <p class="muted"><button type="button" class="v1-row" data-learn-jump="learn-fomo-traders">FOMO /ws/alerts trader scorecard →</button></p>
    `;
    bindFomoBoardRows(detailEl);
    detailEl.querySelectorAll("[data-learn-jump]").forEach((btn) => {
      btn.onclick = () => {
        selected = btn.dataset.learnJump;
        renderTape();
        renderLearnDetail(selected);
      };
    });
    return;
  }
  if (row.mint === "learn-fomo-traders") {
    const card = fomoTradersData || {};
    const traders = card.traders || [];
    const below = card.below_min_buys || [];
    const min = card.min_buys != null ? card.min_buys : 5;
    const formula = (card.formula && card.formula.rank_score) || card.formula || "";
    let focusHtml = "";
    if (fomoTraderFocus) {
      const needle = String(fomoTraderFocus);
      const match = traders.find((t) => (t.trader_key || "") === needle || `uid:${t.user_id}` === needle)
        || below.find((t) => (t.trader_key || "") === needle || `uid:${t.user_id}` === needle);
      if (match) {
        const samples = (match.samples || []).slice(-8).map((s) =>
          `<span class="muted">${esc(s.symbol || s.mint || "?")} · lead ${s.lead_early ? "yes" : "no"} · ${s.outcome_multiple != null ? s.outcome_multiple + "×" : "—"}</span>`
        ).join(" · ");
        focusHtml = `<p class="section-label">Selected</p><div class="model-card">
          <div><span>Trader</span><b>${esc(match.trader || match.user_id || "?")}</b></div>
          <div><span>Buys</span><b>${match.n_buys}</b></div>
          <div><span>Lead early</span><b>${match.lead_early}</b></div>
          <div><span>Score</span><b>${match.rank_score != null ? Number(match.rank_score).toFixed(3) : "—"}</b></div>
        </div>${samples ? `<p class="board-sub">${samples}</p>` : ""}`;
      }
    }
    const belowHtml = below.length
      ? `<p class="section-label">Below ${min} buys (unranked)</p>
        <p class="board-sub">These handles are tracked but hidden from rank until they clear the sample floor.</p>
        <div class="v1-list">${below.slice(0, 12).map((r) => fomoTraderDeskRowHtml(r)).join("")}</div>`
      : `<p class="board-sub muted">Everyone with activity in-window is ranked, or there are no sub-threshold traders.</p>`;
    detailEl.innerHTML = `
      <div class="card-top">
        <div class="who">
          <span class="ph">${esc(row.glyph || "FT")}</span>
          <div>
            <h3>${esc(row.symbol)}</h3>
            <p class="sub">${esc(row.name || "")}</p>
          </div>
        </div>
        <div class="score-badge ${traders.length ? "high" : "mid"}">${traders.length}<small>ranked</small></div>
      </div>
      <p class="thesis">${esc(row.thesis || card.note || "")}</p>
      <p class="muted">paper_only · armed=false · Learn display only. No FOMO→buys wiring.</p>
      <div class="stat-grid">
        <div><span>Window</span><b>${card.window_days ?? 7}d</b></div>
        <div><span>Min USD</span><b>$${esc(String(card.min_usd ?? "—"))}</b></div>
        <div><span>Min buys</span><b>${min}</b></div>
        <div><span>Since</span><b>${esc(String(card.since || "—").slice(0, 10))}</b></div>
      </div>
      ${formula ? `<p class="board-sub">Rank formula · ${esc(typeof formula === "string" ? formula : formula.rank_score || "")}</p>` : ""}
      ${focusHtml}
      <p class="section-label">Ranked traders</p>
      ${traders.length
        ? `<div class="v1-list">${traders.map((r) => fomoTraderDeskRowHtml(r)).join("")}</div>`
        : `<p class="board-sub">No ranked traders yet. Need at least <b>${min}</b> qualifying buys in the rolling window (≥ min USD). Check <code>/api/fomo-alerts/recent</code> for raw flow.</p>`}
      ${belowHtml}
      <p class="section-label">Related</p>
      <p class="muted"><button type="button" class="v1-row" data-learn-jump="learn-fomo">FOMO trending sanity →</button></p>
    `;
    bindFomoTraderRows(detailEl);
    detailEl.querySelectorAll("[data-learn-jump]").forEach((btn) => {
      btn.onclick = () => {
        selected = btn.dataset.learnJump;
        renderTape();
        renderLearnDetail(selected);
      };
    });
    return;
  }
  if (row.mint === "learn-ritual") {
    const d = dayDeltaData || {};
    const t = d.today || {};
    const y = d.yesterday || {};
    const ritual = (d.ritual || []).map((line) => `<p class="board-sub">${esc(line)}</p>`).join("");
    const signalSkips = Object.entries(t.skip_reasons_signal || {}).sort((a, b) => b[1] - a[1]);
    const otherSkips = Object.entries(t.skip_reasons || {}).filter(([k]) => !signalSkips.some((s) => s[0] === k)).sort((a, b) => b[1] - a[1]);
    const familyOf = (k) => {
      const s = String(k || "").toLowerCase();
      if (s.includes("copycat") || s.includes("cc-fomo")) return "copycat";
      if (s.startsWith("v1 si-pr")) return "si";
      return "signal";
    };
    const skips = [...signalSkips, ...otherSkips].slice(0, 8)
      .map(([k, v]) => `<button type="button" class="v1-row"><b>${esc(k)}</b><span class="muted">${esc(familyOf(k))}</span><span>${v}</span></button>`).join("");
    detailEl.innerHTML = `
      <div class="card-top">
        <div class="who">
          <span class="ph">${esc(row.glyph || "DY")}</span>
          <div>
            <h3>${esc(row.symbol)}</h3>
            <p class="sub">${esc(row.name || "")}</p>
          </div>
        </div>
        <div class="score-badge ${row.ok === true ? "high" : row.ok === false ? "low" : "mid"}">${esc((d.rank_policy || "?").slice(0, 6))}<small>policy</small></div>
      </div>
      <p class="thesis">${esc(row.thesis || "")}</p>
      <div class="stat-grid">
        <div><span>Today hard-tag (opens)</span><b>${t.thesis_coverage != null ? Math.round(Number(t.thesis_coverage) * 100) + "%" : "—"}</b></div>
        <div><span>Yday hard-tag (opens)</span><b>${y.thesis_coverage != null ? Math.round(Number(y.thesis_coverage) * 100) + "%" : "—"}</b></div>
        <div><span>Hit2 today</span><b>${t.hit2x_n ?? 0}/${t.closed_n ?? 0}</b></div>
        <div><span>Δhit2</span><b>${d.delta && d.delta.hit2x_rate != null ? (Number(d.delta.hit2x_rate) * 100).toFixed(0) + "pp" : "—"}</b></div>
      </div>
      <p class="section-label">Checklist</p>
      ${ritual || `<p class="muted">Loading…</p>`}
      <p class="section-label">Skip reasons today · signal first, copycat carved</p>
      <div class="v1-list">${skips || `<p class="muted">None yet.</p>`}</div>
      ${d.paper_misses ? `<p class="section-label">Miss autopsies</p>
        <p class="board-sub">${esc(((d.ritual || [])[6]) || `runners ${d.paper_misses.n_runners ?? 0} / duds ${d.paper_misses.n_duds ?? 0}`)}</p>
        <p class="board-sub">${esc(((d.ritual || [])[7]) || `FOMO trending no-Hunt ${(d.paper_misses.fomo_trend_no_hunt || {}).n_joined ?? 0}`)}</p>` : ""}
    `;
    return;
  }
  if (row.mint === "learn-sanity") {
    const s = sanityLoopData || {};
    const checks = (s.checks || []).map((c) => {
      const st = c.status === "pass" ? "up" : (c.status === "fail" ? "down" : "");
      return `<button type="button" class="v1-row" title="${esc(c.detail || "")}">
        <b>${esc(c.name || "?")}</b>
        <span class="muted">${esc(c.level || "")}</span>
        <span class="${st}">${esc(c.status || "?")}</span>
      </button>`;
    }).join("");
    const acts = (s.improve || []).map((a) => `<p class="board-sub"><b>${esc(String(a.action || "").replace(/_/g, " "))}</b> — ${esc(a.why || "")} <span class="muted">(${esc(a.sanity || "")})</span></p>`).join("");
    const applied = s.applied ? `<p class="board-sub">Last apply: ${esc((s.applied.applied || []).join(", ") || s.applied.reason || "none")}</p>` : "";
    detailEl.innerHTML = `
      <div class="card-top">
        <div class="who">
          <span class="ph">${esc(row.glyph || "SN")}</span>
          <div>
            <h3>${esc(row.symbol)}</h3>
            <p class="sub">${esc(row.name || "")}</p>
          </div>
        </div>
        <div class="score-badge ${row.ok === true ? "high" : row.ok === false ? "low" : "mid"}">${row.ok === true ? "ok" : row.ok === false ? "!" : "?"}<small>loop</small></div>
      </div>
      <p class="thesis">${esc(((s.rules || [])[0]) || row.thesis || "")}</p>
      <div class="stat-grid">
        <div><span>Hard</span><b class="${s.hard_ok ? "up" : "down"}">${s.hard_ok ? "ok" : "fail"}</b></div>
        <div><span>FOMO</span><b>${(s.fomo && s.fomo.miss_n) ?? "—"} miss</b></div>
        <div><span>Hijack</span><b>${(s.veto && s.veto.hijack_keep) ? "keep" : "review"}</b></div>
        <div><span>TW EV</span><b>${(s.paper && s.paper.this_window_avg_return_pct) != null ? s.paper.this_window_avg_return_pct + "%" : "—"}</b></div>
      </div>
      <p class="section-label">Checks</p>
      <div class="v1-list">${checks || `<p class="muted">Loading…</p>`}</div>
      <p class="section-label">Improve (gated)</p>
      ${acts || `<p class="muted">Hold course.</p>`}
      ${applied}
      <p class="section-label">Run jobs</p>
      <p class="board-sub"><button type="button" class="linkish" id="sanity-apply-btn"${s.hard_ok ? "" : " disabled"}>Apply enrich/freeze</button> · worker also runs on paper sync · never auto-buys</p>
      <p class="section-label">Mission</p>
      <p class="muted">Reality first: door + veto dust + paper EV. Alpha ideas wait behind green hard checks.</p>
    `;
    const btn = detailEl.querySelector("#sanity-apply-btn");
    if (btn) {
      btn.onclick = () => {
        btn.disabled = true;
        btn.textContent = "Applying…";
        j(withChain("/api/sanity-loop") + "&apply=true", { timeoutMs: 90000 })
          .then((out) => {
            sanityLoopData = out || sanityLoopData;
            buildLearnRows();
            renderTape();
            renderLearnDetail("learn-sanity");
            renderLearnPane();
          })
          .catch(() => { btn.disabled = false; btn.textContent = "Apply enrich/freeze"; });
      };
    }
    return;
  }
  if (row.mint === "learn-early-diff") {
    const ed = earlyDiffData || {};
    const proof = ed.cohort_proof || {};
    const items = (ed.items || []).slice(0, 12);
    const list = items.length
      ? `<div class="v1-list">${items.map((r) => {
          const sc = r.divergence_score != null ? Number(r.divergence_score).toFixed(2) : "—";
          return `<button type="button" class="v1-row" data-mint="${esc(r.mint || "")}">
            <b>${esc(r.symbol || "?")}</b>
            <span class="muted">${esc(sc)}</span>
            <span class="muted">${esc(r.mint ? String(r.mint).slice(0, 8) : "—")}</span>
          </button>`;
        }).join("")}</div>`
      : `<p class="muted">No recent launches scored yet — cohort proof still runs offline.</p>`;
    detailEl.innerHTML = `
      <div class="card-top">
        <div class="who">
          <span class="ph">${esc(row.glyph || "ED")}</span>
          <div>
            <h3>${esc(row.symbol)}</h3>
            <p class="sub">${esc(row.name || "")}</p>
          </div>
        </div>
        <div class="score-badge ${row.ok === true ? "high" : row.ok === false ? "low" : "mid"}">${proof.separation_ok ? "sep" : "?"}<small>cohort</small></div>
      </div>
      <p class="thesis">${esc(ed.note || row.thesis || "")}</p>
      <div class="stat-grid">
        <div><span>Scanned</span><b>${ed.n_scanned ?? 0}</b></div>
        <div><span>Survivors</span><b>${(ed.survivors || {}).n_survivors ?? 0}</b></div>
        <div><span>P med</span><b>${proof.printer_score_median ?? "—"}</b></div>
        <div><span>Alerts</span><b>${(ed.alerts || []).length}</b></div>
      </div>
      <p class="section-label">Closer look (live)</p>
      ${list}
      <p class="section-label">Survivor re-score (h2/h3/h4 only)</p>
      <p class="muted">${(ed.survivors && ed.survivors.n_survivors) ? `${ed.survivors.n_survivors} first-hour survivors` : "No first-hour survivors to re-score at h2/h3/h4."}</p>
      <p class="section-label">Mission</p>
      <p class="muted">Shadow ranker only. No fills. No Hunt-floor change. FEATURE_NAMES stays 66.</p>
    `;
    detailEl.querySelectorAll("[data-mint]").forEach((btn) => {
      btn.onclick = () => {
        if (btn.dataset.mint) {
          setDeskTab("picks");
          openDetail(btn.dataset.mint).catch(() => {});
        }
      };
    });
    return;
  }
  if (row.mint === "learn-veto") {
    const vr = vetoRetroData || {};
    const ver = vr.verdict || {};
    const needles = (vr.needles || []).slice(0, 12);
    const list = needles.length
      ? `<div class="v1-list">${needles.map((n) => {
          const held = n.peak5_held_sellable_rate != null ? `${Math.round(Number(n.peak5_held_sellable_rate) * 100)}% held` : "";
          const dust = n.peak5_dust_rate != null ? `${Math.round(Number(n.peak5_dust_rate) * 100)}% dust` : "";
          const last = n.avg_last_t0 != null ? `${Number(n.avg_last_t0).toFixed(2)}× last` : "";
          return `<button type="button" class="v1-row" title="${esc(n.note || "")}">
            <b>${esc(n.veto)}</b>
            <span class="muted">${esc(n.class || "")}</span>
            <span class="muted">n ${n.n ?? 0}</span>
            <span class="muted">${esc(dust || held || last || "—")}</span>
          </button>`;
        }).join("")}</div>`
      : `<p class="muted">No gate vetoes scored yet.</p>`;
    detailEl.innerHTML = `
      <div class="card-top">
        <div class="who">
          <span class="ph">${esc(row.glyph || "VT")}</span>
          <div>
            <h3>${esc(row.symbol)}</h3>
            <p class="sub">${esc(row.name || "")}</p>
          </div>
        </div>
        <div class="score-badge ${row.ok === true ? "high" : row.ok === false ? "low" : "mid"}">${row.ok === true ? "ok" : row.ok === false ? "!" : "?"}<small>loop</small></div>
      </div>
      <p class="thesis">${esc(ver.summary || row.thesis || "")}</p>
      <div class="stat-grid">
        <div><span>Pass</span><b>${vr.n_gate_pass ?? "—"}</b></div>
        <div><span>Veto</span><b>${vr.n_gate_veto ?? "—"}</b></div>
        <div><span>Watch</span><b>${(ver.watch || []).length}</b></div>
        <div><span>Hijack</span><b class="${ver.hijack_keep ? "up" : "down"}">${ver.hijack_keep ? "keep" : "review"}</b></div>
      </div>
      <p class="section-label">Needles</p>
      ${list}
      <p class="section-label">Mission</p>
      <p class="muted">Feedback loop only — never auto-promotes a hard veto into a buy.</p>
    `;
    return;
  }
  detailEl.innerHTML = `
    <div class="card-top">
      <div class="who">
        <span class="ph">${esc(row.glyph || "?")}</span>
        <div>
          <h3>${esc(row.symbol)}</h3>
          <p class="sub">${esc(row.name || "")}</p>
        </div>
      </div>
      <div class="score-badge ${row.ok === true ? "high" : row.ok === false ? "low" : "mid"}">${row.ok === true ? "ok" : row.ok === false ? "!" : "?"}<small>loop</small></div>
    </div>
    <p class="thesis">${esc(row.thesis || "")}</p>
    <div class="stat-grid">
      <div><span>Pulse</span><b>${esc(row.pulse || "—")}</b></div>
      <div><span>Age</span><b>${esc(row.age || "—")}</b></div>
      <div><span>n</span><b>${row.n != null ? row.n : "—"}</b></div>
      <div><span>Signal</span><b>${esc(row.signal || "—")}</b></div>
    </div>
    <p class="section-label">Mission</p>
    <p class="muted">Optimize learning-loop speed and honesty. Short-list forward P&amp;L is the secondary bar. Hunt flood stays a sensor.</p>
  `;
}

// ---------------------------------------------------------------------------
// Board: honest calibration from the decision ledger
// ---------------------------------------------------------------------------

function binBounds(b) {
  const m = String(b.bin || "").match(/([\d.]+)-([\d.]+)/);
  const lo = m ? Number(m[1]) : Number(b.lo || 0);
  const hi = m ? Number(m[2]) : Number(b.hi || lo + 0.1);
  return { lo, hi };
}

function boardRows() {
  const cal = boardData.calibration;
  if (!cal || !Array.isArray(cal.bins)) return [];
  return cal.bins.map((b) => {
    const { lo, hi } = binBounds(b);
    return { ...b, lo, hi, mint: `bin-${b.bin || lo}`, symbol: `${Math.round(lo * 100)}–${Math.round(hi * 100)}` };
  }).sort((a, b) => b.lo - a.lo);
}

function renderBoardRows(items) {
  if (!boardData.calibration) {
    rowsEl.innerHTML = `<p class="tape-note">Loading the honest board…</p>`;
    return;
  }
  if (!items.length) {
    rowsEl.innerHTML = `<p class="tape-note">No resolved decisions yet on this chain. The board fills 24h after the first frozen Entry decisions.</p>`;
    return;
  }
  rowsEl.innerHTML = items.map((b) => {
    const n = Number(b.n || 0);
    const pred = b.predicted != null ? Number(b.predicted) : (b.lo + b.hi) / 2;
    const hit2 = b.hit2x != null ? Number(b.hit2x) : null;
    const hit5 = b.hit5x != null ? Number(b.hit5x) : null;
    const dead = b.dead != null ? Number(b.dead) : null;
    const gap = hit2 != null && n >= 8 ? hit2 - pred : null;
    const gapCls = gap == null ? "" : gap >= -0.1 ? "high" : gap >= -0.3 ? "mid" : "veto";
    return `<button type="button" class="tape-row${n < 8 ? " thin" : ""}${b.mint === selected ? " on" : ""}" data-bin="${esc(b.mint)}">
      <span class="tok"><span class="ph">${esc(String(Math.round(b.lo * 100)))}</span><span><b>Entry ${esc(b.symbol)}</b><small>${n} resolved${b.median_multiple ? ` · med ${Number(b.median_multiple).toFixed(2)}×` : ""}${b.unfilled ? ` · ${Math.round(Number(b.unfilled) * 100)}% no fill` : ""}${b.unobserved ? ` · ${Math.round(Number(b.unobserved) * 100)}% no tape` : ""}${b.under_half != null && !b.unobserved ? ` · ${Math.round(Number(b.under_half) * 100)}% under ½` : ""}</small></span></span>
      <span class="num">${n || "—"}</span>
      <span class="num">${Math.round(pred * 100)}%</span>
      <span class="sc-pill ${gapCls}" title="${gap == null ? "Too few resolved to judge" : `Hit 2× ${Math.round(hit2 * 100)}% vs predicted ${Math.round(pred * 100)}%`}">${hit2 == null ? "—" : Math.round(hit2 * 100) + "%"}</span>
      <span class="num">${hit5 == null ? "—" : Math.round(hit5 * 100) + "%"}</span>
      <span class="num ${dead != null && dead > 0.5 ? "down" : ""}">${dead == null ? "—" : Math.round(dead * 100) + "%"}</span>
    </button>`;
  }).join("");
  rowsEl.querySelectorAll("[data-bin]").forEach((btn) => {
    btn.onclick = () => { selected = btn.dataset.bin; renderTape(); renderBoardDetail(btn.dataset.bin); };
  });
}

function renderBoardPane() {
  const wk = boardData.weekly;
  const art = boardData.artifacts;
  const cal = boardData.calibration;
  const chainLabel = CHAIN === "robinhood" ? "Robinhood" : "Solana";
  chartHead.innerHTML = `<div class="picked"><span class="ph">B</span><div>
    <h2>Honest board <small class="muted">${esc(chainLabel)}</small></h2>
    <div class="meta"><span>frozen Entry</span><span>2× from the first fillable print</span><span>${esc(String(wk?.horizon_hours || 24))}h forward</span><span>backfill excluded</span></div>
  </div></div>`;
  let html = "";
  if (wk && Array.isArray(wk.weeks)) {
    const L = wk.lines || chainLines();
    const LL = wk.legacy_lines || LEGACY_LINES;
    const slots = [["hi", L.hi], ["lo", L.lo]];
    const mixed = L.scorer !== "legacy";
    html += `<h4>By week · desk lines</h4><p class="board-sub">Each cell is hit-2× / resolved for decisions at or above the line that week, each decision read against the lines of the model that wrote it${mixed ? ` (first-sight ${linePct(L.lo)} / ${linePct(L.hi)}; legacy weeks ${linePct(LL.lo)} / ${linePct(LL.hi)})` : ""}. A line is honest when its hit rate is at or above the line itself: a ${linePct(L.hi)} line should hit 2× about ${linePct(L.hi)}% of the time.</p>`;
    html += `<table class="board"><thead><tr><th>Week</th><th>Decided</th>${slots.map(([s, thr]) => `<th>≥${linePct(thr)} (${lineName(L, s)}) hit 2×</th>`).join("")}<th>≥${linePct(L.hi)} med ×</th></tr></thead><tbody>`;
    for (const row of wk.weeks.slice().reverse()) {
      const sc = row.scorers || {};
      const mix = mixed && sc.legacy && sc.first_sight ? ` <small class="muted">${sc.first_sight} fs · ${sc.legacy} legacy</small>` : mixed && sc.legacy && !sc.first_sight ? ` <small class="muted">legacy</small>` : "";
      html += `<tr><td>${esc(row.week)}${mix}</td><td>${row.decisions}</td>`;
      for (const [s, thr] of slots) {
        const c = (row.lines || {})[s] || {};
        const r = Number(c.resolved || 0);
        const rate = c.hit2x_rate;
        const cls = r < 5 || rate == null ? "" : rate >= Number(thr) ? "gap-ok" : rate < Number(thr) * 0.6 ? "gap-bad" : "";
        html += `<td class="${cls}">${r ? `${Math.round(Number(rate || 0) * 100)}% <small class="muted">/${r}</small>` : "—"}</td>`;
      }
      const chi = (row.lines || {}).hi || {};
      html += `<td>${chi.median_multiple != null ? Number(chi.median_multiple).toFixed(2) + "×" : "—"}</td></tr>`;
    }
    html += `</tbody></table>`;
  }
  const fs = boardData.firstSight;
  if (fs) {
    const promoted = (fs.artifacts || []).find((a) => a.promoted) || null;
    const latest = (fs.artifacts || [])[0] || null;
    const show = promoted || latest;
    const nFeat = (fs.features || []).length;
    const rhShadow = fs.shadow && !fs.scores_entry;
    html += `<h4>First-sight model${fs.scores_entry ? " · scores Entry" : " · shadow"}</h4><p class="board-sub">${esc(fs.label || "")}. Inputs are the columns frozen at first sight (t0 tape, timing${rhShadow ? "" : ", holders, creator"}, socials)${nFeat ? ` — ${nFeat} columns` : ""}, so every judged decision trains it — no repaired feature vector. ${fs.scores_entry ? (promoted ? "Entry on this chain is this probability." : "Entry falls back to the legacy blend until a fit is promoted.") : rhShadow ? "Fitted in shadow on this chain: holder and concentration columns are rewritten by the 2-minute refresh, so they are left out; Entry stays the legacy blend until this fit is switched on." : "Not applied on this chain."}</p>`;
    if (show) {
      const unk = show.holders_unknown_valid;
      const oldLabel = Number(show.label_version || 1) < 2;
      const unf = show.unfilled_valid;
      html += `<div class="model-card">
        <div><span>Version</span><b class="${show.promoted ? "up" : ""}">v${show.version}${show.promoted ? " · promoted" : " · not promoted"}</b></div>
        <div><span>Rows</span><b>${show.n_train} / ${show.n_valid}<small class="muted"> · ${show.live_rows ?? 0} live${unk != null ? ` · ${Math.round(unk * 100)}% no holder count` : ""}</small></b></div>
        <div title="Label 2 judges 2× from the first fillable print, the way Paper buys. Label 1 judged curve entries from the migration t0 and paid a token for graduating."><span>Label</span><b class="${oldLabel ? "down" : ""}">${oldLabel ? "sight print (v1)" : "first fillable print"}${unf != null ? `<small class="muted"> · ${Math.round(unf * 100)}% never fillable</small>` : ""}</b></div>
        <div><span>AUC</span><b>${show.auc ?? "—"}<small class="muted"> vs Entry ${show.baseline_auc ?? "—"}</small></b></div>
        <div><span>Top decile 2×</span><b>${show.precision_top_decile != null ? Math.round(show.precision_top_decile * 100) + "%" : "—"}<small class="muted"> vs Entry ${show.baseline_top_decile != null ? Math.round(show.baseline_top_decile * 100) + "%" : "—"}</small></b></div>
        <div><span>Top decile 5×</span><b>${show.hit5x_top_decile != null ? Math.round(show.hit5x_top_decile * 100) + "%" : "—"}</b></div>
        <div><span>Brier</span><b>${show.brier ?? "—"}<small class="muted"> vs ${show.incumbent_brier ?? "—"}</small></b></div>
      </div>
      <p class="board-sub">${esc(show.reason || "")}${show.created_at ? ` · ${esc(new Date(show.created_at).toLocaleString())}` : ""}</p>`;
      const ln = show.lines || {};
      if (ln.hi || ln.lo) {
        const DL = fs.desk_lines || FIRST_SIGHT_LINES;
        const cell = (slot, c) => c ? `<div><span>≥${linePct(c.threshold)} · ${lineName(DL, slot)}</span><b>${c.hit2x != null ? Math.round(c.hit2x * 100) + "% 2×" : "—"}<small class="muted"> · ${c.hit5x != null ? Math.round(c.hit5x * 100) + "% 5×" : "—"} · ${c.per_day ?? "—"}/day · ${c.share != null ? Math.round(c.share * 100) + "% of launches" : ""}</small></b></div>` : "";
        html += `<h4>Desk lines on this scale</h4><p class="board-sub">What the two lines did on the validation tail of the promoted fit. These are the thresholds Hunt colours, the paper ledger fills and tickets write against on this chain${fs.scores_entry && promoted ? "" : " once a fit is promoted"}.</p>
        <div class="model-card">${cell("hi", ln.hi)}${cell("lo", ln.lo)}</div>`;
      }
    } else {
      html += `<p class="board-sub">No fit yet — needs 2,000 judged decisions and a 7-day validation tail.</p>`;
    }
  }
  const psc = (boardData.scorecard || {}).scorecard;
  if (psc) {
    const tw = psc.this_window || {};
    const lc = psc.leftover_clock || {};
    const nr = psc.no_run || {};
    const op = psc.open || {};
    const fmtAvg = (row) => row.avg_return_pct != null ? `${row.avg_return_pct}%` : "—";
    html += `<h4>Paper scorecard · this-window vs leftover-clock</h4><p class="board-sub">This-window is live dump + ride. Leftover-clock is the 24h hold timer. No-run is a never-1.5× grave after 1h. Opens split by buy line vs watch. Do not mix the avgs.</p>
      <div class="model-card">
        <div><span>This-window</span><b>${fmtAvg(tw)}<small class="muted"> · ${tw.n || 0} closed · ${tw.wins || 0} wins</small></b></div>
        <div><span>Leftover-clock</span><b>${fmtAvg(lc)}<small class="muted"> · ${lc.n || 0} closed · ${lc.wins || 0} wins</small></b></div>
        <div><span>No-run</span><b>${fmtAvg(nr)}<small class="muted"> · ${nr.n || 0} closed · ${nr.wins || 0} wins</small></b></div>
        <div><span>Open</span><b>${op.n || 0}<small class="muted"> · ${op.hi_n || 0} buy · ${op.watch_n || 0} watch</small></b></div>
      </div>`;
    const byLine = psc.this_window_by_line || {};
    const lineEv = (name, row) => `<div><span>${name}</span><b>${fmtAvg(row || {})}<small class="muted"> · ${(row || {}).n || 0} acted · ${(row || {}).wins || 0} wins</small></b></div>`;
    html += `<h4>This-window EV by line</h4><p class="board-sub">Live dump and ride only, split by buy line vs watch. Graves stay in no-run and the 24h clock. The lines do not move. This is not a training label.</p>
      <div class="model-card">${lineEv("Buy line", byLine.hi)}${lineEv("Watch", byLine.watch)}</div>`;
    const v1 = psc.paper_v1;
    if (v1 && v1.cap) {
      const today = v1.today || {};
      const nameRows = today.names || [];
      const nameList = nameRows.length
        ? `<div class="v1-list">${nameRows.slice(0, 8).map((row) => {
            const tags = (row.tags || []).map((t) => `<span class="v1-tag">${esc(t)}</span>`).join("");
            const ts = row.thesis_score != null ? Number(row.thesis_score).toFixed(2) : "—";
            return `<button type="button" class="v1-row" data-mint="${esc(row.mint || "")}" title="Paper short list · thesis ${ts}">
              <b>${esc(row.symbol || row.mint || "?")}</b>
              <span class="muted">${esc(row.status || "")}</span>
              <span class="muted">E ${row.entry_p != null ? Math.round(row.entry_p * 100) : "—"}</span>
              <span class="muted">thesis ${ts}</span>
              ${tags}
            </button>`;
          }).join("")}</div>`
        : `<div class="muted">No short-list names today yet.</div>`;
      html += `<h4>paperV1 · daily short list (paper only)</h4><p class="board-sub">${esc(v1.rule || "")}</p>
        <div class="model-card">
          <div><span>Today</span><b>${today.taken_chain != null ? today.taken_chain : (today.taken || 0)}/${v1.cap_per_chain || 3}<small class="muted"> · total ${today.taken || 0}/${v1.cap} · ${today.locked ? "locked" : "open until 23:00 UTC"} · ${today.queued || 0} queued</small></b></div>
          <div><span>Short-list 2×</span><b>${v1.hit2x != null ? Math.round(Number(v1.hit2x) * 100) + "%" : "—"}<small class="muted"> · ${v1.hit2x_n || 0}/${v1.closed || 0}</small></b></div>
          <div><span>Short-list 5×</span><b>${v1.hit5x != null ? Math.round(Number(v1.hit5x) * 100) + "%" : "—"}<small class="muted"> · ${v1.hit5x_n || 0}</small></b></div>
          <div><span>Closed avg</span><b>${v1.avg_return_pct != null ? v1.avg_return_pct + "%" : "—"}<small class="muted"> · ${v1.wins || 0} wins · ${v1.open || 0} open · ${v1.skipped || 0} skipped</small></b></div>
        </div>
        ${nameList}`;
    }
    const sh = psc.shadow_late || {};
    if (sh.n) {
      const vetoes = sh.by_veto || {};
      const vetoCell = (name, row) => `<div><span>${esc(name)}</span><b>${fmtAvg(row || {})}<small class="muted"> · ${(row || {}).n || 0} · ${(row || {}).wins || 0} hit 2×</small></b></div>`;
      html += `<h4>Shadow late book</h4><p class="board-sub">Pre-pumped, start-high, and late chase, scored with the same moonbag. Not a fill. The live book does not include these.</p>
        <div class="model-card"><div><span>All</span><b>${fmtAvg(sh)}<small class="muted"> · ${sh.n} closed · ${sh.open || 0} open</small></b></div>${Object.keys(vetoes).map((k) => vetoCell(k, vetoes[k])).join("")}</div>`;
    }
    const buckets = psc.buckets || [];
    if (buckets.length) {
      html += `<h4>First-sight paper buckets</h4><p class="board-sub">Watch is the line below buy. RH watch fills only when Live ≥ 0.50. Sol watch also fills a fresh fat book when the holder count is known.</p>
        <div class="model-card">${buckets.map((b) => `<div><span>${esc(b.bin)} · ${linePct(b.lo)}–${b.bin === "hi" ? "+" : linePct(b.hi)}</span><b>${b.n || 0}<small class="muted"> · avg ${b.avg_return_pct != null ? b.avg_return_pct + "%" : "—"} · ${b.wins || 0} wins</small></b></div>`).join("")}</div>`;
    }
  }
  if (art) {
    const promoted = (art.artifacts || []).find((a) => a.promoted) || null;
    const latest = (art.artifacts || [])[0] || null;
    html += `<h4>Batch fit · entry model</h4><p class="board-sub">${esc(art.label || "")}${promoted ? "" : " · no promoted fit — the online model and rules score the desk"}. Fits train only on live decisions with the feature vector frozen at first sight; a fit that trained on seeded rows is demoted.</p>`;
    const show = promoted || latest;
    if (show) {
      html += `<div class="model-card">
        <div><span>Version</span><b class="${show.promoted ? "up" : ""}">v${show.version}${show.promoted ? " · promoted" : " · not promoted"}</b></div>
        <div><span>Rows</span><b>${show.n_train} / ${show.n_valid}</b></div>
        <div><span>Brier</span><b>${show.brier ?? "—"}<small class="muted"> vs ${show.incumbent_brier ?? "—"}</small></b></div>
        <div><span>AUC</span><b>${show.auc ?? "—"}<small class="muted"> vs ${show.incumbent_auc ?? "—"}</small></b></div>
        <div><span>Hit 2× at 90</span><b>${show.hit2x_at_90 != null ? Math.round(show.hit2x_at_90 * 100) + "%" : "—"}<small class="muted"> /${show.n_at_90 ?? 0}</small></b></div>
        <div><span>Frozen rows</span><b class="${show.live_only ? "up" : "down"}">${show.live_only ? "yes" : "no · seed"}</b></div>
      </div>
      <p class="board-sub">${esc(show.reason || "")}${show.created_at ? ` · ${esc(new Date(show.created_at).toLocaleString())}` : ""}</p>`;
    } else {
      html += `<p class="board-sub">No fit yet — needs 400 resolved live decisions.</p>`;
    }
  }
  const lv = boardData.live;
  if (lv) {
    const promoted = (lv.artifacts || []).find((a) => a.promoted) || null;
    const latest = (lv.artifacts || [])[0] || null;
    const show = promoted || latest;
    const s = lv.samples || {};
    const bySrc = s.resolved_by_source || {};
    const srcText = Object.keys(bySrc).length ? ` (${Object.entries(bySrc).map(([k, v]) => `${v} ${k}`).join(", ")})` : "";
    html += `<h4>Live model · t+15 print${promoted ? " · promoted" : " · exit test"}</h4><p class="board-sub">${esc(lv.label || "")}. The second look: after the Entry decision, does the tape 15 minutes in say the book is still alive? Samples come from our one-minute bars where the Hunt tape reached the mint, else from the t15m snapshot every non-backfill token gets. ${promoted ? "Exposed as live_model_p." : "Not applied to the desk until a fit beats Entry alone."}</p>
      <div class="model-card">
        <div><span>Samples</span><b>${s.samples ?? "—"}<small class="muted"> · ${s.resolved ?? 0} judged${srcText}</small></b></div>
        <div><span>Positives</span><b>${s.positives ?? "—"}<small class="muted"> · ${s.resolved ? Math.round(((s.positives || 0) / s.resolved) * 100) + "% of judged" : "—"}</small></b></div>
        ${show ? `<div><span>Version</span><b class="${show.promoted ? "up" : ""}">v${show.version}${show.promoted ? " · promoted" : " · not promoted"}</b></div>
        <div><span>AUC</span><b>${show.auc ?? "—"}<small class="muted"> vs Entry ${show.incumbent_auc ?? "—"}</small></b></div>
        <div><span>Top decile 2×</span><b>${pct(show.precision_top_decile)}<small class="muted"> vs Entry ${pct(show.baseline_top_decile)}</small></b></div>
        <div><span>Top decile ran</span><b>${pct(show.hit5x_top_decile)}<small class="muted"> vs ${pct(show.runner_base_rate)} of the slice</small></b></div>` : `<div><span>Fit</span><b>—<small class="muted"> · needs 300 judged samples</small></b></div>`}
      </div>${show && show.reason ? `<p class="board-sub">${esc(show.reason)}${show.created_at ? ` · ${esc(new Date(show.created_at).toLocaleString())}` : ""}</p>` : ""}`;
  }
  const rn = boardData.runner;
  const rnShow = rn && (rn.artifacts || [])[0];
  if (rnShow) {
    html += `<h4>Runner head · shadow</h4><p class="board-sub">${esc(rn.label || "")}. Same t+15 rows and inputs as the Live fit, asked a different question: did it run. Never promoted; paper and the Hunt card do not read it. The 2× column is the promoted Live head ranked against the runner label, so this card says whether a runner needs its own model.</p>
      <div class="model-card">
        <div><span>Version</span><b>v${rnShow.version}<small class="muted"> · shadow · ${rnShow.n_train ?? "—"} train / ${rnShow.n_valid ?? "—"} valid</small></b></div>
        <div><span>AUC</span><b>${rnShow.auc ?? "—"}<small class="muted"> vs 2× head ${rnShow.live_2x_auc ?? "—"} · Entry ${rnShow.baseline_auc ?? "—"}</small></b></div>
        <div><span>Top decile ran</span><b>${pct(rnShow.precision_top_decile)}<small class="muted"> vs 2× head ${pct(rnShow.live_2x_top_decile)} · Entry ${pct(rnShow.baseline_top_decile)} · base ${pct(rnShow.base_rate)}</small></b></div>
        <div><span>≥ 0.20</span><b>${pct(rnShow.share_ge_020)}<small class="muted"> of the slice · ${pct(rnShow.hit_ge_020)} ran</small></b></div>
      </div>${rnShow.created_at ? `<p class="board-sub">${esc(new Date(rnShow.created_at).toLocaleString())}</p>` : ""}`;
  }
  if (cal && cal.note) html += `<p class="board-sub">${esc(cal.note)}</p>`;
  boardPane.innerHTML = html || `<p class="board-sub">Loading…</p>`;
  boardPane.querySelectorAll(".v1-row[data-mint]").forEach((btn) => {
    btn.onclick = () => {
      if (btn.dataset.mint) openDetail(btn.dataset.mint);
    };
  });
}

function renderBoardDetail(binId) {
  const rows = boardRows();
  const b = rows.find((r) => r.mint === binId) || rows[rows.length - 1];
  if (!b) {
    detailEl.innerHTML = `<p class="muted">The board reads the decision ledger: every first-sight Entry score is frozen when written, then judged against the token's own forward tape. Nothing here is recomputed from repaired rows.</p>`;
    return;
  }
  const n = Number(b.n || 0);
  const pred = b.predicted != null ? Number(b.predicted) : (b.lo + b.hi) / 2;
  const hit2 = Number(b.hit2x || 0);
  const gap = hit2 - pred;
  detailEl.innerHTML = `
    <div class="card-top">
      <div class="who"><span class="ph">${esc(String(Math.round(b.lo * 100)))}</span><div><h3>Entry ${esc(b.symbol)}</h3><p class="sub">${n} resolved decisions</p></div></div>
      <div class="score-badge ${gap >= -0.1 ? "high" : gap >= -0.3 ? "mid" : "low"}">${Math.round(hit2 * 100)}<small>hit 2×</small></div>
    </div>
    <p class="thesis">${n < 8
      ? "Too few resolved decisions in this bin to judge the score."
      : gap >= -0.1
        ? `This bin is honest: it promised ~${Math.round(pred * 100)}% and delivered ${Math.round(hit2 * 100)}%.`
        : `This bin over-promises: ~${Math.round(pred * 100)}% predicted, ${Math.round(hit2 * 100)}% delivered. Paper reads Entry, so this is where paper loses.`}</p>
    <div class="stat-grid">
      <div><span>Predicted</span><b>${Math.round(pred * 100)}%</b></div>
      <div><span>Hit 2×</span><b class="${gap >= -0.1 ? "up" : "down"}">${Math.round(hit2 * 100)}%</b></div>
      <div><span>Hit 5×</span><b>${b.hit5x != null ? Math.round(Number(b.hit5x) * 100) + "%" : "—"}</b></div>
      <div><span>Dead pool</span><b>${b.dead != null ? Math.round(Number(b.dead) * 100) + "%" : "—"}</b></div>
      <div><span>Median ×</span><b>${b.median_multiple ? Number(b.median_multiple).toFixed(2) + "×" : "—"}</b></div>
      <div><span>Under ½</span><b class="${Number(b.under_half || 0) > 0.3 ? "down" : ""}">${b.under_half != null ? Math.round(Number(b.under_half) * 100) + "%" : "—"}</b></div>
      <div title="Never printed again after entry — nobody traded it, so there was no exit. Counted as a loss."><span>No tape</span><b class="${Number(b.unobserved || 0) > 0.3 ? "down" : ""}">${b.unobserved != null ? Math.round(Number(b.unobserved) * 100) + "%" : "—"}</b></div>
      <div title="Scored on the curve or a thin book and never printed on a sellable pool inside 24h — nothing to buy. Counted as a loss."><span>No fill</span><b class="${Number(b.unfilled || 0) > 0.3 ? "down" : ""}">${b.unfilled != null ? Math.round(Number(b.unfilled) * 100) + "%" : "—"}</b></div>
      <div title="Judged from a later pool print rather than the sight print (curve and thin-book entries)"><span>Filled later</span><b>${b.filled_later != null ? Math.round(Number(b.filled_later) * 100) + "%" : "—"}</b></div>
      <div title="Hit 2× among the decisions a market actually showed us afterwards"><span>Hit 2× · with tape</span><b>${b.hit2x_observed != null ? Math.round(Number(b.hit2x_observed) * 100) + "%" : "—"}<small class="muted"> /${b.n_observed ?? 0}</small></b></div>
    </div>
    <p class="muted" style="font-size:0.78rem">Bins are frozen Entry at first sight. A hit is a 2× / 5× print from the decision's own entry mcap within 24h that we actually recorded on a pool with sellable liquidity, with a live pool at judgement; a bonding-curve entry is re-based to its first pool print; a name that never printed again is a loss.</p>`;
}

// ---------------------------------------------------------------------------
// Tape strip: our own one-minute bars above the Dex embed
// ---------------------------------------------------------------------------

function stripPoints(data) {
  const pts = [];
  for (const s of data.snaps || []) {
    const t = new Date(s.t).getTime();
    if (t > 0 && Number(s.mcap) > 0) pts.push({ t, v: Number(s.mcap), kind: s.kind || "snap" });
  }
  for (const b of data.bars || []) {
    const t = new Date(b.t).getTime();
    if (t > 0 && Number(b.mcap) > 0) pts.push({ t, v: Number(b.mcap), kind: "bar", holders: b.holders, liq: b.liq });
  }
  pts.sort((a, b) => a.t - b.t);
  const out = [];
  for (const p of pts) {
    const prev = out[out.length - 1];
    if (prev && Math.abs(prev.t - p.t) < 20000) {
      if (p.kind === "bar") out[out.length - 1] = p;
      continue;
    }
    out.push(p);
  }
  return out;
}

function drawTapeStrip(data, t) {
  if (!tapeStrip) return;
  const pts = stripPoints(data);
  const t0 = Number(data.t0_mcap || 0);
  if (pts.length < 2) {
    tapeStrip.hidden = false;
    tapeStrip.innerHTML = `<div class="strip-legend"><span class="tag">Our tape</span></div><div class="strip-empty">${pts.length ? "One print so far — the strip draws from the second minute." : "No prints stored yet for this book."}</div>`;
    return;
  }
  const w = 900;
  const h = 118;
  const padL = 12;
  const padR = 60;
  const padT = 26;
  const padB = 12;
  const vals = pts.map((p) => p.v);
  let lo = Math.min(...vals);
  let hi = Math.max(...vals);
  if (t0 > 0) { lo = Math.min(lo, t0); hi = Math.max(hi, t0); }
  const two = t0 > 0 ? t0 * 2 : 0;
  if (two && two <= hi * 1.35) hi = Math.max(hi, two);
  const span = Math.max(hi - lo, Math.max(hi, 1) * 0.08);
  const ta = pts[0].t;
  const tb = pts[pts.length - 1].t;
  const dt = Math.max(tb - ta, 60000);
  const X = (ms) => padL + ((ms - ta) / dt) * (w - padL - padR);
  const Y = (v) => h - padB - ((v - lo) / span) * (h - padT - padB);
  const coords = pts.map((p) => `${X(p.t).toFixed(1)},${Y(p.v).toFixed(1)}`);
  const last = pts[pts.length - 1];
  const first = pts[0];
  const up = last.v >= (t0 || first.v);
  const color = up ? "#3ee0a0" : "#ff7a72";
  const bars = pts.filter((p) => p.kind === "bar").length;
  const mult = t0 > 0 ? last.v / t0 : 0;
  const peak = Math.max(...vals);
  const hold = pts.filter((p) => p.holders != null && p.holders > 0);
  const holderLine = hold.length >= 2
    ? (() => {
      const hs = hold.map((p) => p.holders);
      const hlo = Math.min(...hs);
      const hhi = Math.max(...hs);
      const hspan = Math.max(hhi - hlo, 1);
      const hy = (v) => h - padB - ((v - hlo) / hspan) * (h - padT - padB) * 0.55;
      return `<polyline fill="none" stroke="#c4b5fd" stroke-width="1.2" stroke-dasharray="2 3" opacity="0.8" points="${hold.map((p) => `${X(p.t).toFixed(1)},${hy(p.holders).toFixed(1)}`).join(" ")}" />`;
    })()
    : "";
  const guide = (v, label, cls) => {
    if (!(v > 0) || v < lo || v > hi) return "";
    const y = Y(v).toFixed(1);
    return `<line x1="${padL}" x2="${w - padR}" y1="${y}" y2="${y}" stroke="${cls}" stroke-width="1" stroke-dasharray="4 4" opacity="0.7" />
      <text x="${w - padR + 6}" y="${(Number(y) + 3.5).toFixed(1)}" fill="${cls}" font-size="10" font-family="ui-monospace, Menlo, monospace">${label}</text>`;
  };
  const marks = pts.filter((p) => p.kind !== "bar" && p.kind !== "snap").slice(0, 6).map((p) => `<circle cx="${X(p.t).toFixed(1)}" cy="${Y(p.v).toFixed(1)}" r="2.4" fill="#8ab4ff"><title>${esc(p.kind)} · ${fmtMcap(p.v)}</title></circle>`).join("");
  tapeStrip.hidden = false;
  tapeStrip.innerHTML = `<div class="strip-legend">
      <span class="tag">Our tape</span>
      <span>now <b>${fmtMcap(last.v)}</b></span>
      ${t0 > 0 ? `<span>t0 <b>${fmtMcap(t0)}</b></span>` : ""}
      <span>peak <b>${fmtMcap(peak)}</b></span>
      ${mult ? `<span class="${multipleClass(mult)}">${mult.toFixed(2)}×</span>` : ""}
      <span>${bars ? `${bars} min bars` : "snapshots only"}${hold.length >= 2 ? ` · holders ${hold[0].holders}→${hold[hold.length - 1].holders}` : ""}</span>
      <span>${fmtAge((Date.now() - ta) / 60000)} span</span>
    </div>
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Our recorded tape for ${esc(tapeTicker(t || {}))}">
      <defs><linearGradient id="stripFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${color}" stop-opacity="0.28"/><stop offset="100%" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
      <path d="M${coords[0]} L${coords.join(" L")} L${X(last.t).toFixed(1)},${h - padB} L${X(first.t).toFixed(1)},${h - padB} Z" fill="url(#stripFill)" />
      ${guide(t0, "t0", "#8b90a0")}
      ${guide(two, "2×", "#3ee0a0")}
      ${holderLine}
      <polyline fill="none" stroke="${color}" stroke-width="1.8" stroke-linejoin="round" points="${coords.join(" ")}" />
      ${marks}
      <circle cx="${X(last.t).toFixed(1)}" cy="${Y(last.v).toFixed(1)}" r="3" fill="${color}" />
    </svg>`;
}

async function loadTapeStrip(t) {
  const mint = t?.mint || "";
  if (!tapeStrip || !mint || t.wallet_only || deskTab === "board") {
    if (tapeStrip) tapeStrip.hidden = true;
    return;
  }
  const hit = stripCache.get(mint);
  if (hit && Date.now() - hit.at < 45000) {
    drawTapeStrip(hit.data, t);
    return;
  }
  if (!hit) {
    tapeStrip.hidden = false;
    tapeStrip.innerHTML = `<div class="strip-legend"><span class="tag">Our tape</span><span class="muted">loading…</span></div>`;
  }
  try {
    const data = await j(`/api/tokens/${encodeURIComponent(mint)}/tape`, { timeoutMs: 8000 });
    stripCache.set(mint, { at: Date.now(), data });
    if (stripCache.size > 12) stripCache.delete(stripCache.keys().next().value);
    if (selected === mint) drawTapeStrip(data, t);
  } catch (e) {
    if (selected === mint && !hit) tapeStrip.hidden = true;
  }
}

// ---------------------------------------------------------------------------
// Ledger block on the research card
// ---------------------------------------------------------------------------

function fmtWhen(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const mins = (Date.now() - d.getTime()) / 60000;
  return `${fmtAge(mins)} ago`;
}

function ledgerBlock(t) {
  const L = t.ledger;
  if (!L || (!L.entry && !L.gate && !L.fill && !L.ticket)) return "";
  const rows = [];
  if (L.entry) {
    const now = entryPct(t);
    const frozen = Number(L.entry.entry_p || 0) * 100;
    const drift = now && frozen ? now - frozen : 0;
    rows.push(`<div class="row"><span>Entry decision</span><b>${frozen.toFixed(0)} @ ${fmtMcap(L.entry.entry_mcap)} · ${fmtWhen(L.entry.at)}</b></div>`);
    if (Math.abs(drift) >= 3) {
      rows.push(`<div class="row"><span>Card now</span><b class="${drift < 0 ? "down" : "warn"}">${now.toFixed(0)} (${drift > 0 ? "+" : ""}${drift.toFixed(0)} after entry)</b></div>`);
    }
    const r = L.entry.result;
    const ageH = L.entry.at ? (Date.now() - new Date(L.entry.at).getTime()) / 3600000 : 0;
    const settled = ageH >= 24;
    if (r) {
      const cls = r.hit2x ? "up" : r.dead ? "down" : "";
      rows.push(`<div class="row"><span>${settled ? "Judged" : "So far"}</span><b class="${cls}">${r.hit5x ? "5× hit" : r.hit2x ? "2× hit" : r.dead ? "dead pool" : "no 2×"} · ${Number(r.multiple || 0).toFixed(2)}×${r.entry_rebased ? ` · judged from the ${r.fill_mcap ? "$" + Math.round(Number(r.fill_mcap) / 1000) + "k " : ""}fill${r.fill_multiple ? ` (${Number(r.fill_multiple).toFixed(1)}× the sight print)` : ""}` : r.unfilled ? " · never fillable" : ""}${settled ? "" : ` · ${fmtAge(ageH * 60)} in`}</b></div>`);
    } else {
      rows.push(`<div class="row"><span>So far</span><b>open · ${Number(t.t0_mcap || 0) > 0 ? "24h forward" : "waiting for a pool"}</b></div>`);
    }
  }
  for (const ln of L.lines || []) {
    const thr = ln.threshold != null ? linePct(ln.threshold) : (ln.line === "line90" ? 90 : 70);
    rows.push(`<div class="row"><span>Crossed ${thr}${ln.scorer === "first_sight" ? ` <small class="muted">${lineName(FIRST_SIGHT_LINES, ln.slot || (ln.line === "line90" ? "hi" : "lo"))}</small>` : ""}</span><b>${fmtWhen(ln.at)}</b></div>`);
  }
  if (L.gate) {
    rows.push(`<div class="row"><span>Gate</span><b class="${L.gate.veto ? "down" : "up"}">${L.gate.veto ? esc(L.gate.veto) : "filled"} · ${fmtWhen(L.gate.at)}</b></div>`);
  }
  if (L.fill) {
    const f = L.fill;
    rows.push(`<div class="row"><span>Paper fill</span><b class="${f.status === "open" ? "warn" : f.return_pct != null && f.return_pct >= 0 ? "up" : "down"}">${esc(f.status)} @ ${fmtMcap(f.entry_mcap)} · ${f.multiple != null ? f.multiple.toFixed(2) + "×" : "—"}${f.return_pct != null ? " · " + fmtPnl(f.return_pct) : ""}${f.exit_reason ? " · " + esc(f.exit_reason) : ""}</b></div>`);
  }
  if (L.ticket) {
    const tk = L.ticket;
    rows.push(`<div class="row"><span>Ticket #${tk.id}</span><b class="${tk.status === "confirmed" ? "up" : tk.status === "skipped" ? "down" : "warn"}">${esc(tk.status)} · $${Math.round(tk.size_usd || 0)} · slip ${Number(tk.slippage_pct || 0).toFixed(1)}% · stop ${Number(tk.stop_mult || 0).toFixed(2)}× · take ${Number(tk.take_mult || 0).toFixed(0)}×</b></div>`);
  }
  const actions = L.ticket && L.ticket.status === "shadow"
    ? `<div class="actions"><button type="button" class="confirm" data-ticket="${L.ticket.id}" data-status="confirmed">Confirm ticket</button><button type="button" class="skip" data-ticket="${L.ticket.id}" data-status="skipped">Skip</button><span class="actions-note">records the answer · no order</span></div>`
    : L.ticket ? `<div class="actions"><button type="button" data-ticket="${L.ticket.id}" data-status="shadow">Reopen as shadow</button></div>` : "";
  return `<p class="section-label">Ledger</p><div class="ledger">${rows.join("")}${actions}</div>`;
}

async function setTicketStatus(id, status) {
  try {
    await j(`/api/tickets/${id}/status?status=${encodeURIComponent(status)}`, { method: "POST", timeoutMs: 8000 });
  } catch (e) {
    return;
  }
  stripCache.delete(selected);
  ticketItems = ticketItems.map((tk) => (String(tk.id) === String(id) ? { ...tk, status } : tk));
  if (lastDetail && lastDetail.ledger && lastDetail.ledger.ticket && String(lastDetail.ledger.ticket.id) === String(id)) {
    lastDetail = { ...lastDetail, ledger: { ...lastDetail.ledger, ticket: { ...lastDetail.ledger.ticket, status } } };
    renderDetail(lastDetail);
  }
  renderTape();
}

function bindTicketActions() {
  detailEl.querySelectorAll("[data-ticket][data-status]").forEach((btn) => {
    btn.onclick = () => { btn.disabled = true; setTicketStatus(btn.dataset.ticket, btn.dataset.status); };
  });
}

function renderChartHead(t) {
  if (!t) {
    chartHead.innerHTML = `<p class="muted">Select a name on the tape.</p>`;
    return;
  }
  const initials = String(tapeTicker(t) || "?").slice(0, 2).toUpperCase();
  const img = t.image_url
    ? `<img src="${esc(t.image_url)}" alt="" />`
    : `<span class="ph">${esc(initials)}</span>`;
  const x = liveMultiple(t);
  const xLinks = xAccountLinks(t);
  chartHead.innerHTML = `<div class="picked">
    ${img}
    <div>
      <h2 class="${t.has_dev_x || t.dev_handle ? "has-dev-x" : ""}">${esc(tapeTicker(t))} <small class="muted">${esc(tapeSub(t))}</small></h2>
      <div class="meta">
        <span>${fmtMcap(liveMcap(t))}</span>
        <span class="${multipleClass(x)}">${x ? x.toFixed(2) + "×" : "—"}</span>
        <span>${fmtAge(ageOf(t))}</span>
        ${t.launchpad ? `<span class="pad">${esc(t.launchpad)}</span>` : ""}
        ${xLinks}
      </div>
    </div>
  </div>`;
}

function hidePairFrames() {
  pairCache.forEach((rec) => rec.frame.classList.remove("on"));
}

function evictPairCache(keepMint) {
  while (pairCache.size > PAIR_CACHE_MAX) {
    const oldest = [...pairCache.keys()].find((mint) => mint !== keepMint && mint !== selected);
    if (!oldest) break;
    const rec = pairCache.get(oldest);
    rec.frame.remove();
    pairCache.delete(oldest);
  }
}

function pairPath(url) {
  try {
    return new URL(url, "https://dexscreener.com").pathname.replace(/\/+$/, "");
  } catch (e) {
    return String(url || "");
  }
}

function kickPairFrame(frame) {
  if (!frame) return;
  const prevW = frame.style.width;
  const prevH = frame.style.height;
  frame.style.width = "99.5%";
  frame.style.height = "99.5%";
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      frame.style.width = prevW || "100%";
      frame.style.height = prevH || "100%";
      try {
        if (frame.contentWindow) frame.contentWindow.dispatchEvent(new Event("resize"));
      } catch (e) {
        /* Dex is cross-origin */
      }
    });
  });
}

function revealPair(mint, frame) {
  if (selected !== mint) return;
  if (pairSkel) pairSkel.classList.remove("on");
  if (pairFallback) pairFallback.classList.remove("on");
  window.clearTimeout(pairHangTimer);
  kickPairFrame(frame);
}

function showPairSpark(t) {
  const pts = snapshotPoints(t);
  if (!pairSkel) return;
  if (!pts.length) {
    pairSkel.innerHTML = "";
    pairSkel.classList.remove("on");
    return;
  }
  drawPairSpark(pts);
  pairSkel.classList.add("on");
}

function drawPairSpark(pts) {
  const w = 800;
  const h = 360;
  const pad = 18;
  const min = Math.min(...pts.map((p) => p.value));
  const max = Math.max(...pts.map((p) => p.value));
  const span = Math.max(max - min, 1);
  const t0 = pts[0].time;
  const t1 = pts[pts.length - 1].time;
  const dt = Math.max(t1 - t0, 1);
  const coords = pts.map((p) => {
    const x = pad + ((p.time - t0) / dt) * (w - 2 * pad);
    const y = h - pad - ((p.value - min) / span) * (h - 2 * pad);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const last = pts[pts.length - 1];
  const first = coords[0];
  const lastPt = coords[coords.length - 1];
  pairSkel.innerHTML = `<svg class="tape-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Pair preview">
    <defs>
      <linearGradient id="tapeFill" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#8ab4ff" stop-opacity="0.34"/>
        <stop offset="100%" stop-color="#8ab4ff" stop-opacity="0"/>
      </linearGradient>
    </defs>
    <path d="M${first} L${coords.join(" L")} L${lastPt.split(",")[0]},${h - pad} L${pad},${h - pad} Z" fill="url(#tapeFill)" />
    <polyline fill="none" stroke="#8ab4ff" stroke-width="2.6" points="${coords.join(" ")}" />
  </svg>
  <div class="chart-fallback on">${fmtMcap(last.value)} loading pair…</div>`;
}

function armPairHang(mint, dex) {
  if (pairFallback) pairFallback.classList.remove("on");
  if (pairDex) {
    pairDex.href = dex || "#";
    pairDex.style.display = dex ? "" : "none";
  }
  window.clearTimeout(pairHangTimer);
  pairHangTimer = window.setTimeout(() => {
    if (selected !== mint) return;
    if (pairFallback) pairFallback.classList.add("on");
  }, 4000);
}

function mountPairFrame(mint, src, { show = true } = {}) {
  let hit = pairCache.get(mint);
  if (hit && pairPath(hit.src) !== pairPath(src)) {
    hit.frame.remove();
    pairCache.delete(mint);
    hit = null;
  }
  if (!hit) {
    const frame = document.createElement("iframe");
    frame.title = "DexScreener pair chart";
    frame.allow = "clipboard-write";
    frame.referrerPolicy = "no-referrer-when-downgrade";
    if (show) frame.classList.add("on");
    const rec = { src, frame, loaded: false, born: Date.now(), seen: Boolean(show) };
    pairHost.appendChild(frame);
    frame.addEventListener("load", () => {
      try {
        const loc = frame.contentWindow && frame.contentWindow.location && frame.contentWindow.location.href;
        if (!loc || loc === "about:blank") return;
      } catch (e) {
        /* cross-origin Dex document — real pair page */
      }
      rec.loaded = true;
      if (rec.seen) revealPair(mint, frame);
    });
    frame.src = src;
    pairCache.set(mint, rec);
    evictPairCache(mint);
    hit = rec;
  }
  if (show) {
    hidePairFrames();
    const firstShow = !hit.seen;
    hit.frame.classList.add("on");
    hit.seen = true;
    if (hit.loaded) {
      // Doing well re-renders the tape under the cursor, so hover
      // prefetch finishes Dex's shell at opacity 0. The chart stays
      // on "Loading pair…" until the iframe remounts visible.
      if (firstShow) {
        hit.loaded = false;
        hit.born = Date.now();
        hit.frame.src = src;
      } else {
        revealPair(mint, hit.frame);
      }
    } else if (firstShow && Date.now() - hit.born > 400) {
      hit.born = Date.now();
      hit.frame.src = src;
    }
  }
  return hit;
}

function prefetchPair(t) {
  const mint = t?.mint || "";
  const src = embedUrl(t?.links?.dex);
  if (!mint || !src || pairCache.has(mint) || pairCache.size >= PAIR_CACHE_MAX) return;
  mountPairFrame(mint, src, { show: false });
}

function drawPairChart(t) {
  const mint = t?.mint || "";
  const src = embedUrl(t?.links?.dex);
  const dex = t?.links?.dex || "";
  const hit = pairCache.get(mint);
  if (hit && hit.loaded && hit.seen && (!src || pairPath(hit.src) === pairPath(src))) {
    hidePairFrames();
    hit.frame.classList.add("on");
    revealPair(mint, hit.frame);
    return;
  }
  if (!hit || !hit.loaded || !hit.seen) showPairSpark(t);
  if (!src) {
    hidePairFrames();
    if (!pairHost.querySelector(".chart-empty") && pairSkel && !pairSkel.classList.contains("on")) {
      pairHost.insertAdjacentHTML(
        "beforeend",
        `<div class="chart-empty">No DexScreener pair yet.</div>`
      );
    }
    return;
  }
  const empty = pairHost.querySelector(".chart-empty");
  if (empty) empty.remove();
  mountPairFrame(mint, src, { show: true });
  if (!pairCache.get(mint)?.loaded) armPairHang(mint, dex);
}

function snapshotPoints(t) {
  const snaps = (t?.snapshots || []).filter((s) => Number(s.mcap_usd || 0) > 0 && s.taken_at);
  const pts = snaps.map((s) => ({
    time: Math.floor(new Date(s.taken_at).getTime() / 1000),
    value: Number(s.mcap_usd),
  })).sort((a, b) => a.time - b.time);
  const seen = new Set();
  const uniq = pts.filter((p) => {
    if (seen.has(p.time)) return false;
    seen.add(p.time);
    return true;
  });
  if (uniq.length) return uniq;
  const t0 = Number(t?.t0_mcap || 0);
  const last = liveMcap(t || {});
  const start = t?.migrated_at || t?.first_seen_at || t?.created_at;
  if (t0 > 0 && last > 0 && start) {
    const a = Math.floor(new Date(start).getTime() / 1000);
    const b = Math.floor(Date.now() / 1000);
    if (b > a) return [{ time: a, value: t0 }, { time: b, value: last }];
  }
  return [];
}

function walletTable(wallets) {
  if (!wallets.length) return "";
  const rows = wallets.slice(0, 8).map((w) => `<tr>
    <td>${esc((w.owner || w.address || "").slice(0, 6))}…</td>
    <td>${w.pct != null ? Number(w.pct).toFixed(1) + "%" : "—"}</td>
  </tr>`).join("");
  return `<p class="section-label">Top wallets</p><table class="wallets"><thead><tr><th>Addr</th><th>%</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderDetail(t) {
  if (!t) {
    detailEl.innerHTML = `<p class="muted">Select a token for the research card.</p>`;
    return;
  }
  const x = liveMultiple(t);
  const score = liveConviction(t);
  const entry = entryPct(t);
  const reasons = t.reasons || [];
  const risks = t.risk_flags || [];
  const initials = String(tapeTicker(t) || "?").slice(0, 2).toUpperCase();
  const img = t.image_url
    ? `<img src="${esc(t.image_url)}" alt="" />`
    : `<span class="ph">${esc(initials)}</span>`;
  detailEl.innerHTML = `
    <div class="card-top">
      <div class="who">
        ${img}
        <div>
          <h3>${esc(tapeTicker(t))}</h3>
          <p class="sub">${esc(tapeSub(t))}</p>
        </div>
      </div>
      <div class="score-badge ${scoreClass(score)}" title="${t.watch_only ? "Watch preview — not entry p(good)" : "Live conviction"}">${score ? score.toFixed(0) : "—"}<small>${t.watch_only ? "prev" : "live"}</small></div>
    </div>
    <p class="muted" style="margin:0 0 0.6rem">${t.watch_only
      ? `Preview ${score ? score.toFixed(0) : "—"}% · curve only. Entry score lands at migrate.`
      : t.paper_only
        ? t.paper_veto
          ? `Gate veto · ${esc(t.veto || "not a fill")}. ${entryDetailLine(t, entry)} stays on the ledger for calibration.`
          : t.status === "queued" || t.paper_wait
            ? `paperV1 queued · Entry ${entry ? entry.toFixed(0) : "—"}% · waiting for a daily slot (${pickMeta.cap_per_chain || 3}/chain).`
            : t.status === "skipped"
              ? `paperV1 skipped at lock · Entry ${entry ? entry.toFixed(0) : "—"}%.`
              : t.tags || t.thesis_score != null
                ? `paperV1 ${esc(t.status || (t.paper_open ? "open" : "closed"))} · thesis ${t.thesis_score != null ? Number(t.thesis_score).toFixed(2) : "—"} · Entry ${entry ? entry.toFixed(0) : "—"}%${t.live_p != null ? ` · Live ${Math.round(Number(t.live_p) * 100)}` : ""}. Paper only.`
                : `Paper ${t.paper_open ? "open" : "closed"} · filled now at ${entry ? entry.toFixed(0) : "—"}%${entryLineLabel(entry, cardLines(t)) ? ` · ${entryLineLabel(entry, cardLines(t))} line${entryEqNote(entry, cardLines(t)) ? ` (${entryEqNote(entry, cardLines(t))})` : ""}` : ""}. Half sold at 2×, rest rides to 10× or 24h.`
        : `${entryDetailLine(t, entry)} · frozen for paper · ${entryTierTitle(entry, cardLines(t))}. Live ${liveCapped(t) ? `capped at ${Math.round(Number(t.live_cap) * 100)} on a thin book` : "can move"}.`}</p>
    ${t.paper_only && !t.paper_veto ? `<p class="muted" style="margin:0 0 0.6rem">Mark ${fmtPnl(t.return_pct != null ? Number(t.return_pct) : paperPnl(t))} · simulated, no live order.${t.exit_reason ? ` · ${esc(t.exit_reason)}` : ""}</p>` : ""}
    ${(t.tags && t.tags.length) || t.thesis_detail ? `<p class="section-label">Thesis rank</p><div class="flags">${thesisTagsHtml(t.tags)}${t.thesis_detail ? `<span>gh ${Number(t.thesis_detail.github_auth || 0).toFixed(2)}</span><span>dev ${Number(t.thesis_detail.real_project || 0).toFixed(2)}</span><span>cto ${Number(t.thesis_detail.cto || 0).toFixed(2)}</span><span>meme ${Number(t.thesis_detail.name_quality || 0).toFixed(2)}</span>` : ""}</div>` : ""}
    <p class="thesis">${esc(typeof t.thesis === "string" ? t.thesis : (t.thesis?.summary || pickMeta.rule || "No thesis stored yet."))}</p>
    ${t.meme_quality && t.meme_quality.meme_only ? `<p class="section-label">Meme quality</p><p class="muted" style="margin:0 0 0.6rem">Learn score ${Number(t.meme_quality.score || 0).toFixed(2)}${t.meme_quality.hard_veto ? ` · hard veto ${esc(t.meme_quality.hard_veto)}` : ""}${t.meme_quality.late ? " · late" : ""}. Not a short-list pass.</p>` : ""}
    ${t.hold_conviction && t.hold_conviction.score != null ? `<p class="section-label">Hold conviction</p><p class="muted" style="margin:0 0 0.6rem">After buy ${Number(t.hold_conviction.score).toFixed(2)} · ${esc(t.hold_conviction.policy || "neutral")}. Exit layer only — entry lines unchanged.</p>` : ""}
    <div class="stat-grid">
      <div><span>Last</span><b>${fmtMcap(liveMcap(t))}</b></div>
      <div><span>t0</span><b>${fmtMcap(t.t0_mcap)}</b></div>
      <div><span>Multiple</span><b class="${multipleClass(x)}">${x ? x.toFixed(2) + "×" : "—"}</b></div>
      <div><span>Liq</span><b>${fmtMcap(t.last_liq)}</b></div>
      <div><span>Holders</span><b>${t.holder_count || "—"}</b></div>
      <div><span>Top10</span><b>${t.top10_pct ? Number(t.top10_pct).toFixed(1) + "%" : "—"}</b></div>
    </div>
    ${ledgerBlock(t)}
    ${reasons.length || risks.length || t.holder_rewards || (t.has_dev_x && t.dev_handle) ? `<p class="section-label">Signals</p><div class="flags">
      ${t.holder_rewards ? `<span>Pays holders ${fmtMcap(t.holder_rewards.distributed_usd)}${t.holder_rewards.meme_pair ? ` · meme pair${t.holder_rewards.quote_symbol ? " " + esc(t.holder_rewards.quote_symbol) : ""}` : ""}</span>` : ""}
      ${t.has_dev_x && t.dev_handle ? `<span class="dev-x">Public dev X @${esc(String(t.dev_handle).replace(/^@/, ""))}</span>` : ""}
      ${reasons.map((row) => `<span>${esc(row)}</span>`).join("")}
      ${risks.map((row) => `<span class="risk">${esc(row)}</span>`).join("")}
    </div>` : ""}
    <p class="creator muted">Creator ${(t.creator || "").slice(0, 6)}… · ${t.creator_prior_launches || 0} prior · ${t.creator_prior_wins || 0} wins · ${t.creator_prior_rugs || 0} rugs</p>
    <p class="section-label">Links</p>
    <p class="links">
      ${t.links?.pons ? `<a href="${t.links.pons}" target="_blank" rel="noreferrer">Pons</a>` : ""}
      ${t.links?.pump ? `<a href="${t.links.pump}" target="_blank" rel="noreferrer">Pump</a>` : ""}
      ${t.links?.dex ? `<a href="${t.links.dex}" target="_blank" rel="noreferrer">Dex</a>` : ""}
      ${t.links?.explorer ? `<a href="${t.links.explorer}" target="_blank" rel="noreferrer">${t.chain === "robinhood" ? "Explorer" : "Solscan"}</a>` : ""}
      ${t.links?.gmgn ? `<a href="${t.links.gmgn}" target="_blank" rel="noreferrer">GMGN</a>` : ""}
      ${t.github_url ? `<a href="${esc(t.github_url)}" target="_blank" rel="noreferrer">GitHub</a>` : ""}
      ${xAccountLinks(t)}
    </p>
    ${walletTable(t.top_wallets || [])}
  `;
  bindTicketActions();
}

function showPane(which) {
  if (boardPane) boardPane.classList.toggle("on", which === "board");
  if (learnPane) learnPane.classList.toggle("on", which === "learn");
  if (fomoPane) fomoPane.classList.toggle("on", which === "fomo");
  if (pairHost) pairHost.classList.toggle("on", which !== "board" && which !== "learn" && which !== "fomo");
  if ((which === "board" || which === "learn" || which === "fomo") && tapeStrip) tapeStrip.hidden = true;
}

async function openDetail(mint) {
  selected = mint;
  showPane("pair");
  const preview = tapeRow(mint) || activeItems().find((row) => row.mint === mint) || { mint, symbol: mint.slice(0, 6) };
  lastDetail = preview;
  renderTape();
  renderChartHead(preview);
  renderDetail(preview);
  drawPairChart(preview);
  loadTapeStrip(preview);
  history.replaceState(null, "", `#${encodeURIComponent(mint)}`);
  let t = preview;
  try {
    t = await j(`/api/tokens/${mint}`, { timeoutMs: 8000 });
  } catch (e) {
    t = preview;
  }
  if (selected !== mint) return;
  t = mergeTapeCard(t, tapeRow(mint) || preview);
  paintDumpedTape(mint, t);
  const tape = tapeRow(mint) || preview;
  if (tape && tape.paper_only) {
    t.paper_only = true;
    t.paper_open = tape.paper_open;
    t.paper_wait = tape.paper_wait;
    t.paper_veto = tape.paper_veto;
    t.veto = tape.veto;
    t.return_pct = tape.return_pct;
    t.status = tape.status || t.status;
    t.tags = tape.tags || t.tags;
    t.thesis_score = tape.thesis_score;
    t.meme_quality = tape.meme_quality;
    t.hold_conviction = tape.hold_conviction;
    t.thesis_detail = tape.thesis_detail || tape.thesis;
    t.exit_reason = tape.exit_reason || t.exit_reason;
    t.entry_mcap = tape.entry_mcap || t.t0_mcap;
    if (tape.entry_p != null) t.entry_p = tape.entry_p;
    if (tape.live_p != null) t.live_p = tape.live_p;
  }
  lastDetail = t;
  renderChartHead(t);
  renderDetail(t);
  drawPairChart(t);
  loadTapeStrip(t);
}

async function openWallet(owner) {
  selected = owner;
  showPane("pair");
  if (tapeStrip) tapeStrip.hidden = true;
  const preview = walletItems.find((w) => w.owner === owner) || { owner, score: 0, grade: "—" };
  lastDetail = { ...preview, wallet_only: true };
  renderTape();
  renderWalletHead(preview);
  renderWalletDetail(preview);
  history.replaceState(null, "", `#wallet-${encodeURIComponent(owner)}`);
  let w = preview;
  try {
    w = await j(withChain(`/api/wallets/${encodeURIComponent(owner)}`), { timeoutMs: 8000 });
  } catch (e) {
    w = preview;
  }
  if (selected !== owner) return;
  lastDetail = { ...w, wallet_only: true };
  renderWalletHead(w);
  renderWalletDetail(w);
}

function renderWalletHead(w) {
  if (!w) {
    chartHead.innerHTML = `<p class="muted">Select a wallet on the tape.</p>`;
    return;
  }
  const owner = w.owner || "";
  const short = owner.length > 10 ? `${owner.slice(0, 4)}…${owner.slice(-4)}` : owner;
  chartHead.innerHTML = `<div class="picked">
    <span class="ph">${esc(String(w.grade || "?").slice(0, 1))}</span>
    <div>
      <h2>${esc(short || "Wallet")} <small class="muted">${w.gold ? "gold · " : ""}${esc(w.grade || "—")}</small></h2>
      <div class="meta">
        <span>${Number(w.score || 0).toFixed(0)} score</span>
        <span>${w.n_sized || 0}/${w.n_early || 0} sized</span>
        <span>${w.n_wins || 0} FOMO wins</span>
        ${w.best_symbol ? `<span>${esc(w.best_symbol)} ${Number(w.best_multiple || 0).toFixed(1)}×</span>` : ""}
      </div>
    </div>
  </div>`;
}

function renderWalletDetail(w) {
  if (!w) {
    detailEl.innerHTML = `<p class="muted">Select a wallet for the research card.</p>`;
    return;
  }
  const score = Number(w.score || 0);
  const hits = (w.hits || []).slice(0, 16).map((h) => {
    const x = Number(h.multiple || 0);
    const tag = h.win ? "win" : (h.kind === "fomo" && !h.win ? "rug" : (h.kind || ""));
    return `<span${h.win ? "" : " class=\"risk\""}>${esc(h.symbol || (h.mint || "").slice(0, 6))} ${x ? x.toFixed(1) + "×" : ""} ${esc(tag)}</span>`;
  }).join("");
  detailEl.innerHTML = `
    <div class="card-top">
      <div class="who">
        <span class="ph">${esc(String(w.grade || "?").slice(0, 1))}</span>
        <div>
          <h3>${esc((w.owner || "").slice(0, 6))}…</h3>
          <p class="sub">${w.gold ? "Gold: sized first-hour and later on a 5×." : w.parked ? "Parked stored map. Historical books do not mint gold." : "Stored Early + FOMO map. Not a personal watchlist."}</p>
        </div>
      </div>
      <div class="score-badge ${scoreClass(score)}" title="Wallet research score">${score ? score.toFixed(0) : "—"}<small>wal</small></div>
    </div>
    <p class="muted" style="margin:0 0 0.6rem">Heuristic only — not entry p(good). No extra GMGN.</p>
    <p class="thesis">${w.gold
      ? "Gold list: sized in the first hour and also showed up on a confirmed 5× FOMO map."
      : w.parked
        ? "Stored first-hour / FOMO map from an older book. Parked so it cannot mint gold until it shows up on a this-window sized ∩ 5×."
        : "Repeat first-hour buyers and wallets that appear on confirmed runners. Parked historical books do not mint gold."}</p>
    <div class="stat-grid">
      <div><span>Early</span><b>${w.n_sized || 0}/${w.n_early || 0}</b></div>
      <div><span>FOMO</span><b>${w.n_wins || 0}/${w.n_fomo || 0}</b></div>
      <div><span>Rugs</span><b>${w.n_rugs || 0}</b></div>
      <div><span>Still in</span><b>${w.n_still_in || 0}</b></div>
      <div><span>Created</span><b>${w.n_created || 0}</b></div>
      <div><span>Alpha</span><b>${w.alpha_runs || 0}</b></div>
    </div>
    ${hits ? `<p class="section-label">Books</p><div class="flags">${hits}</div>` : ""}
    <p class="section-label">Links</p>
    <p class="links">
      <a href="${walletExplorer(w.owner)}" target="_blank" rel="noreferrer">${CHAIN === "robinhood" ? "Explorer" : "Solscan"}</a>
      ${CHAIN === "sol" ? `<a href="https://gmgn.ai/sol/address/${esc(w.owner)}" target="_blank" rel="noreferrer">GMGN</a>` : ""}
    </p>
  `;
}

function setDeskTab(tab) {
  deskTab = tab;
  document.querySelectorAll(".desk-tabs [data-desk]").forEach((btn) => {
    btn.classList.toggle("on", btn.dataset.desk === tab);
  });
  // Tab switches always update the hash; selecting a mint later replaces it.
  if (TAB_LABELS[tab]) history.replaceState(null, "", `#${tab}`);
  if (tab === "board") {
    showPane("board");
    renderBoardPane();
    renderBoardDetail(selected);
  } else if (tab === "learn") {
    showPane("learn");
    renderLearnPane();
    renderLearnDetail(selected);
  } else if (tab === "fomo") {
    showPane("fomo");
    renderFomoPane();
    renderFomoDetail(selected);
  } else if ((boardPane && boardPane.classList.contains("on")) || (learnPane && learnPane.classList.contains("on")) || (fomoPane && fomoPane.classList.contains("on"))) {
    showPane("pair");
    if (lastDetail && !lastDetail.wallet_only && !lastDetail.learn_only && lastDetail.mint) {
      renderChartHead(lastDetail);
      renderDetail(lastDetail);
      drawPairChart(lastDetail);
      loadTapeStrip(lastDetail);
    } else {
      renderChartHead(null);
      renderDetail(null);
    }
  }
  renderTape();
  loadTabData(tab);
}

function heliusCapped(health) {
  const note = String(health?.loops?.hunt_tape?.note || "");
  return note.includes("helius_capped");
}

function loopStale(health) {
  // A worker loop whose heartbeat is older than 10 minutes is down, not slow.
  const loops = health?.loops || {};
  const stale = [];
  for (const [name, info] of Object.entries(loops)) {
    if (!info) continue;
    // Hourly loops: a 10-minute quiet is not a down worker. FOMO 402 sit-out
    // still heartbeats; do not paint that as stale.
    if (name === "batch_fit" || name === "live_fit" || name === "fomo_trending") continue;
    const age = info.age_s != null ? Number(info.age_s) : (info.at ? (Date.now() - new Date(info.at).getTime()) / 1000 : null);
    if (age != null && age > 10 * 60) stale.push(name.replace("_", " "));
  }
  return stale;
}

function applyHealth(health) {
  healthState = health || {};
  statusEl.classList.add("live");
  const bits = [];
  if (health.image_rev) bits.push(String(health.image_rev).replace(/^stack-/, ""));
  bits.push("live", `${Math.round(TAPE_REFRESH_MS / 1000)}s`);
  const warn = [];
  if (health.bitquery) bits.push("v4");
  if (health.github) bits.push("gh");
  if (heliusCapped(health)) warn.push("helius capped");
  const stale = loopStale(health);
  if (stale.length) warn.push(`${stale.join(", ")} stale`);
  statusEl.classList.toggle("warn", warn.length > 0);
  statusText.textContent = bits.concat(warn).join(" · ");
  statusEl.title = warn.length
    ? (heliusCapped(health) ? "Helius key is at its plan cap: Sol holder tape parked, new Sol holder counts blank until it resets. " : "") + (stale.length ? `Worker loops without a heartbeat for 10+ min: ${stale.join(", ")}.` : "")
    : `API and worker reporting · ${health.image_rev || "image?"} · paper 1–5/day oversight`;
  const wire = document.getElementById("wire");
  if (wire) wire.hidden = Boolean(health.alerts);
  if (health.ledger) honest.counts = health.ledger;
}

function mergeTapeCard(detail, card) {
  const detailLast = Number(detail.last_mcap || 0);
  const tapeLast = Number(card.last_mcap || 0);
  const tapeFatter = detailLast > 0 && tapeLast > detailLast * 1.05;
  const skipLive = tapeFatter || dumpedOffAth(detail);
  const keys = skipLive
    ? [
        "t0_mcap", "entry_p", "preview_p", "holder_count",
        "top10_pct", "image_url", "symbol", "name", "twitter_handle", "dev_handle", "has_dev_x",
        "claimed_brand_x", "twitter_followers", "twitter_tweets", "twitter_age_days",
        "twitter_bio_match", "github_age_days", "github_url", "live_cap", "holder_rewards",
      ]
    : [
        "last_mcap", "t0_mcap", "max_mcap", "last_liq", "multiple",
        "conviction_p", "entry_p", "score", "promise_p", "preview_p", "holder_count",
        "top10_pct", "risk_flags", "image_url", "symbol", "name", "twitter_handle", "dev_handle", "has_dev_x",
        "claimed_brand_x", "twitter_followers", "twitter_tweets", "twitter_age_days",
        "twitter_bio_match", "github_age_days", "github_url", "live_cap", "holder_rewards",
      ];
  const next = { ...detail };
  for (const key of keys) {
    if (card[key] != null) next[key] = card[key];
  }
  if (card.thesis) next.thesis = card.thesis;
  if (card.bloom) next.bloom = true;
  return next;
}

function tapeRow(mint) {
  const pools = deskTab === "board" || deskTab === "learn" || deskTab === "fomo" ? [] : activeItems();
  return pools.concat(pickItems, lastItems, hotItems, bloomItems, paperItems).find((t) => t.mint === mint);
}

function paintDumpedTape(mint, detail) {
  const last = Number(detail?.last_mcap || 0);
  if (!mint || last <= 0) return;
  for (const list of [lastItems, hotItems, bloomItems]) {
    const i = list.findIndex((row) => row.mint === mint);
    if (i < 0) continue;
    if (Number(list[i].last_mcap || 0) > last * 1.05 || dumpedOffAth(detail)) {
      list[i] = {
        ...list[i],
        last_mcap: last,
        last_liq: detail.last_liq,
        score: detail.score,
        p_good: detail.p_good,
        conviction_p: dumpedOffAth(detail) ? null : detail.conviction_p,
        risk_flags: detail.risk_flags,
      };
    }
  }
}

function loadBoardBundle() {
  if (Date.now() - boardData.loadedAt < 60000 && boardData.calibration) {
    return Promise.resolve(boardData);
  }
  return Promise.all([
    j(withChain("/api/model/calibration?days=21"), { timeoutMs: 20000 }).catch(() => null),
    j(withChain("/api/ledger/weekly?weeks=8"), { timeoutMs: 20000 }).catch(() => null),
    j(withChain("/api/model/artifacts"), { timeoutMs: 12000 }).catch(() => null),
    j(withChain("/api/model/artifacts?kind=first_sight"), { timeoutMs: 12000 }).catch(() => null),
    j(withChain("/api/model/artifacts?kind=live"), { timeoutMs: 12000 }).catch(() => null),
    j(withChain("/api/paper/scorecard"), { timeoutMs: 12000 }).catch(() => null),
    j(withChain("/api/model/artifacts?kind=live_runner"), { timeoutMs: 12000 }).catch(() => null),
  ]).then(([calibration, weekly, artifacts, firstSight, live, scorecard, runner]) => {
    boardData = {
      calibration: calibration || boardData.calibration,
      weekly: weekly || boardData.weekly,
      artifacts: artifacts || boardData.artifacts,
      firstSight: firstSight || boardData.firstSight,
      live: live || boardData.live,
      scorecard: scorecard || boardData.scorecard,
      runner: runner || boardData.runner,
      loadedAt: Date.now(),
    };
    if (weekly) honest = { ...honest, weekly, loadedAt: Date.now() };
    return boardData;
  });
}

function loadTabData(tab) {
  if (tab === "picks") {
    j(withChain("/api/paper/v1"), { timeoutMs: 15000 })
      .then((book) => {
        pickItems = (book.items || []).map(mapPickRow);
        applyPickMeta(book);
        renderMetrics({});
        if (deskTab === "picks") { renderTape(); autoSelect(); }
      })
      .catch(() => {});
  } else if (tab === "learn") {
    Promise.all([
      j(withChain("/api/paper/v1"), { timeoutMs: 15000 }).catch(() => null),
      j(withChain("/api/paper/v1/review"), { timeoutMs: 15000 }).catch(() => null),
      j("/api/paper/v1/report", { timeoutMs: 15000 }).catch(() => null),
      j(withChain("/api/paper/v1/runners-retro"), { timeoutMs: 20000 }).catch(() => null),
      j(withChain("/api/year-winners"), { timeoutMs: 20000 }).catch(() => null),
      j(withChain("/api/paper/v1/miss-cohort"), { timeoutMs: 20000 }).catch(() => null),
      j(`${withChain("/api/paper/v1/offline-sprint")}&repair=true`, { timeoutMs: 45000 }).catch(() => null),
      j("/api/fomo-trending", { timeoutMs: 25000 }).catch(() => null),
      j("/api/fomo-alerts/traders?limit=50", { timeoutMs: 25000 }).catch(() => null),
      j("/api/fomo-alerts/flow", { timeoutMs: 25000 }).catch(() => null),
      j(withChain("/api/paper/veto-retro"), { timeoutMs: 45000 }).catch(() => null),
      j(withChain("/api/sanity-loop"), { timeoutMs: 60000 }).catch(() => null),
      j(withChain("/api/paper/v1/day-delta"), { timeoutMs: 45000 }).catch(() => null),
      j(withChain("/api/paper/v1/production-gate"), { timeoutMs: 45000 }).catch(() => null),
      j(withChain("/api/paper/v1/early-diff"), { timeoutMs: 45000 }).catch(() => null),
      loadBoardBundle(),
    ]).then(([book, review, report, runnersRetro, yearWinners, missCohort, offlineSprint, fomoTrending, fomoTraders, fomoFlow, vetoRetro, sanityLoop, dayDelta, productionGate, earlyDiff]) => {
      if (book) {
        pickItems = (book.items || []).map(mapPickRow);
        applyPickMeta(book);
      }
      if (review) {
        reviewData = {
          day: review.day || "",
          taken: review.taken || 0,
          locked: Boolean(review.locked),
          room: review.room,
          by_chain: review.by_chain || null,
          silence: review.silence || "",
          picked: review.picked || [],
          queued: review.queued || [],
          skipped: review.skipped || [],
          shadow: review.shadow || [],
          closed: review.closed || [],
          metrics: review.metrics || null,
          loadedAt: Date.now(),
        };
      }
      if (report) {
        reportData = {
          text: report.text || "",
          risk: report.risk || null,
          totals: report.totals || null,
          loadedAt: Date.now(),
        };
      }
      runnersRetroData = runnersRetro || null;
      yearWinnersData = yearWinners || null;
      missCohortData = missCohort || null;
      offlineSprintData = offlineSprint || null;
      fomoTrendingData = fomoTrending || null;
      fomoTradersData = fomoTraders || null;
      fomoAlertsFlowData = fomoFlow || null;
      vetoRetroData = vetoRetro || null;
      sanityLoopData = sanityLoop || null;
      dayDeltaData = dayDelta || null;
      productionGateData = productionGate || null;
      earlyDiffData = earlyDiff || null;
      buildLearnRows();
      if (deskTab === "learn") {
        renderTape();
        renderLearnPane();
        if (!selected || !String(selected).startsWith("learn-")) selected = learnRows[0]?.mint || selected;
        renderLearnDetail(selected);
      }
      renderMetrics({});
    });
  } else if (tab === "hot") {
    j(withChain("/api/doing-well"), { timeoutMs: 8000 })
      .then((hot) => { hotItems = hot.items || []; if (deskTab === "hot") renderTape(); })
      .catch(() => {});
  } else if (tab === "bloom") {
    j(withChain("/api/bloom"), { timeoutMs: 8000 })
      .then((bloom) => { bloomItems = bloom.items || []; if (deskTab === "bloom") renderTape(); })
      .catch(() => {});
  } else if (tab === "wallets") {
    j(withChain("/api/wallets?limit=80"), { timeoutMs: 20000 })
      .then((wallets) => {
        walletItems = wallets.items || wallets || [];
        walletMeta = { wallets: wallets.wallets || walletItems.length, gold: wallets.gold || 0, parked: wallets.parked || 0 };
        if (deskTab === "wallets") renderTape();
      })
      .catch(() => {});
  } else if (tab === "watch") {
    j(withChain("/api/watch"), { timeoutMs: 8000 })
      .then((watch) => {
        watchItems = (watch.graduating_soon || watch.items || []).map((row) => {
          const prog = Number(row.progress || 0);
          const bonded = prog ? Math.round(prog <= 1 ? prog * 100 : prog) : 0;
          const prev = Number(row.preview_p || 0);
          return {
            mint: row.mint,
            symbol: row.symbol,
            name: bonded ? `${row.name || row.symbol || ""} · ${bonded}%` : (row.name || row.symbol || ""),
            last_mcap: row.mcap_usd || row.last_mcap || 0,
            t0_mcap: row.t0_mcap || 0,
            score: row.score || (prev ? prev * 100 : 0),
            preview_p: prev,
            progress: prog,
            thesis: (row.preview_reasons || []).join("; "),
            reasons: row.preview_reasons || [],
            risk_flags: row.preview_flags || [],
            launchpad: row.launchpad || "",
            image_url: row.image_url || "",
            first_seen_at: row.first_seen_at || row.first_seen || row.created_at || null,
            creator: row.creator || "",
            links: { pons: row.pons, gmgn: row.gmgn, dex: row.dex },
            watch_only: true,
          };
        });
        if (deskTab === "watch") renderTape();
      })
      .catch(() => {});
  } else if (tab === "paper") {
    j(withChain("/api/paper?min_p=0.9&target=2&ride=10&strategy=moonbag&gated=true"), { timeoutMs: 20000 })
      .then((paper) => {
        const paperLines = paper.lines || chainLines();
        const waiting = (paper.waiting || []).map((row) => ({
          mint: row.mint,
          symbol: row.symbol,
          name: row.symbol || "",
          p_good: row.p_good,
          entry_p: row.p_good,
          entry_mcap: row.entry_mcap,
          t0_mcap: row.entry_mcap,
          last_mcap: row.entry_mcap && row.multiple ? row.entry_mcap * row.multiple : row.entry_mcap,
          multiple: row.multiple,
          score: Number(row.p_good || 0) * 100,
          scorer: paperLines.scorer,
          desk_lines: paperLines,
          paper_open: false,
          paper_wait: true,
          paper_only: true,
          thesis: `Graduation at Entry ≥ ${linePct(paperLines.hi)}. Held — not a paper fill.`,
        }));
        const open = (paper.open || []).map((row) => ({
          mint: row.mint,
          symbol: row.symbol,
          name: row.symbol || "",
          p_good: row.p_good,
          entry_p: row.p_good,
          entry_mcap: row.entry_mcap,
          t0_mcap: row.entry_mcap,
          last_mcap: row.entry_mcap && row.multiple ? row.entry_mcap * row.multiple : row.entry_mcap,
          multiple: row.multiple,
          score: Number(row.p_good || 0) * 100,
          scorer: paperLines.scorer,
          desk_lines: paperLines,
          paper_open: true,
          paper_only: true,
          thesis: `This-window Entry ≥ ${linePct(paperLines.hi)} (${lineName(paperLines, "hi")}) fill now if liquid and unvetoed. Half banks at a liquidity-confirmed 2×; the rest rides to 10× or the 24h print.`,
        }));
        const closed = (paper.closed || []).map((row) => ({
          mint: row.mint,
          symbol: row.symbol,
          name: row.symbol || "",
          p_good: row.p_good,
          entry_p: row.p_good,
          entry_mcap: row.entry_mcap,
          t0_mcap: row.entry_mcap,
          last_mcap: row.entry_mcap && row.multiple ? row.entry_mcap * row.multiple : row.entry_mcap,
          multiple: row.multiple,
          score: Number(row.p_good || 0) * 100,
          return_pct: row.return_pct,
          scorer: paperLines.scorer,
          desk_lines: paperLines,
          paper_open: false,
          paper_only: true,
          thesis: "Paper close. Half was sold at 2× when the tape confirmed; the rest exited at 10× or 24h.",
        }));
        const vetoed = (paper.vetoed || []).map((row) => ({
          mint: row.mint,
          symbol: row.symbol || row.mint.slice(0, 6),
          name: row.symbol || "",
          p_good: row.p_good,
          entry_p: row.p_good,
          entry_mcap: row.entry_mcap,
          t0_mcap: row.entry_mcap,
          last_mcap: row.entry_mcap,
          multiple: 0,
          score: Number(row.p_good || 0) * 100,
          veto: row.veto,
          created_at: row.at,
          first_seen_at: row.at,
          scorer: paperLines.scorer,
          desk_lines: paperLines,
          paper_open: false,
          paper_veto: true,
          paper_only: true,
          thesis: `Gate veto: ${row.veto}. The Entry ≥ ${linePct(paperLines.hi)} decision stays on the ledger for calibration; no paper fill was written.`,
        }));
        paperItems = waiting.concat(open, closed, vetoed);
        paperMeta = {
          open: Number(paper.open_positions || open.length || 0),
          closed: Number(paper.closed_trades || closed.length || 0),
          pending: Number(paper.pending || waiting.length || 0),
          vetoed: vetoed.length,
          wins: Number(paper.wins || 0),
          avg: paper.avg_return_pct,
          total: paper.total_return_pct,
          rule: paper.strategy || "",
          scorecard: paper.scorecard || null,
        };
        if (deskTab === "paper") { renderTape(); autoSelect(); }
      })
      .catch(() => {});
  } else if (tab === "tickets") {
    j(withChain("/api/tickets?limit=100"), { timeoutMs: 12000 })
      .then((res) => {
        ticketItems = (res.items || res || []).map((tk) => ({ ...tk, created_at: tk.created_at, first_seen_at: tk.created_at }));
        if (deskTab === "tickets") { renderTape(); autoSelect(); }
      })
      .catch(() => {});
  } else if (tab === "board") {
    loadBoardBundle().then(() => {
      if (deskTab === "board") {
        renderTape();
        renderBoardPane();
        if (!selected || !String(selected).startsWith("bin-")) {
          const rows = boardRows();
          const top = rows.find((r) => r.lo >= chainLines().hi) || rows[0];
          if (top) { selected = top.mint; renderTape(); }
        }
        renderBoardDetail(selected);
      }
    });
  } else if (tab === "fomo") {
    j("/api/fomo-trending", { timeoutMs: 25000 })
      .then((card) => {
        fomoTrendingData = card || null;
        if (deskTab !== "fomo") return;
        renderTape();
        renderFomoPane();
        if (!selected || !fomoTabRows().some((r) => r.mint === selected)) {
          selected = fomoTabRows()[0]?.mint || selected;
        }
        renderFomoDetail(selected);
      })
      .catch(() => {
        if (deskTab === "fomo") {
          fomoTrendingData = null;
          renderTape();
          renderFomoPane();
          detailEl.innerHTML = `<p class="muted">Could not load /api/fomo-trending — retrying on the next refresh.</p>`;
        }
      });
  }
}

function autoSelect() {
  // Landing on a tab with nothing picked leaves two empty panes; pick the first row.
  if (deskTab === "learn") {
    if (!selected || !String(selected).startsWith("learn-")) {
      selected = (learnRows[0] && learnRows[0].mint) || selected;
    }
    renderLearnDetail(selected);
    return;
  }
  if (deskTab === "fomo") {
    if (!selected || !fomoTabRows().some((r) => r.mint === selected)) {
      selected = fomoTabRows()[0]?.mint || selected;
    }
    renderFomoDetail(selected);
    return;
  }
  if (selected && (lastDetail?.wallet_only || activeItems().some((t) => t.mint === selected))) return;
  const first = sorted(filtered(activeItems()))[0];
  if (first && first.mint) openDetail(first.mint).catch(() => {});
}

const HONEST_REFRESH_MS = 90000;

function refreshHonest() {
  if (Date.now() - honest.loadedAt < HONEST_REFRESH_MS) return Promise.resolve();
  honest.loadedAt = Date.now();
  return j(withChain("/api/ledger/weekly"), { timeoutMs: 20000 })
    .then((weekly) => { honest = { ...honest, weekly, loadedAt: Date.now() }; })
    .catch(() => {});
}

async function refresh() {
  try {
    const hours = liveOnly.checked ? (CHAIN === "robinhood" ? "12" : "18") : "168";
    const hunt = await j(withChain(`/api/hunt?limit=80&hours=${hours}`), { timeoutMs: 12000 });
    lastItems = hunt.items || hunt;
    renderTape();
    if (selected && lastDetail && !lastDetail.wallet_only && deskTab !== "board" && deskTab !== "fomo" && lastDetail.mint) {
      const card = tapeRow(selected);
      if (card) {
        lastDetail = mergeTapeCard(lastDetail, card);
        renderChartHead(lastDetail);
        renderDetail(lastDetail);
        loadTapeStrip(lastDetail);
      } else {
        j(`/api/tokens/${selected}`, { timeoutMs: 8000 }).then((row) => {
          if (!row || selected !== row.mint) return;
          lastDetail = row;
          renderChartHead(row);
          renderDetail(row);
          loadTapeStrip(row);
        }).catch(() => {});
      }
    }
    Promise.all([
      j("/health", { timeoutMs: 5000 }).catch(() => ({})),
      j(withChain("/api/status"), { timeoutMs: 8000 }).catch(() => null),
      refreshHonest(),
      deskTab === "picks" || deskTab === "learn"
        ? Promise.resolve(null)
        : j(withChain("/api/paper/v1"), { timeoutMs: 12000 }).catch(() => null),
    ]).then(([health, status, _honest, book]) => {
      applyHealth(health || {});
      if (book) {
        pickItems = (book.items || []).map(mapPickRow);
        applyPickMeta(book);
      }
      renderMetrics(status || {});
    });
    loadTabData(deskTab);
  } catch (e) {
    statusEl.classList.remove("live");
    statusText.textContent = "retrying…";
  }
}

minScore.oninput = () => { minLabel.textContent = `${minScore.value}%`; renderTape(); };
minScore.onchange = refresh;
liveOnly.onchange = refresh;
sortSel.onchange = renderTape;
searchEl.oninput = renderTape;
document.querySelectorAll(".desk-tabs [data-desk]").forEach((btn) => {
  btn.onclick = () => setDeskTab(btn.dataset.desk);
});
document.getElementById("scan").onclick = async () => {
  const btn = document.getElementById("scan");
  btn.disabled = true;
  try { await j(withChain("/api/scan-now"), { method: "POST" }); await refresh(); }
  finally { btn.disabled = false; }
};

const hashMint = decodeURIComponent((location.hash || "").replace(/^#/, ""));
refresh().then(() => {
  if (hashMint.startsWith("wallet-")) setDeskTab("learn");
  else if (TAB_LABELS[hashMint]) setDeskTab(hashMint);
  else if (LEGACY_TAB_REDIRECT[hashMint]) setDeskTab(LEGACY_TAB_REDIRECT[hashMint]);
  else if (hashMint) openDetail(hashMint).catch(() => {});
  else setDeskTab("picks");
});
setInterval(refresh, TAPE_REFRESH_MS);
// Keyboard: j/k walk the tape, 1-5 switch tabs, / focuses search.
document.addEventListener("keydown", (ev) => {
  const tag = (ev.target && ev.target.tagName) || "";
  if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") {
    if (ev.key === "Escape") ev.target.blur();
    return;
  }
  if (ev.key === "/") { ev.preventDefault(); searchEl.focus(); return; }
  const tabs = ["picks", "learn", "live", "fomo", "board"];
  if (/^[1-5]$/.test(ev.key)) { setDeskTab(tabs[Number(ev.key) - 1]); return; }
  if (ev.key !== "j" && ev.key !== "k") return;
  const rows = [...rowsEl.querySelectorAll(".tape-row")];
  if (!rows.length) return;
  const idx = rows.findIndex((r) => r.classList.contains("on"));
  const next = rows[Math.min(rows.length - 1, Math.max(0, idx + (ev.key === "j" ? 1 : -1)))];
  if (next) { next.click(); next.scrollIntoView({ block: "nearest" }); }
});
