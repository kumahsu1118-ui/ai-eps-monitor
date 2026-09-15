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
    ready: false,
    chart: null,
    sort: { key: "ticker", dir: "asc" },
    revFilters: { ticker: "", year: "", date: "" },
    chartTicker: null,
    chartYear: "2027E",
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
    /* ratio 0.67 → +67.67% */
    const s = fmtPct(v, 2, false);
    if (s == null) return naCell();
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

  function momentumCell(m) {
    if (isMissing(m)) return naCell();
    return el("span", { className: momentumClass(m), text: String(m) });
  }

  function pe(price, eps) {
    if (isMissing(price) || isMissing(eps) || Number(eps) === 0) return null;
    return Number(price) / Number(eps);
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

  async function loadAll() {
    const [
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

    state.watchlist = (watchlist && watchlist.tickers) || Object.keys(companies || {});
    state.meta = meta || {};
    state.companies = companies || {};
    state.valuation = (valuation && valuation.rows) || (Array.isArray(valuation) ? valuation : []);
    state.revisions = (revisions && revisions.revisions) || (Array.isArray(revisions) ? revisions : []);
    state.epsHistory = epsHistory || {};
    state.earnings = earnings || {};
    state.alerts = (alerts && alerts.alerts) || [];
    state.ready = true;
    if (!state.chartTicker && state.watchlist.length) {
      state.chartTicker = state.watchlist[0];
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
      if (route.name === "earnings" && r === "earnings") active = true;
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
    set("#meta-last-updated", m.lastUpdatedDisplay || m.lastUpdated);
    set("#meta-source", m.primarySource);
    set("#meta-refresh", m.latestSuccessfulRefresh || m.lastUpdatedDisplay);
    const fw = $("#footer-watchlist");
    if (fw) fw.textContent = "Watchlist: " + state.watchlist.join(", ");
  }

  /* ---------- summary cards ---------- */
  function computeSummaries() {
    const rows = state.valuation || [];
    let largestUp = null;
    let largestDown = null;
    let lowestPe = null;
    let fastestCagr = null;

    rows.forEach((r) => {
      if (r.rev1M != null && !Number.isNaN(Number(r.rev1M))) {
        if (Number(r.rev1M) > 0 && (!largestUp || Number(r.rev1M) > Number(largestUp.rev1M))) {
          largestUp = r;
        }
        if (Number(r.rev1M) < 0 && (!largestDown || Number(r.rev1M) < Number(largestDown.rev1M))) {
          largestDown = r;
        }
      }
      if (r.pe27 != null && !Number.isNaN(Number(r.pe27))) {
        if (!lowestPe || Number(r.pe27) < Number(lowestPe.pe27)) lowestPe = r;
      }
      // CAGR only when 2026E exists (cagr2628 already null if missing)
      if (r.cagr2628 != null && r.eps26 != null && !Number.isNaN(Number(r.cagr2628))) {
        if (!fastestCagr || Number(r.cagr2628) > Number(fastestCagr.cagr2628)) {
          fastestCagr = r;
        }
      }
    });

    return { largestUp, largestDown, lowestPe, fastestCagr };
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

    grid.appendChild(
      card(
        "Largest EPS Upgrade (1M)",
        s.largestUp && s.largestUp.ticker,
        s.largestUp ? revCell(s.largestUp.rev1M) : null
      )
    );
    grid.appendChild(
      card(
        "Largest EPS Downgrade (1M)",
        s.largestDown && s.largestDown.ticker,
        s.largestDown ? revCell(s.largestDown.rev1M) : null
      )
    );
    grid.appendChild(
      card(
        "Lowest 2027E PE",
        s.lowestPe && s.lowestPe.ticker,
        s.lowestPe ? fmtNum(s.lowestPe.pe27, 2) + "x" : null
      )
    );
    grid.appendChild(
      card(
        "Fastest 2026–2028 EPS CAGR",
        s.fastestCagr && s.fastestCagr.ticker,
        s.fastestCagr ? growthCell(s.fastestCagr.cagr2628) : null
      )
    );

    parent.appendChild(grid);
  }

  /* ---------- alerts ---------- */
  function renderAlerts(parent) {
    const box = el("div", { className: "alerts-box section" });
    box.appendChild(el("h2", { className: "section-title", text: "Important Alerts" }));
    if (!state.alerts || !state.alerts.length) {
      box.appendChild(el("div", { className: "alerts-empty", text: "No material alerts" }));
    } else {
      state.alerts.forEach((a) => {
        const msg = typeof a === "string" ? a : a.message || a.title || JSON.stringify(a);
        box.appendChild(el("div", { className: "alert-item", text: msg }));
      });
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
        text: "Calendar-aligned EPS; reported FY labels shown under consensus. 1M rev = Seeking Alpha short-window proxy on CY2027E.",
      })
    );

    const tableWrap = el("div", { className: "table-wrap" });
    const table = el("table", { className: "data" });
    const thead = el("thead");
    const hr = el("tr");
    [
      ["Ticker", "left"],
      ["Price", ""],
      ["2026E EPS", ""],
      ["2027E EPS", ""],
      ["2028E EPS", ""],
      ["2027E PE", ""],
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
      const e26 = (c.eps && c.eps["2026E"]) || {};
      const e27 = (c.eps && c.eps["2027E"]) || {};
      const e28 = (c.eps && c.eps["2028E"]) || {};
      const pe27 = pe(c.price, e27.consensus);
      const tr = el("tr");

      const tdT = el("td", { className: "ticker left" });
      tdT.appendChild(el("a", { href: "#/company/" + t, text: t }));
      tr.appendChild(tdT);

      const tdP = el("td");
      tdP.appendChild(numCell(c.price, 2));
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
      tdPe.appendChild(numCell(pe27, 2));
      tr.appendChild(tdPe);

      const tdRev = el("td");
      tdRev.appendChild(revCell(e27.rev1M));
      tr.appendChild(tdRev);

      const tdMom = el("td");
      tdMom.appendChild(momentumCell(c.momentum));
      tr.appendChild(tdMom);

      const tdLast = el("td", { className: "left" });
      if (isMissing(c.lastEarnings)) tdLast.appendChild(naCell());
      else tdLast.textContent = c.lastEarnings;
      tr.appendChild(tdLast);

      const tdNext = el("td", { className: "left" });
      if (isMissing(c.nextEarnings)) tdNext.appendChild(naCell());
      else tdNext.textContent = c.nextEarnings;
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
    root.appendChild(
      el("p", {
        className: "section-note",
        text: "Forward PE = Price / Consensus EPS. CAGR 2026–2028 = (EPS28/EPS26)^(1/2)−1. Click headers to sort.",
      })
    );

    const cols = [
      { key: "ticker", label: "Ticker", align: "left" },
      { key: "price", label: "Price" },
      { key: "eps26", label: "2026E EPS" },
      { key: "eps27", label: "2027E EPS" },
      { key: "eps28", label: "2028E EPS" },
      { key: "pe26", label: "2026E PE" },
      { key: "pe27", label: "2027E PE" },
      { key: "pe28", label: "2028E PE" },
      { key: "growth27", label: "27E Growth" },
      { key: "growth28", label: "28E Growth" },
      { key: "cagr2628", label: "CAGR 26–28" },
      { key: "rev1M", label: "1M Rev" },
      { key: "momentum", label: "Momentum", align: "left" },
    ];

    if (!state.sort.key) {
      state.sort = { key: "pe27", dir: "asc" };
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
            state.sort.dir = c.key === "ticker" || c.key === "momentum" ? "asc" : "asc";
          }
          route();
        },
      });
      hr.appendChild(th);
    });
    thead.appendChild(hr);
    table.appendChild(thead);

    const tbody = el("tbody");
    const rows = sortValuation(state.valuation);
    rows.forEach((r) => {
      const tr = el("tr");
      cols.forEach((c) => {
        const td = el("td", { className: c.align === "left" ? "left" : "" });
        const v = r[c.key];
        if (c.key === "ticker") {
          td.classList.add("ticker");
          td.appendChild(el("a", { href: "#/company/" + r.ticker, text: r.ticker }));
        } else if (c.key === "rev1M") {
          td.appendChild(revCell(v));
        } else if (c.key === "growth27" || c.key === "growth28" || c.key === "cagr2628") {
          td.appendChild(c.key === "cagr2628" ? cagrCell(v) : growthCell(v));
        } else if (c.key === "momentum") {
          td.appendChild(momentumCell(v));
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
    const year = state.chartYear || "2027E";
    const series = ((state.epsHistory[ticker] || {})[year] || []).filter((p) => p && !isMissing(p.eps));
    const colors = chartColors();

    state.chart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels: series.map((p) => p.date),
        datasets: [
          {
            label: ticker + " " + year + " EPS",
            data: series.map((p) => p.eps),
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
                return p && p.reportedFiscalLabel ? "FY: " + p.reportedFiscalLabel : "";
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
            title: { display: true, text: "Consensus EPS", color: colors.text, font: { size: 11 } },
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
        text: "Built from revision history Current EPS by calendar slot. One point per year is expected until subsequent snapshots.",
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
    ["2026E", "2027E", "2028E"].forEach((y) => {
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
    ["2026E", "2027E", "2028E"].forEach((y) => {
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

  /* ---------- earnings ---------- */
  function renderEarnings() {
    const root = el("div", { className: "section" });
    root.appendChild(el("h2", { className: "section-title", text: "Earnings" }));
    root.appendChild(
      el("p", {
        className: "section-note",
        text: "Digest stubs ready for post-report fills. Last/next dates from the latest snapshot.",
      })
    );

    const grid = el("div", { className: "earnings-grid" });
    state.watchlist.forEach((t) => {
      const e = state.earnings[t] || {};
      const c = state.companies[t] || {};
      const card = el("div", { className: "earn-card" });
      card.appendChild(
        el("h3", null, [
          el("a", { href: "#/company/" + t, text: t, style: "color:inherit;font-weight:700" }),
        ])
      );
      const dates = el("div", { className: "dates" });
      dates.appendChild(document.createTextNode("Last: "));
      dates.appendChild(isMissing(e.lastEarnings || c.lastEarnings) ? naCell() : document.createTextNode(e.lastEarnings || c.lastEarnings));
      dates.appendChild(document.createTextNode("  ·  Next: "));
      dates.appendChild(isMissing(e.nextEarnings || c.nextEarnings) ? naCell() : document.createTextNode(e.nextEarnings || c.nextEarnings));
      card.appendChild(dates);

      function stubSection(title, arr) {
        card.appendChild(el("div", { className: "stub-note", text: title }));
        const list = el("ul", { className: "earn-section-list" + (!arr || !arr.length ? " empty" : "") });
        if (!arr || !arr.length) {
          list.appendChild(el("li", { text: "No items yet" }));
        } else {
          arr.forEach((item) => list.appendChild(el("li", { text: typeof item === "string" ? item : JSON.stringify(item) })));
        }
        card.appendChild(list);
      }

      stubSection("Positives", e.positives);
      stubSection("Negatives", e.negatives);
      stubSection("Uncertainties", e.uncertainties);

      if (e.guidance) {
        card.appendChild(el("div", { className: "stub-note", text: "Guidance: " + e.guidance }));
      } else {
        card.appendChild(el("div", { className: "stub-note", text: "Guidance: —" }));
      }
      if (e.commentary) {
        card.appendChild(el("div", { className: "stub-note", text: e.commentary }));
      }

      grid.appendChild(card);
    });
    root.appendChild(grid);
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
      if (c.price != null) {
        chip.appendChild(document.createTextNode("  " + fmtNum(c.price, 2)));
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
    const years = ["2026E", "2027E", "2028E"];
    const datasets = years
      .map((y, i) => {
        const series = (hist[y] || []).filter((p) => p && !isMissing(p.eps));
        if (!series.length) return null;
        return {
          label: y,
          data: series.map((p) => ({ x: p.date, y: p.eps })),
          borderColor: palette[i % palette.length],
          backgroundColor: "transparent",
          pointRadius: 4,
          borderWidth: 2,
          tension: 0,
        };
      })
      .filter(Boolean);

    // Use category labels union
    const labels = Array.from(
      new Set(
        years.flatMap((y) => (hist[y] || []).map((p) => p.date).filter(Boolean))
      )
    ).sort();

    const ds2 = years
      .map((y, i) => {
        const series = hist[y] || [];
        const byDate = {};
        series.forEach((p) => {
          if (p && !isMissing(p.eps)) byDate[p.date] = p.eps;
        });
        const data = labels.map((d) => (byDate[d] != null ? byDate[d] : null));
        if (data.every((v) => v == null)) return null;
        return {
          label: y,
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

    state.chart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: { labels, datasets: ds2.length ? ds2 : datasets },
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
            title: { display: true, text: "Consensus EPS", color: colors.text },
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
    const priceEl = el("div", { className: "price" });
    if (isMissing(c.price)) priceEl.appendChild(naCell());
    else priceEl.textContent = "$" + fmtNum(c.price, 2);
    header.appendChild(priceEl);
    const bits = el("div", { className: "meta-bits" });
    bits.appendChild(momentumCell(c.momentum));
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
    kv("Last Earnings", isMissing(c.lastEarnings) ? null : c.lastEarnings);
    kv("Next Earnings", isMissing(c.nextEarnings) ? null : c.nextEarnings);
    kv("FY Note", isMissing(c.fyNote) ? null : c.fyNote);
    ["2026E", "2027E", "2028E", "2029E"].forEach((y) => {
      const e = (c.eps && c.eps[y]) || {};
      const cons = fmtNum(e.consensus, 2);
      const lab = e.reportedFiscalLabel ? " (" + e.reportedFiscalLabel + ")" : "";
      kv(y + " EPS", cons == null ? null : cons + lab);
      kv(y + " 1M Rev", revCell(e.rev1M));
    });
    const pe27 = pe(c.price, ((c.eps || {})["2027E"] || {}).consensus);
    kv("2027E PE", pe27 == null ? null : fmtNum(pe27, 2));
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
        const li = el("li");
        const left = el("span");
        left.textContent = d.name || "—";
        if (d.note) {
          left.title = d.note;
        }
        li.appendChild(left);
        const st = driverLabel(d.status);
        li.appendChild(el("span", { className: "driver-status " + st.cls, text: st.text }));
        ul.appendChild(li);
      });
    }
    drv.appendChild(ul);
    grid.appendChild(drv);
    root.appendChild(grid);

    /* chart */
    const chartPanel = el("div", { className: "panel section" });
    chartPanel.appendChild(el("h3", { text: "EPS History" }));
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
          (earn.nextEarnings || c.nextEarnings || "—"),
      })
    );
    ["positives", "negatives", "uncertainties"].forEach((key) => {
      const title = key.charAt(0).toUpperCase() + key.slice(1);
      earnPanel.appendChild(el("div", { className: "stub-note", text: title }));
      const list = el("ul", { className: "earn-section-list empty" });
      const arr = earn[key] || [];
      if (!arr.length) list.appendChild(el("li", { text: "No items yet" }));
      else arr.forEach((x) => list.appendChild(el("li", { text: String(x) })));
      earnPanel.appendChild(list);
    });
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
    state.sort = { key: "pe27", dir: "asc" };
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
