/* Aura Ops console. Vanilla JS, no build step.

   REFRESH MODEL (deliberate, read-cost first): NOTHING auto-refreshes. Each
   tab fetches once on its first view and then serves from client memory; only
   the Refresh button re-fetches the active tab, and it enforces a 60s cooldown
   between hits (with a visible countdown). The server adds its own TTL caches
   on top, so even a spammed refresh cannot multiply Firestore reads. The only
   timer in this file is the cooldown countdown label; it never touches the
   network. Gate behavior (localStorage passcode, 401 -> gate) is unchanged. */

(() => {
  "use strict";

  const gate = document.getElementById("gate");
  const appEl = document.getElementById("app");
  const content = document.getElementById("content");
  const gateMsg = document.getElementById("gateMsg");
  const pcInput = document.getElementById("pc");
  const stamp = document.getElementById("stamp");
  const refreshBtn = document.getElementById("refresh");
  const KEY = "ops_pc";

  const REFRESH_COOLDOWN_MS = 60000;

  const state = {
    tab: "overview",
    charts: {},
    tabData: {},            // tab -> payload (kept until an explicit refresh)
    overviewCore: null,
    overviewAnalytics: null,
    lastRefreshAt: 0,
    cooldownTimer: null,
    llmCostRange: "7d",
    llmToolsRange: "7d",
    llmToolFilter: "",
    logs: { services: "", severity: "DEFAULT", q: "", hours: 24 },
  };

  /* ── helpers ─────────────────────────────────────────────────────── */
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const ago = (iso) => {
    if (!iso) return "";
    const d = (Date.now() - new Date(iso).getTime()) / 1000;
    if (!isFinite(d)) return "";
    if (d < 60) return Math.floor(d) + "s";
    if (d < 3600) return Math.floor(d / 60) + "m";
    if (d < 86400) return Math.floor(d / 3600) + "h";
    return Math.floor(d / 86400) + "d";
  };

  const NA = '<span class="faint">n/a</span>';
  const num = (v) => (v === null || v === undefined) ? NA : esc(String(v));
  const ms = (v) => (v === null || v === undefined) ? NA : esc(Math.round(v).toLocaleString()) + "ms";
  const usd = (v) => "$" + Number(v || 0).toFixed(v >= 100 ? 0 : 2);
  const compact = (v) => {
    if (v === null || v === undefined) return "n/a";
    const n = Number(v);
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return String(n);
  };

  const PALETTE = ["#2dd4bf", "#a78bfa", "#60a5fa", "#fbbf24", "#f472b6", "#4ade80", "#f87171", "#a3a3a3"];

  if (window.Chart) {
    Chart.defaults.color = "#a3a3a3";
    Chart.defaults.borderColor = "rgba(255,255,255,.08)";
    Chart.defaults.font.family = 'ui-monospace, "Cascadia Code", Consolas, monospace';
    Chart.defaults.font.size = 10.5;
    Chart.defaults.font.weight = 600;
    Chart.defaults.plugins.legend.labels.boxWidth = 10;
    Chart.defaults.animation = false;
  }

  function mountChart(canvasId, config) {
    const el = document.getElementById(canvasId);
    if (!el || !window.Chart) return;
    if (state.charts[canvasId]) state.charts[canvasId].destroy();
    state.charts[canvasId] = new Chart(el, config);
  }

  function destroyChartsUnder(container) {
    for (const id of Object.keys(state.charts)) {
      if (!document.getElementById(id) || container.querySelector("#" + id)) {
        try { state.charts[id].destroy(); } catch (e) { /* already gone */ }
        delete state.charts[id];
      }
    }
  }

  /* ── skeletons ───────────────────────────────────────────────────── */
  function skelStrip(n) {
    return `<div class="strip">${Array.from({ length: n }, () => '<div class="skel skel-metric"></div>').join("")}</div>`;
  }

  function skelCard(colClass, { chart = false, lines = 4 } = {}) {
    const body = chart
      ? '<div class="skel skel-chart"></div>'
      : Array.from({ length: lines }, (_, i) =>
          `<div class="skel skel-line ${i % 3 === 1 ? "w80" : i % 3 === 2 ? "w60" : ""}"></div>`).join("");
    return `<div class="card ${colClass}"><div class="skel skel-title"></div>${body}</div>`;
  }

  function skelOverviewCore() {
    return skelStrip(8) + `<div class="grid">
      ${skelCard("col-6", { lines: 6 })}${skelCard("col-6", { lines: 6 })}
      ${skelCard("col-6", { lines: 3 })}${skelCard("col-6", { lines: 3 })}
      ${skelCard("col-12", { lines: 5 })}</div>`;
  }

  function skelOverviewAnalytics() {
    return `${skelCard("col-6", { chart: true })}${skelCard("col-6", { chart: true })}
      ${skelCard("col-8", { chart: true })}${skelCard("col-4", { lines: 4 })}
      ${skelCard("col-6", { lines: 5 })}${skelCard("col-6", { lines: 5 })}`;
  }

  function skelTab() {
    return `<div class="grid">${skelCard("col-6", { lines: 5 })}${skelCard("col-6", { lines: 5 })}
      ${skelCard("col-6", { lines: 6 })}${skelCard("col-6", { lines: 3 })}</div>`;
  }

  /* ── auth + fetch ────────────────────────────────────────────────── */
  const passcode = () => localStorage.getItem(KEY) || "";

  function showGate(msg) {
    appEl.hidden = true;
    gate.hidden = false;
    gateMsg.textContent = msg || "";
    pcInput.value = "";
    pcInput.focus();
  }

  async function api(path) {
    const pc = passcode();
    if (!pc) { showGate(""); throw new Error("no passcode"); }
    const res = await fetch(path, { headers: { Authorization: "Bearer " + pc } });
    if (res.status === 401) {
      localStorage.removeItem(KEY);
      showGate("Wrong passcode.");
      throw new Error("unauthorized");
    }
    if (!res.ok) throw new Error("load failed (" + res.status + ")");
    return res.json();
  }

  /* ── refresh cooldown (the ONLY timer; label-only, never fetches) ─── */
  function refreshCooldownRemainingMs() {
    return Math.max(0, REFRESH_COOLDOWN_MS - (Date.now() - state.lastRefreshAt));
  }

  function updateRefreshButton() {
    const remaining = refreshCooldownRemainingMs();
    if (remaining > 0) {
      refreshBtn.disabled = true;
      refreshBtn.textContent = "Refresh (" + Math.ceil(remaining / 1000) + "s)";
      if (!state.cooldownTimer) {
        state.cooldownTimer = setInterval(updateRefreshButton, 1000);
      }
    } else {
      refreshBtn.disabled = false;
      refreshBtn.textContent = "Refresh";
      if (state.cooldownTimer) {
        clearInterval(state.cooldownTimer);
        state.cooldownTimer = null;
      }
    }
  }

  refreshBtn.onclick = () => {
    if (refreshCooldownRemainingMs() > 0) return;
    state.lastRefreshAt = Date.now();
    updateRefreshButton();
    forceRefreshActiveTab();
  };

  /* ── shell wiring ────────────────────────────────────────────────── */
  document.getElementById("enter").onclick = () => {
    const v = pcInput.value.trim();
    if (!v) return;
    localStorage.setItem(KEY, v);
    boot();
  };
  pcInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("enter").click();
  });
  document.getElementById("lock").onclick = () => { localStorage.removeItem(KEY); showGate(""); };

  document.querySelectorAll("#tabs .tab").forEach((btn) => {
    btn.onclick = () => {
      document.querySelectorAll("#tabs .tab").forEach((b) => b.classList.toggle("active", b === btn));
      activateTab(btn.dataset.tab);
    };
  });

  function setStamp(iso) {
    stamp.textContent = iso ? "updated " + ago(iso) + " ago" : "";
  }

  /* ── shared render pieces ────────────────────────────────────────── */
  function metric(n, label, cls) {
    return `<div class="metric"><div class="n ${cls || ""}">${n ?? "n/a"}</div><div class="l">${label}</div></div>`;
  }

  function funnel(steps) {
    const base = steps.length && steps[0].value ? steps[0].value : 0;
    return steps.map((s) => {
      const v = s.value;
      const pct = (base && v !== null && v !== undefined) ? Math.round((v / base) * 100) : null;
      const width = pct === null ? 0 : Math.max(1, Math.min(100, pct));
      return `<div class="funnel-step">
        <span class="fl">${esc(s.label)}</span>
        <span class="fbar"><i style="width:${width}%"></i></span>
        <span class="fv">${num(v)}${pct !== null && v !== base ? `<span class="pct">${pct}%</span>` : ""}</span>
      </div>`;
    }).join("");
  }

  function crashRow(c) {
    const level = c.level === "fatal" || c.level === "error" ? "red" : "amber";
    return `<div class="crash">
      <span class="when">${ago(c.last_seen)}</span>
      <div class="t">${esc(c.title) || "(no title)"} <span class="tag ${level}">${esc(c.level || "?")}</span>
        <span class="tag gray">${esc(c.os || "")}${c.os_version ? " " + esc(c.os_version) : ""}</span></div>
      <div class="sub">${esc(c.subtitle || "")}</div>
      <div class="meta">${c.events} events · ${c.users} users${c.device ? " · " + esc(c.device) : ""}${c.app_version ? " · v" + esc(c.app_version) : ""}</div>
    </div>`;
  }

  function crashPanel(title, payload) {
    let body;
    if (payload && payload.available === false) {
      body = `<div class="note">${esc(payload.note || "Source not configured.")}</div>`;
    } else if (payload && payload.configured === false) {
      body = `<div class="note">Sentry not configured: set SENTRY_ORG / SENTRY_PROJECT / SENTRY_AUTH_TOKEN in ops/.env.</div>`;
    } else {
      const rows = (payload && payload.crashes) || [];
      body = rows.map(crashRow).join("") || '<p class="empty">No crashes in the window. Genuinely quiet.</p>';
    }
    return `<div class="card col-6"><h2>${esc(title)}</h2><div class="scroll">${body}</div></div>`;
  }

  function latencyCard(title, blocks, chatLatency, voiceStats) {
    const tiles = Object.entries(blocks || {}).map(([platform, p]) =>
      metric(p && p.p95 !== null && p.p95 !== undefined ? ms(p.p95) : "n/a", platform + " p95") +
      metric(p && p.p99 !== null && p.p99 !== undefined ? ms(p.p99) : "n/a", platform + " p99")
    ).join("");
    const chat = chatLatency || {};
    const voice = voiceStats || {};
    const note = (!Object.values(blocks || {}).some((p) => p && p.p95 !== null && p.p95 !== undefined))
      ? `<div class="note">Backend split needs the request_latency_by_platform log-based metric plus clients sending X-Aura-Platform (new builds). Until both exist this reads n/a, not zero.</div>`
      : "";
    return `<div class="card col-6"><h2>${esc(title)}</h2>
      <div class="strip">${tiles}
        ${metric(chat.count ? ms(chat.ttft_p95) : "n/a", "chat ttft p95")}
        ${metric(chat.count ? ms(chat.total_p95) : "n/a", "chat e2e p95")}
        ${metric(chat.count ? ms(chat.total_p99) : "n/a", "chat e2e p99")}
        ${metric(voice.count || "0", "voice sessions 7d")}
        ${metric(voice.elapsed_p95 !== null && voice.elapsed_p95 !== undefined ? ms(voice.elapsed_p95) : "n/a", "voice 1st reply p95")}
      </div>
      ${chat.count ? `<p class="faint">chat latency from ${chat.count} client-observed turns (7d)</p>` : ""}
      ${note}</div>`;
  }

  /* ── OVERVIEW ────────────────────────────────────────────────────── */
  function overviewShell() {
    content.innerHTML = `
      <div id="ov-core">${state.overviewCore ? "" : skelOverviewCore()}</div>
      <div id="ov-analytics" class="grid" style="margin-top:12px">${state.overviewAnalytics ? "" : skelOverviewAnalytics()}</div>`;
  }

  async function loadOverviewCore(force) {
    if (state.overviewCore && !force) { renderOverviewCore(state.overviewCore); return; }
    if (force) {
      const box = document.getElementById("ov-core");
      if (box) { destroyChartsUnder(box); box.innerHTML = skelOverviewCore(); }
    }
    let d;
    try { d = await api("/api/dashboard"); } catch (e) { return; }
    state.overviewCore = d;
    if (state.tab === "overview") renderOverviewCore(d);
  }

  function renderOverviewCore(d) {
    setStamp(d.generated_at);
    const box = document.getElementById("ov-core");
    if (!box) return;

    const m = d.metrics || {};
    const lat = d.latency || {};
    const strip = `<div class="strip">
      ${metric(m.signins_today, "signins today")}
      ${metric(m.new_today, "new today")}
      ${metric(m.active_today, "active today", "accent")}
      ${metric(m.total_users, "total users")}
      ${metric(m.messages_today, "msgs today")}
      ${metric(lat.p95 != null ? lat.p95 + "ms" : "n/a", "api p95 (1h)")}
      ${metric(lat.p99 != null ? lat.p99 + "ms" : "n/a", "api p99 (1h)")}
      ${metric(m.server_errors, "5xx / 1h", m.server_errors ? "danger" : "")}
    </div>`;

    const msgRow = (x) => `<div class="row"><span class="when">${ago(x.at)}</span>
      <span class="who">${esc(x.name)}</span>${x.channel === "voice" ? '<span class="tag">voice</span>' : ""}
      <div class="body">${esc(x.text)}</div></div>`;

    const voiceRow = (v) => `<div class="row"><span class="when">${ago(v.at)}</span>
      <span class="who">${esc(v.name)}</span><span class="tag">${esc(v.duration)} · ${v.turns} turns</span>
      <div class="body muted">${esc(v.summary) || "(no summary)"}</div></div>`;

    const recRow = (r) => {
      const cat = r.category ? `<span class="tag gray">${esc(r.category)}</span>` : "";
      const score = r.score != null ? `<span class="tag">score ${r.score}</span>` : "";
      const tapped = /opened/i.test(r.outcome || "");
      return `<div class="row"><span class="when">${ago(r.at)}</span>
        <span class="who">${esc(r.name)}</span>${cat}${score}
        <div class="body">${esc(r.title) || "(no title)"}</div>
        <div class="body muted">${esc(r.reason)}</div>
        <div class="body ${tapped ? "good" : "muted"}">${esc(r.outcome)}${r.source ? " · " + esc(r.source) : ""}</div></div>`;
    };

    box.innerHTML = strip + `<div class="grid">
      <div class="card col-6"><h2>Latest text messages</h2><div class="scroll">
        ${(d.messages || []).map(msgRow).join("") || '<p class="empty">none</p>'}</div></div>
      <div class="card col-6"><h2>Latest voice sessions</h2><div class="scroll">
        ${(d.voice || []).map(voiceRow).join("") || '<p class="empty">none</p>'}</div></div>
      <div class="card col-6"><h2>Recommender health (recent ticks)</h2>
        ${(d.recommender_health || []).map((h) => `<div class="row"><span class="when">${ago(h.at)}</span>
          <div class="body muted">${esc(h.message)}</div></div>`).join("")
          || '<p class="empty">no tick-health lines yet (INFO logs from the signal engine)</p>'}</div>
      <div class="card col-6"><h2>Top screens (7d, by views)</h2>
        ${(d.screens || []).map((s) => `<div class="row"><span class="when">${s.views}</span>${esc(s.screen)}</div>`).join("")
          || '<p class="empty">PostHog not configured (needs phx_ key + project id)</p>'}</div>
      <div class="card col-12"><h2>Recommendations sent · what / why / did it land</h2><div class="scroll">
        ${(d.recommendations || []).map(recRow).join("") || '<p class="empty">nothing sent yet</p>'}</div></div>
      <div class="card col-8"><h2>Users</h2><div class="scroll"><table>
        <tr><th>Name</th><th>Email</th><th class="num">Logins</th><th>Last active</th><th>Consent</th></tr>
        ${(d.users || []).map((u) => `<tr><td>${esc(u.name)}</td><td class="muted">${esc(u.email)}</td>
          <td class="num">${u.login_count}</td><td>${ago(u.last_active)}</td>
          <td>${u.aura_consent ? '<span class="good">yes</span>' : '<span class="faint">no</span>'}</td></tr>`).join("")}
      </table></div></div>
      <div class="card col-4"><h2>Recent feedback</h2><div class="scroll">
        ${(d.feedback || []).map((f) => `<div class="row"><span class="when">${ago(f.at)}</span>
          <span class="who">${esc(f.username) || "?"}</span><span class="tag gray">${esc(f.category)} · ${esc(f.severity)}</span>
          <div class="body">${esc(f.summary)}</div></div>`).join("") || '<p class="empty">none</p>'}</div></div>
      <div class="card col-12"><h2>Backend errors (multi-service)</h2><div class="scroll">
        ${(d.errors || []).map((e) => `<div class="row"><span class="when">${ago(e.at)}</span>
          <span class="tag red">${esc(e.severity)}</span>${e.service ? `<span class="tag gray">${esc(e.service)}</span>` : ""}
          <div class="body">${esc(e.message)}</div></div>`).join("") || '<p class="empty">none in window</p>'}</div></div>
    </div>`;
  }

  async function loadOverviewAnalytics(force) {
    if (state.overviewAnalytics && !force) { renderOverviewAnalytics(state.overviewAnalytics); return; }
    if (force) {
      const box = document.getElementById("ov-analytics");
      if (box) { destroyChartsUnder(box); box.innerHTML = skelOverviewAnalytics(); }
    }
    let d;
    try { d = await api("/api/overview/analytics"); } catch (e) { return; }
    state.overviewAnalytics = d;
    if (state.tab === "overview") renderOverviewAnalytics(d);
  }

  function renderOverviewAnalytics(d) {
    const box = document.getElementById("ov-analytics");
    if (!box) return;
    destroyChartsUnder(box);

    const r = d.retention || {};
    const nf = d.notification_funnel || {};
    const pf = d.paywall_funnel || {};
    const intents = d.payment_intents || [];

    box.innerHTML = `
      <div class="card col-6" id="llm-cost-card"></div>
      <div class="card col-6" id="llm-tools-card"></div>
      <div class="card col-8"><h2>Retention · daily actives (30d)</h2>
        <div class="strip">
          ${metric(num(r.dau), "DAU", "accent")}${metric(num(r.wau), "WAU")}${metric(num(r.mau), "MAU")}
          ${metric(r.mau ? Math.round(((r.dau || 0) / r.mau) * 100) + "%" : "n/a", "DAU/MAU")}
        </div>
        <div class="chart-box short"><canvas id="dauChart"></canvas></div></div>
      <div class="card col-4"><h2>Notification funnel (7d)</h2>
        ${funnel([
          { label: "sent", value: nf.sent },
          { label: "tapped", value: nf.tapped },
          { label: "session", value: nf.session },
          { label: "action", value: nf.action },
        ])}
        <p class="faint">signal engine origin · names from funnel_events.py</p></div>
      <div class="card col-6"><h2>Cohort retention · weekly (90d)</h2>${cohortTable(r.cohorts || [])}</div>
      <div class="card col-6"><h2>Revenue funnel · beta interest capture (30d)</h2>
        ${funnel([
          { label: "paywall viewed", value: pf.viewed },
          { label: "tier tapped (intent)", value: (pf.intents || []).reduce((a, b) => a + (b.count || 0), 0) || (pf.viewed === null ? null : 0) },
          { label: "intent docs (all time)", value: intents.length },
        ])}
        <table><tr><th>Tier</th><th>Period</th><th class="num">Taps 30d</th></tr>
          ${(pf.intents || []).map((i) => `<tr><td>${esc(i.tier)}</td><td>${esc(i.period)}</td><td class="num">${i.count}</td></tr>`).join("")
            || '<tr><td colspan="3" class="empty">no paywall_intent events yet</td></tr>'}</table>
        <div class="scroll" style="max-height:150px;margin-top:8px">
          ${intents.map((i) => `<div class="row"><span class="when">${ago(i.at)}</span>
            <span class="who">${esc(i.name)}</span><span class="tag">${esc(i.tier)} · ${esc(i.period)}</span></div>`).join("")
            || '<p class="empty">no captured intents in Firestore yet</p>'}</div></div>`;

    renderLlmCostCard(d.llm_cost);
    renderLlmToolsCard(d.llm_tools);

    const daily = r.daily || [];
    mountChart("dauChart", {
      type: "line",
      data: {
        labels: daily.map((x) => x.day.slice(5)),
        datasets: [{
          data: daily.map((x) => x.actives),
          borderColor: "#2dd4bf",
          backgroundColor: "rgba(45,212,191,.12)",
          fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2,
        }],
      },
      options: {
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
      },
    });
  }

  function cohortTable(cells) {
    if (!cells.length) return '<p class="empty">needs PostHog history (cohorts appear once persons span weeks)</p>';
    const byCohort = {};
    for (const c of cells) {
      (byCohort[c.cohort_week] = byCohort[c.cohort_week] || {})[c.week] = c.actives;
    }
    const weeks = [0, 1, 2, 3, 4, 5, 6, 7];
    const rows = Object.keys(byCohort).sort().map((cw) => {
      const base = byCohort[cw][0] || 0;
      const tds = weeks.map((w) => {
        const v = byCohort[cw][w];
        if (v === undefined) return '<td class="c0"></td>';
        const pct = base ? v / base : 0;
        const cls = pct >= 0.75 ? "c4" : pct >= 0.5 ? "c3" : pct >= 0.25 ? "c2" : v > 0 ? "c1" : "c0";
        return `<td class="${cls}" title="${Math.round(pct * 100)}%">${v}</td>`;
      }).join("");
      return `<tr><td class="label">${esc(cw)}</td>${tds}</tr>`;
    }).join("");
    return `<table class="cohort"><tr><th style="text-align:left">Cohort</th>${weeks.map((w) => `<th>W${w}</th>`).join("")}</tr>${rows}</table>`;
  }

  /* ── LLM cost + tools cards (range switches are explicit user actions) ─ */
  function seg(current, ranges, onclickName) {
    return `<span class="seg">${ranges.map((r) =>
      `<button class="${r === current ? "active" : ""}" onclick="${onclickName}('${r}')">${r}</button>`).join("")}</span>`;
  }

  function renderLlmCostCard(payload) {
    const card = document.getElementById("llm-cost-card");
    if (!card) return;
    destroyChartsUnder(card);
    const head = `<h2>LLM cost by model
      <span class="controls">${seg(state.llmCostRange, ["today", "7d", "30d"], "opsSetCostRange")}</span></h2>`;
    if (!payload || payload.configured === false) {
      card.innerHTML = head + `<div class="note">Langfuse not configured: set LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY in ops/.env (and in the backend so calls get traced).</div>`;
      return;
    }
    const models = payload.models || [];
    card.innerHTML = head + `
      <div class="strip">${metric(usd(payload.total_cost || 0), "total " + state.llmCostRange, "accent")}
        ${metric(compact(models.reduce((a, m) => a + (m.tokens || 0), 0)), "tokens")}
        ${metric(compact(models.reduce((a, m) => a + (m.calls || 0), 0)), "llm calls")}</div>
      <div class="chart-box short"><canvas id="costChart"></canvas></div>
      <table><tr><th>Model</th><th class="num">Cost</th><th class="num">Tokens</th><th class="num">Calls</th></tr>
        ${models.map((m) => `<tr><td>${esc(m.model)}</td><td class="num">${usd(m.cost)}</td>
          <td class="num">${compact(m.tokens)}</td><td class="num">${compact(m.calls)}</td></tr>`).join("")
          || '<tr><td colspan="4" class="empty">no traced calls in range (backend must have Langfuse keys set)</td></tr>'}</table>`;

    const daily = payload.daily || [];
    if (daily.length) {
      const days = [...new Set(daily.map((x) => x.day))].sort();
      const models2 = [...new Set(daily.map((x) => x.model))];
      const datasets = models2.map((model, i) => ({
        label: model,
        data: days.map((day) => {
          const hit = daily.find((x) => x.day === day && x.model === model);
          return hit ? hit.cost : 0;
        }),
        backgroundColor: PALETTE[i % PALETTE.length],
        stack: "cost",
      }));
      mountChart("costChart", {
        type: "bar",
        data: { labels: days.map((d2) => d2.slice(5)), datasets },
        options: {
          maintainAspectRatio: false,
          scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true } },
        },
      });
    }
  }

  function renderLlmToolsCard(payload) {
    const card = document.getElementById("llm-tools-card");
    if (!card) return;
    destroyChartsUnder(card);
    const head = `<h2>Tool calls
      <span class="controls">
        <input id="toolFilter" type="search" placeholder="filter tool…" value="${esc(state.llmToolFilter)}" style="width:110px;padding:4px 8px;font-size:11px" />
        ${seg(state.llmToolsRange, ["today", "7d", "30d"], "opsSetToolsRange")}
      </span></h2>`;
    if (!payload || payload.configured === false) {
      card.innerHTML = head + `<div class="note">Langfuse not configured: tool spans appear once backend keys are set.</div>`;
      return;
    }
    const tools = payload.tools || [];
    card.innerHTML = head + `
      <div class="chart-box"><canvas id="toolsChart"></canvas></div>
      <table><tr><th>Tool</th><th class="num">Calls</th><th class="num">p95</th></tr>
        ${tools.slice(0, 12).map((t) => `<tr><td>${esc(t.tool)}</td><td class="num">${compact(t.calls)}</td>
          <td class="num">${t.p95_ms !== null && t.p95_ms !== undefined ? Math.round(t.p95_ms) + "ms" : "n/a"}</td></tr>`).join("")
          || '<tr><td colspan="3" class="empty">no tool spans in range</td></tr>'}</table>`;

    const filterInput = document.getElementById("toolFilter");
    filterInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { state.llmToolFilter = filterInput.value.trim(); refetchLlmTools(); }
    });

    const top = tools.slice(0, 10);
    if (top.length) {
      mountChart("toolsChart", {
        type: "bar",
        data: {
          labels: top.map((t) => t.tool),
          datasets: [{ data: top.map((t) => t.calls), backgroundColor: "#2dd4bf", borderRadius: 4 }],
        },
        options: {
          indexAxis: "y",
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: { x: { beginAtZero: true, ticks: { precision: 0 } } },
        },
      });
    }
  }

  async function refetchLlmCost() {
    try {
      renderLlmCostCard(await api("/api/llm/cost?range=" + state.llmCostRange));
    } catch (e) { /* gate/network handled in api() */ }
  }

  async function refetchLlmTools() {
    try {
      renderLlmToolsCard(await api("/api/llm/tools?range=" + state.llmToolsRange +
        "&tool=" + encodeURIComponent(state.llmToolFilter)));
    } catch (e) { /* handled */ }
  }

  window.opsSetCostRange = (r) => { state.llmCostRange = r; refetchLlmCost(); };
  window.opsSetToolsRange = (r) => { state.llmToolsRange = r; refetchLlmTools(); };

  /* ── MOBILE / DESKTOP tabs ───────────────────────────────────────── */
  function renderPlatformTab(kind, d) {
    destroyChartsUnder(content);
    setStamp(d.generated_at);
    const downloads = d.downloads || {};
    let downloadsCard;
    if (downloads.available === false) {
      downloadsCard = `<div class="card col-6"><h2>Downloads</h2>
        <div class="note">${esc(downloads.note || "not available")}</div></div>`;
    } else {
      const releases = downloads.releases || [];
      downloadsCard = `<div class="card col-6"><h2>Downloads · GitHub Releases</h2>
        <div class="strip">${metric(compact(downloads.total_downloads), "installer downloads", "accent")}
          ${metric(esc(downloads.latest_version || "n/a"), "latest release")}</div>
        <div class="scroll" style="max-height:220px"><table>
          <tr><th>Release</th><th>Published</th><th class="num">Downloads</th></tr>
          ${releases.map((r) => `<tr><td>${esc(r.tag)}</td><td class="muted">${ago(r.published_at)} ago</td>
            <td class="num">${r.downloads}</td></tr>`).join("") || '<tr><td colspan="3" class="empty">no releases</td></tr>'}
        </table></div></div>`;
    }
    content.innerHTML = `<div class="grid">
      ${latencyCard("Latency · backend by platform + client-observed", d.backend_latency, d.chat_latency, d.voice_first_response)}
      ${downloadsCard}
      ${crashPanel(kind === "mobile" ? "Crashes · Crashlytics (7d)" : "Crashes · Sentry (14d)", d.crashes)}
      <div class="card col-6"><h2>Notes</h2><div class="note">
        ${kind === "mobile"
          ? "Store downloads land here when Play / App Store listings go live (both still in review). Crash data requires the Crashlytics BigQuery export toggle in Firebase console."
          : "Desktop = Aura-Desktop (Tauri) via its live Sentry project. Backend latency split needs the new client build (sends X-Aura-Platform) plus the log-based metric."}
      </div></div>
    </div>`;
  }

  /* ── WEB tab ─────────────────────────────────────────────────────── */
  function renderWebTab(d) {
    destroyChartsUnder(content);
    setStamp(d.generated_at);
    const a = d.analytics || {};
    const installs = d.installs || {};
    const pv = a.pageviews_daily || [];
    content.innerHTML = `<div class="grid">
      <div class="card col-8"><h2>auravoiceapp.com · pageviews (30d)</h2>
        <div class="chart-box"><canvas id="pvChart"></canvas></div></div>
      <div class="card col-4"><h2>Top referrers (30d)</h2>
        ${(a.top_referrers || []).map((r) => `<div class="row"><span class="when">${r.views}</span>${esc(r.referrer)}</div>`).join("")
          || '<p class="empty">no referrer data (or PostHog web project not configured)</p>'}</div>
      <div class="card col-6"><h2>Download funnel (30d)</h2>
        ${funnel([
          { label: "download page", value: a.download_page_viewed },
          { label: "download clicked", value: a.download_clicked },
          { label: "installer downloads*", value: installs.total_downloads ?? null },
        ])}
        <p class="faint">*GitHub asset downloads, all-time across releases: a proxy for installs, not a 30d slice.</p></div>
      <div class="card col-6"><h2>Site signals (30d)</h2>
        <div class="strip">
          ${metric(num(a.waitlist_submitted), "waitlist submitted")}
          ${metric(num(a.pricing_viewed), "pricing viewed")}
          ${metric(esc(installs.latest_version || "n/a"), "live desktop version")}
        </div>
        <div class="note">Marketing surface only: aura-web's own PostHog events (download_page_viewed / download_clicked already instrumented in its analytics.ts). If these read 0 with real traffic, aura-web may be on a different PostHog project: set OPS_POSTHOG_WEB_PROJECT_ID.</div></div>
    </div>`;

    if (pv.length) {
      mountChart("pvChart", {
        type: "bar",
        data: {
          labels: pv.map((x) => x.day.slice(5)),
          datasets: [{ data: pv.map((x) => x.views), backgroundColor: "#60a5fa", borderRadius: 4 }],
        },
        options: {
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
        },
      });
    }
  }

  /* ── LOGS tab ────────────────────────────────────────────────────── */
  function renderLogsShell(services) {
    const opts = (services || ["juno-backend", "juno-ops"]).map((s) =>
      `<option value="${esc(s)}" ${state.logs.services === s ? "selected" : ""}>${esc(s)}</option>`).join("");
    content.innerHTML = `
      <div class="card col-12">
        <h2>Log viewer · Cloud Run</h2>
        <div class="log-controls">
          <input id="logQ" type="search" placeholder="search text… (Enter to run)" value="${esc(state.logs.q)}" />
          <select id="logSvc"><option value="">all services</option>${opts}</select>
          <select id="logSev">
            ${["DEFAULT", "INFO", "WARNING", "ERROR"].map((s) =>
              `<option ${state.logs.severity === s ? "selected" : ""}>${s}</option>`).join("")}
          </select>
          <select id="logHours">
            ${[["1", "1h"], ["6", "6h"], ["24", "24h"], ["72", "3d"], ["168", "7d"]].map(([v, l]) =>
              `<option value="${v}" ${String(state.logs.hours) === v ? "selected" : ""}>${l}</option>`).join("")}
          </select>
          <button class="primary" id="logRun">Search</button>
        </div>
        <div id="logNote"></div>
        <div id="logResults" class="scroll" style="max-height:70vh"><p class="empty">Run a search. Nothing loads on its own.</p></div>
      </div>`;
    document.getElementById("logRun").onclick = runLogSearch;
    document.getElementById("logQ").addEventListener("keydown", (e) => { if (e.key === "Enter") runLogSearch(); });
  }

  async function runLogSearch() {
    state.logs.q = document.getElementById("logQ").value.trim();
    state.logs.services = document.getElementById("logSvc").value;
    state.logs.severity = document.getElementById("logSev").value;
    state.logs.hours = parseInt(document.getElementById("logHours").value, 10) || 24;
    const results = document.getElementById("logResults");
    results.innerHTML = Array.from({ length: 8 }, () => '<div class="skel skel-line"></div>').join("");
    let d;
    try {
      d = await api("/api/logs?services=" + encodeURIComponent(state.logs.services) +
        "&severity=" + encodeURIComponent(state.logs.severity) +
        "&q=" + encodeURIComponent(state.logs.q) +
        "&hours=" + state.logs.hours + "&limit=200");
    } catch (e) {
      results.innerHTML = '<p class="bad">Search failed.</p>';
      return;
    }
    document.getElementById("logNote").innerHTML = `<div class="note">${esc(d.voice_note || "")}</div>`;
    const entries = d.entries || [];
    results.innerHTML = entries.map((e) => `<div class="log-line">
        <span class="ts">${esc((e.at || "").replace("T", " ").slice(0, 19))}</span>
        <span class="sev sev-${esc(e.severity)}">${esc(e.severity)}</span>
        <span class="svc">${esc(e.service)}</span>
        <span class="msg">${esc(e.message)}</span>
      </div>`).join("") || '<p class="empty">no matching entries</p>';
  }

  /* ── tab activation: render from memory; fetch ONLY when nothing is
        cached yet (first view). Explicit refresh is the only re-fetch. ── */
  async function activateTab(tab) {
    state.tab = tab;
    destroyChartsUnder(content);

    if (tab === "overview") {
      overviewShell();
      loadOverviewCore(false);
      loadOverviewAnalytics(false);
      return;
    }
    if (tab === "logs") {
      renderLogsShell(["juno-backend", "juno-ops"]);
      return;
    }

    const cached = state.tabData[tab];
    if (cached) {
      if (tab === "web") renderWebTab(cached); else renderPlatformTab(tab, cached);
      return;
    }
    await fetchTab(tab);
  }

  async function fetchTab(tab) {
    content.innerHTML = skelTab();
    let d;
    try { d = await api("/api/tab/" + tab); } catch (e) {
      content.innerHTML = '<p class="bad">Load failed.</p>';
      return;
    }
    if (state.tab !== tab) return;
    state.tabData[tab] = d;
    if (tab === "web") renderWebTab(d); else renderPlatformTab(tab, d);
  }

  function forceRefreshActiveTab() {
    const tab = state.tab;
    if (tab === "overview") {
      loadOverviewCore(true);
      loadOverviewAnalytics(true);
      return;
    }
    if (tab === "logs") {
      runLogSearch();
      return;
    }
    delete state.tabData[tab];
    fetchTab(tab);
  }

  /* ── boot ────────────────────────────────────────────────────────── */
  function boot() {
    gate.hidden = true;
    appEl.hidden = false;
    updateRefreshButton();
    activateTab(state.tab);
  }

  if (passcode()) boot(); else showGate("");
})();
