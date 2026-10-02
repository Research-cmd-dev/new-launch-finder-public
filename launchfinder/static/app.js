const CHAIN = document.documentElement.dataset.chain || "sol";
function withChain(url) {
  const u = new URL(url, location.origin);
  if (!u.searchParams.has("chain")) u.searchParams.set("chain", CHAIN);
  return u.pathname + u.search;
}

const rowsEl = document.getElementById("rows");
const detailEl = document.getElementById("detail");
const metricsEl = document.getElementById("metrics");
const referenceEl = document.getElementById("reference");
const minScore = document.getElementById("min-score");
const minLabel = document.getElementById("min-score-label");
const liveOnly = document.getElementById("live-only");
const sortSel = document.getElementById("sort");
const paperMode = document.getElementById("paper-mode");
let selected = null;
let runnerMap = {};        // mint -> runnerwatch payload
let runnersHeadline = null; // {runners, capture_rate}
let approachingHeadline = null; // {approaching, thin_prints}
let headlineTick = 0;
let lastItems = [];
let hotItems = [];
let lastPaper = null;
let fomoItems = [];
let earlyItems = [];
let scoredItems = [];
let scoredMeta = null;
let earlyMeta = null;
let fomoMeta = null;
function tabFromHash() {
  if (location.hash === "#doing-well") return "hot";
  if (location.hash === "#fomo-wallets") return "fomo";
  if (location.hash === "#early-wallets") return "early";
  if (location.hash === "#wallets") return "wallets";
  return "live";
}
let deskTab = tabFromHash();

function pct(n) { return `${Math.round((n || 0) * 1000) / 10}%`; }
function liveMultiple(t) {
  const t0 = Number(t.t0_mcap || 0);
  const last = Number(t.last_mcap || t.max_mcap || 0);
  if (t0 > 0 && last > 0) return last / t0;
  return Number(t.multiple || 0);
}
function isHeldIngestMiss(t) {
  return Boolean(t.ingest_miss) || (
    liveMultiple(t) >= 10 && Number(t.last_liq || 0) >= 50000 && Number(t.score || 0) < 50
  );
}
function scoreClass(s, t) {
  if (t && isHeldIngestMiss(t)) return "mid";
  return s >= 55 ? "high" : s >= 35 ? "mid" : "low";
}
function scoreCaption(t) {
  const score = Number(t.score || 0);
  if (isHeldIngestMiss(t)) {
    return `${score.toFixed(1)}% ingest call — this book already ran ${liveMultiple(t).toFixed(0)}×`;
  }
  return `${score.toFixed(1)}% chance this is a “good” post-migrate runner`;
}
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c])); }
const HARD = ["start-high rug", "honeypot", "wash trading", "hijack", "coordinated"];
function hardFlag(t) {
  const flags = (t.risk_flags || []).join(" | ").toLowerCase();
  return HARD.some(h => flags.includes(h));
}
function ageOf(t) {
  const raw = t.migrated_at || t.first_seen_at || t.created_at;
  if (!raw) return null;
  const ms = Date.now() - new Date(raw).getTime();
  return ms > 0 ? ms / 60000 : null; // minutes
}
function fmtAge(mins) {
  if (mins == null) return "—";
  if (mins < 90) return `${Math.round(mins)}m`;
  if (mins < 48 * 60) return `${(mins / 60).toFixed(1)}h`;
  return `${(mins / 1440).toFixed(1)}d`;
}
function liveMcap(t) {
  return Number(t.last_mcap || t.max_mcap || 0);
}
function fmtMcap(n) {
  const v = Number(n || 0);
  if (!v) return "—";
  if (v >= 1e9) return `$${(v / 1e9).toFixed(v >= 10e9 ? 0 : 1)}B`;
  if (v >= 1e6) return `$${(v / 1e6).toFixed(v >= 10e6 ? 1 : 2).replace(/\.0$/, "")}M`;
  if (v >= 1e3) return `$${(v / 1e3).toFixed(v >= 10e3 ? 0 : 1).replace(/\.0$/, "")}k`;
  return `$${Math.round(v).toLocaleString()}`;
}

async function j(url, opts) {
  const timeoutMs = (opts && opts.timeoutMs) || 12000;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(url, { ...(opts || {}), signal: ctrl.signal });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  } finally {
    clearTimeout(timer);
  }
}

function renderMetrics(s) {
  const m = s.model || {};
  const cells = [
    ["Tracked", s.tokens],
    ["Live", s.live],
    ["Labeled", s.labeled],
    ["Model acc / n", `${m.accuracy ?? "—"} / ${m.n_train ?? 0}`],
  ];
  if (runnersHeadline) {
    cells.push(["Runners 5×+", runnersHeadline.runners]);
    cells.push(["Capture rate", runnersHeadline.capture_rate == null ? "—" : pct(runnersHeadline.capture_rate)]);
  }
  if (approachingHeadline) {
    cells.push(["Approaching 2×+", approachingHeadline.approaching]);
  }
  if (lastPaper && lastPaper.avg_return_pct != null) {
    cells.push(["Paper avg", `${lastPaper.avg_return_pct}%`]);
  }
  metricsEl.innerHTML = cells.map(([k,v]) => {
    const tone = k === "Paper avg" && Number(lastPaper?.avg_return_pct) > 0 ? " good" : "";
    return `<div class="metric${tone}"><span>${k}</span><b>${v}</b></div>`;
  }).join("");
  const live = CHAIN === "robinhood" ? (s.integrations?.gmgn || s.integrations?.robinhood) : (s.integrations?.solana_ws || true);
  document.querySelector(".dot").classList.toggle("live", live);
  document.getElementById("status-text").textContent = CHAIN === "robinhood"
    ? `Robinhood Chain · pons · Long/HIMS · Flap / Klik / Noxa · GMGN ${s.integrations?.gmgn ? "on" : "off"} · X API ${s.integrations?.x_api ? "on" : "off"}`
    : `scanning every few seconds · Helius ${s.integrations?.helius ? "on" : "off"} · X API ${s.integrations?.x_api ? "on" : "off"} · GMGN ${s.integrations?.gmgn ? "on" : "off"}`;
}

function sortItems(items) {
  const mode = sortSel.value;
  const arr = [...items];
  if (mode === "score") arr.sort((a, b) => (b.score || 0) - (a.score || 0));
  else if (mode === "multiple") arr.sort((a, b) => (b.multiple || 0) - (a.multiple || 0));
  else if (mode === "mcap") arr.sort((a, b) => liveMcap(b) - liveMcap(a));
  else if (mode === "runner") arr.sort((a, b) => ((runnerMap[b.mint]?.runner_p) || 0) - ((runnerMap[a.mint]?.runner_p) || 0));
  return arr; // newest = API order
}

function matchesSearch(t) {
  const q = (document.getElementById("search")?.value || "").trim().toLowerCase();
  if (!q) return true;
  return [t.symbol, t.name, t.mint, t.launchpad].some(v => String(v || "").toLowerCase().includes(q));
}
function matchesFomoSearch(w) {
  const q = (document.getElementById("search")?.value || "").trim().toLowerCase();
  if (!q) return true;
  const hits = (w.hits || []).map(h => `${h.symbol || ""} ${h.mint || ""}`).join(" ");
  return [w.owner, w.best_symbol, w.best_mint, hits].some(v => String(v || "").toLowerCase().includes(q));
}
function shortWallet(owner) {
  const s = String(owner || "");
  if (s.length <= 10) return s;
  return `${s.slice(0, 4)}…${s.slice(-4)}`;
}
function liveHeadHtml() {
  return CHAIN === "robinhood"
    ? `<tr><th>Token</th><th>Pad</th><th>Age</th><th>Score</th><th>Runner</th><th>X</th><th>Holders</th><th>Top10</th><th>Mcap</th><th>Multiple</th></tr>`
    : `<tr><th>Token</th><th>Age</th><th>Score</th><th>Runner</th><th>X</th><th>Holders</th><th>Top10</th><th>Mcap</th><th>Multiple</th></tr>`;
}
function fomoHeadHtml() {
  return `<tr><th>Wallet</th><th>Books</th><th>Best run</th><th>Best ×</th><th>Last %</th></tr>`;
}
function syncDeskHead() {
  const head = document.getElementById("desk-head");
  if (!head) return;
  head.innerHTML = (deskTab === "fomo" || deskTab === "early" || deskTab === "wallets") ? fomoHeadHtml() : liveHeadHtml();
  if (deskTab === "early") {
    head.innerHTML = `<tr><th>Wallet</th><th>5×+</th><th>Best</th><th>Mark ×</th><th>SOL</th><th>In</th><th>FOMO</th></tr>`;
  }
  if (deskTab === "wallets") {
    head.innerHTML = `<tr><th>Wallet</th><th>Score</th><th>Early</th><th>FOMO</th><th>Best</th><th>Mark ×</th></tr>`;
  }
}

function renderRunnersStrip() {
  const el = document.getElementById("runners-strip");
  if (!el) return;
  const items = (runnersHeadline && runnersHeadline.items) || [];
  if (!items.length) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.hidden = false;
  const capture = runnersHeadline.capture_rate == null ? "—" : pct(runnersHeadline.capture_rate);
  el.innerHTML = `<div class="board-head"><h2>Confirmed 5×+</h2><span class="muted">${runnersHeadline.runners} names · capture ${capture}</span></div>
    <div class="strip">${items.slice(0, 14).map(t =>
      `<a class="runner-chip" href="${esc(t.gmgn)}" target="_blank" rel="noreferrer"><span>${esc(t.symbol || t.mint.slice(0, 6))}</span><b>${t.multiple}×</b>${t.flagged_at_entry ? "" : '<span class="miss">missed</span>'}</a>`
    ).join("")}</div>`;
}

function syncDeskTabs() {
  document.querySelectorAll(".desk-tabs [data-desk]").forEach((btn) => {
    btn.classList.toggle("on", btn.dataset.desk === deskTab);
  });
  const meta = document.getElementById("desk-meta");
  if (meta) {
    if (deskTab === "hot" && hotItems.length) meta.textContent = `${hotItems.length} names · 2×+ now`;
    else if (deskTab === "fomo" && fomoMeta) meta.textContent = `${fomoMeta.wallets || 0} wallets · ${fomoMeta.mapped || 0} this-window of ${fomoMeta.top_n || 100} mapped`;
    else if (deskTab === "early" && earlyMeta) {
      const liveSized = earlyMeta.sized_live != null ? earlyMeta.sized_live : earlyMeta.sized;
      const sized = liveSized != null ? `${liveSized} this-window sized ≥${earlyMeta.min_sol || 0.1} SOL` : `${earlyMeta.wallets || 0} wallets`;
      meta.textContent = `${sized} · ${earlyMeta.still_in || 0} still in · ${earlyMeta.fomo_overlap || 0} also FOMO`;
    }
    else if (deskTab === "wallets" && scoredMeta) meta.textContent = `${scoredMeta.wallets || 0} scored · ${scoredMeta.gold || 0} gold (sized ∩ FOMO win)`;
    else meta.textContent = "";
  }
}

function setDeskTab(tab) {
  deskTab = tab === "hot" ? "hot" : (tab === "fomo" ? "fomo" : (tab === "early" ? "early" : (tab === "wallets" ? "wallets" : "live")));
  const hash = deskTab === "hot" ? "#doing-well" : (deskTab === "fomo" ? "#fomo-wallets" : (deskTab === "early" ? "#early-wallets" : (deskTab === "wallets" ? "#wallets" : "")));
  history.replaceState(null, "", hash || (location.pathname + location.search));
  renderActiveDesk();
}

function renderActiveDesk() {
  syncDeskHead();
  syncDeskTabs();
  if (deskTab === "fomo") renderFomoRows();
  else if (deskTab === "early") renderEarlyRows();
  else if (deskTab === "wallets") renderScoredRows();
  else renderRows(deskTab === "hot" ? hotItems : lastItems, { remember: false });
}

function renderScoredRows() {
  const shown = (scoredItems || []).filter(matchesFomoSearch);
  rowsEl.innerHTML = shown.map(w => `
    <tr data-scored="${esc(w.owner)}" class="${selected === w.owner ? "active" : ""}">
      <td>
        <div class="token">
          <div><strong>${esc(shortWallet(w.owner))}</strong><small>${esc(w.grade || "—")} · ${esc(w.owner)}</small></div>
        </div>
      </td>
      <td>${w.score != null ? Number(w.score).toFixed(0) : "—"}</td>
      <td>${w.n_sized || 0}<small> / ${w.n_early || 0}</small></td>
      <td>${w.n_wins || 0}<small> / ${w.n_fomo || 0}</small>${w.n_rugs ? ` <small>−${w.n_rugs}</small>` : ""}</td>
      <td>${esc(w.best_symbol || "—")}</td>
      <td>${w.best_multiple ? Number(w.best_multiple).toFixed(1) + "×" : "—"}</td>
    </tr>
  `).join("") || `<tr><td colspan="6" class="muted">No scored wallets yet. Built from stored Early + FOMO + alpha maps — no extra GMGN.</td></tr>`;
  rowsEl.querySelectorAll("tr[data-scored]").forEach(tr => {
    tr.onclick = () => openScoredDetail(tr.dataset.scored);
  });
}

function openScoredDetail(owner) {
  selected = owner;
  const w = (scoredItems || []).find(x => x.owner === owner);
  if (!w) return;
  detailEl.classList.remove("empty");
  const hits = (w.hits || []).map(h =>
    `<div class="ref-row"><span><a href="/api/tokens/${esc(h.mint)}">${esc(h.symbol || (h.mint || "").slice(0, 6))}</a> <small>${esc(h.kind || "")}</small></span><b>${Number(h.multiple || 0).toFixed(1)}× · ${h.win ? "win" : "rug"}</b></div>`
  ).join("") || `<p class="muted">No stored hits.</p>`;
  detailEl.innerHTML = `
    <h3>Wallet ${esc(w.grade || "—")} · ${Number(w.score || 0).toFixed(0)}</h3>
    <p class="muted"><a href="${walletExplorer(w.owner)}" target="_blank" rel="noreferrer">${esc(w.owner)}</a></p>
    <p>${w.gold ? "Gold list: sized first-hour (or a sized RH map share) and later FOMO on a confirmed run. Parked historical books do not mint gold. " : ""}Early ${w.n_sized || 0}/${w.n_early || 0} sized · FOMO ${w.n_wins || 0} wins / ${w.n_rugs || 0} rugs · alpha ${w.alpha_runs || 0} · created ${w.n_created || 0}.</p>
    <p class="muted">Stored maps only — no extra GMGN HTTP. Heuristic score, not a FEATURE_NAMES slot.</p>
    <h4>Books</h4>
    ${hits}
    <p class="links"><a href="${walletExplorer(w.owner)}" target="_blank" rel="noreferrer">${CHAIN === "robinhood" ? "Explorer" : "Solscan"}</a></p>
  `;
  document.querySelectorAll("tbody tr").forEach(tr => tr.classList.toggle("active", tr.dataset.scored === owner));
}

function renderEarlyRows() {
  const shown = (earlyItems || []).filter(matchesFomoSearch);
  rowsEl.innerHTML = shown.map(w => `
    <tr data-early="${esc(w.owner)}" class="${selected === w.owner ? "active" : ""}">
      <td>
        <div class="token">
          <div><strong>${esc(shortWallet(w.owner))}</strong><small>${esc(w.owner)}</small></div>
        </div>
      </td>
      <td>${w.n_profitable || 0}<small> / ${w.n_runners || 0}</small></td>
      <td>${esc(w.best_symbol || "—")}</td>
      <td>${w.best_mark_multiple ? Number(w.best_mark_multiple).toFixed(1) + "×" : "—"}</td>
      <td>${w.sum_sol_spent ? Number(w.sum_sol_spent).toFixed(2) : "—"}</td>
      <td>${w.n_still_in || 0}</td>
      <td>${w.fomo ? "yes" : "—"}</td>
    </tr>
  `).join("") || `<tr><td colspan="7" class="muted">No this-window first-hour buys yet. Parked ZCAT stays analytics. Helius only — no extra GMGN.</td></tr>`;
  rowsEl.querySelectorAll("tr[data-early]").forEach(tr => {
    tr.onclick = () => openEarlyDetail(tr.dataset.early);
  });
}

function openEarlyDetail(owner) {
  selected = owner;
  const w = (earlyItems || []).find(x => x.owner === owner);
  if (!w) return;
  detailEl.classList.remove("empty");
  const hits = (w.hits || []).map(h =>
    `<div class="ref-row"><span><a href="/api/tokens/${esc(h.mint)}">${esc(h.symbol || h.mint.slice(0, 6))}</a></span><b>${Number(h.mark_multiple || 0).toFixed(1)}× · ${Number(h.sol_spent || 0).toFixed(2)} SOL · ${h.still_holding ? "still in" : "exited"} · ${Math.round((h.age_at_buy_s || 0) / 60)}m</b></div>`
  ).join("") || `<p class="muted">No early hits.</p>`;
  const firstMin = w.fastest_buy_s ? `${Math.round(Number(w.fastest_buy_s) / 60)}m` : "—";
  detailEl.innerHTML = `
    <h3>Early wallet${w.fomo ? " · FOMO overlap" : ""}</h3>
    <p class="muted"><a href="${walletExplorer(w.owner)}" target="_blank" rel="noreferrer">${esc(w.owner)}</a></p>
    <p>First-hour buy on <b>${w.n_runners}</b> runner${w.n_runners === 1 ? "" : "s"} · <b>${w.n_profitable || 0}</b> still ≥5× at mark. Helius swaps only — no extra GMGN HTTP.</p>
    <p class="muted">Best ${esc(w.best_symbol || "—")} · ${Number(w.best_mark_multiple || 0).toFixed(1)}× · ${Number(w.sum_sol_spent || 0).toFixed(2)} SOL in · first fill ${firstMin} · still in ${w.n_still_in || 0}</p>
    <h4>Books</h4>
    ${hits}
    <p class="links"><a href="${walletExplorer(w.owner)}" target="_blank" rel="noreferrer">${CHAIN === "robinhood" ? "Explorer" : "Solscan"}</a></p>
  `;
  document.querySelectorAll("tbody tr").forEach(tr => tr.classList.toggle("active", tr.dataset.early === owner));
}

function renderFomoRows() {
  const shown = (fomoItems || []).filter(matchesFomoSearch);
  rowsEl.innerHTML = shown.map(w => `
    <tr data-wallet="${esc(w.owner)}" class="${selected === w.owner ? "active" : ""}">
      <td>
        <div class="token">
          <div><strong>${esc(shortWallet(w.owner))}</strong><small>${esc(w.owner)}</small></div>
        </div>
      </td>
      <td>${w.n_tokens || 0}${w.early ? " <small>early</small>" : ""}</td>
      <td>${esc(w.best_symbol || "—")}</td>
      <td>${w.best_multiple ? Number(w.best_multiple).toFixed(1) + "×" : "—"}</td>
      <td>${w.last_pct ? Number(w.last_pct).toFixed(1) + "%" : "—"}</td>
    </tr>
  `).join("") || `<tr><td colspan="5" class="muted">No stored holder maps on the top 100 confirmed 5×+ yet. Maps fill from Helius / Blockscout — no extra GMGN calls.</td></tr>`;
  rowsEl.querySelectorAll("tr[data-wallet]").forEach(tr => {
    tr.onclick = () => openFomoDetail(tr.dataset.wallet);
  });
}

function openFomoDetail(owner) {
  selected = owner;
  const w = (fomoItems || []).find(x => x.owner === owner);
  if (!w) return;
  detailEl.classList.remove("empty");
  const hits = (w.hits || []).map(h =>
    `<div class="ref-row"><span><a href="/api/tokens/${esc(h.mint)}">${esc(h.symbol || h.mint.slice(0, 6))}</a></span><b>${Number(h.multiple || 0).toFixed(1)}× · ${Number(h.pct || 0).toFixed(1)}%</b></div>`
  ).join("") || `<p class="muted">No book hits.</p>`;
  detailEl.innerHTML = `
    <h3>FOMO wallet</h3>
    <p class="muted"><a href="${walletExplorer(w.owner)}" target="_blank" rel="noreferrer">${esc(w.owner)}</a></p>
    <p>Sat in the top holder map of <b>${w.n_tokens}</b> of the desk's top 100 confirmed 5×+ runners.${w.early ? " Also in the first-hour early-wallet book." : ""} Creator and pool addresses are excluded. Stored maps only — no extra GMGN HTTP.</p>
    <p class="muted">Best run ${esc(w.best_symbol || "—")} · ${Number(w.best_multiple || 0).toFixed(1)}× · last ${Number(w.last_pct || 0).toFixed(1)}%</p>
    <h4>Books</h4>
    ${hits}
    <p class="links"><a href="${walletExplorer(w.owner)}" target="_blank" rel="noreferrer">${CHAIN === "robinhood" ? "Explorer" : "Solscan"}</a></p>
  `;
  document.querySelectorAll("tbody tr").forEach(tr => tr.classList.toggle("active", tr.dataset.wallet === owner));
}

function renderRows(items, { remember = true } = {}) {
  if (remember) lastItems = items;
  const floor = deskTab === "hot" ? Number(minScore?.value || 0) : 0;
  const shown = sortItems(items).filter(matchesSearch).filter((t) => (t.score || 0) >= floor);
  const cols = CHAIN === "robinhood" ? 10 : 9;
  const empty = deskTab === "hot"
    ? "No 2×+ climbs yet — first honest runner still pending."
    : "Waiting for the first newly migrated coin…";
  rowsEl.innerHTML = shown.map(t => {
    const rw = runnerMap[t.mint];
    const runnerCell = rw
      ? `<span class="score ${scoreClass(rw.runner_p * 100)}">${pct(rw.runner_p)}</span>${rw.second_leg ? " ⤴" : ""}`
      : "—";
    const bad = hardFlag(t) ? ` <span class="badge-risk" title="hard-stop flag">🚩</span>` : "";
    const pad = String(t.launchpad || "").toLowerCase();
    const padCell = CHAIN === "robinhood"
      ? `<td><span class="pad${pad === "pons" ? " pons" : ""}">${esc(pad || "—")}</span></td>`
      : "";
    return `
    <tr data-mint="${esc(t.mint)}" class="${selected === t.mint ? "active" : ""}">
      <td>
        <div class="token">
          <img src="${esc(t.image_url)}" alt="" onerror="this.style.visibility='hidden'" />
          <div><strong>${esc(t.symbol || "?")}${bad}</strong><small>${esc(t.name)}</small></div>
        </div>
      </td>
      ${padCell}
      <td>${fmtAge(ageOf(t))}</td>
      <td class="score ${scoreClass(t.score || 0, t)}" title="${isHeldIngestMiss(t) ? "Ingest miss — book already ran" : ""}">${(t.score || 0).toFixed(1)}%</td>
      <td>${runnerCell}</td>
      <td>${t.twitter_handle ? `@${esc(t.twitter_handle)} · ${t.twitter_followers}` : "—"}</td>
      <td>${t.holder_count || "—"}</td>
      <td>${t.top10_pct ? t.top10_pct.toFixed(1) + "%" : "—"}</td>
      <td title="${liveMcap(t) ? "$" + Math.round(liveMcap(t)).toLocaleString() : ""}">${fmtMcap(liveMcap(t))}</td>
      <td>${t.multiple ? t.multiple.toFixed(2) + "x" : "—"}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="${cols}" class="muted">${empty}</td></tr>`;
  rowsEl.querySelectorAll("tr[data-mint]").forEach(tr => {
    tr.onclick = () => openDetail(tr.dataset.mint);
  });
}

async function openDetail(mint) {
  selected = mint;
  const t = await j(`/api/tokens/${mint}`);
  detailEl.classList.remove("empty");
  detailEl.innerHTML = `
    <h3>${esc(t.symbol)} · ${esc(t.name)}</h3>
    <p class="score ${scoreClass(t.score, t)}">${scoreCaption(t)}</p>
    <p>${esc(t.thesis)}</p>
    <div class="flags">
      ${(t.reasons || []).map(x => `<span>${esc(x)}</span>`).join("")}
      ${(t.risk_flags || []).map(x => `<span class="risk">${esc(x)}</span>`).join("")}
    </div>
    <p class="muted">Creator ${esc(t.creator).slice(0,6)}… · prior ${t.creator_prior_launches} / wins ${t.creator_prior_wins} / rugs ${t.creator_prior_rugs} · holds ${t.creator_hold_pct || 0}%</p>
    <p class="muted">Holders ${t.holder_count || "—"} · top1 ${t.top1_pct || 0}% · top10 ${t.top10_pct || 0}% · fresh top wallets ${t.fresh_wallet_pct || 0}%</p>
    ${runnerLine(runnerMap[mint])}
    ${gmgnLine(t.gmgn)}
    ${walletTable(t.top_wallets || [])}
    <p class="links">
      ${t.links?.pons ? `<a href="${t.links.pons}" target="_blank" rel="noreferrer">pons</a>` : ""}
      ${t.links?.pump ? `<a href="${t.links.pump}" target="_blank" rel="noreferrer">Pump</a>` : ""}
      ${t.links?.dex ? `<a href="${t.links.dex}" target="_blank" rel="noreferrer">DexScreener</a>` : ""}
      ${t.links?.explorer ? `<a href="${t.links.explorer}" target="_blank" rel="noreferrer">${t.chain === "robinhood" ? "Explorer" : "Solscan"}</a>` : (t.links?.solscan ? `<a href="${t.links.solscan}" target="_blank" rel="noreferrer">Solscan</a>` : "")}
      ${t.links?.gmgn ? `<a href="${t.links.gmgn}" target="_blank" rel="noreferrer">GMGN</a>` : ""}
      ${t.twitter_handle ? `<a href="https://x.com/${esc(t.twitter_handle)}" target="_blank" rel="noreferrer">X</a>` : ""}
      ${t.github_url ? `<a href="${esc(t.github_url)}" target="_blank" rel="noreferrer">GitHub</a>` : ""}
    </p>
  `;
  document.querySelectorAll("tbody tr").forEach(tr => tr.classList.toggle("active", tr.dataset.mint === mint));
}

function runnerLine(rw) {
  if (!rw) return "";
  const f = rw.forensics || {};
  const bits = [
    `runner potential <b>${pct(rw.runner_p)}</b>`,
    `${rw.mcap_ratio}× entry`,
    rw.second_leg ? "second leg ⤴" : "",
    f.funding_cluster != null ? `funding cluster ${pct(f.funding_cluster)}` : "",
    f.diamond != null ? `diamond ${pct(f.diamond)}` : "",
    f.early_exit != null ? `early exits ${pct(f.early_exit)}` : "",
  ].filter(Boolean).join(" · ");
  const flags = (rw.forensic_flags || []).map(x => `<span class="risk">${esc(x)}</span>`).join("");
  return `<p class="muted">${bits}</p>${flags ? `<div class="flags">${flags}</div>` : ""}`;
}

function fmtCompact(n) {
  const x = Number(n) || 0;
  if (Math.abs(x) >= 1e6) return (x / 1e6).toFixed(1) + "M";
  if (Math.abs(x) >= 1e3) return (x / 1e3).toFixed(1) + "K";
  if (Math.abs(x) >= 10) return String(Math.round(x));
  return String(Math.round(x * 10) / 10);
}
function gmgnLine(g) {
  if (!g || !g.present) return `<p class="muted">GMGN trenches snapshot not loaded for this name (factory/Dex ingest skips extra GMGN calls).</p>`;
  const bits = [];
  if (g.og) bits.push("OG");
  if (g.launchpad) bits.push(esc(g.launchpad));
  const buy = Number(g.buy_tax_pct) || 0;
  const sell = Number(g.sell_tax_pct) || 0;
  if (buy || sell) bits.push(`tax ${fmtCompact(buy)}/${fmtCompact(sell)}%`);
  const prog = Number(g.progress) || 0;
  if (prog >= 0.02 && prog < 0.995) bits.push(`bonded ${Math.round((prog <= 1 ? prog * 100 : prog))}%`);
  if (g.swaps_1h) bits.push(`TX ${fmtCompact(g.swaps_1h)}`);
  if (g.volume_1h) bits.push(`V $${fmtCompact(g.volume_1h)}`);
  if (g.net_buy_24h) bits.push(`N+$${fmtCompact(g.net_buy_24h)}`);
  if (g.lock_pct) bits.push(`lock ${fmtCompact(g.lock_pct)}%`);
  if (g.burned) bits.push("burned");
  if (g.dev_hold_pct) bits.push(`dev ${fmtCompact(g.dev_hold_pct)}%`);
  if (g.top10_pct) bits.push(`top10 ${fmtCompact(g.top10_pct)}%`);
  if (g.dev_sold) bits.push("dev sold");
  else if (g.creator_status === "creator_hold") bits.push("dev holding");
  if (g.smart_degen) bits.push(`smart ${g.smart_degen}`);
  if (g.renowned) bits.push(`KOL ${g.renowned}`);
  if (g.twitter_username) bits.push(`@${esc(g.twitter_username)}${g.twitter_followers ? " " + fmtCompact(g.twitter_followers) : ""}`);
  bits.push(`rug ${g.rug_risk || 0}`);
  bits.push(`insider ${g.insider_pct || 0}%`);
  bits.push(`sniper ${g.sniper_pct || 0}%`);
  const flags = [
    g.honeypot ? `<span class="risk">honeypot</span>` : "",
    g.bundled ? `<span class="risk">bundled</span>` : "",
    g.cto ? `<span class="risk">CTO</span>` : "",
  ].filter(Boolean).join(" ");
  return `<p class="muted">GMGN ${bits.join(" · ")}</p>${flags ? `<div class="flags">${flags}</div>` : ""}`;
}

function walletExplorer(owner) {
  return CHAIN === "robinhood"
    ? `https://robinhoodchain.blockscout.com/address/${esc(owner)}`
    : `https://solscan.io/account/${esc(owner)}`;
}

function walletTable(wallets) {
  if (!wallets.length) {
    return CHAIN === "robinhood"
      ? `<p class="muted">No wallet map yet. Holder lookup runs on the next research pass.</p>`
      : `<p class="muted">No wallet map yet. Helius owner lookup runs on the next research pass.</p>`;
  }
  const rows = wallets.map(w => {
    const tag = w.label ? ` <span class="risk">${esc(w.label)}</span>` : "";
    const fresh = w.fresh ? ` <span class="risk">fresh</span>` : "";
    const age = w.age_hours != null ? `${w.age_hours}h sample` : "—";
    return `<tr>
      <td><a href="${walletExplorer(w.owner)}" target="_blank" rel="noreferrer">${esc(w.owner).slice(0,4)}…${esc(w.owner).slice(-4)}</a>${tag}${fresh}</td>
      <td>${(w.pct || 0).toFixed(2)}%</td>
      <td>${w.sigs ?? "—"}</td>
      <td>${age}</td>
    </tr>`;
  }).join("");
  return `<table class="wallets"><thead><tr><th>Wallet</th><th>%</th><th>Sigs</th><th>Age</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderEval(model) {
  const el = document.getElementById("evaluation");
  const e = model.evaluation || {};
  if (!e.n) { el.innerHTML = `<p class="muted">Waiting for labeled outcomes…</p>`; return; }
  el.innerHTML = [
    ["Resolved outcomes", e.n],
    ["Base win rate", pct(e.base_win_rate)],
    ["Top-decile precision", pct(e.precision_top_decile)],
    ["Bottom-decile win rate", pct(e.win_rate_bottom_decile)],
    ["Brier score (lower = better)", e.brier],
  ].map(([k,v]) => `<div class="ref-row"><span>${k}</span><b>${v}</b></div>`).join("");
}

function renderCalibration(data) {
  const el = document.getElementById("calibration");
  const bins = (data.bins || []).filter(b => b.n >= 10);
  if (!bins.length) { el.innerHTML = `<p class="muted">Waiting…</p>`; return; }
  el.innerHTML = bins.map(b =>
    `<div class="ref-row"><span>${esc(b.bin)} <small class="muted">n=${b.n}</small></span><b>${pct(b.predicted)} → ${pct(b.actual)}</b></div>`
  ).join("");
}

function paperQuery() {
  const raw = (paperMode && paperMode.value) || "moonbag-5-10";
  const parts = raw.split("-");
  const mode = parts[0] === "flat" ? "flat" : "moonbag";
  const tgt = parts[1] || (mode === "flat" ? "5" : "5");
  const ride = parts[2] || "10";
  const minP = CHAIN === "robinhood" ? "0.5" : "0.7";
  return `/api/paper?min_p=${minP}&target=${tgt}&ride=${ride}&strategy=${mode}`;
}

function renderPaper(p) {
  lastPaper = p;
  const el = document.getElementById("paper");
  if (!p || (!p.closed_trades && !p.open_positions)) {
    el.innerHTML = CHAIN === "robinhood"
      ? `<p class="muted">No p≥50% names yet — RH scores cap below Solana’s 0.70 buy line.</p>`
      : `<p class="muted">No positions yet.</p>`;
    return;
  }
  const head = [
    ["Rule", p.strategy || "paper"],
    ["Open", p.open_positions],
    ["Closed", p.closed_trades],
    ["Wins", p.wins],
    ["Avg return", p.avg_return_pct == null ? "—" : `${p.avg_return_pct}%`],
  ].map(([k,v]) => `<div class="ref-row"><span>${k}</span><b>${v}</b></div>`).join("");
  const rows = (p.closed || []).slice(0, 8).map(t =>
    `<div class="ref-row"><span>${esc(t.symbol || t.mint.slice(0,6))}</span><b class="${t.return_pct >= 0 ? "" : "risk"}">${t.return_pct > 0 ? "+" : ""}${t.return_pct}%</b></div>`
  ).join("");
  el.innerHTML = head + (rows ? `<h4>Recent closes</h4>${rows}` : "");
}

function renderConviction(data) {
  const el = document.getElementById("conviction");
  const a = data.tier_a || [];
  const b = data.tier_b || [];
  if (!a.length && !b.length) { el.innerHTML = `<p class="muted">No Tier A/B tokens right now — that's normal.</p>`; return; }
  const row = (t, tier) =>
    `<div class="ref-row"><span><b class="${tier === "A" ? "score high" : "muted"}">${tier}</b> <a href="${esc(t.gmgn)}" target="_blank" rel="noreferrer">${esc(t.symbol || t.mint.slice(0,6))}</a>${t.second_leg ? " ⤴" : ""}</span><b>${pct(t.runner_p)} · ${t.mcap_ratio}x</b></div>`;
  el.innerHTML = a.map(t => row(t, "A")).join("") + b.slice(0, 6).map(t => row(t, "B")).join("");
}

function renderProjects(data) {
  const el = document.getElementById("projects");
  const rows = (data.items || []).slice(0, 8);
  if (!rows.length) { el.innerHTML = `<p class="muted">None found yet — they're rare.</p>`; return; }
  el.innerHTML = rows.map(t =>
    `<div class="ref-row"><span><a href="${esc(t.gmgn)}" target="_blank" rel="noreferrer">${esc(t.symbol)}</a> <a href="${esc(t.github)}" target="_blank" rel="noreferrer">gh${t.github_stars ? "★" + t.github_stars : ""}</a> ${t.x_handle ? `<a href="https://x.com/${esc(t.x_handle)}" target="_blank" rel="noreferrer">@${esc(t.x_handle)}</a>` : ""}</span><b>${pct(t.p_good)}</b></div>`
  ).join("");
}

function renderApproaching(data) {
  const el = document.getElementById("approaching");
  if (!el) return;
  const rows = data.items || [];
  const thin = data.thin || [];
  if (!rows.length && !thin.length) {
    el.innerHTML = `<p class="muted">No 2×+ climbs yet — first honest 5× still pending.</p>`;
    return;
  }
  const line = (t, extra) => {
    const liq = t.last_liq ? ` · $${Math.round(t.last_liq).toLocaleString()} liq` : "";
    return `<div class="ref-row"><span><a href="${esc(t.gmgn)}" target="_blank" rel="noreferrer">${esc(t.symbol || t.mint.slice(0,6))}</a>${extra}</span><b>${(t.multiple || 0).toFixed(2)}× · ${t.holders || "?"} h${liq} · ${pct(t.entry_p)}</b></div>`;
  };
  const honest = rows.map(t => line(t, "")).join("");
  const prints = thin.length
    ? `<h4>Thin prints <small class="muted">(&lt;20 holders — not a runner)</small></h4>` + thin.slice(0, 4).map(t => line(t, " <span class=\"badge-risk\">thin</span>")).join("")
    : "";
  el.innerHTML = honest + prints;
}

function renderRunnerWatch(data) {
  const el = document.getElementById("runnerwatch");
  const rows = (data.items || []).slice(0, 10);
  if (!rows.length) { el.innerHTML = `<p class="muted">Waiting for 1h snapshots…</p>`; return; }
  el.innerHTML = rows.map(t => {
    const leg = t.second_leg ? " ⤴" : "";
    return `<div class="ref-row"><span><a href="${esc(t.gmgn)}" target="_blank" rel="noreferrer">${esc(t.symbol || t.mint.slice(0,6))}</a>${leg}</span><b>${pct(t.runner_p)} · ${t.mcap_ratio}x</b></div>`;
  }).join("");
}

function renderWatch(data) {
  const el = document.getElementById("watch");
  const rows = data.graduating_soon || [];
  if (!rows.length) { el.innerHTML = `<p class="muted">Waiting for pons factory / near-completion data…</p>`; return; }
  el.innerHTML = rows.slice(0, 10).map(t => {
    const href = t.pons || t.gmgn;
    const prog = t.progress ? ` · ${Math.round(Number(t.progress) <= 1 ? t.progress * 100 : t.progress)}%` : "";
    const warm = t.prewarmed ? " · ready" : "";
    return `<div class="ref-row"><span><a href="${esc(href)}" target="_blank" rel="noreferrer">${esc(t.symbol || t.mint.slice(0,6))}</a></span><b>${t.mcap_usd ? "$" + Math.round(t.mcap_usd).toLocaleString() : "—"}${prog}${warm}</b></div>`;
  }).join("");
}

function renderRef(data) {
  const block = (title, rows) => `
    <h4>${title}</h4>
    ${rows.map(t => `<div class="ref-row"><span>${esc(t.symbol)}</span><b>${(t.multiple || 0).toFixed(2)}x · ${t.score.toFixed(0)}%</b></div>`).join("") || `<p class="muted">Not enough labels yet.</p>`}
  `;
  referenceEl.innerHTML = block("Winners the model is learning from", data.winners) + block("Losers / dumps", data.losers);
}

function applyFomoPayload(fomo) {
  fomoItems = Array.isArray(fomo) ? fomo : (fomo.items || []);
  fomoMeta = {
    mapped: Number(fomo.mapped || 0),
    wallets: Number(fomo.wallets || fomoItems.length || 0),
    top_n: Number(fomo.top_n || 100),
    source: String(fomo.source || "stored_holder_maps"),
  };
}
function applyEarlyPayload(early) {
  earlyItems = Array.isArray(early) ? early : (early.items || []);
  earlyMeta = {
    wallets: Number(early.wallets || earlyItems.length || 0),
    sized: Number(early.sized || 0),
    still_in: Number(early.still_in || 0),
    fomo_overlap: Number(early.fomo_overlap || 0),
    min_sol: Number(early.min_sol || 0.1),
    source: String(early.source || "helius_early_swaps"),
  };
}
function applyScoredPayload(scored) {
  const incoming = Array.isArray(scored) ? scored : (scored.items || []);
  const q = (document.getElementById("search")?.value || "").trim();
  const pinned = q.length >= 32
    ? scoredItems.find((w) => w.owner === q || String(w.owner || "").toLowerCase() === q.toLowerCase())
    : null;
  scoredItems = incoming;
  if (pinned && !scoredItems.some((w) => w.owner === pinned.owner)) scoredItems = [pinned, ...scoredItems];
  scoredMeta = {
    wallets: Number(scored.wallets || scoredItems.length || 0),
    gold: Number(scored.gold || 0),
    scored: Number(scored.scored || scoredItems.length || 0),
    source: String(scored.source || "stored_maps_and_early_swaps"),
  };
}

async function refreshWalletDesks() {
  const [fomo, early, scored] = await Promise.all([
    j(withChain("/api/fomo-wallets")).catch(() => ({ items: fomoItems })),
    j(withChain("/api/early-wallets")).catch(() => ({ items: earlyItems })),
    j(withChain("/api/wallets")).catch(() => ({ items: scoredItems })),
  ]);
  applyFomoPayload(fomo);
  applyEarlyPayload(early);
  applyScoredPayload(scored);
  if (deskTab === "fomo" || deskTab === "early" || deskTab === "wallets") renderActiveDesk();
}

let refreshBusy = false;
async function refresh() {
  if (refreshBusy) return;
  refreshBusy = true;
  try {
  const hist = liveOnly.checked ? false : null;
  const q = new URLSearchParams({ min_score: minScore.value, limit: "80", fresh_hours: liveOnly.checked ? (CHAIN === "robinhood" ? "12" : "4") : "168" });
    if (hist === false) q.set("historical", "false");
  const [status, tokens, ref, model, paper, watch, calib, rw, projects, hot] = await Promise.all([
    j(withChain("/api/status")),
    j(withChain(`/api/tokens?${q}`)),
    j(withChain("/api/reference")),
    j(withChain("/api/model")),
    j(withChain(paperQuery()), { timeoutMs: 20000 }),
    j(withChain("/api/watch")),
    j(withChain("/api/model/calibration")),
    j(withChain("/api/runnerwatch")),
    j(withChain("/api/projects")),
    j(withChain("/api/doing-well")).catch(() => ({ items: hotItems })),
  ]);
  const conviction = await j(withChain("/api/conviction"));
  runnerMap = {};
  (rw.items || []).forEach(it => { runnerMap[it.mint] = it; });
  if (headlineTick % 5 === 0) {
    try { runnersHeadline = await j(withChain("/api/runners?min_multiple=5")); } catch (e) { /* keep old */ }
    try { approachingHeadline = await j(withChain("/api/approaching?min_multiple=2")); } catch (e) { /* keep old */ }
  }
  headlineTick += 1;
  lastPaper = paper;
  lastItems = tokens;
  hotItems = hot.items || [];
  renderMetrics(status);
  renderRunnersStrip();
  renderActiveDesk();
  renderRef(ref);
  renderEval(model);
  renderPaper(paper);
  renderWatch(watch);
  renderCalibration(calib);
  renderRunnerWatch(rw);
  renderProjects(projects);
  renderConviction(conviction);
  renderApproaching(approachingHeadline || { items: [], thin: [] });
  refreshWalletDesks().catch(() => {});
  } finally {
    refreshBusy = false;
  }
}

minScore.oninput = () => {
  minLabel.textContent = `${minScore.value}%`;
  renderActiveDesk();
};
minScore.onchange = refresh;
liveOnly.onchange = refresh;
sortSel.onchange = () => renderActiveDesk();
if (paperMode) paperMode.onchange = refresh;
const searchEl = document.getElementById("search");
if (searchEl) searchEl.oninput = () => renderActiveDesk();
document.querySelectorAll(".desk-tabs [data-desk]").forEach((btn) => {
  btn.onclick = () => setDeskTab(btn.dataset.desk);
});
window.addEventListener("hashchange", () => {
  setDeskTab(tabFromHash());
});
if (searchEl) {
  const _search = searchEl.oninput;
  searchEl.oninput = () => {
    if (typeof _search === "function") _search();
    const q = (searchEl.value || "").trim();
    if (deskTab === "wallets" && q.length >= 32) {
      j(withChain(`/api/wallets/${encodeURIComponent(q)}`)).then((card) => {
        if (!card || !card.owner) return;
        if (!scoredItems.some((w) => w.owner === card.owner)) scoredItems = [card, ...scoredItems];
        else scoredItems = scoredItems.map((w) => (w.owner === card.owner ? card : w));
        renderActiveDesk();
        openScoredDetail(card.owner);
      }).catch(() => {});
    }
  };
}
syncDeskTabs();
document.getElementById("scan").onclick = async () => {
  document.getElementById("scan").disabled = true;
  try { await j(withChain("/api/scan-now"), { method: "POST" }); await refresh(); }
  finally { document.getElementById("scan").disabled = false; }
};

refresh();
setInterval(refresh, 12000);
