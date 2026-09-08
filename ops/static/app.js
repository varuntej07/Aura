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
    providerCostRange: "7d",
    userFilter: null,        // {key, label, uids} — which count the table is showing
    openUser: null,          // uid of the drawer that is open
    logs: { services: "", severity: "ERROR", q: "", hours: 24 },
  };

  /* ── helpers ─────────────────────────────────────────────────────── */
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const ago = window.AuraOpsTime.ago;
  const inspectTime = window.AuraOpsTime.inspect;
  const isDisplayableTime = window.AuraOpsTime.isDisplayable;
  const timePhrase = window.AuraOpsTime.phrase;

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
    if (!iso) {
      stamp.textContent = "";
      return;
    }
    const time = inspectTime(iso);
    stamp.textContent = time.state === "invalid"
      ? "data timestamp unavailable"
      : "updated " + timePhrase(iso);
  }

  function setStampLabel(label) {
    stamp.textContent = label || "";
  }

  /* ── shared render pieces ────────────────────────────────────────── */
  /* `value` is HTML, deliberately: num()/ms()/usd() return markup (the styled
     "n/a" span), so this cannot escape its input. Every call site that passes a
     RAW provider string must therefore use metricText() below instead. Keeping
     the two apart is what stops a provider-supplied version string from being
     injected the next time someone adds a tile. */
  function metric(value, label, cls) {
    return `<div class="metric"><div class="n ${cls || ""}">${value ?? "n/a"}</div><div class="l">${esc(label)}</div></div>`;
  }

  /* A tile whose value came from a provider as plain text. */
  function metricText(value, label, cls) {
    return metric(esc(value ?? "n/a"), label, cls);
  }

  /* A tile that drills down: clicking it shows the rows behind the number. */
  function metricDrill(value, label, filterKey, cls) {
    return `<div class="metric drill" data-drill="${esc(filterKey)}" tabindex="0" role="button"
      title="Show the ${esc(label)}"><div class="n ${cls || ""}">${value ?? "n/a"}</div>
      <div class="l">${esc(label)}</div></div>`;
  }

  function pageHeading(eyebrow, title, summary, sources) {
    const sourceHtml = (sources || []).map((source) =>
      `<span class="source-pill ${source.tone || ""}" title="${esc(source.detail || "")}">${esc(source.label)}</span>`
    ).join("");
    return `<section class="page-heading">
      <div>
        <p class="eyebrow">${esc(eyebrow)}</p>
        <h1>${esc(title)}</h1>
        <p class="summary">${esc(summary)}</p>
      </div>
      ${sourceHtml ? `<div class="source-strip" aria-label="Data source status">${sourceHtml}</div>` : ""}
    </section>`;
  }

  function timeLabel(iso) {
    const time = inspectTime(iso);
    const timestampMs = Date.parse(iso);
    const title = Number.isFinite(timestampMs)
      ? new Date(timestampMs).toLocaleString()
      : "Timestamp unavailable";
    return `<time class="when ${time.state === "future" ? "future" : ""}" datetime="${esc(iso)}" title="${esc(title)}">${esc(time.label)}</time>`;
  }

  /* A funnel renders conversion percentages ONLY when every step counts the same
     population over the same window. Pass {comparable:false} when the steps come
     from different sources or time ranges (e.g. 30d web clicks vs all-time
     installer fetches): then the bars are scaled to the largest step and no
     percentage is shown, because "818%" is not a conversion rate, it is two
     unrelated numbers divided by each other. */
  function funnel(steps, { comparable = true } = {}) {
    const values = steps.map((s) => s.value).filter((v) => v !== null && v !== undefined);
    const base = comparable
      ? (steps.length && steps[0].value ? steps[0].value : 0)
      : (values.length ? Math.max(...values) : 0);
    return steps.map((s) => {
      const v = s.value;
      const known = v !== null && v !== undefined;
      const ratio = base && known ? v / base : null;
      const width = ratio === null ? 0 : Math.max(1, Math.min(100, Math.round(ratio * 100)));
      const showPct = comparable && ratio !== null && v !== base;
      return `<div class="funnel-step">
        <span class="fl">${esc(s.label)}${s.note ? `<span class="fnote">${esc(s.note)}</span>` : ""}</span>
        <span class="fbar"><i style="width:${width}%"></i></span>
        <span class="fv">${num(v)}${showPct ? `<span class="pct">${Math.round(ratio * 100)}%</span>` : ""}</span>
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
    // One availability key across every provider (`available`), so a panel can
    // never accidentally read the wrong one and render a dead source as empty.
    if (payload && payload.available === false) {
      body = `<div class="note">${esc(payload.note || "Source not configured.")}</div>`;
    } else {
      const rows = (payload && payload.crashes) || [];
      body = rows.map(crashRow).join("") || '<p class="empty">No crashes in the window. Genuinely quiet.</p>';
    }
    return `<div class="card col-6"><h2>${esc(title)}</h2><div class="scroll">${body}</div></div>`;
  }

  function percentileTiles(stats, label) {
    const values = stats || {};
    return ["p50", "p95", "p99"].map((p) =>
      metric(values[p] !== null && values[p] !== undefined ? ms(values[p]) : "n/a", label + " " + p)
    ).join("");
  }

  function latencyCard(title, blocks, chatLatency, voiceStats, workerStats) {
    const tiles = Object.entries(blocks || {}).map(([platform, p]) =>
      metric(p && p.p95 !== null && p.p95 !== undefined ? ms(p.p95) : "n/a", platform + " p95") +
      metric(p && p.p99 !== null && p.p99 !== undefined ? ms(p.p99) : "n/a", platform + " p99")
    ).join("");
    const chat = chatLatency || {};
    const voice = voiceStats || {};
    const worker = workerStats || {};
    const note = (!Object.values(blocks || {}).some((p) => p && p.p95 !== null && p.p95 !== undefined))
      ? `<div class="note">Backend split needs the request_latency_by_platform log-based metric plus clients sending X-Aura-Platform (new builds). Until both exist this reads n/a, not zero.</div>`
      : "";
    return `<div class="card col-6"><h2>${esc(title)}</h2>
      <div class="strip">${tiles}
        ${percentileTiles({
          p50: chat.ttft_p50, p95: chat.ttft_p95, p99: chat.ttft_p99,
        }, "chat first text")}
        ${percentileTiles({
          p50: chat.total_p50, p95: chat.total_p95, p99: chat.total_p99,
        }, "chat complete")}
        ${percentileTiles({
          p50: voice.elapsed_p50, p95: voice.elapsed_p95, p99: voice.elapsed_p99,
        }, "voice start to talk")}
        ${percentileTiles(worker.worker_first_talk, "worker start to talk")}
        ${percentileTiles(worker.reply_to_first_talk, "user stop to audio")}
      </div>
      ${chat.count ? `<p class="faint">chat latency from ${chat.count} client-observed turns (7d)</p>` : ""}
      ${worker.count ? `<p class="faint">voice worker latency from ${worker.count} structured records (7d)</p>` : ""}
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

  /* Which uids sit behind each headline count. The server sends the sets for
     the Firestore-derived counts; "talked today" is derived here from feeds the
     page already has, so it costs no extra read. */
  function overviewCohorts(d) {
    const cohorts = { ...(d.cohorts || {}) };
    cohorts.talked_today = [...todayTalkers(d)];
    return cohorts;
  }

  /* Users who actually said something to Buddy today, by uid.

     This is deliberately NOT the same as `active today`. The writer behind
     active_today (auth_repository.dart) stamps last_active_at on silent session
     restore as well as explicit sign-in, so it means "opened the app". For a
     companion app the number that matters is whether they TALKED, and the gap
     between the two is worth seeing rather than averaging away. */
  function todayTalkers(d) {
    const midnight = new Date();
    midnight.setHours(0, 0, 0, 0);
    const uids = new Set();
    for (const feed of [d.messages || [], d.voice || []]) {
      for (const row of feed) {
        const at = Date.parse(row.at);
        if (Number.isFinite(at) && at >= midnight.getTime() && row.uid) uids.add(row.uid);
      }
    }
    return uids;
  }

  /* Things that want the founder's attention right now, derived entirely from
     data already on the page. A row exists only when its condition is true, so
     an empty strip is a real all-clear rather than a decoration. */
  function attentionItems(d) {
    const m = d.metrics || {};
    const items = [];

    if (m.server_errors) {
      items.push({ tone: "bad", text: `${m.server_errors} server 5xx in the last hour.`, drill: null });
    }
    if (m.server_errors === null || m.server_errors === undefined) {
      items.push({ tone: "warn", text: "Cloud Monitoring is not answering, so reliability numbers read n/a rather than zero.", drill: null });
    }

    const talkers = todayTalkers(d);
    const activeUids = (d.cohorts && d.cohorts.active_today) || [];
    const silent = activeUids.filter((uid) => !talkers.has(uid));
    if (activeUids.length && silent.length === activeUids.length) {
      items.push({
        tone: "bad",
        text: `All ${activeUids.length} people who opened the app today left without talking to Buddy.`,
        drill: "active_today",
      });
    } else if (silent.length) {
      items.push({
        tone: "warn",
        text: `${silent.length} of ${activeUids.length} people who opened the app today did not talk to Buddy.`,
        drill: "active_today",
      });
    }

    const futureStamped = (d.cohorts && d.cohorts.future_stamped) || [];
    if (futureStamped.length) {
      items.push({
        tone: "warn",
        text: `${futureStamped.length} account(s) have a last_active_at in the future, so their `
          + `device clock is wrong. They are excluded from today's counts.`,
        drill: "future_stamped",
      });
    }

    const quietTicks = (d.recommender_health || []).filter((h) => /sent=0\b/.test(h.message || ""));
    if (quietTicks.length) {
      items.push({
        tone: "warn",
        text: `The recommender sent nothing on ${quietTicks.length} of its last ${(d.recommender_health || []).length} ticks.`,
        drill: null,
      });
    }

    const dayAgo = Date.now() - 24 * 3600 * 1000;
    const severe = (d.feedback || []).filter((f) =>
      /high|critical|severe/i.test(f.severity || "") && Date.parse(f.at) >= dayAgo);
    if (severe.length) {
      items.push({ tone: "bad", text: `${severe.length} high-severity piece(s) of feedback in the last 24h.`, drill: null });
    }

    const desktop = d.desktop || {};
    if (desktop.available && desktop.installs === 0) {
      items.push({
        tone: "warn",
        text: "No desktop install has ever reached a signed-in state. Any download count you see elsewhere is fetches, not people.",
        drill: null,
      });
    }

    const spend = d.llm_spend || {};
    const activeCount = m.active_today || 0;
    if (spend.available && activeCount && spend.est_usd !== null && spend.est_usd !== undefined) {
      const perUserPerDay = spend.est_usd / Math.max(1, spend.days) / activeCount;
      if (perUserPerDay > 1) {
        items.push({
          tone: "warn",
          text: `LLM spend is running about ${usd(perUserPerDay)} per active user per day.`,
          drill: null,
        });
      }
    }
    if (spend.available === false) {
      items.push({
        tone: "warn",
        text: "No LLM ledger rows in the window: either nothing ran, or the backend stopped writing users/{uid}/cost.",
        drill: null,
      });
    }
    return items;
  }

  function attentionStrip(items) {
    if (!items.length) {
      return `<section class="attention clear"><span class="dot good"></span>
        Nothing is asking for attention. Every check below passed on the data currently loaded.</section>`;
    }
    return `<section class="attention">${items.map((item) => `
      <div class="att ${esc(item.tone)}${item.drill ? " drill" : ""}"${item.drill ? ` data-drill="${esc(item.drill)}" tabindex="0" role="button"` : ""}>
        <span class="dot ${esc(item.tone)}"></span><span>${esc(item.text)}</span>
      </div>`).join("")}</section>`;
  }

  function renderOverviewCore(d) {
    setStamp(d.generated_at);
    const box = document.getElementById("ov-core");
    if (!box) return;

    const m = d.metrics || {};
    const lat = d.latency || {};
    const cohorts = overviewCohorts(d);
    const talkedToday = cohorts.talked_today.length;

    /* Every count that has a knowable membership is a drill-down. The dashboard
       used to render "6 active today" with no way to learn who the six were. */
    const strip = `<div class="strip">
      ${metricDrill(m.signins_today, "signins today", "signins_today")}
      ${metricDrill(m.new_today, "new today", "new_today")}
      ${metricDrill(m.active_today, "opened app today", "active_today", "accent")}
      ${metricDrill(talkedToday, "talked to Buddy today", "talked_today",
        talkedToday ? "good" : (m.active_today ? "danger" : ""))}
      ${metricDrill(m.total_users, "total users", "total_users")}
      ${metric(m.messages_today, "msgs today")}
      ${metric(lat.p95 != null ? lat.p95 + "ms" : "n/a", "api p95 (1h)")}
      ${metric(m.server_errors, "5xx / 1h", m.server_errors ? "danger" : "")}
    </div>`;

    const visibleMessages = (d.messages || []).filter((message) =>
      isDisplayableTime(message.at));

    const who = (row) =>
      `<span class="who user-link" data-uid="${esc(row.uid || "")}" tabindex="0" role="button">${esc(row.name)}</span>`;

    const msgRow = (x) => {
      const timestamp = inspectTime(x.at);
      const timestampWarning = timestamp.state === "invalid"
          ? '<span class="tag red">invalid timestamp</span>'
          : "";
      return `<div class="row">${timeLabel(x.at)}
        ${who(x)}${x.channel === "voice" ? '<span class="tag">voice</span>' : ""}${timestampWarning}
        <div class="body">${esc(x.text)}</div></div>`;
    };

    const voiceRow = (v) => `<div class="row">${timeLabel(v.at)}
      ${who(v)}<span class="tag">${esc(v.duration)} · ${esc(String(v.turns))} turns</span>
      <div class="body muted">${esc(v.summary) || "(no summary)"}</div></div>`;

    const recRow = (r) => {
      const cat = r.category ? `<span class="tag gray">${esc(r.category)}</span>` : "";
      const score = r.score != null ? `<span class="tag">score ${esc(String(r.score))}</span>` : "";
      const tapped = /opened/i.test(r.outcome || "");
      return `<div class="row">${timeLabel(r.at)}
        ${who(r)}${cat}${score}
        <div class="body">${esc(r.title) || "(no title)"}</div>
        <div class="body muted">${esc(r.reason)}</div>
        <div class="body ${tapped ? "good" : "muted"}">${esc(r.outcome)}${r.source ? " · " + esc(r.source) : ""}</div></div>`;
    };

    const posthogHasData = (d.screens || []).length > 0;
    const heading = pageHeading(
      "Live production health",
      "Operations overview",
      "A single view of user activity, reliability, recommendations, feedback, retention, and spend. Every count is clickable: it shows the people behind it. Refresh is manual to protect Firestore read costs.",
      [
        { label: "Firestore live", detail: "Users, messages, sessions, feedback, and recommendation history" },
        { label: posthogHasData ? "PostHog reporting" : "PostHog: no recent data", tone: posthogHasData ? "" : "warn", detail: "Product analytics, retention, funnels, and client-observed latency" },
        { label: m.server_errors == null ? "Cloud metrics unavailable" : "Cloud metrics live", tone: m.server_errors == null ? "warn" : "", detail: "Cloud Monitoring and Logging reliability signals" },
      ],
    );

    box.innerHTML = heading + attentionStrip(attentionItems(d)) + strip + `<div class="grid">
      <div class="card col-6"><h2>Latest text messages <span class="tag gray">${visibleMessages.length} shown</span></h2><div class="scroll">
        ${visibleMessages.map(msgRow).join("") || '<p class="empty">none</p>'}</div></div>
      <div class="card col-6"><h2>Latest voice sessions <span class="tag gray">${(d.voice || []).length} shown</span></h2><div class="scroll">
        ${(d.voice || []).map(voiceRow).join("") || '<p class="empty">none</p>'}</div></div>
      <div class="card col-6"><h2>Recommender health (recent ticks)</h2>
        ${(d.recommender_health || []).map((h) => `<div class="row">${timeLabel(h.at)}
          <div class="body muted">${esc(h.message)}</div></div>`).join("")
          || '<p class="empty">no tick-health lines yet (INFO logs from the signal engine)</p>'}</div>
      <div class="card col-6"><h2>Top screens (7d, by views)</h2>
        ${(d.screens || []).map((s) => `<div class="row"><span class="when">${esc(String(s.views))}</span>${esc(s.screen)}</div>`).join("")
          || '<p class="empty">PostHog not configured (needs phx_ key + project id)</p>'}</div>
      <div class="card col-12"><h2>Recommendations sent · what / why / did it land</h2><div class="scroll">
        ${(d.recommendations || []).map(recRow).join("") || '<p class="empty">nothing sent yet</p>'}</div></div>
      <div class="card col-8" id="usersCard"><h2>Users <span id="userFilterChip"></span></h2>
        <div class="scroll" id="usersTableBox"></div></div>
      <div class="card col-4"><h2>Recent feedback</h2><div class="scroll">
        ${(d.feedback || []).map((f) => `<div class="row">${timeLabel(f.at)}
          <span class="who">${esc(f.username) || "?"}</span><span class="tag gray">${esc(f.category)} · ${esc(f.severity)}</span>
          <div class="body">${esc(f.summary)}</div></div>`).join("") || '<p class="empty">none</p>'}</div></div>
      <div class="card col-12"><h2>Backend errors (multi-service)</h2><div class="scroll">
        ${(d.errors || []).map((e) => `<div class="row">${timeLabel(e.at)}
          <span class="tag red">${esc(e.severity)}</span>${e.service ? `<span class="tag gray">${esc(e.service)}</span>` : ""}
          <div class="body">${esc(e.message)}</div></div>`).join("") || '<p class="empty">none in window</p>'}</div></div>
    </div>`;

    renderUsersTable(d);
    wireOverviewDrills(d);
  }

  const DRILL_LABELS = {
    signins_today: "signins today",
    new_today: "new today",
    active_today: "opened the app today",
    talked_today: "talked to Buddy today",
    total_users: "all users",
    future_stamped: "accounts with a future-dated clock",
  };

  /* The Users table, filtered to whichever count the founder clicked. */
  function renderUsersTable(d) {
    const box = document.getElementById("usersTableBox");
    const chip = document.getElementById("userFilterChip");
    if (!box) return;

    const cohorts = overviewCohorts(d);
    const filter = state.userFilter;
    const allow = filter ? new Set(cohorts[filter] || []) : null;
    const talkers = todayTalkers(d);
    const rows = (d.users || []).filter((u) => !allow || allow.has(u.uid));

    if (chip) {
      chip.innerHTML = filter
        ? `<span class="filter-chip">showing: ${esc(DRILL_LABELS[filter] || filter)} (${rows.length})
             <button id="clearUserFilter" title="Show all users">clear</button></span>`
        : `<span class="tag gray">${(d.users || []).length} total</span>`;
      const clear = document.getElementById("clearUserFilter");
      if (clear) clear.onclick = () => { state.userFilter = null; renderUsersTable(d); };
    }

    box.innerHTML = `<table>
      <tr><th>Name</th><th>Email</th><th>Surfaces</th><th class="num">Logins</th>
        <th>Last active</th><th>Today</th><th>Consent</th></tr>
      ${rows.map((u) => {
        const surfaces = (u.linked_platforms || []).length
          ? u.linked_platforms.map((pl) => `<span class="tag gray">${esc(pl)}</span>`).join("")
          : `<span class="faint">${esc(u.platform || "?")}</span>`;
        const talked = talkers.has(u.uid);
        const todayCell = u.future_stamped
          ? '<span class="warnfg" title="last_active_at is in the future; device clock is wrong">bad clock</span>'
          : u.active_today
            ? (talked ? '<span class="good">talked</span>' : '<span class="warnfg">opened only</span>')
            : '<span class="faint">-</span>';
        return `<tr class="user-row" data-uid="${esc(u.uid)}">
          <td><span class="user-link" data-uid="${esc(u.uid)}">${esc(u.name)}</span>
            ${u.new_today ? '<span class="tag">new</span>' : ""}</td>
          <td class="muted">${esc(u.email)}</td>
          <td>${surfaces}</td>
          <td class="num">${esc(String(u.login_count))}</td>
          <td>${ago(u.last_active)}</td>
          <td>${todayCell}</td>
          <td>${u.aura_consent ? '<span class="good">yes</span>' : '<span class="faint">no</span>'}</td>
        </tr>`;
      }).join("") || '<tr><td colspan="7" class="empty">no users match this filter</td></tr>'}
    </table>`;
  }

  /* One delegated listener for every drill-down and name link on Overview. */
  function wireOverviewDrills(d) {
    const box = document.getElementById("ov-core");
    if (!box) return;

    const act = (target) => {
      const userLink = target.closest("[data-uid]");
      if (userLink && userLink.dataset.uid) {
        openUserDrawer(userLink.dataset.uid, d);
        return true;
      }
      const drill = target.closest("[data-drill]");
      if (drill && drill.dataset.drill) {
        state.userFilter = state.userFilter === drill.dataset.drill ? null : drill.dataset.drill;
        renderUsersTable(d);
        document.getElementById("usersCard")?.scrollIntoView({ behavior: "smooth", block: "center" });
        return true;
      }
      return false;
    };

    box.onclick = (e) => { act(e.target); };
    box.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") {
        if (act(e.target)) e.preventDefault();
      }
    };
  }

  /* ── per-user drawer ─────────────────────────────────────────────────
     Everything here is a client-side filter over feeds the Overview payload
     ALREADY contains (messages, voice, notifications, feedback, devices,
     spend). Opening it costs zero additional reads. */
  function openUserDrawer(uid, d) {
    if (!uid) return;
    state.openUser = uid;
    const drawer = document.getElementById("drawer");
    if (!drawer) return;

    const user = (d.users || []).find((u) => u.uid === uid) || { uid, name: uid.slice(0, 6) };
    const mine = (rows) => (rows || []).filter((r) => r.uid === uid);
    const messages = mine(d.messages);
    const voice = mine(d.voice);
    const recs = mine(d.recommendations);
    const devices = mine((d.desktop || {}).devices);
    const spendRow = ((d.llm_spend || {}).by_user || []).find((r) => r.uid === uid);

    const feed = (rows, empty, render) =>
      rows.length ? rows.map(render).join("") : `<p class="empty">${esc(empty)}</p>`;

    drawer.innerHTML = `
      <div class="drawer-head">
        <div>
          <h2>${esc(user.name)}</h2>
          <p class="muted">${esc(user.email || "")} · <code>${esc(uid)}</code></p>
        </div>
        <button id="drawerClose" title="Close (Esc)">close</button>
      </div>
      <div class="strip">
        ${metric(esc(String(user.login_count ?? 0)), "logins")}
        ${metric(messages.length, "msgs in feed")}
        ${metric(voice.length, "voice sessions")}
        ${metric(spendRow ? usd(spendRow.est_usd) : NA, "LLM spend 7d")}
      </div>
      <p class="faint">Joined ${esc(timePhrase(user.created_at) || "unknown")} ·
        last active ${esc(timePhrase(user.last_active) || "never")} ·
        surfaces ${esc((user.linked_platforms || []).join(", ") || user.platform || "unknown")}</p>

      <h3>Messages</h3>
      <div class="scroll short">${feed(messages, "nothing in the recent window", (x) =>
        `<div class="row">${timeLabel(x.at)}<div class="body">${esc(x.text)}</div></div>`)}</div>

      <h3>Voice</h3>
      <div class="scroll short">${feed(voice, "no recent voice sessions", (v) =>
        `<div class="row">${timeLabel(v.at)}<span class="tag">${esc(v.duration)} · ${esc(String(v.turns))} turns</span>
         <div class="body muted">${esc(v.summary) || "(no summary)"}</div></div>`)}</div>

      <h3>What Buddy sent them</h3>
      <div class="scroll short">${feed(recs, "nothing sent to this person yet", (r) =>
        `<div class="row">${timeLabel(r.at)}<div class="body">${esc(r.title)}</div>
         <div class="body muted">${esc(r.reason)}</div>
         <div class="body ${/opened/i.test(r.outcome || "") ? "good" : "muted"}">${esc(r.outcome)}</div></div>`)}</div>

      <h3>Desktop installs</h3>
      <div class="scroll short">${feed(devices, "no desktop install signed in on this account", (dev) =>
        `<div class="row">${timeLabel(dev.last_seen)}<span class="tag gray">${esc(dev.platform)}</span>
         <div class="body">${esc(dev.device_name || dev.install_id)}</div></div>`)}</div>`;

    drawer.hidden = false;
    document.body.classList.add("drawer-open");
    document.getElementById("drawerClose").onclick = closeUserDrawer;
  }

  function closeUserDrawer() {
    state.openUser = null;
    const drawer = document.getElementById("drawer");
    if (drawer) drawer.hidden = true;
    document.body.classList.remove("drawer-open");
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

  /* ── segmented range control ─────────────────────────────────────── */
  function seg(current, rangeKeys, onclickName) {
    return `<span class="seg">${rangeKeys.map((r) =>
      `<button class="${r === current ? "active" : ""}" onclick="${onclickName}('${r}')">${r}</button>`).join("")}</span>`;
  }

  /* ── MOBILE / DESKTOP tabs ───────────────────────────────────────── */
  function renderPlatformTab(kind, d) {
    destroyChartsUnder(content);
    setStamp(d.generated_at);
    const downloads = d.downloads || {};
    const installs = d.installs || {};
    let downloadsCard;
    if (downloads.available === false && !installs.available) {
      downloadsCard = `<div class="card col-6"><h2>Downloads</h2>
        <div class="note">${esc(downloads.note || "not available")}</div></div>`;
    } else {
      downloadsCard = adoptionCard(downloads, installs, d.web_funnel || {});
    }
    const backendHasData = Object.values(d.backend_latency || {}).some((item) =>
      item && (item.p95 != null || item.p99 != null));
    const posthogSamples = Number(d.chat_latency?.count || 0) +
      Number(d.voice_first_response?.count || 0);
    const crashSource = kind === "mobile"
      ? {
          label: d.crashes?.available ? "Crashlytics connected" : "Crashlytics setup needed",
          tone: d.crashes?.available ? "" : "warn",
          detail: d.crashes?.note || "Firebase Crashlytics BigQuery export",
        }
      : {
          label: "Errors in Logs",
          detail: "Desktop errors are collected through Cloud Logging. Sentry remains intentionally disabled.",
        };
    const heading = pageHeading(
      kind === "mobile" ? "Android + iOS" : "Windows desktop",
      kind === "mobile" ? "Mobile reliability" : "Desktop reliability",
      kind === "mobile"
        ? "Crash health, backend and client-observed latency, voice responsiveness, and store-readiness signals."
        : "Release adoption, API and conversation latency, voice responsiveness, and operational error coverage for Aura Desktop.",
      [
        { label: backendHasData ? "Backend latency live" : "Backend split pending", tone: backendHasData ? "" : "warn", detail: "Cloud Monitoring log-based request latency by platform" },
        { label: posthogSamples ? `PostHog ${posthogSamples} samples` : "PostHog: no 7d samples", tone: posthogSamples ? "" : "warn", detail: "Client-observed chat and voice response events" },
        crashSource,
        ...(kind === "desktop" ? [{
          label: installs.available
            ? `${installs.installs} signed-in install(s)`
            : "install data unavailable",
          tone: installs.available && installs.installs ? "" : "warn",
          detail: "Firestore linked_devices: installations that reached a signed-in state",
        }] : []),
      ],
    );
    content.innerHTML = heading + `<div class="grid">
      ${latencyCard("Latency · backend by platform + client-observed", d.backend_latency, d.chat_latency, d.voice_first_response, d.voice_worker_latency)}
      ${downloadsCard}
      ${crashPanel(kind === "mobile" ? "Crashes · Firebase Crashlytics (7d)" : "Desktop runtime errors", d.crashes)}
      ${kind === "desktop"
        ? installsCard(installs)
        : `<div class="card col-6"><h2>Notes</h2><div class="note">
            Store downloads land here when Play / App Store listings go live (both still in
            review). Crash data requires the Crashlytics BigQuery export toggle in the Firebase
            console.</div></div>`}
    </div>`;
  }


  /* ── desktop adoption ────────────────────────────────────────────────
     Four numbers from three different systems, deliberately shown side by side
     rather than reduced to one headline, because they measure different things
     and their DISAGREEMENT is the signal.

     GitHub's per-asset download_count is a raw HTTP counter: crawlers, security
     scanners, and every Tauri auto-update re-fetch by an existing install all
     increment it, and none of that can be filtered out through GitHub's API.
     "10 downloads, 0 users" was both numbers being correct at once. The install
     count comes from Firestore linked_devices instead: one doc per installation
     that actually reached a signed-in state. */
  function adoptionCard(fetches, installs, web) {
    /* Four numbers, three sources, three different windows. They are stacked to
       be READ TOGETHER, not divided into each other, which is why no conversion
       percentage is shown. */
    const steps = [
      { label: "download clicked", note: "site, 30d", value: web.download_clicked ?? null },
      { label: "installer fetches", note: "GitHub, all time, bot-inflated",
        value: fetches.installer_fetches ?? fetches.total_downloads ?? null },
      { label: "installs signed in", note: "Firestore, all time",
        value: installs.available ? installs.installs : null },
      { label: "active last 7d", note: "Firestore", value: installs.available ? installs.active_7d : null },
    ];
    const releases = fetches.releases || [];
    const byPlatform = Object.entries(installs.by_platform || {});
    return `<div class="card col-6"><h2>Desktop adoption</h2>
      ${funnel(steps, { comparable: false })}
      <div class="note">${esc(fetches.caveat || "")} Installs come from Firestore
        <code>linked_devices</code>, one doc per installation that signed in.</div>
      ${byPlatform.length ? `<div class="strip">${byPlatform.map(([platform, count]) =>
        metric(compact(count), platform + " installs")).join("")}
        ${metric(compact(installs.users_with_desktop), "accounts w/ desktop")}</div>` : ""}
      <div class="scroll" style="max-height:180px"><table>
        <tr><th>Release</th><th>Published</th><th class="num">Fetches</th></tr>
        ${releases.map((r) => `<tr><td>${esc(r.tag)}</td><td class="muted">${esc(timePhrase(r.published_at))}</td>
          <td class="num">${esc(String(r.downloads))}</td></tr>`).join("")
          || '<tr><td colspan="3" class="empty">no releases</td></tr>'}
      </table></div></div>`;
  }

  function installsCard(installs) {
    const rows = installs.devices || [];
    return `<div class="card col-6"><h2>Signed-in desktop installs</h2><div class="scroll">
      ${rows.map((dev) => `<div class="row">${timeLabel(dev.last_seen)}
        <span class="who">${esc(dev.name)}</span><span class="tag gray">${esc(dev.platform)}</span>
        <div class="body">${esc(dev.device_name || dev.install_id)}</div></div>`).join("")
        || `<p class="empty">No desktop install has signed in yet. Installer fetches above are
            crawlers and auto-updates, not people.</p>`}
    </div></div>`;
  }

  /* ── WEB tab ─────────────────────────────────────────────────────── */
  function renderWebTab(d) {
    destroyChartsUnder(content);
    setStamp(d.generated_at);
    const a = d.analytics || {};
    const fetches = d.fetches || {};
    const installs = d.installs || {};
    const pv = a.pageviews_daily || [];
    content.innerHTML = `<div class="grid">
      <div class="card col-8"><h2>auravoiceapp.com · pageviews (30d)</h2>
        <div class="chart-box"><canvas id="pvChart"></canvas></div></div>
      <div class="card col-4"><h2>Top referrers (30d)</h2>
        ${(a.top_referrers || []).map((r) => `<div class="row"><span class="when">${r.views}</span>${esc(r.referrer)}</div>`).join("")
          || '<p class="empty">no referrer data (or PostHog web project not configured)</p>'}</div>
      <div class="card col-6"><h2>Download funnel</h2>
        ${funnel([
          { label: "download page", note: "30d", value: a.download_page_viewed },
          { label: "download clicked", note: "30d", value: a.download_clicked },
          { label: "installer fetches", note: "all time*", value: fetches.installer_fetches ?? null },
          { label: "installs signed in", note: "all time",
            value: installs.available ? installs.installs : null },
        ], { comparable: false })}
        <p class="faint">*Bot- and auto-update-inflated HTTP fetches, all-time across releases,
          not a 30d slice. The final step is the only one that counts people: one Firestore
          <code>linked_devices</code> doc per installation that signed in.</p></div>
      <div class="card col-6"><h2>Site signals (30d)</h2>
        <div class="strip">
          ${metric(num(a.waitlist_submitted), "waitlist submitted")}
          ${metric(num(a.pricing_viewed), "pricing viewed")}
          ${metricText(fetches.latest_version, "live desktop version")}
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

  /* ── COSTS tab ───────────────────────────────────────────────────── */
  function renderCostsTab(d) {
    setStamp(d.generated_at);
    destroyChartsUnder(content);
    const providers = d.providers || [];
    const usage = (d.usage && d.usage.rows) || [];
    const spend = d.llm_spend || {};
    const rangeKeys = d.ranges || ["today", "7d", "30d"];

    const providerName = (row) => row.console_url
      ? `<a href="${esc(row.console_url)}" target="_blank" rel="noopener noreferrer"
           title="${esc(row.console_label || "Open provider console")}">${esc(row.provider)}
           <span class="ext">&#8599;</span></a>`
      : esc(row.provider);

    content.innerHTML = pageHeading(
      "Spend and usage",
      "Costs",
      "Actual, estimated and manually prorated costs, never mixed. Provider names link straight to that provider's own usage console for anything measured outside this dashboard.",
      [
        { label: spend.available ? "LLM ledger live" : "LLM ledger: no rows", tone: spend.available ? "" : "warn", detail: "Firestore users/{uid}/cost written on every backend LLM call" },
        { label: d.actual_total == null ? "GCP billing not connected" : "GCP billing live", tone: d.actual_total == null ? "warn" : "", detail: "BigQuery billing export (OPS_GCP_BILLING_TABLE)" },
      ],
    ) + `<div class="grid">
      <div class="card col-12"><h2>Provider cost
        <span class="controls">${seg(state.providerCostRange, rangeKeys, "opsSetProviderCostRange")}</span></h2>
        <div class="strip">
          ${metric(d.actual_total === null || d.actual_total === undefined ? NA : usd(d.actual_total), "actual total", "accent")}
          ${metric(d.estimated_total === null || d.estimated_total === undefined ? NA : usd(d.estimated_total), "estimated + manual")}
          ${metric(compact(usage.reduce((n, row) => n + (row.billable || 0), 0)), "billable requests")}
          ${metric(compact(usage.reduce((n, row) => n + (row.rate_limited || 0), 0)), "rate limited")}
        </div>
        <div class="note">Actual, estimated, and manually prorated subscription costs are never
          mixed. A provider marked needs setup stays n/a rather than displaying a false zero.</div>
      </div>
      ${llmSpendCard(spend)}
      <div class="card col-4"><h2>Top spenders (${esc(String(spend.days || 7))}d)</h2><div class="scroll">
        <table><tr><th>User</th><th class="num">Est. cost</th><th class="num">Tokens</th><th class="num">Calls</th></tr>
        ${(spend.by_user || []).map((row) => `<tr>
          <td><span class="user-link">${esc(row.name)}</span></td>
          <td class="num">${usd(row.est_usd)}</td>
          <td class="num">${compact(row.tokens)}</td>
          <td class="num">${compact(row.generations)}</td></tr>`).join("")
          || '<tr><td colspan="4" class="empty">no ledger rows in range</td></tr>'}
        </table></div></div>
      <div class="card col-8"><h2>Cost by provider <span class="tag gray">names link to each console</span></h2>
        <div class="scroll"><table>
        <tr><th>Provider</th><th>Source</th><th>Kind</th><th>Status</th><th class="num">Usage</th><th class="num">Cost</th></tr>
        ${providers.map((row) => `<tr>
          <td>${providerName(row)}</td><td>${esc(row.source || "not connected")}</td>
          <td>${esc(row.cost_kind)}</td><td class="muted">${esc(row.status)}</td>
          <td class="num">${row.usage === null || row.usage === undefined ? NA : compact(row.usage)}</td>
          <td class="num">${row.cost === null || row.cost === undefined ? NA : usd(row.cost)}</td>
        </tr>`).join("") || '<tr><td colspan="6" class="empty">no provider data</td></tr>'}
      </table></div></div>
      <div class="card col-4"><h2>Brave queries by feature</h2><table>
        <tr><th>Feature</th><th class="num">Billable</th><th class="num">Cache</th><th class="num">429</th></tr>
        ${usage.filter((row) => row.provider === "brave").map((row) => `<tr>
          <td>${esc(row.feature)}</td><td class="num">${compact(row.billable)}</td>
          <td class="num">${compact(row.cache_hits)}</td><td class="num">${compact(row.rate_limited)}</td>
        </tr>`).join("") || '<tr><td colspan="4" class="empty">no Brave query events yet</td></tr>'}
      </table></div>
    </div>`;

    const daily = spend.daily || [];
    if (daily.length) {
      mountChart("spendChart", {
        type: "bar",
        data: {
          labels: daily.map((x) => x.day.slice(5)),
          datasets: [{ data: daily.map((x) => x.est_usd), backgroundColor: "#2dd4bf", borderRadius: 4 }],
        },
        options: {
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: { y: { beginAtZero: true } },
        },
      });
    }
  }

  /* Real LLM spend, read from the per-user daily ledger the backend writes on
     every model call. It is a measurement, but an ESTIMATE by construction (the
     backend priced it from a token table, not an invoice), and it carries no
     model field, so it cannot be split per vendor. Both limits are stated on the
     card rather than left for the reader to assume. */
  function llmSpendCard(spend) {
    if (!spend || spend.available === false) {
      return `<div class="card col-8"><h2>LLM spend</h2>
        <div class="note">No rows in <code>users/{uid}/cost</code> for this window. That means
          either nothing ran, or the backend stopped writing the ledger
          (<code>llm_cost_ledger.py</code>). It is not the same as zero cost, so nothing is
          shown as $0.</div></div>`;
    }
    const perDay = spend.days ? spend.est_usd / spend.days : null;
    /* input_tokens and cached_input_tokens are DISJOINT in the ledger: the
       provider reports cache reads separately and the backend stores them in
       their own field, so the cache hit rate is cached / (fresh + cached).
       Dividing by input_tokens alone reads over 100% on a cache-heavy day. */
    const promptTokens = (spend.input_tokens || 0) + (spend.cached_input_tokens || 0);
    const cachedShare = promptTokens
      ? Math.round((spend.cached_input_tokens / promptTokens) * 100)
      : null;
    const totalTokens = promptTokens + (spend.output_tokens || 0);
    return `<div class="card col-8"><h2>LLM spend · live ledger
      <span class="tag gray">${esc(String(spend.docs_found || 0))} daily docs</span></h2>
      <div class="strip">
        ${metric(usd(spend.est_usd), "estimated " + esc(String(spend.days)) + "d", "accent")}
        ${metric(perDay === null ? NA : usd(perDay), "per day")}
        ${metric(compact(totalTokens), "tokens")}
        ${metric(compact(spend.generations), "model calls")}
        ${metric(cachedShare === null ? NA : cachedShare + "%", "prompt cache hits")}
      </div>
      <div class="chart-box short"><canvas id="spendChart"></canvas></div>
      <div class="note">Every backend LLM call increments this ledger, so it covers chat, voice,
        fallbacks and background agents. It is an <b>estimate</b>: the backend prices each call
        from a token table rather than an invoice. It stores no model field, so it cannot be split
        into Claude / Gemini / GPT. Use the console links below for the per-model split.</div>
    </div>`;
  }

  async function refetchProviderCosts() {
    try {
      const d = await api("/api/provider-costs?range=" + state.providerCostRange);
      state.tabData.costs = d;
      if (state.tab === "costs") renderCostsTab(d);
    } catch (e) { /* handled by api() */ }
  }

  window.opsSetProviderCostRange = (r) => {
    state.providerCostRange = r;
    refetchProviderCosts();
  };

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
            ${[["1", "1h"], ["6", "6h"], ["24", "24h"], ["72", "3d"], ["168", "7d"], ["720", "30d"]].map(([v, l]) =>
              `<option value="${v}" ${String(state.logs.hours) === v ? "selected" : ""}>${l}</option>`).join("")}
          </select>
          <button class="primary" id="logRun">Search</button>
        </div>
        <div id="logNote"></div>
        <div id="logResults" class="scroll" style="max-height:70vh"><p class="empty">Loading errors…</p></div>
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
    const grouped = new Map();
    for (const entry of d.entries || []) {
      const key = [entry.severity, entry.service, entry.message].join("|");
      const current = grouped.get(key);
      if (current) {
        current.count += 1;
        if ((entry.at || "") < (current.first_seen || "")) current.first_seen = entry.at;
      } else {
        grouped.set(key, { ...entry, count: 1, first_seen: entry.at });
      }
    }
    const entries = [...grouped.values()];
    results.innerHTML = entries.map((e) => `<div class="log-line">
        <span class="ts">${esc((e.at || "").replace("T", " ").slice(0, 19))}</span>
        <span class="sev sev-${esc(e.severity)}">${esc(e.severity)}</span>
        <span class="svc">${esc(e.service)}</span>
        <span class="msg">${e.count > 1 ? `<span class="tag red">${e.count}×</span> ` : ""}${esc(e.message)}</span>
      </div>`).join("") || '<p class="empty">no matching entries</p>';
  }

  /* ── tab activation: render from memory; fetch ONLY when nothing is
        cached yet (first view). Explicit refresh is the only re-fetch. ── */
  async function activateTab(tab) {
    state.tab = tab;
    closeUserDrawer();
    destroyChartsUnder(content);

    if (tab === "overview") {
      overviewShell();
      loadOverviewCore(false);
      loadOverviewAnalytics(false);
      return;
    }
    if (tab === "logs") {
      renderLogsShell(["juno-backend", "juno-ops", "livekit-worker", "mobile-client"]);
      runLogSearch();
      return;
    }
    if (tab === "architecture") {
      setStampLabel("synthetic model");
      window.AuraArchitectureTwin.mount(content);
      return;
    }

    const cached = state.tabData[tab];
    if (cached) {
      if (tab === "web") renderWebTab(cached);
      else if (tab === "costs") renderCostsTab(cached);
      else renderPlatformTab(tab, cached);
      return;
    }
    await fetchTab(tab);
  }

  async function fetchTab(tab) {
    content.innerHTML = skelTab();
    let d;
    const path = tab === "costs"
      ? "/api/provider-costs?range=" + state.providerCostRange
      : "/api/tab/" + tab;
    try { d = await api(path); } catch (e) {
      content.innerHTML = '<p class="bad">Load failed.</p>';
      return;
    }
    if (state.tab !== tab) return;
    state.tabData[tab] = d;
    if (tab === "web") renderWebTab(d);
    else if (tab === "costs") renderCostsTab(d);
    else renderPlatformTab(tab, d);
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
    if (tab === "architecture") {
      window.AuraArchitectureTwin.mount(content);
      return;
    }
    delete state.tabData[tab];
    fetchTab(tab);
  }

  /* ── keyboard ────────────────────────────────────────────────────────
     A console this dense is faster to drive from the keyboard. Shortcuts are
     ignored while typing so they never eat a character in the log search box. */
  const TAB_ORDER = ["overview", "mobile", "desktop", "web", "costs", "architecture", "logs"];

  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (!gate.hidden) return;

    if (e.key === "Escape") {
      if (state.openUser) { closeUserDrawer(); e.preventDefault(); }
      return;
    }

    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
    if (typing) return;

    if (e.key >= "1" && e.key <= "7") {
      const tab = TAB_ORDER[Number(e.key) - 1];
      const btn = document.querySelector(`#tabs .tab[data-tab="${tab}"]`);
      if (btn) { btn.click(); e.preventDefault(); }
      return;
    }
    if (e.key === "r") {
      if (!refreshBtn.disabled) refreshBtn.click();
      e.preventDefault();
      return;
    }
    if (e.key === "/") {
      const search = document.querySelector("#logQ, #content input[type=search]");
      if (search) { search.focus(); e.preventDefault(); }
    }
  });

  /* ── boot ────────────────────────────────────────────────────────── */
  function boot() {
    gate.hidden = true;
    appEl.hidden = false;
    updateRefreshButton();
    activateTab(state.tab);
  }

  if (passcode()) boot(); else showGate("");
})();
