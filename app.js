/* AI Investment Monitor — SPA. All company metrics from web/data/*.json */
(function () {
  "use strict";

  const DATA_BASE = "./data";
  const NA_TITLE = "Data unavailable from source";

  const state = {
    watchlist: [],
    meta: null,
    companies: {},
    valuation: [],
    revisions: [],
    epsHistory: {},
    earnings: {},
    alerts: [],
    alertEngineStatus: null,
    alertEngineError: null,
    ready: false,
    chart: null,
    sort: { key: "ticker", dir: "asc" },
    revFilters: { ticker: "", year: "", date: "" },
    chartTicker: null,
    chartYear: null, /* set from meta.displayMappedYears on load */
    chartMode: "absolute", /* absolute | index */
  };

  /* ---------- utils ---------- */
  function $(sel, root) {
    return (root || document).querySelector(sel);
  }
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      Object.entries(attrs).forEach(([k, v]) => {
        if (v == null || v === false) return;
        if (k === "className") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k === "html") node.innerHTML = v;
        else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2).toLowerCase(), v);
        else node.setAttribute(k, v === true ? "" : String(v));
      });
    }
    (children || []).forEach((c) => {
      if (c == null) return;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }

  function isMissing(v) {
    return v === null || v === undefined || v === "" || v === "Data unavailable";
  }

  function fmtNum(v, digits) {
    if (isMissing(v) || Number.isNaN(Number(v))) return null;
    const n = Number(v);
    const d = digits == null ? 2 : digits;
    return n.toLocaleString("en-US", {
      minimumFractionDigits: d,
      maximumFractionDigits: d,
    });
  }

  function fmtPct(v, digits, alreadyPercent) {
    if (isMissing(v) || Number.isNaN(Number(v))) return null;
    const n = Number(v);
    const pct = alreadyPercent ? n : n * 100;
    const d = digits == null ? 2 : digits;
    const sign = pct > 0 ? "+" : "";
    return sign + pct.toFixed(d) + "%";
  }

  function fmtRevPct(v) {
    /* rev1M stored as percent points (1.37 = 1.37%) */
    if (isMissing(v) || Number.isNaN(Number(v))) return null;
    const n = Number(v);
    const sign = n > 0 ? "+" : "";
    return sign + n.toFixed(2) + "%";
  }


  function fmtDispersion(v) {
    /* dispersion stored as fraction (H-L)/Cons — display as percent e.g. 57.3% */
    if (isMissing(v) || Number.isNaN(Number(v))) return null;
    const n = Number(v);
    const pct = Math.abs(n) <= 2 ? n * 100 : n; /* tolerate already-percent legacy */
    return pct.toFixed(1) + "%";
  }

  function displayYearKeys() {
    const m = state.meta || {};
    if (Array.isArray(m.displayMappedYears) && m.displayMappedYears.length) {
      return m.displayMappedYears;
    }
    /* Fallback: Taipei calendar year derived from consensus timestamp or local */
    let y = null;
    const iso = m.consensusDataAsOf || m.lastUpdated;
    if (iso) {
      const d = new Date(iso);
      if (!Number.isNaN(d.getTime())) {
        /* approximate Taipei = UTC+8 */
        const taipei = new Date(d.getTime() + 8 * 3600 * 1000);
        y = taipei.getUTCFullYear();
      }
    }
    if (y == null) y = new Date().getFullYear();
    return [y + "E", (y + 1) + "E", (y + 2) + "E", (y + 3) + "E"];
  }

  /* Schedule-aware freshness — mirrors export_web_data.compute_freshness
     Weekday 08:00 Taipei + grace; Friday success → Sat/Sun NOT stale. */
  const COLLECTION_HOUR_TAIPEI = 8;
  const DEFAULT_GRACE_HOURS = 6;

  function taipeiParts(ms) {
    /* Approximate Taipei = UTC+8 for schedule math */
    const d = new Date(ms + 8 * 3600 * 1000);
    return {
      y: d.getUTCFullYear(),
      m: d.getUTCMonth(),
      day: d.getUTCDate(),
      h: d.getUTCHours(),
      weekday: d.getUTCDay(), /* 0=Sun … 6=Sat */
      ms: Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), d.getUTCHours(), d.getUTCMinutes(), d.getUTCSeconds()) - 8 * 3600 * 1000,
    };
  }

  function mostRecentDueCollectionStart(nowMs, graceHours) {
    const graceMs = (graceHours == null ? DEFAULT_GRACE_HOURS : graceHours) * 3600 * 1000;
    for (let i = 0; i < 12; i++) {
      const probe = nowMs - i * 86400000;
      const p = taipeiParts(probe);
      if (p.weekday === 0 || p.weekday === 6) continue;
      /* weekday 08:00 Taipei as UTC ms */
      const startUtc = Date.UTC(p.y, p.m, p.day, COLLECTION_HOUR_TAIPEI, 0, 0) - 8 * 3600 * 1000;
      /* Recompute using the calendar day in Taipei of (now - i days) at 08:00 */
      const dayProbe = new Date(nowMs + 8 * 3600 * 1000 - i * 86400000);
      const y = dayProbe.getUTCFullYear();
      const m = dayProbe.getUTCMonth();
      const day = dayProbe.getUTCDate();
      const wd = dayProbe.getUTCDay();
      if (wd === 0 || wd === 6) continue;
      const start = Date.UTC(y, m, day, COLLECTION_HOUR_TAIPEI, 0, 0) - 8 * 3600 * 1000;
      if (nowMs >= start + graceMs) return start;
    }
    return null;
  }

  function isScheduleStale(iso, nowMs, graceHours) {
    if (!iso) return true;
    const last = Date.parse(iso);
    if (Number.isNaN(last)) return true;
    const now = nowMs == null ? Date.now() : nowMs;
    const grace = graceHours != null ? graceHours : ((state.meta && state.meta.graceHours) != null ? Number(state.meta.graceHours) : DEFAULT_GRACE_HOURS);
    /* Prefer server-exported staleAfter / nextExpected when present */
    const m = state.meta || {};
    if (m.staleAfter && m.nextExpected) {
      /* If server already computed dataStale with same lastSuccessfulCollection, trust combined OR */
    }
    const due = mostRecentDueCollectionStart(now, grace);
    if (due == null) return false;
    return last < due;
  }

  function isClientStale(iso) {
    return isScheduleStale(iso);
  }

  function companyIsStale(c) {
    if (!c) return true;
    if (c.collectionFailed === true) return true;
    const asOf = c.lastSuccessfulCollection || c.collectionAsOf || c.dataAsOf || (state.meta || {}).consensusDataAsOf;
    return isClientStale(asOf);
  }

  function rangeBar(low, cons, high) {
    /* Low — Consensus marker — High (not a progress bar) */
    if (isMissing(low) || isMissing(high) || isMissing(cons)) return null;
    const l = Number(low), h = Number(high), c = Number(cons);
    if (!(h > l)) return null;
    const pct = Math.max(0, Math.min(100, ((c - l) / (h - l)) * 100));
    const wrap = el("div", {
      className: "consensus-range",
      title: "Low " + fmtNum(l, 2) + " · Cons " + fmtNum(c, 2) + " · High " + fmtNum(h, 2),
    });
    wrap.appendChild(el("span", { className: "cr-low", text: fmtNum(l, 2) }));
    wrap.appendChild(el("span", { className: "cr-sep", text: "—" }));
    const mid = el("span", { className: "cr-mid" });
    const track = el("span", { className: "cr-track" });
    const marker = el("span", { className: "cr-marker", title: "Consensus " + fmtNum(c, 2) });
    marker.style.left = pct.toFixed(1) + "%";
    track.appendChild(marker);
    mid.appendChild(track);
    mid.appendChild(el("span", { className: "cr-cons-label", text: fmtNum(c, 2) }));
    wrap.appendChild(mid);
    wrap.appendChild(el("span", { className: "cr-sep", text: "—" }));
    wrap.appendChild(el("span", { className: "cr-high", text: fmtNum(h, 2) }));
    return wrap;
  }

  function nextEarningsLabel(c, e) {
    const src = e || c || {};
    const status = src.nextEarningsStatus || (c && c.nextEarningsStatus) || null;
    const display = src.nextEarnings || (c && c.nextEarnings) || null;
    if (isMissing(display)) return null;
    // Already formatted by exporter as "Mon D, YYYY · Post-Market · Estimated"
    if (String(display).indexOf(" · ") >= 0) return String(display);
    const session = src.nextEarningsSession || (c && c.nextEarningsSession) || null;
    const label = status === "confirmed" ? "Confirmed" : "Estimated";
    const bits = [String(display)];
    if (session) bits.push(session);
    bits.push(label);
    return bits.join(" · ");
  }

  function naCell(display) {
    const span = el("span", { className: "na", title: NA_TITLE, text: display == null ? "—" : display });
    return span;
  }

  function numCell(v, digits) {
    const s = fmtNum(v, digits);
    if (s == null) return naCell();
    return document.createTextNode(s);
  }

  function revCell(v) {
    const s = fmtRevPct(v);
    if (s == null) return naCell();
    const n = Number(v);
    const cls = n > 0 ? "pos" : n < 0 ? "neg" : "";
    return el("span", { className: cls, text: s });
  }

  function growthCell(v) {
    /* ratio 0.67 → +67.67%; string sentinels for zero-crossing / N/M */
    if (typeof v === "string") {
      const cls = /profit/i.test(v) ? "pos" : /loss/i.test(v) ? "neg" : "";
      return el("span", { className: cls || "na", text: v, title: v });
    }
    if (v === null || v === undefined) return naCell("N/M");
    const s = fmtPct(v, 2, false);
    if (s == null) return naCell("N/M");
    const n = Number(v);
    const cls = n > 0 ? "pos" : n < 0 ? "neg" : "";
    return el("span", { className: cls, text: s });
  }

  function cagrCell(v) {
    return growthCell(v);
  }

  function momentumClass(m) {
    if (!m) return "mom-neutral";
    const s = String(m).toLowerCase();
    if (s === "strong positive") return "mom-strong-positive";
    if (s === "positive") return "mom-positive";
    if (s === "strong negative") return "mom-strong-negative";
    if (s === "negative") return "mom-negative";
    return "mom-neutral";
  }

  function momentumCell(m, regime) {
    if (isMissing(m) && isMissing(regime)) return naCell();
    const wrap = el("span", { className: "mom-wrap" });
    if (!isMissing(m)) {
      wrap.appendChild(el("span", { className: momentumClass(m), text: String(m) }));
    }
    if (!isMissing(regime) && String(regime) !== String(m)) {
      wrap.appendChild(el("span", {
        className: "revision-regime",
        text: String(regime),
        title: "Revision regime (near-term Y+1 vs long-term Y+2 SA 1M)",
      }));
    }
    return wrap;
  }

  function companyPrice(c) {
    if (!c) return null;
    if (!isMissing(c.lastClose)) return c.lastClose;
    if (!isMissing(c.price)) return c.price;
    return null;
  }

  function pe(price, eps) {
    if (isMissing(price) || isMissing(eps) || Number(eps) <= 0) return null;
    return Number(price) / Number(eps);
  }

  function toIndexSeries(values) {
    /* first non-null point = 100 */
    let base = null;
    return values.map((v) => {
      if (v == null || Number.isNaN(Number(v))) return null;
      if (base == null) {
        if (Number(v) === 0) return null;
        base = Number(v);
        return 100;
      }
      return (Number(v) / base) * 100;
    });
  }

  function valueWithFy(valueNode, fyLabel) {
    const wrap = el("div");
    wrap.appendChild(valueNode);
    if (fyLabel && !isMissing(fyLabel)) {
      wrap.appendChild(el("span", { className: "fy-sub", text: fyLabel }));
    }
    return wrap;
  }

  function driverLabel(status) {
    const s = String(status || "unchanged").toLowerCase();
    if (s === "improving") return { text: "Improving ↑", cls: "driver-improving" };
    if (s === "deteriorating") return { text: "Deteriorating ↓", cls: "driver-deteriorating" };
    return { text: "Unchanged →", cls: "driver-unchanged" };
  }

  function yearFromAlignment(align) {
    if (!align) return "";
    const m = String(align).match(/CY(\d{4})/i);
    return m ? m[1] + "E" : "";
  }

  /* ---------- data load ---------- */
  async function loadJSON(name) {
    const res = await fetch(DATA_BASE + "/" + name + "?t=" + Date.now());
    if (!res.ok) throw new Error("Failed to load " + name + " (" + res.status + ")");
    return res.json();
  }

  function extractBuildId(obj) {
    if (!obj || typeof obj !== "object") return null;
    return obj.buildId || obj._buildId || null;
  }

  async function loadAll(retryCount) {
    retryCount = retryCount || 0;
    /* Prefer single atomic dashboard.json; fall back to multi-file with buildId consistency */
    let dash = null;
    try {
      dash = await loadJSON("dashboard.json");
    } catch (_) {
      dash = null;
    }

    let watchlist, meta, companies, valuation, revisions, epsHistory, earnings, alerts;
    if (dash && dash.meta && dash.companies) {
      meta = dash.meta || {};
      companies = dash.companies || {};
      valuation = dash.valuation || {};
      revisions = dash.revisions || {};
      epsHistory = dash.epsHistory || {};
      earnings = dash.earnings || {};
      alerts = dash.alerts || {};
      watchlist = dash.watchlist || { tickers: Object.keys(companies) };
      const bid = dash.buildId || (meta && meta.buildId);
      if (bid && meta && !meta.buildId) meta.buildId = bid;
    } else {
      [
        watchlist,
        meta,
        companies,
        valuation,
        revisions,
        epsHistory,
        earnings,
        alerts,
      ] = await Promise.all([
        loadJSON("watchlist.json"),
        loadJSON("meta.json"),
        loadJSON("companies.json"),
        loadJSON("valuation.json"),
        loadJSON("revisions.json"),
        loadJSON("eps_history.json"),
        loadJSON("earnings.json"),
        loadJSON("alerts.json"),
      ]);
      /* Reject silent mixed-generation display */
      const ids = [
        extractBuildId(meta),
        extractBuildId(watchlist),
        extractBuildId(valuation),
        extractBuildId(revisions),
        extractBuildId(alerts),
        extractBuildId(companies),
        extractBuildId(earnings),
        extractBuildId(epsHistory),
      ].filter(Boolean);
      const uniq = Array.from(new Set(ids));
      if (uniq.length > 1) {
        if (retryCount < 3) {
          await new Promise((r) => setTimeout(r, 250 + retryCount * 200));
          return loadAll(retryCount + 1);
        }
        throw new Error("mixed build generation rejected: " + uniq.join(" vs "));
      }
    }

    /* Strip _buildId from ticker maps */
    if (companies && companies._buildId) {
      companies = Object.assign({}, companies);
      delete companies._buildId;
    }
    if (earnings && earnings._buildId) {
      earnings = Object.assign({}, earnings);
      delete earnings._buildId;
    }
    if (epsHistory && epsHistory._buildId) {
      epsHistory = Object.assign({}, epsHistory);
      delete epsHistory._buildId;
    }

    state.watchlist = (watchlist && watchlist.tickers) || Object.keys(companies || {});
    state.meta = meta || {};
    state.companies = companies || {};
    state.valuation = (valuation && valuation.rows) || (Array.isArray(valuation) ? valuation : []);
    state.revisions = (revisions && revisions.revisions) || (Array.isArray(revisions) ? revisions : []);
    state.epsHistory = epsHistory || {};
    state.earnings = earnings || {};
    state.alerts = (alerts && (alerts.activeAlerts || alerts.alerts)) || [];
    state.alertHistory = (alerts && alerts.alertHistory) || state.alerts;
    state.homepageAttentionQueue = (alerts && alerts.homepageAttentionQueue) || [];
    state.changedSinceLastCollection = (alerts && alerts.changedSinceLastCollection) || [];
    state.alertEngineStatus = (alerts && alerts.alertEngineStatus) || (meta && meta.alertEngineStatus) || null;
    state.alertEngineError = (alerts && alerts.alertEngineError) || (meta && meta.alertEngineError) || null;
    state.ready = true;
    if (!state.chartTicker && state.watchlist.length) {
      state.chartTicker = state.watchlist[0];
    }
    const dy = displayYearKeys();
    if (!state.chartYear || dy.indexOf(state.chartYear) < 0) {
      state.chartYear = dy[1] || dy[0] || null;
    }
  }

  /* ---------- theme ---------- */
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem("ai-eps-theme", theme);
    } catch (_) {}
  }

  function initTheme() {
    let theme = "dark";
    try {
      theme = localStorage.getItem("ai-eps-theme") || "dark";
    } catch (_) {}
    applyTheme(theme);
    const btn = $("#theme-toggle");
    if (btn) {
      btn.addEventListener("click", () => {
        const cur = document.documentElement.getAttribute("data-theme") || "dark";
        applyTheme(cur === "dark" ? "light" : "dark");
        // re-render chart if visible for color update
        if (state.chart && getRoute().name === "revisions") {
          renderChart();
        }
        if (state.chart && getRoute().name === "company") {
          renderCompanyChart(getRoute().ticker);
        }
      });
    }
  }

  /* ---------- routing ---------- */
  function getRoute() {
    const hash = (location.hash || "#/").replace(/^#/, "");
    const parts = hash.split("/").filter(Boolean);
    if (!parts.length) return { name: "overview" };
    if (parts[0] === "valuation") return { name: "valuation" };
    if (parts[0] === "revisions") return { name: "revisions" };
    if (parts[0] === "earnings" && parts[1]) return { name: "earningsDetail", ticker: parts[1].toUpperCase() };
    if (parts[0] === "earnings") return { name: "earnings" };
    if (parts[0] === "companies") return { name: "companies" };
    if (parts[0] === "company" && parts[1]) return { name: "company", ticker: parts[1].toUpperCase() };
    return { name: "overview" };
  }

  function setActiveTab(route) {
    document.querySelectorAll(".tab").forEach((a) => {
      const r = a.getAttribute("data-route");
      let active = false;
      if (route.name === "overview" && r === "overview") active = true;
      if (route.name === "valuation" && r === "valuation") active = true;
      if (route.name === "revisions" && r === "revisions") active = true;
      if ((route.name === "earnings" || route.name === "earningsDetail") && r === "earnings") active = true;
      if ((route.name === "companies" || route.name === "company") && r === "companies") active = true;
      a.classList.toggle("active", active);
    });
  }

  function updateHeaderMeta() {
    const m = state.meta || {};
    const set = (id, val) => {
      const n = $(id);
      if (n) n.textContent = val || "—";
    };
    set(
      "#meta-consensus",
      m.consensusDataAsOfDisplay || m.lastUpdatedDisplay || m.consensusDataAsOf || m.lastUpdated
    );
    set(
      "#meta-collection",
      m.lastSuccessfulCollectionDisplay ||
        m.lastSuccessfulCollection ||
        m.lastUpdatedDisplay
    );
    set("#meta-published", m.sitePublishedDisplay || m.sitePublished);
    set("#meta-source", m.primarySource);
    const collEl = $("#meta-collection-status");
    if (collEl) {
      const label = m.collectionStatusLabel || m.collectionStatus;
      if (label && String(m.collectionStatus || "").toLowerCase() === "partial") {
        collEl.textContent = String(label);
        collEl.removeAttribute("hidden");
        collEl.style.display = "";
      } else if (label && String(m.collectionStatus || "").toLowerCase() === "failed") {
        collEl.textContent = String(label);
        collEl.removeAttribute("hidden");
        collEl.style.display = "";
      } else {
        collEl.textContent = "";
        collEl.setAttribute("hidden", "");
        collEl.style.display = "none";
      }
    }
    const staleRow = $("#meta-stale-row");
    if (staleRow) {
      const serverStale = m.dataStale === true || m.dataStale === "true";
      const clientStale = isClientStale(m.consensusDataAsOf || m.lastUpdated);
      const stale = serverStale || clientStale;
      if (stale) {
        staleRow.removeAttribute("hidden");
        staleRow.style.display = "";
        staleRow.classList.add("is-stale-visible");
      } else {
        staleRow.setAttribute("hidden", "");
        staleRow.classList.remove("is-stale-visible");
        staleRow.style.display = "none";
      }
    }
    const fw = $("#footer-watchlist");
    if (fw) {
      const dv = m.dataVersion || m.buildId;
      const short = dv ? String(dv).slice(0, 12) : null;
      fw.textContent =
        "Watchlist: " +
        state.watchlist.join(", ") +
        (short ? " · dataVersion " + short : "");
    }
  }

  /* ---------- summary cards ---------- */
  function periodOf(row, yearKey) {
    if (!row) return {};
    if (row.periods && row.periods[yearKey]) return row.periods[yearKey];
    return {};
  }

  function computeSummaries() {
    const rows = state.valuation || [];
    const years = displayYearKeys();
    const y0 = years[0];
    const y1 = years[1];
    const y2 = years[2];
    let largestUp = null;
    let largestDown = null;
    let largestUpY2 = null;
    let lowestPe = null;
    let fastestCagr = null;

    rows.forEach((r) => {
      const p1 = periodOf(r, y1);
      const p2 = periodOf(r, y2);
      const rev1 = p1.rev1M != null ? p1.rev1M : r.rev1M;
      const rev2 = p2.rev1M != null ? p2.rev1M : r.rev1M28;
      const pe1 = p1.pe != null ? p1.pe : r.pe27;
      const cagr = r.cagrY0Y2 != null ? r.cagrY0Y2 : r.cagr2628;
      const eps0 = (periodOf(r, y0).eps != null ? periodOf(r, y0).eps : r.eps26);

      if (rev1 != null && !Number.isNaN(Number(rev1))) {
        if (Number(rev1) > 0 && (!largestUp || Number(rev1) > Number(largestUp._rev))) {
          largestUp = Object.assign({}, r, { _rev: rev1 });
        }
        if (Number(rev1) < 0 && (!largestDown || Number(rev1) < Number(largestDown._rev))) {
          largestDown = Object.assign({}, r, { _rev: rev1 });
        }
      }
      if (rev2 != null && !Number.isNaN(Number(rev2))) {
        if (Number(rev2) > 0 && (!largestUpY2 || Number(rev2) > Number(largestUpY2._rev))) {
          largestUpY2 = Object.assign({}, r, { _rev: rev2 });
        }
      }
      if (pe1 != null && !Number.isNaN(Number(pe1))) {
        if (!lowestPe || Number(pe1) < Number(lowestPe._pe)) {
          lowestPe = Object.assign({}, r, { _pe: pe1 });
        }
      }
      if (cagr != null && eps0 != null && !Number.isNaN(Number(cagr))) {
        if (!fastestCagr || Number(cagr) > Number(fastestCagr._cagr)) {
          fastestCagr = Object.assign({}, r, { _cagr: cagr });
        }
      }
    });

    return { largestUp, largestDown, largestUpY2, lowestPe, fastestCagr, y0, y1, y2 };
  }

  function renderSummaryCards(parent) {
    const s = computeSummaries();
    const grid = el("div", { className: "summary-grid" });

    function card(label, ticker, detailHtml) {
      const c = el("div", { className: "summary-card" });
      c.appendChild(el("div", { className: "label", text: label }));
      if (ticker) {
        const v = el("div", { className: "value" });
        v.appendChild(
          el("a", {
            className: "ticker-link",
            href: "#/company/" + ticker,
            text: ticker,
          })
        );
        c.appendChild(v);
        const d = el("div", { className: "detail" });
        if (typeof detailHtml === "string") d.textContent = detailHtml;
        else if (detailHtml) d.appendChild(detailHtml);
        c.appendChild(d);
      } else {
        c.appendChild(el("div", { className: "value", text: "—" }));
        c.appendChild(el("div", { className: "detail", text: "No qualifying names" }));
      }
      return c;
    }

    const y0 = s.y0 || "Y0";
    const y1 = s.y1 || "Y1";
    const y2 = s.y2 || "Y2";
    grid.appendChild(
      card(
        "Largest " + y1 + " EPS Upgrade (1M)",
        s.largestUp && s.largestUp.ticker,
        s.largestUp ? revCell(s.largestUp._rev) : null
      )
    );
    grid.appendChild(
      card(
        "Largest " + y1 + " EPS Downgrade (1M)",
        s.largestDown && s.largestDown.ticker,
        s.largestDown ? revCell(s.largestDown._rev) : null
      )
    );
    grid.appendChild(
      card(
        "Largest " + y2 + " EPS Upgrade (1M)",
        s.largestUpY2 && s.largestUpY2.ticker,
        s.largestUpY2 ? revCell(s.largestUpY2._rev) : null
      )
    );
    grid.appendChild(
      card(
        "Lowest Mapped " + y1 + " P/E",
        s.lowestPe && s.lowestPe.ticker,
        s.lowestPe ? fmtNum(s.lowestPe._pe, 2) + "x" : null
      )
    );
    grid.appendChild(
      card(
        "Fastest Mapped " + y0.replace("E","") + "–" + y2 + " EPS CAGR",
        s.fastestCagr && s.fastestCagr.ticker,
        s.fastestCagr ? growthCell(s.fastestCagr._cagr) : null
      )
    );

    parent.appendChild(grid);
  }

  /* ---------- alerts ---------- */
  function alertAgeLabel(a) {
    let days = a && a.ageDays;
    if (days == null && a) {
      const src = a.lastMaterialChangeAt || a.openedAt || a.eventAt || a.eventDate || a.createdAt;
      if (src) {
        const t = Date.parse(src);
        if (!Number.isNaN(t)) days = Math.max(0, Math.floor((Date.now() - t) / 86400000));
      }
    }
    if (days == null || Number.isNaN(Number(days))) return "";
    const n = Number(days);
    if (n <= 0) return "today";
    return n + "d ago";
  }

  function renderAlerts(parent) {
    const box = el("div", { className: "alerts-box section" });
    const titleRow = el("div", { className: "alerts-title-row" });
    titleRow.appendChild(el("h2", { className: "section-title", text: "Important Alerts", style: "margin:0" }));
    box.appendChild(titleRow);
    const status = state.alertEngineStatus || (state.meta && state.meta.alertEngineStatus);
    const attention = state.homepageAttentionQueue || [];
    const allActive = state.alerts || [];
    if (status !== "ok") {
      box.appendChild(
        el("div", {
          className: "alerts-empty alerts-engine-error",
          text: "ALERT ENGINE NOT UPDATED",
        })
      );
      if (state.alertEngineError || (state.meta && state.meta.alertEngineError)) {
        box.appendChild(
          el("div", {
            className: "section-note",
            text: String(state.alertEngineError || state.meta.alertEngineError),
          })
        );
      }
    } else if (!allActive.length && !attention.length) {
      box.appendChild(el("div", { className: "alerts-empty", text: "No material alerts" }));
    } else {
      /* Attention Queue: ranked Important (max 5, max 2/ticker) — NOT first-N of active */
      const top = attention.length ? attention.slice(0, 5) : allActive.slice(0, 5);
      const topIds = {};
      top.forEach((a) => { if (a && a.id) topIds[a.id] = true; });
      const rest = allActive.filter((a) => !(a && a.id && topIds[a.id]));
      top.forEach((a) => {
        const msg = typeof a === "string" ? a : a.message || a.title || JSON.stringify(a);
        const item = el("div", { className: "alert-item" });
        const age = typeof a === "object" ? alertAgeLabel(a) : "";
        item.appendChild(el("span", { className: "alert-msg", text: msg }));
        if (typeof a === "object" && a.confidence) {
          item.appendChild(el("span", {
            className: "alert-confidence",
            text: "Confidence: " + a.confidence + (a.analystCount != null ? " (" + a.analystCount + " analysts)" : ""),
            title: "Analyst coverage confidence",
          }));
        }
        if (age) item.appendChild(el("span", { className: "alert-age", text: age }));
        box.appendChild(item);
      });
      if (rest.length) {
        const details = el("details", { className: "alerts-view-all" });
        details.appendChild(
          el("summary", { text: "View All (" + allActive.length + " active)" })
        );
        rest.forEach((a) => {
          const msg = typeof a === "string" ? a : a.message || a.title || JSON.stringify(a);
          const item = el("div", { className: "alert-item" });
          item.appendChild(el("span", { className: "alert-msg", text: msg }));
          const age = typeof a === "object" ? alertAgeLabel(a) : "";
          if (age) item.appendChild(el("span", { className: "alert-age", text: age }));
          details.appendChild(item);
        });
        box.appendChild(details);
      }
    }
    /* WHAT CHANGED SINCE LAST COLLECTION */
    const changed = state.changedSinceLastCollection || [];
    if (changed.length) {
      const ch = el("div", { className: "changed-since section-note" });
      ch.appendChild(el("div", { className: "section-title", text: "What Changed Since Last Collection", style: "font-size:14px;margin:12px 0 6px" }));
      changed.slice(0, 8).forEach((a) => {
        const msg = typeof a === "string" ? a : a.message || a.title || JSON.stringify(a);
        ch.appendChild(el("div", { className: "alert-item", text: msg }));
      });
      box.appendChild(ch);
    }
    parent.appendChild(box);
  }

  /* ---------- overview table ---------- */
  function renderOverviewTable(parent) {
    const wrap = el("div", { className: "section" });
    wrap.appendChild(el("h2", { className: "section-title", text: "Portfolio Overview" }));
    wrap.appendChild(
      el("p", {
        className: "section-note",
        text: "FY-mapped calendar slots (not true CY EPS); Reported Fiscal Period Ending shown under consensus. 1M rev = Seeking Alpha short-window proxy on the primary forward mapped year (meta.displayMappedYears[1]).",
      })
    );

    const years = displayYearKeys();
    const y0 = years[0] || "";
    const y1 = years[1] || "";
    const y2 = years[2] || "";

    const tableWrap = el("div", { className: "table-wrap" });
    const table = el("table", { className: "data" });
    const thead = el("thead");
    const hr = el("tr");
    [
      ["Ticker", "left"],
      ["Last Close", ""],
      ["Mapped " + y0, ""],
      ["Mapped " + y1, ""],
      ["Mapped " + y2, ""],
      [y1 + " PE", ""],
      ["1M Rev", ""],
      ["Momentum", ""],
      ["Last Earnings", "left"],
      ["Next Earnings", "left"],
    ].forEach(([label, align]) => {
      hr.appendChild(el("th", { className: align === "left" ? "left" : "", text: label }));
    });
    thead.appendChild(hr);
    table.appendChild(thead);

    const tbody = el("tbody");
    state.watchlist.forEach((t) => {
      const c = state.companies[t];
      if (!c) return;
      const e26 = (c.eps && c.eps[y0]) || {};
      const e27 = (c.eps && c.eps[y1]) || {};
      const e28 = (c.eps && c.eps[y2]) || {};
      const px = companyPrice(c);
      const peY1 = pe(px, e27.consensus);
      const stale = companyIsStale(c);
      const tr = el("tr", { className: stale ? "ticker-stale" : "" });

      const tdT = el("td", { className: "ticker left" });
      tdT.appendChild(el("a", { href: "#/company/" + t, text: t }));
      if (c.collectionFailed === true || c.dataFreshnessBadge === "FAILED") {
        tdT.appendChild(el("span", { className: "stale-ticker-badge failed-ticker-badge", text: "FAILED", title: "Collection failed or missing from snapshot — last-known-good shown when available" }));
      } else if (stale || c.dataFreshnessBadge === "STALE") {
        tdT.appendChild(el("span", { className: "stale-ticker-badge", text: "STALE", title: "Per-ticker past weekday 08:00 Taipei collection+grace without success, or using last-known-good" }));
      }
      tr.appendChild(tdT);

      const tdP = el("td");
      tdP.appendChild(numCell(px, 2));
      if (!isMissing(c.afterHours)) {
        tdP.appendChild(el("span", { className: "price-sub", text: "AH " + fmtNum(c.afterHours, 2) }));
      }
      tr.appendChild(tdP);

      function epsTd(e) {
        const td = el("td");
        td.appendChild(numCell(e.consensus, 2));
        if (!isMissing(e.consensus) && e.reportedFiscalLabel) {
          td.appendChild(el("span", { className: "fy-sub", text: e.reportedFiscalLabel }));
        }
        return td;
      }

      tr.appendChild(epsTd(e26));
      tr.appendChild(epsTd(e27));
      tr.appendChild(epsTd(e28));

      const tdPe = el("td");
      tdPe.appendChild(numCell(peY1, 2));
      tr.appendChild(tdPe);

      const tdRev = el("td");
      tdRev.appendChild(revCell(e27.rev1M));
      tr.appendChild(tdRev);

      const tdMom = el("td");
      tdMom.appendChild(momentumCell(c.momentum, c.revisionRegime));
      tr.appendChild(tdMom);

      const tdLast = el("td", { className: "left" });
      if (isMissing(c.lastEarnings)) tdLast.appendChild(naCell());
      else tdLast.textContent = c.lastEarnings;
      tr.appendChild(tdLast);

      const tdNext = el("td", { className: "left" });
      {
        const nxt = nextEarningsLabel(c, state.earnings[t]);
        if (isMissing(nxt)) tdNext.appendChild(naCell());
        else tdNext.textContent = nxt;
      }
      tr.appendChild(tdNext);

      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    tableWrap.appendChild(table);
    wrap.appendChild(tableWrap);
    parent.appendChild(wrap);
  }

  function renderOverview() {
    const root = el("div");
    renderSummaryCards(root);
    renderAlerts(root);
    renderOverviewTable(root);
    return root;
  }

  /* ---------- valuation ---------- */
  function sortValuation(rows) {
    const { key, dir } = state.sort;
    const mul = dir === "asc" ? 1 : -1;
    return rows.slice().sort((a, b) => {
      let va = a[key];
      let vb = b[key];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === "string") return mul * va.localeCompare(vb);
      return mul * (Number(va) - Number(vb));
    });
  }

  function renderValuation() {
    const root = el("div", { className: "section" });
    root.appendChild(el("h2", { className: "section-title", text: "Valuation" }));
    const years = displayYearKeys().slice(0, 3);
    const y0 = years[0] || "";
    const y1 = years[1] || "";
    const y2 = years[2] || "";
    root.appendChild(
      el("p", {
        className: "section-note",
        text:
          "Forward PE = Last Close / Mapped Consensus EPS. Mapped CAGR " +
          y0 +
          "–" +
          y2 +
          " = (EPS" +
          y2 +
          "/EPS" +
          y0 +
          ")^(1/2)−1. FY labels under EPS/PE. Click headers to sort. Years from meta.displayMappedYears.",
      })
    );

    const cols = [
      { key: "ticker", label: "Ticker", align: "left" },
      { key: "lastClose", label: "Last Close" },
      { key: "eps:" + y0, label: "Mapped " + y0, year: y0, field: "eps" },
      { key: "eps:" + y1, label: "Mapped " + y1, year: y1, field: "eps" },
      { key: "eps:" + y2, label: "Mapped " + y2, year: y2, field: "eps" },
      { key: "pe:" + y0, label: y0 + " PE", year: y0, field: "pe" },
      { key: "pe:" + y1, label: y1 + " PE", year: y1, field: "pe" },
      { key: "pe:" + y2, label: y2 + " PE", year: y2, field: "pe" },
      { key: "growth:" + y1, label: y1 + " Growth", year: y1, field: "growthFromPrior" },
      { key: "growth:" + y2, label: y2 + " Growth", year: y2, field: "growthFromPrior" },
      { key: "cagrY0Y2", label: "Mapped CAGR " + y0.replace("E", "") + "–" + y2.replace("E", "") },
      { key: "rev1M:" + y1, label: "1M Rev " + y1, year: y1, field: "rev1M" },
      { key: "momentum", label: "Momentum", align: "left" },
    ];

    if (!state.sort.key || String(state.sort.key).indexOf("pe27") >= 0 || state.sort.key === "pe27") {
      state.sort = { key: "pe:" + y1, dir: "asc" };
    }

    function cellValue(r, c) {
      if (c.year && c.field) {
        const p = periodOf(r, c.year);
        if (p && p[c.field] != null) return p[c.field];
        /* legacy flat fallback */
        if (c.field === "eps") {
          if (c.year === y0) return r.eps26;
          if (c.year === y1) return r.eps27;
          if (c.year === y2) return r.eps28;
        }
        if (c.field === "pe") {
          if (c.year === y0) return r.pe26;
          if (c.year === y1) return r.pe27;
          if (c.year === y2) return r.pe28;
        }
        if (c.field === "rev1M") {
          if (c.year === y1) return r.rev1M;
          if (c.year === y2) return r.rev1M28;
        }
        if (c.field === "growthFromPrior") {
          if (c.year === y1) return r.growth27;
          if (c.year === y2) return r.growth28;
        }
        return null;
      }
      if (c.key === "cagrY0Y2") return r.cagrY0Y2 != null ? r.cagrY0Y2 : r.cagr2628;
      return r[c.key];
    }

    function fyLabel(r, year) {
      const p = periodOf(r, year);
      if (p && p.reportedFiscalLabel) return p.reportedFiscalLabel;
      if (year === y0) return r.reportedFy26;
      if (year === y1) return r.reportedFy27;
      if (year === y2) return r.reportedFy28;
      return null;
    }

    const tableWrap = el("div", { className: "table-wrap" });
    const table = el("table", { className: "data" });
    const thead = el("thead");
    const hr = el("tr");
    cols.forEach((c) => {
      const th = el("th", {
        className:
          "sortable" +
          (c.align === "left" ? " left" : "") +
          (state.sort.key === c.key ? (state.sort.dir === "asc" ? " sorted-asc" : " sorted-desc") : ""),
        text: c.label,
        onClick: () => {
          if (state.sort.key === c.key) {
            state.sort.dir = state.sort.dir === "asc" ? "desc" : "asc";
          } else {
            state.sort.key = c.key;
            state.sort.dir = "asc";
          }
          route();
        },
      });
      hr.appendChild(th);
    });
    thead.appendChild(hr);
    table.appendChild(thead);

    const tbody = el("tbody");
    const rows = state.valuation.slice().sort((a, b) => {
      const { key, dir } = state.sort;
      const mul = dir === "asc" ? 1 : -1;
      const ca = cols.find((x) => x.key === key) || { key: key };
      let va = cellValue(a, ca);
      let vb = cellValue(b, ca);
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === "string") return mul * va.localeCompare(vb);
      return mul * (Number(va) - Number(vb));
    });
    rows.forEach((r) => {
      const tr = el("tr");
      cols.forEach((c) => {
        const td = el("td", { className: c.align === "left" ? "left" : "" });
        const v = cellValue(r, c);
        if (c.key === "ticker") {
          td.classList.add("ticker");
          td.appendChild(el("a", { href: "#/company/" + r.ticker, text: r.ticker }));
        } else if (c.field === "rev1M") {
          td.appendChild(revCell(v));
        } else if (c.field === "growthFromPrior" || c.key === "cagrY0Y2") {
          td.appendChild(c.key === "cagrY0Y2" ? cagrCell(v) : growthCell(v));
        } else if (c.key === "momentum") {
          td.appendChild(momentumCell(v, r.revisionRegime));
        } else if (c.key === "lastClose" || c.key === "price") {
          const px = r.lastClose != null ? r.lastClose : r.price;
          td.appendChild(numCell(px, 2));
          if (!isMissing(r.afterHours)) {
            td.appendChild(el("span", { className: "price-sub", text: "AH " + fmtNum(r.afterHours, 2) }));
          }
        } else if (c.field === "eps" || c.field === "pe") {
          td.appendChild(valueWithFy(numCell(v, 2), fyLabel(r, c.year)));
        } else {
          td.appendChild(numCell(v, 2));
        }
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    tableWrap.appendChild(table);
    root.appendChild(tableWrap);
    return root;
  }


  /* ---------- revisions / charts ---------- */
  function destroyChart() {
    if (state.chart) {
      try {
        state.chart.destroy();
      } catch (_) {}
      state.chart = null;
    }
  }

  function chartColors() {
    const dark = (document.documentElement.getAttribute("data-theme") || "dark") === "dark";
    return {
      grid: dark ? "#30363d" : "#d0d7de",
      text: dark ? "#8b949e" : "#656d76",
      line: dark ? "#58a6ff" : "#0969da",
      point: dark ? "#58a6ff" : "#0969da",
    };
  }

  function renderChart() {
    const canvas = $("#eps-chart");
    if (!canvas || typeof Chart === "undefined") return;
    destroyChart();
    const ticker = state.chartTicker || state.watchlist[0];
    const year = state.chartYear || displayYearKeys()[1] || displayYearKeys()[0];
    const mode = state.chartMode || "absolute";
    const series = ((state.epsHistory[ticker] || {})[year] || []).filter((p) => p && !isMissing(p.eps));
    const colors = chartColors();
    const raw = series.map((p) => p.eps);
    const data = mode === "index" ? toIndexSeries(raw) : raw;
    const yTitle = mode === "index" ? "Revision Index (first = 100)" : "Consensus EPS";
    const label =
      ticker + " " + year + (mode === "index" ? " Revision Index" : " EPS");

    state.chart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels: series.map((p) => p.date),
        datasets: [
          {
            label: label,
            data: data,
            borderColor: colors.line,
            backgroundColor: "transparent",
            pointBackgroundColor: colors.point,
            pointRadius: 4,
            borderWidth: 2,
            tension: 0,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        plugins: {
          legend: {
            labels: { color: colors.text, boxWidth: 12, font: { size: 11 } },
          },
          tooltip: {
            callbacks: {
              afterLabel: function (ctx) {
                const p = series[ctx.dataIndex];
                const bits = [];
                if (p && p.reportedFiscalLabel) bits.push("FY: " + p.reportedFiscalLabel);
                if (mode === "index" && p && !isMissing(p.eps)) bits.push("EPS: " + fmtNum(p.eps, 2));
                return bits.join(" · ");
              },
            },
          },
        },
        scales: {
          x: {
            ticks: { color: colors.text, font: { size: 10 } },
            grid: { color: colors.grid },
          },
          y: {
            ticks: { color: colors.text, font: { size: 10 } },
            grid: { color: colors.grid },
            title: { display: true, text: yTitle, color: colors.text, font: { size: 11 } },
          },
        },
      },
    });
  }

  function filteredRevisions() {
    const f = state.revFilters;
    return state.revisions.filter((r) => {
      if (f.ticker && r.ticker !== f.ticker) return false;
      if (f.year) {
        const y = yearFromAlignment(r.calendarAlignment);
        if (y !== f.year) return false;
      }
      if (f.date && r.date !== f.date) return false;
      return true;
    });
  }

  function renderRevisions() {
    const root = el("div");

    const chartSec = el("div", { className: "section" });
    chartSec.appendChild(el("h2", { className: "section-title", text: "EPS Consensus History" }));
    chartSec.appendChild(
      el("p", {
        className: "section-note",
        text: "Built from daily EPS snapshots (mapped FY slots). Revision Index mode sets the first history point to 100. Table below still uses revision events only.",
      })
    );

    const controls = el("div", { className: "controls" });
    const tickerLabel = el("label", { text: "Company" });
    const tickerSel = el("select", {
      id: "chart-ticker",
      onChange: (e) => {
        state.chartTicker = e.target.value;
        renderChart();
      },
    });
    state.watchlist.forEach((t) => {
      tickerSel.appendChild(
        el("option", { value: t, text: t, selected: t === (state.chartTicker || state.watchlist[0]) })
      );
    });
    tickerLabel.appendChild(tickerSel);
    controls.appendChild(tickerLabel);

    const yearWrap = el("div");
    yearWrap.appendChild(el("span", { className: "section-note", text: "Year  ", style: "margin:0" }));
    const btnGroup = el("div", { className: "btn-group" });
    (state.meta && state.meta.chartYears ? state.meta.chartYears : displayYearKeys().slice(0, 3)).forEach((y) => {
      btnGroup.appendChild(
        el("button", {
          type: "button",
          className: state.chartYear === y ? "active" : "",
          text: y,
          onClick: () => {
            state.chartYear = y;
            route();
          },
        })
      );
    });
    yearWrap.appendChild(btnGroup);
    controls.appendChild(yearWrap);

    const modeWrap = el("div");
    modeWrap.appendChild(el("span", { className: "section-note", text: "Mode  ", style: "margin:0" }));
    const modeGroup = el("div", { className: "btn-group chart-mode-toggle" });
    [
      ["absolute", "Absolute EPS"],
      ["index", "Revision Index"],
    ].forEach(([mode, label]) => {
      modeGroup.appendChild(
        el("button", {
          type: "button",
          className: (state.chartMode || "absolute") === mode ? "active" : "",
          text: label,
          onClick: () => {
            state.chartMode = mode;
            route();
          },
        })
      );
    });
    modeWrap.appendChild(modeGroup);
    controls.appendChild(modeWrap);
    chartSec.appendChild(controls);

    const panel = el("div", { className: "chart-panel" });
    panel.appendChild(el("canvas", { id: "eps-chart" }));
    chartSec.appendChild(panel);
    root.appendChild(chartSec);

    /* history table */
    const histSec = el("div", { className: "section" });
    histSec.appendChild(el("h2", { className: "section-title", text: "Revision History" }));

    const filters = el("div", { className: "controls" });

    const fTicker = el("label", { text: "Ticker" });
    const selT = el("select", {
      onChange: (e) => {
        state.revFilters.ticker = e.target.value;
        route();
      },
    });
    selT.appendChild(el("option", { value: "", text: "All" }));
    state.watchlist.forEach((t) => {
      selT.appendChild(
        el("option", {
          value: t,
          text: t,
          selected: state.revFilters.ticker === t,
        })
      );
    });
    fTicker.appendChild(selT);
    filters.appendChild(fTicker);

    const fYear = el("label", { text: "Year" });
    const selY = el("select", {
      onChange: (e) => {
        state.revFilters.year = e.target.value;
        route();
      },
    });
    selY.appendChild(el("option", { value: "", text: "All" }));
    displayYearKeys().slice(0, 3).forEach((y) => {
      selY.appendChild(
        el("option", { value: y, text: y, selected: state.revFilters.year === y })
      );
    });
    fYear.appendChild(selY);
    filters.appendChild(fYear);

    const fDate = el("label", { text: "Date" });
    const dates = Array.from(new Set(state.revisions.map((r) => r.date).filter(Boolean))).sort();
    const selD = el("select", {
      onChange: (e) => {
        state.revFilters.date = e.target.value;
        route();
      },
    });
    selD.appendChild(el("option", { value: "", text: "All" }));
    dates.forEach((d) => {
      selD.appendChild(
        el("option", { value: d, text: d, selected: state.revFilters.date === d })
      );
    });
    fDate.appendChild(selD);
    filters.appendChild(fDate);

    histSec.appendChild(filters);

    const tableWrap = el("div", { className: "table-wrap" });
    const table = el("table", { className: "data" });
    const thead = el("thead");
    const hr = el("tr");
    [
      ["Date", "left"],
      ["Ticker", "left"],
      ["Fiscal Year", "left"],
      ["Calendar", "left"],
      ["Previous EPS", ""],
      ["Current EPS", ""],
      ["Change", ""],
      ["Revision %", ""],
      ["Reason", "left"],
      ["Source", "left"],
    ].forEach(([l, a]) => hr.appendChild(el("th", { className: a === "left" ? "left" : "", text: l })));
    thead.appendChild(hr);
    table.appendChild(thead);

    const tbody = el("tbody");
    const rows = filteredRevisions().slice().reverse();
    rows.forEach((r) => {
      const tr = el("tr");
      [
        () => {
          const td = el("td", { className: "left", text: r.date || "—" });
          return td;
        },
        () => {
          const td = el("td", { className: "ticker left" });
          td.appendChild(el("a", { href: "#/company/" + r.ticker, text: r.ticker }));
          return td;
        },
        () => el("td", { className: "left", text: r.fiscalYear || "—" }),
        () => el("td", { className: "left", text: r.calendarAlignment || "—" }),
        () => {
          const td = el("td");
          td.appendChild(numCell(r.previousEps, 2));
          return td;
        },
        () => {
          const td = el("td");
          td.appendChild(numCell(r.currentEps, 2));
          return td;
        },
        () => {
          const td = el("td");
          td.appendChild(numCell(r.absoluteChange, 2));
          return td;
        },
        () => {
          const td = el("td");
          if (isMissing(r.revisionPct)) td.appendChild(naCell());
          else {
            const n = Number(r.revisionPct);
            const cls = n > 0 ? "pos" : n < 0 ? "neg" : "";
            td.appendChild(el("span", { className: cls, text: fmtPct(n / 100, 2, false) || String(n) }));
          }
          return td;
        },
        () => el("td", { className: "left", text: r.reason || "—" }),
        () => el("td", { className: "left", text: r.source || "—" }),
      ].forEach((fn) => tr.appendChild(fn()));
      tbody.appendChild(tr);
    });
    if (!rows.length) {
      const tr = el("tr");
      const td = el("td", { className: "left", text: "No revision rows match filters.", colspan: "10" });
      td.style.color = "var(--text-muted)";
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    tableWrap.appendChild(table);
    histSec.appendChild(tableWrap);
    root.appendChild(histSec);

    // chart after DOM attach
    requestAnimationFrame(() => renderChart());
    return root;
  }


  function earnItemText(item) {
    if (item == null) return "—";
    if (typeof item === "string") return item;
    if (typeof item === "object") {
      const text = item.text || item.question || item.title || "";
      const src = item.source || "";
      if (text && src) return text + " [" + src + "]";
      return text || JSON.stringify(item);
    }
    return String(item);
  }

  function appendEarnItems(listEl, arr) {
    if (!arr || !arr.length) {
      listEl.appendChild(el("li", { text: "No items yet" }));
      return;
    }
    arr.forEach((item) => {
      const li = el("li");
      if (item && typeof item === "object" && item.url && (item.text || item.title)) {
        li.appendChild(document.createTextNode((item.text || item.title) + " "));
        li.appendChild(
          el("a", {
            className: "source-link",
            href: item.url,
            target: "_blank",
            rel: "noopener",
            text: "source",
          })
        );
      } else {
        li.textContent = earnItemText(item);
      }
      listEl.appendChild(li);
    });
  }

  /* ---------- earnings ---------- */
  function renderEarnings() {
    const root = el("div", { className: "section" });
    root.appendChild(el("h2", { className: "section-title", text: "Earnings" }));
    root.appendChild(
      el("p", {
        className: "section-note",
        text: "Compact calendar + digest status. Full positives / negatives / Q&A / sources live on each company page.",
      })
    );

    const tableWrap = el("div", { className: "table-wrap" });
    const table = el("table", { className: "data" });
    const thead = el("thead");
    const hr = el("tr");
    ["Ticker", "Last Earnings", "Next Earnings", "Vs Consensus", "Digest"].forEach((lab, i) => {
      hr.appendChild(el("th", { className: i === 0 || i >= 3 ? "left" : "left", text: lab }));
    });
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = el("tbody");
    state.watchlist.forEach((t) => {
      const e = state.earnings[t] || {};
      const c = state.companies[t] || {};
      const tr = el("tr");
      const tdT = el("td", { className: "left" });
      tdT.appendChild(el("a", { href: "#/earnings/" + t, text: t, title: "Earnings Detail" }));
      tr.appendChild(tdT);

      const tdLast = el("td", { className: "left" });
      const last = e.lastEarnings || c.lastEarnings;
      if (isMissing(last)) tdLast.appendChild(naCell());
      else tdLast.textContent = last;
      tr.appendChild(tdLast);

      const tdNext = el("td", { className: "left" });
      const nxt = nextEarningsLabel(c, e);
      if (isMissing(nxt)) tdNext.appendChild(naCell());
      else tdNext.textContent = nxt;
      tr.appendChild(tdNext);

      const tdVs = el("td", { className: "left" });
      const vs =
        e.comparison ||
        (e.results && e.results.vsConsensus) ||
        null;
      if (isMissing(vs)) tdVs.appendChild(naCell());
      else tdVs.textContent = String(vs);
      tr.appendChild(tdVs);

      const tdDig = el("td", { className: "left" });
      const has = e.hasDigest === true || (e.positives && e.positives.length) || (e.qa && e.qa.length);
      tdDig.textContent = has ? "Yes" : "No";
      if (e.exportError) {
        tdDig.appendChild(el("span", { className: "fy-sub", text: "export error" }));
      }
      tr.appendChild(tdDig);

      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    tableWrap.appendChild(table);
    root.appendChild(tableWrap);
    return root;
  }

  /* ---------- earnings detail (distinct from company page) ---------- */
  function renderEarningsDetail(ticker) {
    const c = state.companies[ticker] || {};
    const earn = state.earnings[ticker] || {};
    const root = el("div", { className: "section earnings-detail" });
    root.appendChild(el("h2", { className: "section-title", text: "Earnings Detail — " + ticker }));
    root.appendChild(
      el("p", {
        className: "section-note",
        text: "Dedicated earnings digest view (route #/earnings/" + ticker + "). Not the company snapshot page.",
      })
    );
    root.appendChild(
      el("p", null, [
        el("a", { href: "#/earnings", text: "← Earnings" }),
        document.createTextNode(" · "),
        el("a", { href: "#/company/" + ticker, text: "Company page" }),
      ])
    );

    const panel = el("div", { className: "panel section" });
    panel.appendChild(el("h3", { text: "Report" }));
    const dl = el("dl", { className: "kv" });
    function kv(label, val) {
      dl.appendChild(el("dt", { text: label }));
      const dd = el("dd");
      if (val == null || val === "") dd.appendChild(naCell());
      else dd.textContent = String(val);
      dl.appendChild(dd);
    }
    kv("Period", earn.periodLabel || "—");
    kv("Report date", earn.reportDate || "—");
    kv("Last earnings", earn.lastEarnings || c.lastEarnings || "—");
    kv("Next earnings", nextEarningsLabel(c, earn) || "—");
    panel.appendChild(dl);

    /* Provenance */
    const prov = el("div", { className: "panel section" });
    prov.appendChild(el("h3", { text: "Earnings Provenance" }));
    const act = earn.actuals || {};
    const cc = earn.consensusComparison || {};
    prov.appendChild(el("div", { className: "stub-note", text: "Actuals (Company IR)" }));
    if (act.sourceUrl) {
      prov.appendChild(
        el("div", null, [
          el("a", {
            className: "source-link",
            href: act.sourceUrl,
            target: "_blank",
            rel: "noopener",
            text: "Tier " + (act.sourceTier != null ? act.sourceTier : "?") + " · " + act.sourceUrl,
          }),
        ])
      );
    } else {
      prov.appendChild(el("p", { className: "section-note", text: "No actuals.sourceUrl" }));
    }
    if (act.metrics || act.eps || act.revenue) {
      const m = act.metrics || act;
      prov.appendChild(
        el("p", {
          className: "section-note",
          text:
            "EPS " + (m.eps || act.eps || "—") +
            " · Revenue " + (m.revenue || act.revenue || "—") +
            " · GM " + (m.grossMargin || act.grossMargin || "—"),
        })
      );
    }
    prov.appendChild(el("div", { className: "stub-note", text: "Consensus comparison (beat/miss)" }));
    const vs = cc.vsConsensus || cc.beatMiss || earn.comparison || (earn.results && earn.results.vsConsensus);
    prov.appendChild(el("p", { className: "section-note", text: "Vs consensus: " + (vs || "—") }));
    if (cc.sourceUrl) {
      prov.appendChild(
        el("div", null, [
          el("a", {
            className: "source-link",
            href: cc.sourceUrl,
            target: "_blank",
            rel: "noopener",
            text: "Tier " + (cc.sourceTier != null ? cc.sourceTier : "?") + " · " + cc.sourceUrl,
          }),
        ])
      );
    }
    if (cc.note) {
      prov.appendChild(el("p", { className: "section-note", text: String(cc.note) }));
    }
    root.appendChild(panel);
    root.appendChild(prov);

    ["positives", "negatives", "uncertainties"].forEach((key) => {
      const title = key.charAt(0).toUpperCase() + key.slice(1);
      root.appendChild(el("div", { className: "stub-note", text: title }));
      const list = el("ul", { className: "earn-section-list" + (!(earn[key] || []).length ? " empty" : "") });
      appendEarnItems(list, earn[key] || []);
      root.appendChild(list);
    });
    if (earn.guidance) {
      root.appendChild(el("div", { className: "stub-note", text: "Guidance" }));
      root.appendChild(el("p", { className: "section-note", text: String(earn.guidance) }));
    }
    if (earn.qa && earn.qa.length) {
      root.appendChild(el("div", { className: "stub-note", text: "Important Q&A" }));
      const qlist = el("ul", { className: "earn-section-list" });
      earn.qa.forEach((q) => {
        const li = el("li");
        li.textContent =
          (q.question || "") +
          " → " +
          (q.answer || "") +
          (q.whyMattersForEps ? " (" + q.whyMattersForEps + ")" : "");
        qlist.appendChild(li);
      });
      root.appendChild(qlist);
    }
    document.title = "Earnings Detail — " + ticker + " · AI EPS Monitor";
    return root;
  }

  /* ---------- companies list ---------- */
  function renderCompanies() {
    const root = el("div", { className: "section" });
    root.appendChild(el("h2", { className: "section-title", text: "Companies" }));
    root.appendChild(
      el("p", {
        className: "section-note",
        text: "Watchlist-driven. Add a ticker to watchlist.json + companies.json and re-export — no HTML edits required.",
      })
    );
    const list = el("div", { className: "company-list" });
    state.watchlist.forEach((t) => {
      const c = state.companies[t] || {};
      const chip = el("a", { className: "company-chip", href: "#/company/" + t });
      chip.appendChild(document.createTextNode(t));
      const chipPx = companyPrice(c);
      if (chipPx != null) {
        chip.appendChild(document.createTextNode("  " + fmtNum(chipPx, 2)));
      }
      list.appendChild(chip);
    });
    root.appendChild(list);
    return root;
  }

  /* ---------- company detail ---------- */
  function renderCompanyChart(ticker) {
    const canvas = $("#company-eps-chart");
    if (!canvas || typeof Chart === "undefined") return;
    destroyChart();
    const hist = state.epsHistory[ticker] || {};
    const colors = chartColors();
    const palette = ["#58a6ff", "#3fb950", "#d29922", "#f778ba"];
    const years = (state.meta && state.meta.chartYears) ? state.meta.chartYears : displayYearKeys().slice(0, 3);
    const mode = state.chartMode || "absolute";

    const labels = Array.from(
      new Set(years.flatMap((y) => (hist[y] || []).map((p) => p.date).filter(Boolean)))
    ).sort();

    const ds2 = years
      .map((y, i) => {
        const series = hist[y] || [];
        const byDate = {};
        series.forEach((p) => {
          if (p && !isMissing(p.eps)) byDate[p.date] = p.eps;
        });
        let data = labels.map((d) => (byDate[d] != null ? byDate[d] : null));
        if (data.every((v) => v == null)) return null;
        if (mode === "index") data = toIndexSeries(data);
        return {
          label: y + (mode === "index" ? " idx" : ""),
          data,
          borderColor: palette[i % palette.length],
          backgroundColor: "transparent",
          pointRadius: 4,
          borderWidth: 2,
          spanGaps: true,
          tension: 0,
        };
      })
      .filter(Boolean);

    const yTitle = mode === "index" ? "Revision Index (first = 100)" : "Consensus EPS";

    state.chart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: { labels, datasets: ds2 },
      options: {
        responsive: true,
        plugins: {
          legend: { labels: { color: colors.text, boxWidth: 12, font: { size: 11 } } },
        },
        scales: {
          x: { ticks: { color: colors.text, font: { size: 10 } }, grid: { color: colors.grid } },
          y: {
            ticks: { color: colors.text, font: { size: 10 } },
            grid: { color: colors.grid },
            title: { display: true, text: yTitle, color: colors.text },
          },
        },
      },
    });
  }

  function renderCompany(ticker) {
    const c = state.companies[ticker];
    const root = el("div");
    if (!c) {
      root.appendChild(el("p", { className: "error", text: "Ticker " + ticker + " not in companies.json" }));
      root.appendChild(el("p", null, [el("a", { href: "#/companies", text: "← Companies" })]));
      return root;
    }

    const header = el("div", { className: "company-header" });
    header.appendChild(el("h2", { text: ticker }));
    const px = companyPrice(c);
    const priceEl = el("div", { className: "price" });
    if (isMissing(px)) priceEl.appendChild(naCell());
    else {
      priceEl.textContent = "$" + fmtNum(px, 2);
      if (!isMissing(c.afterHours)) {
        priceEl.appendChild(el("span", { className: "price-sub", text: "After hours $" + fmtNum(c.afterHours, 2) }));
      }
    }
    header.appendChild(priceEl);
    const bits = el("div", { className: "meta-bits" });
    bits.appendChild(momentumCell(c.momentum, c.revisionRegime));
    bits.appendChild(document.createTextNode("  ·  "));
    bits.appendChild(document.createTextNode(c.priceAsOf || ""));
    header.appendChild(bits);
    root.appendChild(header);

    const grid = el("div", { className: "detail-grid" });

    /* snapshot panel */
    const snap = el("div", { className: "panel" });
    snap.appendChild(el("h3", { text: "Snapshot" }));
    const dl = el("dl", { className: "kv" });
    function kv(label, nodeOrText) {
      dl.appendChild(el("dt", { text: label }));
      const dd = el("dd");
      if (nodeOrText == null || nodeOrText === "") dd.appendChild(naCell());
      else if (typeof nodeOrText === "string" || typeof nodeOrText === "number") dd.textContent = String(nodeOrText);
      else dd.appendChild(nodeOrText);
      dl.appendChild(dd);
    }
    kv("Last Close", isMissing(px) ? null : fmtNum(px, 2));
    kv("After Hours", isMissing(c.afterHours) ? null : fmtNum(c.afterHours, 2));
    kv("Last Earnings", isMissing(c.lastEarnings) ? null : c.lastEarnings);
    {
      const nxt = nextEarningsLabel(c, state.earnings[ticker]);
      kv("Next Earnings", isMissing(nxt) ? null : nxt);
      const st = c.nextEarningsStatus || (state.earnings[ticker] || {}).nextEarningsStatus;
      const src = c.nextEarningsSource || (state.earnings[ticker] || {}).nextEarningsSource;
      if (st || src) {
        kv("Next Earnings Status", (st || "estimated") + (src ? " · " + src : ""));
      }
    }
    kv("FY Note", isMissing(c.fyNote) ? null : c.fyNote);
    const years = displayYearKeys();
    years.forEach((y) => {
      const e = (c.eps && c.eps[y]) || {};
      const cons = fmtNum(e.consensus, 2);
      const lab = e.reportedFiscalLabel ? " (" + e.reportedFiscalLabel + ")" : "";
      const slotLabel = "Mapped " + y;
      kv(slotLabel, cons == null ? null : cons + lab);
      if (e.trueCalendarYearEps != null) {
        kv(y + " true CY EPS", fmtNum(e.trueCalendarYearEps, 2));
      }
      if (e.fiscalEqualsCalendar) {
        kv(y + " note", "fiscalEqualsCalendar: true");
      }
      kv(y + " 1M Rev", revCell(e.rev1M));
    });
    const y1 = years[1] || "";
    const y2 = years[2] || "";
    const peY1 = pe(px, ((c.eps || {})[y1] || {}).consensus);
    kv(y1 + " PE (Last Close)", peY1 == null ? null : fmtNum(peY1, 2));
    [y1, y2].forEach((y) => {
      const e = (c.eps && c.eps[y]) || {};
      kv(y + " Analyst Count", isMissing(e.analysts) ? null : fmtNum(e.analysts, 0));
      kv(y + " Consensus Low", isMissing(e.low) ? null : fmtNum(e.low, 2));
      kv(y + " Consensus", isMissing(e.consensus) ? null : fmtNum(e.consensus, 2));
      kv(y + " Consensus High", isMissing(e.high) ? null : fmtNum(e.high, 2));
      const disp = fmtDispersion(e.dispersion);
      kv(y + " Dispersion (H−L)/Cons", disp);
      const bar = rangeBar(e.low, e.consensus, e.high);
      if (bar) kv(y + " Range", bar);
    });
    snap.appendChild(dl);
    grid.appendChild(snap);

    /* drivers */
    const drv = el("div", { className: "panel" });
    drv.appendChild(el("h3", { text: "Earnings Drivers" }));
    const drivers = (c.drivers && c.drivers.drivers) || [];
    if (c.drivers && c.drivers.status) {
      drv.appendChild(el("p", { className: "section-note", text: c.drivers.status }));
    }
    const ul = el("ul", { className: "drivers-list" });
    if (!drivers.length) {
      ul.appendChild(el("li", { text: "No drivers on file" }));
    } else {
      drivers.forEach((d) => {
        const li = el("li", { className: "driver-item" });
        const row = el("button", {
          type: "button",
          className: "driver-row",
          onClick: (ev) => {
            ev.preventDefault();
            li.classList.toggle("expanded");
          },
        });
        const left = el("span", { className: "driver-name" });
        left.textContent = d.name || "—";
        row.appendChild(left);
        const statusVal = d.currentStatus || d.status;
        const st = driverLabel(statusVal);
        row.appendChild(el("span", { className: "driver-status " + st.cls, text: st.text }));
        li.appendChild(row);
        const detail = el("div", { className: "driver-detail" });
        const tip = d.reason || d.note;
        if (tip) detail.appendChild(el("div", { className: "driver-reason", text: tip }));
        if (d.sourceUrl || d.source) {
          if (d.sourceUrl) {
            detail.appendChild(
              el("a", {
                className: "source-link",
                href: d.sourceUrl,
                target: "_blank",
                rel: "noopener",
                text: d.source || "Source",
              })
            );
          } else {
            detail.appendChild(el("div", { className: "driver-source", text: String(d.source) }));
          }
        }
        if (!tip && !d.sourceUrl && !d.source) {
          detail.appendChild(el("div", { className: "driver-reason", text: "No reason on file" }));
        }
        li.appendChild(detail);
        ul.appendChild(li);
      });
    }
    drv.appendChild(ul);
    grid.appendChild(drv);
    root.appendChild(grid);

    /* chart */
    const chartPanel = el("div", { className: "panel section" });
    chartPanel.appendChild(el("h3", { text: "EPS History (mapped slots)" }));
    const modeControls = el("div", { className: "controls" });
    const modeGroup = el("div", { className: "btn-group chart-mode-toggle" });
    [
      ["absolute", "Absolute EPS"],
      ["index", "Revision Index"],
    ].forEach(([mode, label]) => {
      modeGroup.appendChild(
        el("button", {
          type: "button",
          className: (state.chartMode || "absolute") === mode ? "active" : "",
          text: label,
          onClick: () => {
            state.chartMode = mode;
            route();
          },
        })
      );
    });
    modeControls.appendChild(modeGroup);
    chartPanel.appendChild(modeControls);
    const cp = el("div", { className: "chart-panel" });
    cp.appendChild(el("canvas", { id: "company-eps-chart" }));
    chartPanel.appendChild(cp);
    root.appendChild(chartPanel);

    /* earnings stub */
    const earn = state.earnings[ticker] || {};
    const earnPanel = el("div", { className: "panel section" });
    earnPanel.appendChild(el("h3", { text: "Earnings Digest" }));
    earnPanel.appendChild(
      el("p", {
        className: "section-note",
        text:
          "Last: " +
          (earn.lastEarnings || c.lastEarnings || "—") +
          " · Next: " +
          (nextEarningsLabel(c, earn) || "—"),
      })
    );
    ["positives", "negatives", "uncertainties"].forEach((key) => {
      const title = key.charAt(0).toUpperCase() + key.slice(1);
      earnPanel.appendChild(el("div", { className: "stub-note", text: title }));
      const list = el("ul", { className: "earn-section-list" + (!(earn[key] || []).length ? " empty" : "") });
      appendEarnItems(list, earn[key] || []);
      earnPanel.appendChild(list);
    });
    if (earn.guidance) {
      earnPanel.appendChild(el("div", { className: "stub-note", text: "Guidance" }));
      earnPanel.appendChild(el("p", { className: "section-note", text: String(earn.guidance) }));
    }
    if (earn.comparison) {
      earnPanel.appendChild(el("div", { className: "stub-note", text: "Results vs consensus: " + String(earn.comparison) }));
    }
    if (earn.commentary) {
      earnPanel.appendChild(el("div", { className: "stub-note", text: "Management Commentary" }));
      earnPanel.appendChild(el("p", { className: "section-note", text: String(earn.commentary) }));
    }
    if (earn.qa && earn.qa.length) {
      earnPanel.appendChild(el("div", { className: "stub-note", text: "Important Q&A" }));
      const qlist = el("ul", { className: "earn-section-list" });
      earn.qa.forEach((q) => {
        const li = el("li");
        li.textContent = (q.question || "") + " → " + (q.answer || "") + (q.whyMattersForEps ? " (" + q.whyMattersForEps + ")" : "");
        qlist.appendChild(li);
      });
      earnPanel.appendChild(qlist);
    }
    if (earn.sourceHierarchy) {
      earnPanel.appendChild(el("p", { className: "section-note", text: earn.sourceHierarchy }));
    }
    if (earn.sources && earn.sources.length) {
      earnPanel.appendChild(el("div", { className: "stub-note", text: "Digest Sources (by tier)" }));
      earn.sources.forEach((s) => {
        if (!s) return;
        const tier = s.sourceTier != null ? "T" + s.sourceTier + " · " : "";
        const label = tier + (s.title || s.attribution || "source");
        if (s.url) {
          earnPanel.appendChild(
            el("div", null, [
              el("a", { className: "source-link", href: s.url, target: "_blank", rel: "noopener", text: label }),
            ])
          );
        } else {
          earnPanel.appendChild(el("div", { className: "section-note", text: label + (s.note ? " — " + s.note : "") }));
        }
      });
    }
    if (earn.periodLabel) {
      earnPanel.appendChild(el("p", { className: "section-note", text: "Period: " + earn.periodLabel + (earn.reportDate ? " · Report " + earn.reportDate : "") }));
    }
    root.appendChild(earnPanel);

    /* revision history for ticker */
    const revPanel = el("div", { className: "section" });
    revPanel.appendChild(el("h3", { className: "section-title", text: "Revision History" }));
    const tableWrap = el("div", { className: "table-wrap" });
    const table = el("table", { className: "data" });
    const thead = el("thead");
    const hr = el("tr");
    ["Date", "Fiscal Year", "Calendar", "Current EPS", "Reason", "Source"].forEach((l, i) => {
      hr.appendChild(el("th", { className: i < 3 || i >= 4 ? "left" : "", text: l }));
    });
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = el("tbody");
    state.revisions
      .filter((r) => r.ticker === ticker)
      .forEach((r) => {
        const tr = el("tr");
        tr.appendChild(el("td", { className: "left", text: r.date || "—" }));
        tr.appendChild(el("td", { className: "left", text: r.fiscalYear || "—" }));
        tr.appendChild(el("td", { className: "left", text: r.calendarAlignment || "—" }));
        const tdE = el("td");
        tdE.appendChild(numCell(r.currentEps, 2));
        tr.appendChild(tdE);
        tr.appendChild(el("td", { className: "left", text: r.reason || "—" }));
        tr.appendChild(el("td", { className: "left", text: r.source || "—" }));
        tbody.appendChild(tr);
      });
    table.appendChild(tbody);
    tableWrap.appendChild(table);
    revPanel.appendChild(tableWrap);
    root.appendChild(revPanel);

    /* sources */
    const src = el("div", { className: "panel section" });
    src.appendChild(el("h3", { text: "Sources" }));
    if (c.sourceUrl) {
      src.appendChild(
        el("a", { className: "source-link", href: c.sourceUrl, target: "_blank", rel: "noopener", text: c.sourceUrl })
      );
    } else {
      src.appendChild(naCell());
    }
    src.appendChild(
      el("p", {
        className: "section-note",
        text: "Source: " + (c.source || state.meta.primarySource || "—") + " · Updated: " + (c.updateTime || "—"),
      })
    );
    root.appendChild(src);

    requestAnimationFrame(() => renderCompanyChart(ticker));
    return root;
  }

  /* ---------- router ---------- */
  function route() {
    if (!state.ready) return;
    destroyChart();
    const r = getRoute();
    setActiveTab(r);
    updateHeaderMeta();
    if (r.name !== "earningsDetail") {
      document.title = "AI Investment EPS & Earnings Monitor";
    }
    const app = $("#app");
    if (!app) return;
    app.innerHTML = "";
    let view;
    try {
      if (r.name === "valuation") {
        if (!state.sort.key || state.sort.key === "ticker") {
          /* keep user sort; default pe27 asc on first visit via flag */
        }
        view = renderValuation();
      } else if (r.name === "revisions") view = renderRevisions();
      else if (r.name === "earnings") view = renderEarnings();
      else if (r.name === "earningsDetail") view = renderEarningsDetail(r.ticker);
      else if (r.name === "companies") view = renderCompanies();
      else if (r.name === "company") view = renderCompany(r.ticker);
      else view = renderOverview();
    } catch (err) {
      console.error(err);
      view = el("p", { className: "error", text: "Render error: " + (err && err.message) });
    }
    app.appendChild(view);
  }

  async function boot() {
    initTheme();
    // default valuation sort
    state.sort = { key: "pe:" + (displayYearKeys()[1] || ""), dir: "asc" };
    const app = $("#app");
    try {
      await loadAll();
      route();
    } catch (err) {
      console.error(err);
      if (app) {
        app.innerHTML = "";
        app.appendChild(
          el("p", {
            className: "error",
            text: "Failed to load dashboard data. Run tools/export_web_data.py and serve the web/ directory. " + (err && err.message),
          })
        );
      }
    }
    window.addEventListener("hashchange", () => route());
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
