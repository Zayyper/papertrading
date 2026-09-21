/* hl-niche-screener web UI. Vanilla JS, no build step.
   Talks to the local server in server.py: jobs (subprocesses of the CLI), runs (files in out/), config. */
(() => {
  "use strict";

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private window etc. */ } },
  };

  async function api(method, url, body, raw) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.body = raw ? body : JSON.stringify(body);
      opts.headers["Content-Type"] = raw ? "text/plain; charset=utf-8" : "application/json";
    }
    const r = await fetch(url, opts);
    let data = null;
    try { data = await r.json(); } catch { /* non-JSON error page */ }
    if (!r.ok) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
    return data;
  }

  // ---------------------------------------------------------------- formatting
  const isNum = (x) => typeof x === "number" && Number.isFinite(x);
  const fmtPct = (x, nd = 1) => x === "inf" ? "∞" : x === "-inf" ? "−∞" : isNum(x) ? (x * 100).toFixed(nd) + "%" : "n/a";
  const fmtNum = (x, nd = 2) => x === "inf" ? "∞" : x === "-inf" ? "−∞" : isNum(x) ? x.toLocaleString("en-US", { minimumFractionDigits: nd, maximumFractionDigits: nd }) : "n/a";
  const fmtInt = (x) => isNum(x) ? Math.round(x).toLocaleString("en-US") : "n/a";
  const fmtUsd = (x) => isNum(x) ? (x < 0 ? "−" : "") + "$" + Math.abs(x).toLocaleString("en-US", { maximumFractionDigits: 0 }) : "n/a";
  const fmtUsdShort = (x) => { if (!isNum(x)) return ""; const a = Math.abs(x); const s = a >= 1e6 ? (a / 1e6).toFixed(1) + "M" : a >= 1e3 ? (a / 1e3).toFixed(a >= 1e4 ? 0 : 1) + "k" : a.toFixed(0); return (x < 0 ? "−" : "") + "$" + s; };
  const fmtDate = (ms) => isNum(ms) ? new Date(ms).toISOString().slice(0, 10) : "";
  const signCls = (x) => isNum(x) ? (x > 0 ? "pos" : x < 0 ? "neg" : "") : "";
  const pct = (x, nd = 1) => `<span class="${signCls(x)}">${fmtPct(x, nd)}</span>`;
  const shortAddr = (a) => a ? a.slice(0, 6) + "…" + a.slice(-4) : "";
  const elapsed = (s) => { s = Math.max(0, Math.floor(s)); const m = Math.floor(s / 60), h = Math.floor(m / 60); return h ? `${h}h ${m % 60}m` : m ? `${m}m ${s % 60}s` : `${s}s`; };
  const parseNum = (s) => { const v = parseFloat(String(s).replace(/,/g, "")); return Number.isFinite(v) ? v : null; };

  let toastTimer = null;
  function toast(msg, kind = "err") {
    let el = $(".toast");
    if (!el) { el = document.createElement("div"); document.body.appendChild(el); }
    el.className = "toast " + kind; el.textContent = msg;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => el.remove(), 5000);
  }

  // ---------------------------------------------------------------- state / views
  const state = {
    view: store.get("view", "run"), job: null, since: 0, pollTimer: null, tick: null,
    runs: [], runId: null, detail: null, config: null, tradersFilter: { passedOnly: false, q: "" },
  };

  function showView(name) {
    state.view = name; store.set("view", name);
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + name));
    $$(".tab").forEach((t) => { const on = t.dataset.view === name; t.classList.toggle("active", on); t.setAttribute("aria-selected", on); });
    if (name === "results") refreshRuns().then(redrawCharts);
    if (name === "paper") startPaperPolling(); else stopPaperPolling();
    if (name === "pump") startPumpPolling(); else stopPumpPolling();
    if (name === "config" && !state.config) loadConfig();
    if (name === "design") renderDesign();
  }

  // ---------------------------------------------------------------- jobs
  const consoleOut = $("#console-out");

  function appendLines(lines) {
    if (!lines || !lines.length) return;
    const atBottom = consoleOut.scrollHeight - consoleOut.scrollTop - consoleOut.clientHeight < 48;
    const frag = document.createDocumentFragment();
    for (const l of lines) {
      const span = document.createElement("span");
      span.textContent = l + "\n";
      if (/\bWARNING\b|\bwarning\b/.test(l)) span.className = "line-warn";
      if (/Traceback|Error\b|error:|giving up/.test(l)) span.className = "line-err";
      frag.appendChild(span);
    }
    consoleOut.appendChild(frag);
    if (atBottom) consoleOut.scrollTop = consoleOut.scrollHeight;
  }

  function setJob(job, reset) {
    state.job = job;
    const status = job ? job.status : "idle";
    const badge = $("#console-status"), pill = $("#job-pill");
    badge.textContent = status; badge.className = "badge " + status;
    pill.textContent = job && status === "running" ? `running ${job.cmd}` : "idle";
    pill.className = "badge " + (status === "running" ? "running" : "");
    $("#console-cmd").textContent = job ? `python -m hl_screener ${job.argv.join(" ")}` : "";
    $("#btn-stop").hidden = !(job && status === "running");
    $("#btn-results").hidden = !(job && status === "done" && (job.cmd === "run" || job.cmd === "demo"));
    $$("form.action button[type=submit]").forEach((b) => { b.disabled = status === "running"; });
    if (reset) { consoleOut.textContent = ""; state.since = 0; $("#pool-panel").hidden = true; $("#inspect-panel").hidden = true; }
    updateElapsed();
  }

  function updateElapsed() {
    const j = state.job;
    $("#console-elapsed").textContent = j ? elapsed((j.ended || Date.now() / 1000) - j.started) : "";
  }

  async function startJob(body) {
    try {
      const r = await api("POST", "/api/jobs", body);
      setJob(r.job, true);
      appendLines(r.job.lines); state.since = r.job.next;
      startPolling();
      $("#console-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (e) { toast(e.message); }
  }

  function startPolling() {
    stopPolling();
    state.pollTimer = setInterval(pollJob, 700);
    state.tick = setInterval(updateElapsed, 1000);
    pollJob();
  }
  function stopPolling() {
    clearInterval(state.pollTimer); clearInterval(state.tick);
    state.pollTimer = state.tick = null;
    updateElapsed();
  }

  async function pollJob() {
    let r;
    try { r = await api("GET", `/api/jobs/current?since=${state.since}`); } catch { return; /* server busy or gone; try again next tick */ }
    const job = r.job;
    if (!job) { setJob(null); stopPolling(); return; }
    if (state.job && state.job.id !== job.id) { setJob(job, true); }
    appendLines(job.lines); state.since = job.next;
    setJob(job);
    if (job.status !== "running") { stopPolling(); onJobFinished(job); }
  }

  function onJobFinished(job) {
    if (job.cmd === "pool") renderPool(consoleOut.textContent);
    if (job.cmd === "inspect") renderInspect(consoleOut.textContent, job.argv);
    if ((job.cmd === "run" || job.cmd === "demo") && job.status === "done") {
      refreshRuns({ selectNewest: true });
      toast(`${job.cmd} finished — results are ready`, "ok");
    }
  }

  // pool: "0x…  equity=    12,345  month_roi=+0.123  alltime_vlm=1,234,567"
  const POOL_RE = /^(0x[0-9a-fA-F]{40})\s+equity=\s*(\S+)\s+month_roi=\s*(\S+)\s+alltime_vlm=\s*(\S+)/;
  function renderPool(text) {
    const rows = [];
    for (const l of text.split("\n")) {
      const m = POOL_RE.exec(l);
      if (m) rows.push({ address: m[1].toLowerCase(), equity: parseNum(m[2]), month_roi: parseNum(m[3]), vlm: parseNum(m[4]) });
    }
    if (!rows.length) return;
    $("#pool-panel").hidden = false;
    $("#pool-count").textContent = `${rows.length} accounts would be screened`;
    sortableTable($("#pool-table"), [
      { key: "address", label: "Address", render: (r) => `<span class="addr">${r.address}</span>` },
      { key: "equity", label: "Equity", num: true, render: (r) => fmtUsd(r.equity) },
      { key: "month_roi", label: "Leaderboard month ROI", num: true, render: (r) => pct(r.month_roi) },
      { key: "vlm", label: "All-time volume", num: true, render: (r) => fmtUsd(r.vlm) },
      { key: "", label: "", render: (r) => `<button class="btn btn-tertiary btn-sm" data-inspect="${r.address}">Inspect</button>` },
    ], rows, { sortKey: "equity", dir: -1 });
  }

  // inspect: "     key: value" blocks, then round-trip lines
  function renderInspect(text, argv) {
    const metrics = [], follower = [], trips = [];
    let section = 0;
    for (const l of text.split("\n")) {
      if (/^--- follower/.test(l)) { section = 1; continue; }
      if (/^--- last/.test(l)) { section = 2; continue; }
      const m = /^\s*([A-Za-z_]\w*):\s(.*)$/.exec(l);
      if (section < 2 && m) (section === 0 ? metrics : follower).push([m[1], m[2]]);
      else if (section === 2 && l.trim()) trips.push(l);
    }
    if (!metrics.length && !follower.length) return;
    $("#inspect-panel").hidden = false;
    $("#inspect-addr").textContent = (argv || []).find((a) => /^0x/i.test(a)) || "";
    $("#inspect-metrics").innerHTML = kvHtml(metrics);
    $("#inspect-follower").innerHTML = kvHtml(follower);
    $("#inspect-trips").textContent = trips.join("\n");
  }
  const prettyVal = (v) => { const f = parseFloat(v); return Number.isFinite(f) && /^-?\d+\.\d{5,}(e-?\d+)?$/.test(v.trim()) ? f.toFixed(4) : v; };
  const kvHtml = (pairs) => `<dl class="kv">${pairs.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(prettyVal(v))}</dd>`).join("")}</dl>`;

  // ---------------------------------------------------------------- tables
  function cmpVals(a, b, dir) {
    const nil = (x) => x === null || x === undefined || x === "";
    if (nil(a) && nil(b)) return 0;
    if (nil(a)) return 1;            // nulls last whatever the direction
    if (nil(b)) return -1;
    const num = (x) => x === "inf" ? Infinity : x === "-inf" ? -Infinity : x;
    a = num(a); b = num(b);
    if (typeof a === "number" && typeof b === "number") return (a - b) * dir;
    if (typeof a === "boolean" && typeof b === "boolean") return ((a ? 1 : 0) - (b ? 1 : 0)) * dir;
    return String(a).localeCompare(String(b)) * dir;
  }

  function sortableTable(container, cols, rows, opts = {}) {
    const st = container._sort || (container._sort = { key: opts.sortKey || null, dir: opts.dir || -1 });
    const sorted = rows.slice();
    if (st.key) sorted.sort((a, b) => cmpVals(a[st.key], b[st.key], st.dir));
    const th = cols.map((c) => `<th class="${c.num ? "num" : ""}${c.key ? " sortable" : ""}${st.key && st.key === c.key ? " sorted" + (st.dir > 0 ? " asc" : "") : ""}" data-key="${c.key || ""}" title="${esc(c.title || "")}">${c.label}</th>`).join("");
    const body = sorted.length
      ? sorted.map((r, i) => `<tr>${cols.map((c) => `<td class="${c.num ? "num" : ""}">${c.render ? c.render(r, i) : esc(r[c.key])}</td>`).join("")}</tr>`).join("")
      : `<tr><td colspan="${cols.length}" class="empty">${esc(opts.empty || "Nothing to show.")}</td></tr>`;
    container.innerHTML = `<table class="data"><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table>`;
    container.querySelectorAll("th.sortable").forEach((h) => h.addEventListener("click", () => {
      const k = h.dataset.key;
      if (st.key === k) st.dir = -st.dir; else { st.key = k; st.dir = -1; }
      sortableTable(container, cols, rows, opts);
    }));
  }

  // ---------------------------------------------------------------- runs
  async function refreshRuns(opts = {}) {
    let r;
    try { r = await api("GET", "/api/runs"); } catch (e) { toast(e.message); return; }
    state.runs = r.runs;
    if (opts.selectNewest && state.runs.length) state.runId = state.runs[0].id;
    if (state.runId && !state.runs.some((x) => x.id === state.runId)) { state.runId = null; state.detail = null; }
    if (!state.runId && state.runs.length) state.runId = state.runs[0].id;
    renderRunList();
    if (state.runId && (!state.detail || state.detail.id !== state.runId || opts.selectNewest)) selectRun(state.runId);
    if (!state.runs.length) { state.detail = null; $("#run-detail").innerHTML = ""; }
  }

  function renderRunList() {
    const el = $("#runs-list");
    if (!state.runs.length) {
      el.innerHTML = `<div class="card"><div class="card-title">No runs yet</div><p class="card-body">Run the demo to see what a report looks like, or start a real screen from the Run tab.</p><div class="card-foot"><button class="btn btn-secondary" id="btn-empty-demo">Run demo</button></div></div>`;
      return;
    }
    el.innerHTML = state.runs.map((r) => `
      <button class="run-item${r.id === state.runId ? " active" : ""}" data-run="${r.id}">
        <span class="title"><span>${esc(r.date)}${r.kind === "demo" ? ' <span class="muted">demo</span>' : ""}</span><span class="badge ${r.proceed ? "done" : "failed"}">${r.proceed ? "YES" : "NO"}</span></span>
        <span class="caption">pool ${fmtInt(r.pool_size)} · shortlist ${fmtInt(r.shortlist_n)} · OOS ${fmtPct(r.shortlist_roi)}</span>
      </button>`).join("");
  }

  async function selectRun(id) {
    state.runId = id; renderRunList();
    const el = $("#run-detail");
    if (!state.detail || state.detail.id !== id) el.innerHTML = `<div class="empty">Loading…</div>`;
    try { state.detail = await api("GET", "/api/runs/" + id); renderDetail(); }
    catch (e) { el.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  }

  const CHECK_LABELS = {
    shortlist_has_leaders: (c) => `Shortlist has ≥ ${c.min_shortlist_leaders ?? 3} leaders`,
    beats_naive_top_n: (c) => `Beats naive top-${c.naive_top_n ?? 5} basket`,
    beats_btc_hold: () => "Beats BTC buy & hold",
    drawdown_ok: (c) => `Shortlist drawdown ≤ ${fmtPct(c.max_drawdown, 0)}`,
    positive_oos: () => "Positive out-of-sample",
  };
  const FILE_LABELS = { report: "report.md", traders: "traders.csv", shortlist: "shortlist.csv", shortlist_trades: "shortlist_trades.csv", run_json: "run.json" };
  const tile = (label, value, sub) => `<div class="tile"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub || ""}</div></div>`;
  const sectionTitle = (t, c) => `<h3 class="section-title">${t}${c ? `<span class="caption">${c}</span>` : ""}</h3>`;

  function renderDetail() {
    const d = state.detail, el = $("#run-detail");
    if (!d) { el.innerHTML = ""; return; }
    const run = d.run || {}, b = run.benchmarks || {}, cfg = run.config || {}, v = b.verdict || {}, pers = b.persistence || {};
    const passed = d.traders.filter((t) => t.passed === true);
    const btc = b.btc_hold || {};
    const H = [];

    H.push(`<div class="detail-head">
      <p class="eyebrow">${d.kind === "demo" ? "Demo · synthetic traders" : "Real screen"} · ${esc(d.date)}</p>
      <h2 class="headline">${d.kind === "demo" ? "Demo run" : "Hyperliquid niche-trader screen"}</h2>
      <p class="lede">In-sample ${esc(run.is_start)} → ${esc(run.is_end)}, out-of-sample ${esc(run.is_end)} → ${esc(run.end)}. Latency ${cfg.latency_s}s, taker ${cfg.taker_fee_bps} + builder ${cfg.builder_fee_bps} bps per leg, follower equity ${fmtUsd(cfg.follower_equity_usd)} per leader, max leverage ${cfg.follower_max_leverage}×, funding ${cfg.apply_funding ? "on" : "off"}.</p>
    </div>`);

    const checks = Object.entries(v.checks || {});
    H.push(`<div class="verdict ${v.proceed_to_paper_test ? "yes" : "no"}">
      <div><div class="caption">Proceed to forward paper test</div><div class="word">${v.proceed_to_paper_test ? "YES" : "NO"}</div></div>
      <ul class="checks">${checks.map(([k, ok]) => `<li class="${ok ? "ok" : "bad"}"><span class="ico">${ok ? "✓" : "✕"}</span>${esc((CHECK_LABELS[k] || (() => k))(cfg))}</li>`).join("")}</ul>
    </div>`);

    H.push(`<div class="tiles">
      ${tile("Pool", fmtInt(run.pool_size), "accounts sampled")}
      ${tile("Usable history", fmtInt(run.loaded), "old enough, has fills")}
      ${tile("Passed filters", fmtInt(passed.length), "in-sample")}
      ${tile("Shortlist", fmtInt((run.shortlist || []).length), `max ${cfg.shortlist_size ?? "?"}`)}
      ${tile("Persistence", isNum(pers.spearman) ? fmtNum(pers.spearman, 2) : "n/a", `Spearman, n = ${pers.n ?? 0}`)}
    </div>`);

    H.push(sectionTitle("Out-of-sample benchmarks", "same simulator, same window"));
    const baskets = [["Shortlist (niche)", b.shortlist], ["All that passed filters", b.all_passed], [`Naive top-${cfg.naive_top_n ?? 5} by in-sample ROI`, b.naive_top_n]];
    H.push(`<div class="panel tight"><div class="table-wrap"><table class="data">
      <thead><tr><th>Basket</th><th class="num">Leaders</th><th class="num">Trades</th><th class="num">Follower ROI</th><th class="num">Max DD</th><th class="num">Leaders positive</th></tr></thead>
      <tbody>${baskets.map(([n, p]) => { p = p || {}; return `<tr><td>${n}</td><td class="num">${fmtInt(p.n_leaders)}</td><td class="num">${fmtInt(p.n_trades)}</td><td class="num">${pct(p.roi)}</td><td class="num">${fmtPct(p.max_dd)}</td><td class="num">${fmtPct(p.leaders_positive_share, 0)}</td></tr>`; }).join("")}
      <tr><td>BTC buy &amp; hold</td><td class="num tertiary">–</td><td class="num tertiary">–</td><td class="num">${pct(btc.roi)}</td><td class="num">${fmtPct(btc.max_dd)}</td><td class="num tertiary">–</td></tr></tbody></table></div></div>`);
    H.push(`<p class="caption" style="margin-top:8px">Persistence: Spearman(in-sample score, out-of-sample follower ROI) = ${isNum(pers.spearman) ? fmtNum(pers.spearman, 2) : "n/a"} over n = ${pers.n ?? 0} traders that passed filters. Near zero or negative means the in-sample screen carries no information; do not run the paper test on that shortlist.</p>`);

    H.push(`<div class="chart-2">
      <div class="panel tight"><div class="panel-head"><div class="panel-title">Shortlist basket, cumulative follower PnL</div><div class="panel-actions"><span class="caption">USD, in-sample then out-of-sample</span></div></div><div class="chart" id="chart-cum"></div></div>
      <div class="panel tight"><div class="panel-head"><div class="panel-title">Does the in-sample score persist?</div><div class="panel-actions"><span class="caption">one dot per trader that passed</span></div></div><div class="chart" id="chart-persist"></div></div>
    </div>`);

    H.push(sectionTitle("Shortlist", `${d.shortlist.length} leaders ranked by copy-adjusted score (follower ROI ÷ max drawdown)`));
    H.push(`<div class="panel tight"><div class="table-wrap" id="tbl-shortlist"></div></div>`);

    H.push(sectionTitle("Why traders were dropped", "in-sample filters; one trader can fail several"));
    H.push(`<div class="panel tight"><div class="chart" id="chart-drops"></div></div>`);

    H.push(sectionTitle("Census", "every pooled account with at least 10 in-sample trades, cut by style: the overview behind any strategy"));
    H.push(`<div id="census"></div>`);

    H.push(sectionTitle("All pooled traders", `${d.traders.length} with usable history`));
    H.push(`<div class="panel tight">
      <div class="panel-head"><div class="toolbar"><div class="seg" id="seg-passed"><button data-v="all" class="active">All</button><button data-v="passed">Passed only</button></div><input id="traders-search" type="search" placeholder="filter by address, name or drop reason" spellcheck="false"></div><div class="panel-actions"><span class="caption" id="traders-count"></span></div></div>
      <div class="table-wrap" id="tbl-traders" style="max-height: 560px"></div></div>`);

    H.push(sectionTitle("Files", esc(d.dir)));
    H.push(`<div class="downloads">${Object.entries(d.files || {}).filter(([, ok]) => ok).map(([k]) => `<a class="btn btn-secondary btn-sm" href="/api/runs/${d.id}/files/${k}" download>${FILE_LABELS[k] || k}</a>`).join("")}</div>`);
    H.push(`<details class="panel report"><summary>Report as written by the CLI</summary><div class="md">${md(d.report_md)}</div></details>`);

    el.innerHTML = H.join("");

    redrawCharts();
    renderCensus($("#census"), d.traders);
    renderShortlist(d);
    state.tradersFilter = { passedOnly: false, q: "" };
    renderTraders(d);
    $("#seg-passed").addEventListener("click", (e) => {
      const btn = e.target.closest("button"); if (!btn) return;
      $$("#seg-passed button").forEach((x) => x.classList.toggle("active", x === btn));
      state.tradersFilter.passedOnly = btn.dataset.v === "passed"; renderTraders(d);
    });
    $("#traders-search").addEventListener("input", (e) => { state.tradersFilter.q = e.target.value; renderTraders(d); });
  }

  function renderShortlist(d) {
    const rows = d.shortlist.map((r, i) => ({ ...r, _rank: i + 1 }));
    sortableTable($("#tbl-shortlist"), [
      { key: "_rank", label: "#", num: true, render: (r) => r._rank },
      { key: "address", label: "Address", render: (r) => `<span class="addr" title="${esc(r.address)}">${shortAddr(r.address)}</span>${r.display_name ? ` <span class="muted">${esc(r.display_name)}</span>` : ""}` },
      { key: "account_value", label: "Equity", num: true, render: (r) => fmtUsd(r.account_value) },
      { key: "is_n_trades", label: "IS trades", num: true, render: (r) => fmtInt(r.is_n_trades) },
      { key: "is_f_roi", label: "IS follower ROI", num: true, render: (r) => pct(r.is_f_roi) },
      { key: "is_f_max_dd", label: "IS DD", num: true, render: (r) => fmtPct(r.is_f_max_dd) },
      { key: "is_f_copy_gap", label: "Copy gap", num: true, title: "leader ROI − follower ROI on the same trades", render: (r) => fmtPct(r.is_f_copy_gap) },
      { key: "is_f_avg_penalty_bps", label: "Penalty", num: true, title: "modelled adverse move per leg, bps", render: (r) => fmtNum(r.is_f_avg_penalty_bps, 1) + " bps" },
      { key: "is_median_hold_min", label: "Med hold", num: true, render: (r) => fmtNum(r.is_median_hold_min, 0) + " min" },
      { key: "is_median_leverage", label: "Med lev", num: true, render: (r) => fmtNum(r.is_median_leverage, 1) + "×" },
      { key: "is_thin_share", label: "Thin", num: true, title: "share of notional in thin coins", render: (r) => fmtPct(r.is_thin_share, 0) },
      { key: "oos_f_roi", label: "OOS follower ROI", num: true, render: (r) => pct(r.oos_f_roi) },
      { key: "oos_f_max_dd", label: "OOS DD", num: true, render: (r) => fmtPct(r.oos_f_max_dd) },
      { key: "score", label: "Score", num: true, render: (r) => fmtNum(r.score, 2) },
      { key: "", label: "", render: (r) => `<button class="btn btn-tertiary btn-sm" data-inspect="${esc(r.address)}">Inspect</button>` },
    ], rows, { sortKey: "_rank", dir: 1, empty: "Shortlist is empty: no trader passed every filter." });
  }

  function renderTraders(d) {
    const f = state.tradersFilter, q = f.q.trim().toLowerCase();
    const rows = d.traders.filter((r) => (!f.passedOnly || r.passed === true) && (!q || [r.address, r.display_name, r.reasons].some((x) => String(x || "").toLowerCase().includes(q))));
    $("#traders-count").textContent = `${rows.length} of ${d.traders.length}`;
    sortableTable($("#tbl-traders"), [
      { key: "address", label: "Address", render: (r) => `<span class="addr" title="${esc(r.address)}">${shortAddr(r.address)}</span>` },
      { key: "display_name", label: "Name", render: (r) => `<span class="muted">${esc(r.display_name || "")}</span>` },
      { key: "account_value", label: "Equity", num: true, render: (r) => fmtUsd(r.account_value) },
      { key: "is_n_trades", label: "IS trades", num: true, render: (r) => fmtInt(r.is_n_trades) },
      { key: "is_profit_factor", label: "PF", num: true, render: (r) => fmtNum(r.is_profit_factor, 2) },
      { key: "is_roi_trades", label: "Leader ROI", num: true, render: (r) => pct(r.is_roi_trades) },
      { key: "is_f_roi", label: "IS follower ROI", num: true, render: (r) => pct(r.is_f_roi) },
      { key: "oos_f_roi", label: "OOS follower ROI", num: true, render: (r) => pct(r.oos_f_roi) },
      { key: "score", label: "Score", num: true, render: (r) => r.passed ? fmtNum(r.score, 2) : `<span class="tertiary">${fmtNum(r.score, 2)}</span>` },
      { key: "reasons", label: "Dropped because", render: (r) => r.reasons ? `<span class="muted">${esc(String(r.reasons).split(";").join(" · "))}</span>` : `<span class="pos">passed</span>` },
      { key: "", label: "", render: (r) => `<button class="btn btn-tertiary btn-sm" data-inspect="${esc(r.address)}">Inspect</button>` },
    ], rows, { sortKey: "score", dir: -1, empty: "No trader matches." });
  }

  // ---------------------------------------------------------------- census (population by style)
  const median = (a) => { const s = a.filter(isNum).sort((x, y) => x - y); if (!s.length) return NaN; const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
  const share = (rows, pred) => rows.length ? rows.filter(pred).length / rows.length : NaN;
  function ranks(v) { const idx = v.map((x, i) => [x, i]).sort((a, b) => a[0] - b[0]); const r = new Array(v.length); let i = 0; while (i < idx.length) { let j = i; while (j + 1 < idx.length && idx[j + 1][0] === idx[i][0]) j++; const avg = (i + j) / 2; for (let k = i; k <= j; k++) r[idx[k][1]] = avg; i = j + 1; } return r; }
  function spearman(x, y) {
    if (x.length < 5) return NaN;
    const rx = ranks(x), ry = ranks(y), n = x.length, mx = rx.reduce((a, b) => a + b, 0) / n, my = ry.reduce((a, b) => a + b, 0) / n;
    let sxy = 0, sxx = 0, syy = 0;
    for (let i = 0; i < n; i++) { sxy += (rx[i] - mx) * (ry[i] - my); sxx += (rx[i] - mx) ** 2; syy += (ry[i] - my) ** 2; }
    return sxx && syy ? sxy / Math.sqrt(sxx * syy) : NaN;
  }
  const bucketOf = (x, edges, labels) => { if (!isNum(x)) return null; for (let i = 0; i < edges.length; i++) if (x < edges[i]) return labels[i]; return labels[edges.length]; };
  const CENSUS_CUTS = [
    { title: "By holding time", caption: "median hold of closed trades", key: "is_median_hold_min", edges: [5, 60, 1440], labels: ["scalp, under 5 min", "5 to 60 min", "1 to 24 hours", "swing, over a day"] },
    { title: "By leverage", caption: "median position size ÷ account equity", key: "is_median_leverage", edges: [0.5, 1.5, 5, 15], labels: ["under 0.5×", "0.5 to 1.5×", "1.5 to 5×", "5 to 15×", "over 15×"] },
    { title: "By account age", caption: "days since the account first had equity", key: "is_account_age_days", edges: [180, 365, 730], labels: ["under 6 months", "6 to 12 months", "1 to 2 years", "over 2 years"] },
  ];
  function renderCensus(container, traders) {
    const rows = traders.filter((r) => isNum(r.is_n_trades) && r.is_n_trades >= 10);
    if (rows.length < 20) { container.innerHTML = `<div class="panel tight"><div class="empty">Fewer than 20 accounts with 10 or more trades, not enough for a census.</div></div>`; return; }
    const blown = (r) => (isNum(r.is_liquidations) && r.is_liquidations > 0) || (isNum(r.is_max_dd_portfolio) && r.is_max_dd_portfolio > 0.5);
    const cols = `<th>group</th><th class="num">accounts</th><th class="num" title="leader net > 0 on closed trades, in-sample">leader profitable</th><th class="num">median leader ROI, IS</th><th class="num">median leader ROI, OOS</th><th class="num" title="copier return > 0 in-sample">copier profitable</th><th class="num" title="copier return > 0 in both windows">copier profitable both</th><th class="num" title="modelled adverse move per leg">copy penalty</th><th class="num" title="at least one liquidation, or the account's own curve fell more than 50%">liquidated or DD > 50%</th><th class="num">median trades</th>`;
    let html = "";
    for (const cut of CENSUS_CUTS) {
      const groups = new Map(cut.labels.map((l) => [l, []]));
      for (const r of rows) { const b = bucketOf(r[cut.key], cut.edges, cut.labels); if (b) groups.get(b).push(r); }
      const body = [...groups.entries()].filter(([, g]) => g.length).map(([label, g]) => `<tr><td>${label}</td><td class="num">${g.length}</td>
        <td class="num">${fmtPct(share(g, (r) => r.is_roi_trades > 0), 0)}</td><td class="num">${pct(median(g.map((r) => r.is_roi_trades)), 0)}</td><td class="num">${pct(median(g.map((r) => r.oos_roi_trades)), 0)}</td>
        <td class="num">${fmtPct(share(g, (r) => r.is_f_roi > 0), 0)}</td><td class="num">${fmtPct(share(g, (r) => r.is_f_roi > 0 && r.oos_f_roi > 0), 0)}</td>
        <td class="num">${fmtNum(median(g.map((r) => r.is_f_avg_penalty_bps)), 1)} bps</td><td class="num">${fmtPct(share(g, blown), 0)}</td><td class="num">${fmtInt(median(g.map((r) => r.is_n_trades)))}</td></tr>`).join("");
      html += `<div class="panel tight" style="margin-bottom: var(--space-md)"><div class="panel-head"><div class="panel-title">${cut.title}</div><div class="panel-actions"><span class="caption">${cut.caption}</span></div></div><div class="table-wrap"><table class="data"><thead><tr>${cols}</tr></thead><tbody>${body}</tbody></table></div></div>`;
    }
    const both = rows.filter((r) => isNum(r.oos_n_trades) && r.oos_n_trades >= 5);
    const pair = (a, b) => { const x = [], y = []; for (const r of both) if (isNum(r[a]) && isNum(r[b])) { x.push(r[a]); y.push(r[b]); } return [x, y]; };
    const tests = [["is_roi_trades", "oos_roi_trades", "leader's own return, first window → hidden window"], ["is_profit_factor", "oos_roi_trades", "profit factor → later return"], ["is_win_rate", "oos_roi_trades", "win rate → later return"], ["is_positive_bucket_share", "oos_roi_trades", "consistency → later return"], ["is_f_roi", "oos_f_roi", "copier return, first window → hidden window"], ["score", "oos_f_roi", "screen score → later copier return"]];
    const sorted = both.filter((r) => isNum(r.is_roi_trades) && isNum(r.oos_roi_trades)).sort((a, b) => a.is_roi_trades - b.is_roi_trades);
    const q = Math.floor(sorted.length / 5), bottom = sorted.slice(0, q), top = sorted.slice(-q);
    html += `<div class="panel tight"><div class="panel-head"><div class="panel-title">Does the past predict the future?</div><div class="panel-actions"><span class="caption">Spearman rank correlation over ${both.length} accounts with 5+ trades in the hidden window; 0 = no information, 1 = perfect</span></div></div>
      <div class="table-wrap"><table class="data"><thead><tr><th>measured in the first window</th><th class="num">correlation with the hidden window</th><th class="num">accounts</th></tr></thead><tbody>
      ${tests.map(([a, b, label]) => { const [x, y] = pair(a, b); const rho = spearman(x, y); return `<tr><td>${label}</td><td class="num ${isNum(rho) && Math.abs(rho) >= 0.2 ? "pos" : ""}">${isNum(rho) ? (rho >= 0 ? "+" : "") + rho.toFixed(2) : "n/a"}</td><td class="num">${x.length}</td></tr>`; }).join("")}</tbody></table></div>
      <div class="panel-body caption">Top fifth by first-window return: ${fmtPct(share(top, (r) => r.oos_roi_trades > 0), 0)} profitable later, median ${fmtPct(median(top.map((r) => r.oos_roi_trades)), 0)}. Bottom fifth: ${fmtPct(share(bottom, (r) => r.oos_roi_trades > 0), 0)} profitable later, median ${fmtPct(median(bottom.map((r) => r.oos_roi_trades)), 0)}. Everyone: ${fmtPct(share(both, (r) => r.oos_roi_trades > 0), 0)}, median ${fmtPct(median(both.map((r) => r.oos_roi_trades)), 0)}.</div></div>`;
    container.innerHTML = html;
  }

  // ---------------------------------------------------------------- charts (inline SVG)
  // Each chart is drawn at the container's pixel width so text never scales with the panel.
  function redrawCharts() {
    const d = state.detail;
    if (!d || !$("#chart-cum")) return;
    drawCumulative($("#chart-cum"), d);
    drawPersistence($("#chart-persist"), d.traders.filter((t) => t.passed === true));
    drawDrops($("#chart-drops"), (d.run && d.run.drop_counts) || {});
  }
  const chartWidth = (container) => Math.max(320, (container.clientWidth || 640) - 32);
  let resizeTimer = null;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(redrawCharts, 150); });

  function niceTicks(lo, hi, n) {
    if (!(hi > lo)) return [lo];
    const raw = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
    return out;
  }
  const timeTicks = (x0, x1, n) => Array.from({ length: n + 1 }, (_, i) => x0 + (x1 - x0) * i / n);

  function tooltipAt(container, tip, clientX, clientY, html) {
    const r = container.getBoundingClientRect();
    tip.innerHTML = html; tip.hidden = false;
    const w = tip.offsetWidth, x = clientX - r.left, y = clientY - r.top;
    tip.style.left = (x + 14 + w > r.width ? x - w - 14 : x + 14) + "px";
    tip.style.top = Math.max(0, y - 12) + "px";
  }

  function drawCumulative(container, d) {
    const trades = (d.trades || []).filter((t) => isNum(t.close_time) && isNum(t.net_pnl)).sort((a, b) => a.close_time - b.close_time);
    if (trades.length < 2) { container.innerHTML = `<div class="empty">No simulated shortlist trades in this run.</div>`; return; }
    const pts = []; let cum = 0;
    trades.forEach((t, i) => { cum += t.net_pnl; pts.push({ t: t.close_time, v: cum, n: i + 1, w: t.window }); });
    const split = Date.parse((d.run.is_end || "") + "T00:00:00Z");
    const W = chartWidth(container), H = 250, m = { l: 56, r: 16, t: 18, b: 28 };
    const x0 = pts[0].t, x1 = pts[pts.length - 1].t;
    const vs = pts.map((p) => p.v), vmin = Math.min(0, ...vs), vmax = Math.max(0, ...vs), pad = (vmax - vmin) * 0.08 || 1;
    const y0 = vmin - pad, y1 = vmax + pad;
    const X = (t) => m.l + (t - x0) / Math.max(1, x1 - x0) * (W - m.l - m.r);
    const Y = (v) => m.t + (y1 - v) / (y1 - y0) * (H - m.t - m.b);
    const yt = niceTicks(y0, y1, 4), xt = timeTicks(x0, x1, Math.max(2, Math.floor((W - m.l - m.r) / 150)));
    let s = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Cumulative follower PnL of the shortlist basket over time">`;
    s += `<g class="grid">${yt.map((v) => `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}"/>`).join("")}</g>`;
    s += `<g class="axis">${yt.map((v) => `<text x="${m.l - 8}" y="${(Y(v) + 4).toFixed(1)}" text-anchor="end">${fmtUsdShort(v)}</text>`).join("")}`;
    s += xt.map((t, i) => `<text x="${X(t).toFixed(1)}" y="${H - 8}" text-anchor="${i === 0 ? "start" : i === xt.length - 1 ? "end" : "middle"}">${fmtDate(t)}</text>`).join("") + `</g>`;
    s += `<line class="zero" x1="${m.l}" x2="${W - m.r}" y1="${Y(0).toFixed(1)}" y2="${Y(0).toFixed(1)}"/>`;
    if (isNum(split) && split > x0 && split < x1) s += `<line class="split" x1="${X(split).toFixed(1)}" x2="${X(split).toFixed(1)}" y1="${m.t}" y2="${H - m.b}"/><text class="split-label" x="${(X(split) + 6).toFixed(1)}" y="${m.t + 10}">out-of-sample →</text>`;
    s += `<path class="series" d="${pts.map((p, i) => (i ? "L" : "M") + X(p.t).toFixed(1) + " " + Y(p.v).toFixed(1)).join(" ")}"/>`;
    s += `<line class="crosshair" data-ch x1="0" x2="0" y1="${m.t}" y2="${H - m.b}" visibility="hidden"/><circle class="dot" data-dot r="4.5" visibility="hidden"/>`;
    s += `<rect class="hit" data-hit x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}"/></svg><div class="tooltip" hidden></div>`;
    container.innerHTML = s;
    const svg = container.querySelector("svg"), tip = container.querySelector(".tooltip");
    const ch = svg.querySelector("[data-ch]"), dot = svg.querySelector("[data-dot]");
    svg.addEventListener("mousemove", (e) => {
      const r = svg.getBoundingClientRect(); const sx = (e.clientX - r.left) * W / r.width;
      let best = pts[0], bd = Infinity;
      for (const p of pts) { const dd = Math.abs(X(p.t) - sx); if (dd < bd) { bd = dd; best = p; } }
      ch.setAttribute("x1", X(best.t)); ch.setAttribute("x2", X(best.t)); ch.removeAttribute("visibility");
      dot.setAttribute("cx", X(best.t)); dot.setAttribute("cy", Y(best.v)); dot.removeAttribute("visibility");
      tooltipAt(container, tip, e.clientX, e.clientY, `<b>${fmtDate(best.t)}</b> · ${best.w === "oos" ? "out-of-sample" : "in-sample"}<br>cumulative <b class="${signCls(best.v)}">${fmtUsd(best.v)}</b> after ${best.n} trades`);
    });
    svg.addEventListener("mouseleave", () => { tip.hidden = true; ch.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); });
  }

  function drawPersistence(container, passed) {
    const pts = passed.filter((r) => isNum(r.score) && isNum(r.oos_f_roi)).map((r) => ({ x: r.score, y: r.oos_f_roi, a: r.address }));
    if (pts.length < 2) { container.innerHTML = `<div class="empty">Fewer than two traders passed the filters, so there is nothing to correlate.</div>`; return; }
    const W = chartWidth(container), H = 250, m = { l: 56, r: 16, t: 18, b: 40 };
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
    const padx = (Math.max(...xs) - Math.min(...xs)) * 0.1 || 1, pady = (Math.max(...ys) - Math.min(...ys)) * 0.1 || 0.05;
    const x0 = Math.min(0, ...xs) - padx, x1 = Math.max(0, ...xs) + padx, y0 = Math.min(0, ...ys) - pady, y1 = Math.max(0, ...ys) + pady;
    const X = (x) => m.l + (x - x0) / (x1 - x0) * (W - m.l - m.r), Y = (y) => m.t + (y1 - y) / (y1 - y0) * (H - m.t - m.b);
    const yt = niceTicks(y0, y1, 4), xt = niceTicks(x0, x1, 5);
    let s = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="In-sample score versus out-of-sample follower ROI, one dot per trader">`;
    s += `<g class="grid">${yt.map((v) => `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}"/>`).join("")}</g>`;
    s += `<g class="axis">${yt.map((v) => `<text x="${m.l - 8}" y="${(Y(v) + 4).toFixed(1)}" text-anchor="end">${fmtPct(v, 0)}</text>`).join("")}${xt.map((v) => `<text x="${X(v).toFixed(1)}" y="${H - 22}" text-anchor="middle">${fmtNum(v, Math.abs(x1 - x0) < 5 ? 1 : 0)}</text>`).join("")}`;
    s += `<text class="axis-title" x="${(m.l + W - m.r) / 2}" y="${H - 6}" text-anchor="middle">in-sample score (follower ROI ÷ max DD)  →  out-of-sample follower ROI ↑</text></g>`;
    s += `<line class="zero" x1="${m.l}" x2="${W - m.r}" y1="${Y(0).toFixed(1)}" y2="${Y(0).toFixed(1)}"/><line class="split" x1="${X(0).toFixed(1)}" x2="${X(0).toFixed(1)}" y1="${m.t}" y2="${H - m.b}"/>`;
    s += pts.map((p, i) => `<circle class="dot" cx="${X(p.x).toFixed(1)}" cy="${Y(p.y).toFixed(1)}" r="4.5"/><circle class="hit" data-i="${i}" cx="${X(p.x).toFixed(1)}" cy="${Y(p.y).toFixed(1)}" r="11"/>`).join("");
    s += `</svg><div class="tooltip" hidden></div>`;
    container.innerHTML = s;
    const tip = container.querySelector(".tooltip");
    container.querySelectorAll("circle.hit").forEach((c) => {
      c.addEventListener("mousemove", (e) => { const p = pts[+c.dataset.i]; tooltipAt(container, tip, e.clientX, e.clientY, `<b>${shortAddr(p.a)}</b><br>in-sample score <b>${fmtNum(p.x, 2)}</b><br>out-of-sample follower ROI <b class="${signCls(p.y)}">${fmtPct(p.y)}</b>`); });
      c.addEventListener("mouseleave", () => { tip.hidden = true; });
    });
  }

  function drawDrops(container, counts) {
    const items = Object.entries(counts).map(([k, v]) => ({ k, v: +v || 0 })).sort((a, b) => b.v - a.v);
    if (!items.length) { container.innerHTML = `<div class="empty">No trader was dropped by the in-sample filters.</div>`; return; }
    const rowH = 26, W = Math.min(720, chartWidth(container)), labelW = 150, m = { l: labelW + 8, r: 48 }, H = items.length * rowH + 6;
    const max = Math.max(...items.map((i) => i.v)) || 1, X = (v) => m.l + v / max * (W - m.l - m.r);
    let s = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Number of traders failing each in-sample filter">`;
    items.forEach((it, i) => {
      const y = i * rowH + 4, w = Math.max(0, X(it.v) - m.l), r = Math.min(4, w);
      const path = `M${m.l} ${y} H${m.l + w - r} a${r} ${r} 0 0 1 ${r} ${r} v${18 - 2 * r} a${r} ${r} 0 0 1 -${r} ${r} H${m.l} Z`;
      s += `<text class="cat" x="${m.l - 8}" y="${y + 13}" text-anchor="end">${esc(it.k)}</text><path class="bar" d="${path}"><title>${esc(it.k)}: ${it.v}</title></path><text class="bar-label" x="${m.l + w + 6}" y="${y + 13}">${it.v}</text>`;
    });
    container.innerHTML = s + `</svg>`;
  }

  // ---------------------------------------------------------------- markdown (the CLI report)
  function md(src) {
    const lines = (src || "").replace(/\r/g, "").split("\n"), out = [];
    const inline = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
    const cells = (r) => r.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
    let i = 0;
    while (i < lines.length) {
      const l = lines[i];
      if (/^#{1,3} /.test(l)) { const n = l.match(/^#+/)[0].length; out.push(`<h${n}>${inline(l.replace(/^#+\s*/, ""))}</h${n}>`); i++; continue; }
      if (/^\|/.test(l)) {
        const rows = []; while (i < lines.length && /^\|/.test(lines[i])) rows.push(lines[i++]);
        const head = cells(rows[0]), align = rows[1] ? cells(rows[1]).map((c) => /:$/.test(c) ? "num" : "") : [], body = rows.slice(2).map(cells);
        out.push(`<table class="data"><thead><tr>${head.map((h, j) => `<th class="${align[j] || ""}">${inline(h)}</th>`).join("")}</tr></thead><tbody>${body.map((r) => `<tr>${r.map((c, j) => `<td class="${align[j] || ""}">${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
        continue;
      }
      if (/^- /.test(l)) { const items = []; while (i < lines.length && /^- /.test(lines[i])) items.push(lines[i++].slice(2)); out.push(`<ul>${items.map((x) => `<li>${inline(x)}</li>`).join("")}</ul>`); continue; }
      if (l.trim() === "") { i++; continue; }
      const para = []; while (i < lines.length && lines[i].trim() !== "" && !/^(#|\||- )/.test(lines[i])) para.push(lines[i++]);
      out.push(`<p>${inline(para.join(" "))}</p>`);
    }
    return out.join("\n");
  }

  // ---------------------------------------------------------------- paper trading
  const fmtTime = (ms) => { if (!isNum(ms)) return ""; const d = new Date(ms); return d.toLocaleDateString(undefined, { month: "2-digit", day: "2-digit" }) + " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }); };
  const ago = (s) => !isNum(s) ? "never" : s < 90 ? `${Math.round(s)}s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : `${(s / 3600).toFixed(1)} h ago`;
  const paper = { timer: null, logSince: 0, logLines: [] };

  function startPaperPolling() { stopPaperPolling(); loadPaper(); paper.timer = setInterval(loadPaper, 5000); }
  function stopPaperPolling() { clearInterval(paper.timer); paper.timer = null; }

  async function loadPaper() {
    let d;
    try { d = await api("GET", `/api/paper?log_since=${paper.logSince}`); } catch (e) { toast(e.message); return; }
    renderPaper(d);
  }

  function renderPaper(d) {
    const svc = d.service, st = d.status || {};
    const running = svc && svc.status === "running";
    const external = !running && st.exists && isNum(st.heartbeat_age_s) && st.heartbeat_age_s < 120;   // e.g. the `paper` container in Coolify
    $$(".paper-local").forEach((el) => { el.hidden = external; });
    $("#paper-external").hidden = !external;
    if (external) $("#paper-external").textContent = `Runs as its own service, separate from this page. Following ${(st.leaders || []).length} leaders from ${st.leaders_file || "its leaders file"}, ${fmtUsd((st.config || {}).equity_base)} each. Stop or restart it where it runs, for example the paper service in Coolify.`;
    const files = d.leaders_files || [], sel = $("#paper-leaders-file");
    if (sel.dataset.files !== files.join("|")) {   // rebuild only when the list changes, so a choice survives the 5 s refresh
      sel.dataset.files = files.join("|");
      sel.innerHTML = files.length ? files.map((f) => `<option value="${esc(f)}">${esc(f)}</option>`).join("") : `<option value="">no leaders file yet: run a screen first</option>`;
    }
    $("#form-paper").elements.equity.placeholder = `${d.defaults.equity} (config)`;
    $("#btn-paper-start").disabled = running || !files.length;
    $("#btn-paper-stop").hidden = !running;
    const badge = $("#paper-service-badge");
    badge.textContent = svc ? svc.status : external ? "live" : (st.exists ? "stopped" : "idle");
    badge.className = "badge " + (svc ? svc.status : external ? "running" : "");
    $("#paper-heartbeat").textContent = st.exists ? `database ${esc(d.db.split(/[\\/]/).slice(-2).join("/"))} · last heartbeat ${ago(st.heartbeat_age_s)}` : "no paper database yet";
    if (svc && svc.lines && svc.lines.length) { paper.logLines.push(...svc.lines); paper.logLines = paper.logLines.slice(-300); paper.logSince = svc.next; $("#paper-log").textContent = paper.logLines.join("\n"); $("#paper-log").scrollTop = 1e9; }
    if (svc && paper.logSince > svc.next) { paper.logSince = 0; paper.logLines = []; }
    const L = st.leaders || [];
    const base = L.reduce((a, l) => a + (l.equity_base || 0), 0), equity = L.reduce((a, l) => a + (l.equity || 0), 0);
    const fills = L.reduce((a, l) => a + (l.n_paper_fills || 0), 0);
    const wSlip = L.filter((l) => isNum(l.avg_slippage_bps) && l.n_paper_fills), slip = wSlip.length ? wSlip.reduce((a, l) => a + l.avg_slippage_bps * l.n_paper_fills, 0) / wSlip.reduce((a, l) => a + l.n_paper_fills, 0) : NaN;
    const wMod = L.filter((l) => isNum(l.model_penalty_bps)), model = wMod.length ? wMod.reduce((a, l) => a + l.model_penalty_bps, 0) / wMod.length : NaN;
    const wLat = L.filter((l) => isNum(l.avg_latency_ms) && l.n_paper_fills), lat = wLat.length ? wLat.reduce((a, l) => a + l.avg_latency_ms * l.n_paper_fills, 0) / wLat.reduce((a, l) => a + l.n_paper_fills, 0) : NaN;
    $("#paper-tiles").innerHTML = tile("Leaders", fmtInt(L.length), `${L.reduce((a, l) => a + (l.positions || []).length, 0)} open positions`) +
      tile("All accounts", fmtUsd(equity), base ? `<span class="${signCls(equity - base)}">${fmtPct(equity / base - 1)}</span> on ${fmtUsd(base)}` : "") +
      tile("Follower fills", fmtInt(fills), `${L.reduce((a, l) => a + (l.n_late || 0), 0)} late · ${L.reduce((a, l) => a + (l.n_thin_book || 0), 0)} beyond the book`) +
      tile("Slippage, measured", isNum(slip) ? fmtNum(slip, 1) + " bps" : "n/a", isNum(model) ? `model assumed ${fmtNum(model, 1)} bps` : "") +
      tile("Latency", isNum(lat) ? fmtNum(lat / 1000, 1) + " s" : "n/a", "fill seen → priced; config assumes 3 s") +
      tile("Leader liquidations", fmtInt(L.reduce((a, l) => a + (l.leader_liquidations || 0), 0)), "since the test started");
    sortableTable($("#paper-leaders"), [
      { key: "address", label: "Leader", render: (l) => `<span class="addr" title="${esc(l.address)}">${shortAddr(l.address)}</span>${l.name ? ` <span class="muted">${esc(l.name)}</span>` : ""}` },
      { key: "equity", label: "Equity", num: true, render: (l) => fmtUsd(l.equity) },
      { key: "roi", label: "Return", num: true, render: (l) => pct(l.roi) },
      { key: "realized", label: "Realized", num: true, render: (l) => `<span class="${signCls(l.realized)}">${fmtUsd(l.realized)}</span>` },
      { key: "unrealized", label: "Unrealized", num: true, render: (l) => `<span class="${signCls(l.unrealized)}">${fmtUsd(l.unrealized)}</span>` },
      { key: "fees", label: "Fees", num: true, render: (l) => fmtUsd(l.fees) },
      { key: "funding", label: "Funding", num: true, render: (l) => `<span class="${signCls(l.funding)}">${fmtNum(l.funding, 2)}</span>` },
      { key: "n_paper_fills", label: "Fills", num: true, render: (l) => fmtInt(l.n_paper_fills) },
      { key: "avg_slippage_bps", label: "Slip bps", num: true, title: "measured, average per fill", render: (l) => fmtNum(l.avg_slippage_bps, 1) },
      { key: "model_penalty_bps", label: "Model bps", num: true, title: "what the screen assumed", render: (l) => fmtNum(l.model_penalty_bps, 1) },
      { key: "avg_latency_ms", label: "Latency", num: true, render: (l) => isNum(l.avg_latency_ms) ? fmtNum(l.avg_latency_ms / 1000, 1) + " s" : "n/a" },
      { key: "leader_roi", label: "Leader's own", num: true, title: "change in the leader's real account value since the test started", render: (l) => pct(l.leader_roi) },
      { key: "model_oos_roi", label: "Screen OOS", num: true, title: "copier return the screen measured in the hidden window", render: (l) => pct(l.model_oos_roi) },
      { key: "last_leader_fill", label: "Last leader trade", render: (l) => `<span class="muted">${fmtTime(l.last_leader_fill)}</span>` },
    ], L, { sortKey: "equity", dir: -1, empty: st.exists ? "No leaders in the database." : "Start the service to create the paper accounts." });
    const charts = $("#paper-charts");
    charts.innerHTML = L.map((l) => `<div class="panel tight"><div class="mini"><div class="mini-head"><b>${shortAddr(l.address)}</b><span class="caption ${signCls(l.roi)}">${fmtPct(l.roi)}</span></div><div class="chart" style="padding:0" data-addr="${esc(l.address)}"></div></div></div>`).join("");
    for (const l of L) drawMini(charts.querySelector(`[data-addr="${l.address}"]`), (st.snapshots || {})[l.address] || [], l);
    const pos = L.flatMap((l) => (l.positions || []).map((p) => ({ ...p, leader: l.address })));
    sortableTable($("#paper-positions"), [
      { key: "leader", label: "Leader", render: (p) => `<span class="addr">${shortAddr(p.leader)}</span>` },
      { key: "coin", label: "Coin", render: (p) => esc(p.coin) },
      { key: "size", label: "Side", render: (p) => p.size > 0 ? "long" : "short" },
      { key: "notional", label: "Notional", num: true, render: (p) => fmtUsd(p.notional) },
      { key: "entry_px", label: "Entry", num: true, render: (p) => fmtNum(p.entry_px, 4) },
      { key: "mark", label: "Mark", num: true, render: (p) => fmtNum(p.mark, 4) },
      { key: "unrealized", label: "Unrealized", num: true, render: (p) => `<span class="${signCls(p.unrealized)}">${fmtNum(p.unrealized, 2)}</span>` },
      { key: "opened_at", label: "Opened", render: (p) => `<span class="muted">${fmtTime(p.opened_at)}</span>` },
    ], pos, { sortKey: "opened_at", dir: -1, empty: "No open positions." });
    sortableTable($("#paper-fills"), [
      { key: "time", label: "Seen", render: (f) => `<span class="muted">${fmtTime(f.time)}</span>` },
      { key: "address", label: "Leader", render: (f) => `<span class="addr">${shortAddr(f.address)}</span>` },
      { key: "coin", label: "Coin", render: (f) => esc(f.coin) },
      { key: "action", label: "Action", render: (f) => `${f.action}${f.late ? ' <span class="badge failed">late</span>' : ""}` },
      { key: "side", label: "Side", render: (f) => f.side === "B" ? "buy" : "sell" },
      { key: "notional", label: "Notional", num: true, render: (f) => fmtUsd(f.notional) },
      { key: "leader_px", label: "Leader px", num: true, render: (f) => fmtNum(f.leader_px, 4) },
      { key: "fill_px", label: "Fill px", num: true, render: (f) => fmtNum(f.fill_px, 4) },
      { key: "slippage_bps", label: "Slip bps", num: true, render: (f) => `<span class="${f.slippage_bps > 0 ? "neg" : "pos"}">${fmtNum(f.slippage_bps, 1)}</span>` },
      { key: "latency_ms", label: "Latency", num: true, render: (f) => fmtNum(f.latency_ms / 1000, 1) + " s" },
      { key: "fee", label: "Fee", num: true, render: (f) => fmtNum(f.fee, 2) },
      { key: "realized", label: "Realized", num: true, render: (f) => `<span class="${signCls(f.realized)}">${fmtNum(f.realized, 2)}</span>` },
      { key: "levels_used", label: "Book levels", num: true, render: (f) => `${f.levels_used}${f.unfilled > 0 ? " +" : ""}` },
    ], st.fills || [], { sortKey: "time", dir: -1, empty: "No follower fills yet. They appear when a leader trades." });
    $("#paper-events").textContent = (st.events || []).map((e) => `[${fmtTime(e.time)}] ${e.level}: ${e.message}`).join("\n");
  }

  function drawMini(container, snaps, l) {
    const pts = snaps.filter((s) => isNum(s[0]) && isNum(s[1]));
    if (pts.length < 2) { container.innerHTML = `<div class="caption" style="padding: 18px 0">Waiting for snapshots (one every five minutes).</div>`; return; }
    const base = l.equity_base || 1000, ls = l.leader_equity_start;
    const lead = isNum(ls) && ls > 0 ? pts.filter((s) => isNum(s[2])).map((s) => [s[0], s[2] / ls * base]) : [];
    const W = chartWidth(container) + 32, H = 120, m = { l: 44, r: 8, t: 8, b: 18 };
    const x0 = pts[0][0], x1 = pts[pts.length - 1][0];
    const ys = pts.map((p) => p[1]).concat(lead.map((p) => p[1]), [base]);
    const y0 = Math.min(...ys), y1 = Math.max(...ys), pad = (y1 - y0) * 0.1 || base * 0.01;
    const X = (t) => m.l + (t - x0) / Math.max(1, x1 - x0) * (W - m.l - m.r), Y = (v) => m.t + (y1 + pad - v) / (y1 - y0 + 2 * pad) * (H - m.t - m.b);
    const path = (arr) => arr.map((p, i) => (i ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1)).join(" ");
    container.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Equity of the paper account and of the leader">
      <line class="zero" x1="${m.l}" x2="${W - m.r}" y1="${Y(base).toFixed(1)}" y2="${Y(base).toFixed(1)}"/>
      <g class="axis"><text x="${m.l - 6}" y="${Y(base) + 4}" text-anchor="end">${fmtUsdShort(base)}</text><text x="${m.l}" y="${H - 4}">${fmtDate(x0)}</text><text x="${W - m.r}" y="${H - 4}" text-anchor="end">${fmtDate(x1)}</text></g>
      ${lead.length > 1 ? `<path class="series leader" d="${path(lead)}"/>` : ""}
      <path class="series" d="${path(pts)}"/></svg>`;
  }

  $("#form-paper").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try { await api("POST", "/api/paper/start", { leaders: f.elements.leaders.value, equity: f.elements.equity.value }); paper.logSince = 0; paper.logLines = []; loadPaper(); }
    catch (err) { toast(err.message); }
  });
  $("#btn-paper-stop").addEventListener("click", async () => { try { await api("POST", "/api/paper/stop"); loadPaper(); } catch (err) { toast(err.message); } });

  // ---------------------------------------------------------------- pump.fun
  const pump = { timer: null };
  function startPumpPolling() { stopPumpPolling(); loadPump(); pump.timer = setInterval(loadPump, 30000); }
  function stopPumpPolling() { clearInterval(pump.timer); pump.timer = null; }
  async function loadPump() {
    let d;
    try { d = await api("GET", "/api/pump"); } catch (e) { toast(e.message); return; }
    renderPump(d);
  }
  const solscan = (a) => `<a class="addr" href="https://solscan.io/account/${esc(a)}" target="_blank" rel="noopener" title="${esc(a)}">${shortAddr(a)}</a>`;
  const solAmt = (x) => isNum(x) ? `<span class="${signCls(x)}">${x >= 0 ? "+" : "−"}${Math.abs(x).toFixed(2)}</span>` : "n/a";
  const flags = (r) => (r.launched ? ` <span class="badge failed" title="launched ${r.launched} tokens">dev</span>` : "") +
    (r.twins ? ` <span class="badge failed" title="${r.twins} other wallets bought the same tokens in the same slot on at least half of its tokens">cluster ×${r.twins + 1}</span>` : "");

  function renderPump(d) {
    const st = d.stats || {}, rep = d.report || {}, p = rep.params || {};
    const hb = isNum(st.heartbeat) ? d.now - st.heartbeat : NaN, live = isNum(hb) && hb < 120;
    $("#pump-tiles").innerHTML = !d.exists
      ? `<div class="empty" style="grid-column: 1 / -1">No pump.fun data yet. Start the collector: <code>python -m hl_screener pump collect</code>, or the pump service in Coolify.</div>`
      : tile("Collector", live ? "live" : "stalled", `heartbeat ${ago(hb)}${st.reconnects ? ` · ${fmtInt(st.reconnects)} reconnects` : ""}`) +
        tile("Collecting for", isNum(st.since) ? elapsed(d.now - st.since) : "n/a", "since the first start") +
        tile("Tokens seen", fmtInt(st.mints), `${fmtInt(st.non_sol || 0)} not quoted in SOL, skipped`) +
        tile("Trades stored", fmtInt(st.trades), st.parse_errors ? `${fmtInt(st.parse_errors)} undecodable events` : "every event decoded") +
        tile("Ranking", rep.generated ? ago(d.now - rep.generated) : "not yet", rep.generated ? `${fmtInt((rep.counts || {}).wallets_ranked)} wallets · refreshed every 30 min` : "the first one comes 30 min after start");
    const b = rep.base || {};
    $("#pump-base").textContent = isNum(b.profitable_share)
      ? `Over ${fmtNum((rep.window || {}).hours, 1)} h: ${fmtPct(b.profitable_share, 0)} of the ${fmtInt(b.wallets)} wallets with ${p.min_tokens}+ tokens made money. ` +
        (isNum(b.h1_winners_h2_profitable_share) ? `Of the ${fmtInt(b.h1_winners)} that made money in the first half, ${fmtPct(b.h1_winners_h2_profitable_share, 0)} also did in the second, against ${fmtPct(b.h2_profitable_share, 0)} of everyone: that gap is how much past profit says about future profit.` : "")
      : "";
    const empty = rep.empty || !rep.generated ? "No ranking yet: it is computed every 30 minutes from what has been collected." : "No wallet qualifies yet. It fills in as hours of data accumulate.";
    sortableTable($("#pump-traders"), [
      { key: "addr", label: "Wallet", render: (r) => solscan(r.addr) + (r.golden ? ' <span class="badge done">golden</span>' : "") + flags(r) },
      { key: "n", label: "Tokens", num: true, render: (r) => fmtInt(r.n) },
      { key: "win_rate", label: "Win rate", num: true, render: (r) => fmtPct(r.win_rate, 0) },
      { key: "pnl_sol", label: "PnL, SOL", num: true, render: (r) => solAmt(r.pnl_sol) },
      { key: "roi", label: "ROI", num: true, title: "profit over SOL spent, its own trades", render: (r) => pct(r.roi) },
      { key: "copy_roi", label: "Copier ROI", num: true, title: `profit per ${p.stake_sol} SOL copied buy, landing ${p.latency_slots} slots after the wallet`, render: (r) => pct(r.copy_roi) },
      { key: "copy_roi_h1", label: "1st half", num: true, render: (r) => pct(r.copy_roi_h1) },
      { key: "copy_roi_h2", label: "2nd half", num: true, render: (r) => pct(r.copy_roi_h2) },
      { key: "top2_share", label: "Top-2 share", num: true, title: "share of its winnings from its two best tokens", render: (r) => fmtPct(r.top2_share, 0) },
      { key: "avg_hold_min", label: "Avg hold", num: true, render: (r) => isNum(r.avg_hold_min) ? fmtNum(r.avg_hold_min, 1) + " min" : "n/a" },
      { key: "snipe_share", label: "Snipes", num: true, title: "share of its tokens bought within a couple of slots of creation", render: (r) => fmtPct(r.snipe_share, 0) },
    ], rep.traders || [], { sortKey: "copy_roi", dir: -1, empty });
    const snipers = (rep.snipers || []).map((r) => ({ ...r, snipe_win_rate: r.snipes ? r.snipe_wins / r.snipes : null }));
    sortableTable($("#pump-snipers"), [
      { key: "addr", label: "Wallet", render: (r) => solscan(r.addr) + flags(r) },
      { key: "snipes", label: "Snipes", num: true, render: (r) => fmtInt(r.snipes) },
      { key: "block0", label: "Same slot", num: true, title: "bought in the creation slot itself: bundled with the launch or colocated, not copyable", render: (r) => fmtInt(r.block0) },
      { key: "snipe_share", label: "Of its tokens", num: true, render: (r) => fmtPct(r.snipe_share, 0) },
      { key: "snipe_pnl_sol", label: "Snipe PnL, SOL", num: true, render: (r) => solAmt(r.snipe_pnl_sol) },
      { key: "snipe_win_rate", label: "Snipes won", num: true, render: (r) => fmtPct(r.snipe_win_rate, 0) },
      { key: "n", label: "Tokens", num: true, render: (r) => fmtInt(r.n) },
      { key: "pnl_sol", label: "All PnL, SOL", num: true, render: (r) => solAmt(r.pnl_sol) },
    ], snipers, { sortKey: "snipes", dir: -1, empty });
  }

  // ---------------------------------------------------------------- config
  const KEY_PARAMS = [
    ["pool_max_accounts", "Pool size"], ["equity_band", "Equity band"], ["lookback_days", "Lookback, days"], ["oos_days", "Out-of-sample, days"],
    ["latency_s", "Latency, s"], ["min_hold", "Min median hold"], ["max_thin_notional_share", "Max thin-coin share"], ["max_top2_share", "Max top-2 share"],
    ["min_profit_factor", "Min profit factor"], ["max_drawdown", "Max drawdown"], ["follower_equity_usd", "Follower equity"], ["follower_max_leverage", "Follower max leverage"],
    ["taker_fee_bps", "Taker fee, bps"], ["builder_fee_bps", "Builder fee, bps"], ["apply_funding", "Funding"], ["shortlist_size", "Shortlist size"],
  ];
  function renderKeyParams(v, minHold) {
    const val = (k) => {
      if (k === "equity_band") return `${fmtUsd(v.equity_min_usd)} – ${fmtUsd(v.equity_max_usd)}`;
      if (k === "min_hold") return `${fmtNum(minHold, 0)} min`;
      if (k === "follower_equity_usd") return fmtUsd(v[k]);
      if (["max_thin_notional_share", "max_top2_share", "max_drawdown"].includes(k)) return fmtPct(v[k], 0);
      if (k === "follower_max_leverage") return `${v[k]}×`;
      if (k === "apply_funding") return v[k] ? "on" : "off";
      return String(v[k]);
    };
    $("#config-kv").innerHTML = KEY_PARAMS.map(([k, label]) => `<dt>${label}</dt><dd>${esc(val(k))}</dd>`).join("");
  }
  function setConfigStatus(msg, cls) { const el = $("#config-status"); el.textContent = msg; el.className = "status-line " + (cls || ""); }
  async function loadConfig() {
    try {
      const c = state.config = await api("GET", "/api/config");
      $("#config-text").value = c.text;
      $("#config-path").textContent = c.path || "(no config file — built-in defaults, read-only)";
      $("#config-text").disabled = !c.path; $("#btn-config-save").disabled = !c.path;
      setConfigStatus(c.error ? "Config does not load: " + c.error : "", c.error ? "err" : "");
      renderKeyParams(c.values, c.min_hold_minutes);
    } catch (e) { toast(e.message); }
  }
  async function saveConfig() {
    try {
      const c = state.config = await api("PUT", "/api/config", $("#config-text").value, true);
      renderKeyParams(c.values, c.min_hold_minutes);
      setConfigStatus("Saved " + new Date().toLocaleTimeString(), "ok");
    } catch (e) { setConfigStatus(e.message, "err"); }
  }

  // ---------------------------------------------------------------- design tab
  const COLOR_TOKENS = ["primary", "primary-hover", "primary-focus", "on-primary", "ink", "ink-muted", "ink-subtle", "ink-tertiary", "canvas", "surface-1", "surface-2", "surface-3", "surface-4", "hairline", "hairline-strong", "hairline-tertiary", "brand-secure", "semantic-success", "semantic-danger", "semantic-warning"];
  const TYPE_TOKENS = ["display-md", "headline", "card-title", "subhead", "body-lg", "body", "body-sm", "caption", "button", "eyebrow", "mono"];
  function renderDesign() {
    const cs = getComputedStyle(document.documentElement);
    $("#swatches").innerHTML = COLOR_TOKENS.map((t) => { const v = cs.getPropertyValue("--color-" + t).trim(); return `<div class="swatch"><div class="chip" style="background:${v}"></div><div class="meta"><b>${t}</b>${v}</div></div>`; }).join("");
    $("#type-ramp").innerHTML = TYPE_TOKENS.map((t) => `<div><span class="tok">${t}</span><span style="font: var(--type-${t}); letter-spacing: var(--track-${t}, 0)">${t === "mono" ? "0x1a2b…c3d4 · 39.4% · n=17" : "Copy-adjusted return beats both benchmarks"}</span></div>`).join("");
  }

  // ---------------------------------------------------------------- wiring
  $$(".tab").forEach((t) => t.addEventListener("click", () => showView(t.dataset.view)));

  $$("form.action").forEach((f) => f.addEventListener("submit", (e) => {
    e.preventDefault();
    const fd = new FormData(f), body = { cmd: f.dataset.cmd };
    for (const [k, v] of fd.entries()) body[k] = v;
    if (fd.get("no_split") !== null) body.no_split = true;
    if (fd.get("cached_only") !== null) body.cached_only = true;
    startJob(body);
  }));

  $("#btn-stop").addEventListener("click", async () => { try { await api("POST", "/api/jobs/current/stop"); } catch (e) { toast(e.message); } });
  $("#btn-clear").addEventListener("click", () => { consoleOut.textContent = ""; });
  $("#btn-results").addEventListener("click", () => { showView("results"); refreshRuns({ selectNewest: true }); });
  $("#btn-config-save").addEventListener("click", saveConfig);
  $("#btn-config-reload").addEventListener("click", loadConfig);

  document.addEventListener("click", (e) => {
    const ins = e.target.closest("[data-inspect]");
    if (ins) {
      const form = $("#form-inspect"); form.elements.address.value = ins.dataset.inspect;
      showView("run"); form.scrollIntoView({ behavior: "smooth", block: "center" });
      if (!(state.job && state.job.status === "running")) form.requestSubmit();
      return;
    }
    const run = e.target.closest("[data-run]");
    if (run) { selectRun(run.dataset.run); return; }
    if (e.target.closest("#btn-empty-demo")) { showView("run"); startJob({ cmd: "demo" }); }
  });

  // ---------------------------------------------------------------- init
  (async () => {
    showView(state.view);
    try {
      const s = await api("GET", "/api/status");
      state.runs = s.runs || [];
      if (s.config_error) toast("config.toml does not load: " + s.config_error);
      if (s.job) {
        setJob(s.job, true);
        state.since = 0;
        if (s.job.status === "running") startPolling(); else await pollJob();
      }
    } catch (e) { toast("Cannot reach the local server: " + e.message); }
  })();
})();
