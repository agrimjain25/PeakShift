/* PeakShift front end. No libraries: charts are plain SVG. */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const SVG_NS = 'http://www.w3.org/2000/svg';

  const state = {
    config: null,
    mode: 'preset',
    presetId: 'heatwave',
    manualStart: '',
    manualDirty: false,
    csvText: '',
    csvName: '',
    result: null,
    baseline: null,
    ref: null,
    lastKey: null,
    cursor: 0,
    playing: false,
    timer: null,
    busy: false,
    queued: false,
    hover: -1,
  };

  /* ------------------------------------------------------------ helpers --- */
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function svg(tag, attrs, parent, text) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const key in attrs) node.setAttribute(key, attrs[key]);
    if (text !== undefined) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  }

  const hhmm = (ts) => ts.slice(11, 16);
  const fmt1 = (n) => Number(n).toFixed(1);
  const fmt0 = (n) => Math.round(Number(n)).toString();
  const pct = (n) => (n >= 0.995 ? '>99%' : n <= 0.005 ? '<1%' : Math.round(n * 100) + '%');
  const niceMax = (v) => Math.ceil(v / 50) * 50;

  function dateLabel(ts) {
    const d = new Date(ts.slice(0, 10) + 'T00:00:00Z');
    return d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
  }

  function safeStorage(op, key, value) {
    try {
      return op === 'get' ? window.localStorage.getItem(key) : window.localStorage.setItem(key, value);
    } catch (e) {
      return null;
    }
  }

  const REASON_TEXT = {
    DEMAND_ABOVE_160_KW: 'demand above 160 kW',
    PEAK_WINDOW_18_22: 'inside the 18:00 to 22:59 window',
    DEMAND_AT_OR_BELOW_160_KW_OUTSIDE_PEAK_WINDOW: 'demand at or below 160 kW, outside the window',
  };
  const reasonsText = (list) => list.map((r) => REASON_TEXT[r] || r).join(' and ');
  const MODE_NAME = { MAIN_GRID: 'Main grid', RENEWABLE_STORAGE: 'Renewable storage' };

  /* -------------------------------------------------------------- theme --- */
  function initTheme() {
    const saved = safeStorage('get', 'peakshift-theme');
    if (saved === 'light' || saved === 'dark') document.documentElement.dataset.theme = saved;
    $('theme-toggle').addEventListener('click', () => {
      const current = document.documentElement.dataset.theme ||
        (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
      const next = current === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = next;
      safeStorage('set', 'peakshift-theme', next);
    });
  }

  /* ---------------------------------------------------------------- api --- */
  async function request(url, options) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 60000);
    let response;
    try {
      response = await fetch(url, Object.assign({ signal: controller.signal }, options));
    } catch (e) {
      const err = new Error(e.name === 'AbortError' ? 'The forecast took too long. Try again.' : 'Could not reach the PeakShift server.');
      err.kind = 'network';
      throw err;
    } finally {
      clearTimeout(timer);
    }
    let body = null;
    try { body = await response.json(); } catch (e) { /* not JSON */ }
    if (!response.ok) {
      const err = new Error((body && body.error) || 'The server answered with an error (' + response.status + ').');
      err.kind = (body && body.kind) || 'server';
      throw err;
    }
    return body;
  }

  function batteryFromForm() {
    const num = (id) => Number($(id).value);
    return {
      capacity_kwh: num('b-capacity'),
      initial_soc_pct: num('b-initial'),
      pv_kwp: num('b-pv'),
      reserve_pct: num('b-reserve'),
      max_discharge_kw: num('b-discharge'),
      max_charge_kw: num('b-charge'),
    };
  }

  function currentRows() {
    if (state.mode === 'preset') return presetById(state.presetId).rows;
    return manualRows();
  }

  function inputKey() {
    if (state.mode === 'preset') return 'p:' + state.presetId;
    if (state.mode === 'csv') return 'c:' + state.csvName + ':' + state.csvText.length;
    return 'm:' + JSON.stringify(manualRows());
  }

  function inputLabel() {
    if (state.mode === 'preset') return 'preset:' + state.presetId;
    return state.mode === 'csv' ? 'csv' : 'form';
  }

  function fetchRun(delta, log) {
    const battery = batteryFromForm();
    if (state.mode === 'csv') {
      if (!state.csvText) {
        const err = new Error('Choose a CSV file first.');
        err.kind = 'intake';
        return Promise.reject(err);
      }
      const q = new URLSearchParams();
      Object.keys(battery).forEach((k) => q.set(k, battery[k]));
      q.set('temp_delta_c', delta);
      if (!log) q.set('log', '0');
      return request('/api/forecast?' + q.toString(), { method: 'POST', headers: { 'Content-Type': 'text/csv' }, body: state.csvText });
    }
    return request('/api/forecast', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rows: currentRows(), battery, temp_delta_c: delta, source: inputLabel(), log }),
    });
  }

  async function run() {
    if (state.busy) { state.queued = true; return; }
    state.busy = true;
    setBusy(true);
    $('form-msg').textContent = '';
    try {
      const delta = Number($('stress').value);
      const key = inputKey();
      const result = await fetchRun(delta, true);
      if (delta === 0) {
        state.ref = { key, result };
        state.baseline = null;
      } else {
        if (!state.ref || state.ref.key !== key) {
          try { state.ref = { key, result: await fetchRun(0, false) }; } catch (e) { state.ref = null; }
        }
        state.baseline = state.ref ? state.ref.result : null;
      }
      adopt(result, key);
    } catch (err) {
      showError(err);
    } finally {
      state.busy = false;
      setBusy(false);
      if (state.queued) { state.queued = false; run(); }
    }
  }

  function setBusy(on) {
    const btn = $('run');
    btn.disabled = on;
    btn.textContent = on ? 'Asking the model' : 'Run forecast';
    if (on) setPill('wait', 'Contacting model');
  }

  /* ----------------------------------------------------- result adoption --- */
  function adopt(result, key) {
    hideError();
    state.result = result;
    const hours = result.hours;
    if (state.lastKey !== key || state.cursor >= hours.length) {
      let peak = 0;
      hours.forEach((h, i) => { if (h.demand_kw > hours[peak].demand_kw) peak = i; });
      state.cursor = peak;
    }
    state.lastKey = key;
    stop();
    const scrub = $('scrub');
    scrub.max = String(hours.length - 1);
    scrub.value = String(state.cursor);
    renderAll(true);
    loadRuns();
  }

  function renderAll(animate) {
    renderPill();
    renderBanner();
    renderKpis();
    renderAdvisories();
    renderHero(animate);
    renderSoc(animate);
    renderSignals();
    renderTable();
    renderChecks();
    renderModel();
    renderState();
  }

  function setPill(kind, text) {
    const pill = $('model-pill');
    pill.className = 'pill pill-' + kind;
    pill.textContent = text;
  }

  function renderPill() {
    const m = state.result.model;
    if (m.status === 'live') setPill('live', 'Model live · ' + m.latency_ms + ' ms');
    else setPill('cached', 'Saved result · model unreachable');
  }

  function showError(err) {
    const box = $('error');
    box.replaceChildren(el('strong', '', err.kind === 'intake' ? 'Check the weather data' : 'No forecast this time'), el('span', '', err.message));
    box.hidden = false;
    $('form-msg').textContent = err.kind === 'intake' ? err.message : '';
    if (String(err.kind).indexOf('model_') === 0 || err.kind === 'network') setPill('down', 'Model unreachable');
    else if (state.result) renderPill();
    else setPill('down', 'No forecast yet');
  }

  function hideError() { $('error').hidden = true; }

  /* ------------------------------------------------------------- banner --- */
  function renderBanner() {
    const box = $('banner');
    const res = state.result;
    if (!res.alerts.length) { box.hidden = true; return; }
    const first = res.alerts[0];
    const span = hhmm(first.start) + (first.hours > 1 ? ' to ' + hhmm(first.end) : '');
    const more = res.alerts.length > 1 ? ' Another ' + (res.alerts.length - 1) + ' overload window' + (res.alerts.length > 2 ? 's follow' : ' follows') + '.' : '';
    const body = el('div');
    body.append(
      el('strong', '', 'Critical overload expected ' + span),
      el('span', '', 'Demand peaks at ' + fmt0(res.summary.peak_kw) + ' kW and is at or above 200 kW for ' + res.summary.overload_hours + ' hour' + (res.summary.overload_hours === 1 ? '' : 's') + ' of this window.' + more)
    );
    box.replaceChildren(body);
    box.hidden = false;
  }

  /* --------------------------------------------------------------- KPIs --- */
  function kpi(label, value, unit, sub, cls) {
    const box = el('div', 'kpi' + (cls ? ' ' + cls : ''));
    const big = el('b', '', value);
    if (unit) big.append(el('small', '', unit));
    box.append(el('span', 'k-l', label), big, el('span', 'k-s', sub));
    return box;
  }

  function renderKpis() {
    const s = state.result.summary;
    const box = $('kpis');
    const worst = state.result.hours.reduce((a, h) => (h.battery.soc_pct < a.battery.soc_pct ? h : a), state.result.hours[0]);
    box.replaceChildren(
      kpi('Peak demand', fmt0(s.peak_kw), ' kW', 'at ' + hhmm(s.peak_timestamp), s.peak_kw >= 200 ? 'kpi-alert' : ''),
      kpi('Critical overload', String(s.overload_hours), s.overload_hours === 1 ? ' hour' : ' hours', s.overload_hours ? 'at or above 200 kW' : 'none expected', s.overload_hours ? 'kpi-alert' : ''),
      kpi('On renewable storage', String(s.storage_hours), ' of ' + s.hours + ' h', s.grid_hours + ' hours on the main grid', ''),
      kpi('Grid draw at its peak', fmt0(s.grid_peak_with_storage_kw), ' kW', 'was ' + fmt0(s.grid_peak_without_storage_kw) + ' kW without storage', ''),
      kpi('Lowest battery charge', fmt0(s.min_soc_pct), '%', s.depleted_at ? 'reserve reached at ' + hhmm(s.depleted_at) : 'at ' + hhmm(worst.timestamp), s.depleted_at ? 'kpi-alert' : ''),
      kpi('Close calls', String(s.uncertain_hours), s.uncertain_hours === 1 ? ' hour' : ' hours', 'a small forecast error could flip them', '')
    );
  }

  function renderAdvisories() {
    const list = $('advisories');
    const items = state.result.advisories.slice();
    if (!items.some((a) => a.level === 'critical' || a.level === 'warning')) {
      items.unshift({ level: 'ok', text: 'No critical overload is forecast, and the battery holds through the window.' });
    }
    list.replaceChildren(...items.map((a) => el('li', 'lv-' + a.level, a.text)));
  }

  /* ------------------------------------------------------- hero chart --- */
  function runsOfPeakWindow(hours) {
    const runs = [];
    let start = null;
    hours.forEach((h, i) => {
      const inside = h.hour >= 18 && h.hour <= 22;
      if (inside && start === null) start = i;
      if (!inside && start !== null) { runs.push([start, i]); start = null; }
    });
    if (start !== null) runs.push([start, hours.length]);
    return runs;
  }

  function buildTip(h, extra) {
    const tip = el('div');
    const head = el('div', 'tip-row');
    head.append(el('b', '', hhmm(h.timestamp)), el('span', 'tip-dim', dateLabel(h.timestamp)));
    tip.append(head);
    const rows = [
      ['Demand', fmt1(h.demand_kw) + ' kW'],
      ['Likely range', fmt0(h.band_low_kw) + ' to ' + fmt0(h.band_high_kw) + ' kW'],
      ['Source', MODE_NAME[h.mode]],
    ];
    if (h.alert) rows.push(['Alert', 'Critical overload']);
    if (extra) extra.forEach((r) => rows.push(r));
    rows.forEach((r) => {
      const row = el('div', 'tip-row');
      row.append(el('span', 'tip-dim', r[0]), el('span', '', r[1]));
      tip.append(row);
    });
    tip.append(el('div', 'tip-dim', reasonsText(h.reasons)));
    return tip;
  }

  function showTip(node, evt) {
    const tip = $('tip');
    tip.replaceChildren(node);
    tip.hidden = false;
    const pad = 14;
    const rect = tip.getBoundingClientRect();
    let left = evt.clientX + pad;
    let top = evt.clientY - rect.height - 10;
    if (left + rect.width > window.innerWidth - 8) left = evt.clientX - rect.width - pad;
    if (top < 8) top = evt.clientY + pad;
    tip.style.left = Math.max(8, left) + 'px';
    tip.style.top = top + 'px';
  }

  function hideTip() { $('tip').hidden = true; }

  function renderHero(animate) {
    const host = $('hero-chart');
    const res = state.result;
    if (!res) return;
    const hours = res.hours;
    const n = hours.length;
    const W = Math.max(300, host.clientWidth || 760);
    const compact = W < 520;
    const m = { l: compact ? 38 : 46, r: 12, t: 22 };
    const plotH = compact ? 210 : 270;
    const ribbonY = m.t + plotH + 14;
    const ribbonH = compact ? 24 : 30;
    const H = ribbonY + ribbonH + 26;
    const innerW = W - m.l - m.r;
    const slab = innerW / n;
    const base = state.baseline ? state.baseline.hours : null;
    $('legend-base').hidden = !(base && base.length === n);
    let top = Math.max(250, ...hours.map((h) => h.band_high_kw));
    if (base) top = Math.max(top, ...base.map((h) => h.band_high_kw));
    const ymax = niceMax(top);
    const X = (i) => m.l + slab * i;
    const Y = (v) => m.t + plotH * (1 - v / ymax);

    const root = svg('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': 'Predicted demand by hour with the chosen source for each hour', class: animate ? '' : 'noanim' });
    const t = res.rules;

    for (let v = 0; v <= ymax; v += 50) {
      svg('line', { class: 'gridline', x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }, root);
      svg('text', { class: 'ax', x: m.l - 8, y: Y(v) + 4, 'text-anchor': 'end' }, root, String(v));
    }
    svg('text', { class: 'ax', x: m.l - 8, y: 11, 'text-anchor': 'end' }, root, 'kW');

    runsOfPeakWindow(hours).forEach((r) => {
      svg('rect', { class: 'window', x: X(r[0]), y: m.t, width: slab * (r[1] - r[0]), height: plotH }, root);
      if (slab * (r[1] - r[0]) > 96) svg('text', { class: 'window-label', x: X(r[0]) + 8, y: m.t + plotH - 8 }, root, 'Peak window');
    });

    svg('line', { class: 'thr-storage', x1: m.l, x2: W - m.r, y1: Y(t.storage_threshold_kw), y2: Y(t.storage_threshold_kw) }, root);
    svg('line', { class: 'thr-alert', x1: m.l, x2: W - m.r, y1: Y(t.overload_threshold_kw), y2: Y(t.overload_threshold_kw) }, root);
    svg('text', { class: 'thr-storage-t', x: m.l + 6, y: Y(t.storage_threshold_kw) - 6 }, root, compact ? '160' : '160 kW: storage above this');
    svg('text', { class: 'thr-alert-t', x: m.l + 6, y: Y(t.overload_threshold_kw) - 6 }, root, compact ? '200' : '200 kW: critical');

    const cx = (i) => X(i) + slab / 2;
    let band = hours.map((h, i) => (i ? 'L' : 'M') + cx(i) + ' ' + Y(h.band_high_kw)).join(' ');
    band += hours.slice().reverse().map((h, k) => 'L' + cx(n - 1 - k) + ' ' + Y(h.band_low_kw)).join(' ') + 'Z';
    svg('path', { class: 'band', d: band }, root);

    if (base && base.length === n) {
      svg('path', { class: 'line-base', d: base.map((h, i) => (i ? 'L' : 'M') + cx(i) + ' ' + Y(h.demand_kw)).join(' ') }, root);
    }
    const line = svg('path', { class: 'line' + (animate ? ' draw' : ''), d: hours.map((h, i) => (i ? 'L' : 'M') + cx(i) + ' ' + Y(h.demand_kw)).join(' ') }, root);
    if (animate) line.style.setProperty('--len', String(Math.ceil(line.getTotalLength ? line.getTotalLength() : 2000)));

    hours.forEach((h, i) => {
      svg('circle', { class: h.mode === 'RENEWABLE_STORAGE' ? 'dot-storage' : 'dot-grid', cx: cx(i), cy: Y(h.demand_kw), r: n > 48 ? 2.5 : 5 }, root);
      if (h.alert && n <= 48) svg('circle', { class: 'dot-ring', cx: cx(i), cy: Y(h.demand_kw), r: 8.5 }, root);
    });

    const step = n <= 24 ? (compact ? 3 : 1) : n <= 72 ? 6 : 24;
    hours.forEach((h, i) => {
      const cls = (h.mode === 'RENEWABLE_STORAGE' ? 'slab-storage' : 'slab-grid') + ' slab';
      const r = svg('rect', { class: cls, x: X(i) + 1, y: ribbonY, width: Math.max(1, slab - 2), height: ribbonH, rx: Math.min(4, slab / 3), style: '--i:' + i }, root);
      r.setAttribute('data-i', i);
      if (h.alert) svg('rect', { class: 'slab-alert slab', x: X(i) + 1, y: ribbonY - 6, width: Math.max(1, slab - 2), height: 4, rx: 2, style: '--i:' + i }, root);
      if (h.hour % step === 0) svg('text', { class: 'ax', x: cx(i), y: ribbonY + ribbonH + 16, 'text-anchor': 'middle' }, root, (n <= 24 && !compact ? String(h.hour).padStart(2, '0') : String(h.hour).padStart(2, '0') + 'h'));
      if (n > 24 && h.hour === 0) svg('text', { class: 'ax ax-strong', x: X(i) + 2, y: ribbonY + ribbonH + 16 }, root, dateLabel(h.timestamp).split(' ').slice(0, 2).join(' '));
    });
    svg('text', { class: 'ax', x: m.l - 8, y: ribbonY + ribbonH / 2 + 4, 'text-anchor': 'end' }, root, 'Source');

    const cur = Math.min(state.cursor, n - 1);
    svg('line', { class: 'cursor-line', x1: cx(cur), x2: cx(cur), y1: m.t, y2: ribbonY + ribbonH + 4 }, root);
    svg('circle', { class: 'cursor-ring', cx: cx(cur), cy: Y(hours[cur].demand_kw), r: 9 }, root);
    svg('rect', { class: 'cursor-cap', x: cx(cur) - 21, y: ribbonY + ribbonH + 2, width: 42, height: 18, rx: 9 }, root);
    svg('text', { class: 'cursor-cap-t', x: cx(cur), y: ribbonY + ribbonH + 15 }, root, hhmm(hours[cur].timestamp));

    const hit = svg('rect', { class: 'hit', x: m.l, y: m.t, width: innerW, height: ribbonY + ribbonH - m.t }, root);
    const indexAt = (evt) => {
      const box = root.getBoundingClientRect();
      const px = (evt.clientX - box.left) * (W / box.width) - m.l;
      return Math.max(0, Math.min(n - 1, Math.floor(px / slab)));
    };
    const extraFor = (h) => [
      ['Sure about source', pct(h.mode_confidence)],
      ['Battery', fmt0(h.battery.soc_pct) + '%'],
    ];
    hit.addEventListener('pointermove', (evt) => { const i = indexAt(evt); showTip(buildTip(hours[i], extraFor(hours[i])), evt); });
    hit.addEventListener('pointerleave', hideTip);
    hit.addEventListener('click', (evt) => { stop(); setCursor(indexAt(evt)); });

    host.replaceChildren(root);
  }

  /* -------------------------------------------------------- battery chart --- */
  function renderSoc(animate) {
    const host = $('soc-chart');
    const res = state.result;
    if (!res) return;
    const hours = res.hours;
    const n = hours.length;
    const W = Math.max(280, host.clientWidth || 520);
    const compact = W < 460;
    const m = { l: compact ? 36 : 44, r: 10, t: 12 };
    const socH = compact ? 120 : 150;
    const barsY = m.t + socH + 22;
    const barsH = compact ? 76 : 90;
    const H = barsY + barsH + 24;
    const innerW = W - m.l - m.r;
    const slab = innerW / n;
    const X = (i) => m.l + slab * i;
    const cx = (i) => X(i) + slab / 2;
    const Ys = (p) => m.t + socH * (1 - p / 100);
    const root = svg('svg', { viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': 'Battery charge level by hour', class: animate ? '' : 'noanim' });

    [0, 50, 100].forEach((p) => {
      svg('line', { class: 'gridline', x1: m.l, x2: W - m.r, y1: Ys(p), y2: Ys(p) }, root);
      svg('text', { class: 'ax', x: m.l - 6, y: Ys(p) + 4, 'text-anchor': 'end' }, root, p + '%');
    });
    const reserve = res.battery_config.reserve_pct;
    svg('line', { class: 'reserve-line', x1: m.l, x2: W - m.r, y1: Ys(reserve), y2: Ys(reserve) }, root);
    const pts = hours.map((h, i) => [cx(i), Ys(h.battery.soc_pct)]);
    svg('path', { class: 'soc-area', d: 'M' + pts[0][0] + ' ' + Ys(0) + pts.map((p) => ' L' + p[0] + ' ' + p[1]).join('') + ' L' + pts[n - 1][0] + ' ' + Ys(0) + 'Z' }, root);
    const line = svg('path', { class: 'soc-line' + (animate ? ' draw' : ''), d: pts.map((p, i) => (i ? 'L' : 'M') + p[0] + ' ' + p[1]).join(' ') }, root);
    if (animate) line.style.setProperty('--len', String(Math.ceil(line.getTotalLength ? line.getTotalLength() : 1500)));

    const maxKw = Math.max(50, ...hours.map((h) => Math.max(h.battery.discharge_kw, h.battery.charge_kw)));
    const mid = barsY + barsH / 2;
    const scale = (barsH / 2 - 2) / maxKw;
    svg('line', { class: 'axis-line', x1: m.l, x2: W - m.r, y1: mid, y2: mid }, root);
    svg('text', { class: 'ax', x: m.l - 6, y: mid - barsH / 2 + 10, 'text-anchor': 'end' }, root, 'out');
    svg('text', { class: 'ax', x: m.l - 6, y: mid + barsH / 2 - 2, 'text-anchor': 'end' }, root, 'in');
    hours.forEach((h, i) => {
      const dis = h.battery.discharge_kw * scale;
      const chg = h.battery.charge_kw * scale;
      if (dis > 0.5) svg('rect', { class: 'bar-dis', x: X(i) + 1, y: mid - dis, width: Math.max(1, slab - 2), height: dis }, root);
      if (chg > 0.5) svg('rect', { class: 'bar-chg', x: X(i) + 1, y: mid, width: Math.max(1, slab - 2), height: chg }, root);
      const step = n <= 24 ? (compact ? 6 : 3) : n <= 72 ? 12 : 24;
      if (h.hour % step === 0) svg('text', { class: 'ax', x: cx(i), y: H - 6, 'text-anchor': 'middle' }, root, String(h.hour).padStart(2, '0') + 'h');
    });

    const cur = Math.min(state.cursor, n - 1);
    svg('line', { class: 'cursor-line', x1: cx(cur), x2: cx(cur), y1: m.t, y2: barsY + barsH }, root);
    svg('circle', { class: 'cursor-ring', cx: cx(cur), cy: Ys(hours[cur].battery.soc_pct), r: 6 }, root);

    const hit = svg('rect', { class: 'hit', x: m.l, y: m.t, width: innerW, height: barsY + barsH - m.t }, root);
    const indexAt = (evt) => {
      const box = root.getBoundingClientRect();
      const px = (evt.clientX - box.left) * (W / box.width) - m.l;
      return Math.max(0, Math.min(n - 1, Math.floor(px / slab)));
    };
    hit.addEventListener('pointermove', (evt) => {
      const h = hours[indexAt(evt)];
      const b = h.battery;
      const tip = el('div');
      const head = el('div', 'tip-row');
      head.append(el('b', '', hhmm(h.timestamp)), el('span', 'tip-dim', MODE_NAME[h.mode]));
      tip.append(head);
      [['Charge', fmt0(b.soc_pct) + '% (' + fmt0(b.soc_kwh) + ' kWh)'], ['Discharging', fmt0(b.discharge_kw) + ' kW'], ['Charging', fmt0(b.charge_kw) + ' kW'], ['Solar', fmt0(b.pv_kw) + ' kW'], ['Grid help', fmt0(b.grid_assist_kw) + ' kW']].forEach((r) => {
        const row = el('div', 'tip-row');
        row.append(el('span', 'tip-dim', r[0]), el('span', '', r[1]));
        tip.append(row);
      });
      showTip(tip, evt);
    });
    hit.addEventListener('pointerleave', hideTip);
    hit.addEventListener('click', (evt) => { stop(); setCursor(indexAt(evt)); });

    host.replaceChildren(root);
  }

  /* ------------------------------------------------------ active state --- */
  function renderState() {
    const res = state.result;
    if (!res) return;
    const h = res.hours[Math.min(state.cursor, res.hours.length - 1)];
    $('st-clock').textContent = hhmm(h.timestamp);
    $('st-date').textContent = dateLabel(h.timestamp);
    const badge = $('st-badge');
    const storage = h.mode === 'RENEWABLE_STORAGE';
    badge.className = 'badge ' + (storage ? 'badge-storage' : 'badge-grid');
    badge.replaceChildren(el('i'), el('span', '', MODE_NAME[h.mode]));
    $('st-demand').textContent = fmt1(h.demand_kw);
    $('st-range').textContent = 'likely ' + fmt0(h.band_low_kw) + ' to ' + fmt0(h.band_high_kw);
    $('st-soc').textContent = fmt0(h.battery.soc_pct);
    const b = h.battery;
    $('st-flow').textContent = b.discharge_kw > 0.5 ? 'discharging ' + fmt0(b.discharge_kw) + ' kW' : b.charge_kw > 0.5 ? 'charging ' + fmt0(b.charge_kw) + ' kW' : 'idle';
    $('st-soc-fill').style.width = Math.max(0, Math.min(100, b.soc_pct)) + '%';
    $('st-why').textContent = 'Why: ' + reasonsText(h.reasons) + '.';
    $('st-sure').textContent = 'Sure about the source: ' + pct(h.mode_confidence) + '.';
    const chips = $('st-chips');
    chips.replaceChildren();
    if (h.alert) chips.append(el('span', 'chip chip-alert', 'Critical overload'));
    else if (h.alert_uncertain) chips.append(el('span', 'chip chip-watch', 'Overload chance ' + pct(h.p_alert)));
    if (h.mode_uncertain) chips.append(el('span', 'chip chip-watch', 'Close call'));
    if (b.grid_assist_kw > 0.5) chips.append(el('span', 'chip chip-watch', 'Grid helping ' + fmt0(b.grid_assist_kw) + ' kW'));
    $('scrub').value = String(state.cursor);
    document.querySelectorAll('#hours-body tr').forEach((tr) => tr.classList.toggle('is-cursor', Number(tr.dataset.i) === state.cursor));
  }

  function setCursor(i) {
    const n = state.result ? state.result.hours.length : 0;
    state.cursor = Math.max(0, Math.min(n - 1, i));
    renderState();
    renderHero(false);
    renderSoc(false);
  }

  /* -------------------------------------------------------------- replay --- */
  function setPlayIcon(playing) {
    $('play-icon').setAttribute('d', playing ? 'M7 5h4v14H7zM13 5h4v14h-4z' : 'M8 5v14l11-7z');
    $('play-label').textContent = playing ? 'Pause' : 'Replay the day';
  }

  function stop() {
    state.playing = false;
    clearInterval(state.timer);
    setPlayIcon(false);
  }

  function togglePlay() {
    if (!state.result) return;
    if (state.playing) { stop(); return; }
    const last = state.result.hours.length - 1;
    if (state.cursor >= last) setCursor(0);
    state.playing = true;
    setPlayIcon(true);
    state.timer = setInterval(() => {
      if (state.cursor >= last) { stop(); return; }
      setCursor(state.cursor + 1);
      if (state.cursor >= last) stop();
    }, 850);
  }

  /* ------------------------------------------------------------- signals --- */
  function renderSignals() {
    const res = state.result;
    const items = [];
    res.transitions.forEach((t) => {
      const cls = t.signal === 'SWITCH_TO_RENEWABLE_STORAGE' ? 'to-storage' : t.signal === 'SWITCH_TO_MAIN_GRID' ? 'to-grid' : '';
      items.push({ ts: t.timestamp, order: 0, name: t.signal, cls, why: (t.signal === 'SET_INITIAL_MODE' ? 'Start on ' + MODE_NAME[t.to_mode].toLowerCase() + ': ' : '') + reasonsText(t.reasons) + ' (' + fmt0(t.demand_kw) + ' kW).' });
    });
    res.alerts.forEach((a) => {
      items.push({ ts: a.start, order: 1, name: a.signal, cls: 'is-alert', why: (a.hours > 1 ? hhmm(a.start) + ' to ' + hhmm(a.end) + ', ' : '') + 'peak ' + fmt0(a.peak_kw) + ' kW at ' + hhmm(a.peak_timestamp) + '.' });
    });
    items.sort((a, b) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : a.order - b.order));
    $('signals').replaceChildren(...items.map((it) => {
      const li = el('li');
      li.append(el('span', 'sig-time', hhmm(it.ts)), el('span', 'sig-name ' + it.cls, it.name), el('span', 'sig-why', it.why));
      return li;
    }));
    $('sig-json').textContent = JSON.stringify(res.signals, null, 2);
  }

  /* --------------------------------------------------------------- table --- */
  function renderTable() {
    const res = state.result;
    const multiDay = res.hours.length > 24;
    $('hours-body').replaceChildren(...res.hours.map((h, i) => {
      const tr = el('tr');
      tr.dataset.i = String(i);
      const hour = el('td', '', (multiDay ? h.timestamp.slice(5, 10) + ' ' : '') + hhmm(h.timestamp));
      const demand = el('td', 'num', fmt1(h.demand_kw));
      const range = el('td', 'num dim', fmt0(h.band_low_kw) + ' to ' + fmt0(h.band_high_kw));
      const src = el('td');
      const chip = el('span', 'src ' + (h.mode === 'RENEWABLE_STORAGE' ? 's-storage' : 's-grid'));
      chip.append(el('i'), document.createTextNode(MODE_NAME[h.mode]));
      src.append(chip);
      const alert = el('td', h.alert ? 'al' : 'dim', h.alert ? 'Critical' : 'None');
      const sure = el('td', 'num', pct(h.mode_confidence));
      const b = h.battery;
      const batt = el('td', 'num', fmt0(b.soc_pct) + '%' + (b.discharge_kw > 0.5 ? ' ↓' : b.charge_kw > 0.5 ? ' ↑' : ''));
      const notes = [];
      if (h.mode_uncertain || h.alert_uncertain) notes.push('Close call');
      if (b.grid_assist_kw > 0.5) notes.push('Grid helping ' + fmt0(b.grid_assist_kw) + ' kW');
      const note = el('td', 'dim', notes.join(', '));
      tr.append(hour, demand, range, src, alert, sure, batt, note);
      tr.addEventListener('click', () => { stop(); setCursor(i); });
      return tr;
    }));
  }

  function download(name, text, type) {
    const blob = new Blob([text], { type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function scheduleCsv() {
    const rows = ['timestamp,predicted_demand_kw,source,critical_overload,battery_charge_pct,battery_discharge_kw,battery_charge_kw,grid_assist_kw,sure_about_source'];
    state.result.hours.forEach((h) => {
      rows.push([h.timestamp, h.demand_kw, h.mode, h.alert ? 'yes' : 'no', h.battery.soc_pct, h.battery.discharge_kw, h.battery.charge_kw, h.battery.grid_assist_kw, h.mode_confidence].join(','));
    });
    return rows.join('\n') + '\n';
  }

  /* ----------------------------------------------- data check, model, log --- */
  const CHECK_ICON = 'M5 12.5l4.5 4.5L19 7.5';
  const FIX_ICON = 'M12 5v8m0 4v.5';

  function checkItem(kind, text) {
    const li = el('li', kind);
    const icon = svg('svg', { viewBox: '0 0 24 24', 'aria-hidden': 'true' });
    svg('path', { d: kind === 'ok' ? CHECK_ICON : FIX_ICON }, icon);
    li.append(icon, el('span', '', text));
    return li;
  }

  function renderChecks() {
    const c = state.result.cleaning;
    const items = [checkItem('ok', c.rows_used + ' of ' + c.rows_in + ' rows read and used.')];
    const total = (o) => Object.keys(o).reduce((s, k) => s + o[k], 0);
    const names = { ambient_temp_c: 'temperature', humidity_pct: 'humidity', solar_irradiance_wm2: 'solar' };
    const detail = (o) => Object.keys(o).filter((k) => o[k]).map((k) => o[k] + ' ' + names[k]).join(', ');
    let fixes = 0;
    if (total(c.invalid_to_missing)) { items.push(checkItem('fix', 'Treated ' + total(c.invalid_to_missing) + ' impossible reading(s) as missing (' + detail(c.invalid_to_missing) + ').')); fixes++; }
    if (total(c.filled)) { items.push(checkItem('fix', 'Filled ' + total(c.filled) + ' missing value(s) from the hours around them (' + detail(c.filled) + ').')); fixes++; }
    if (total(c.clipped)) { items.push(checkItem('fix', 'Clipped ' + total(c.clipped) + ' value(s) that were just outside their valid range.')); fixes++; }
    if (c.duplicates_removed) { items.push(checkItem('fix', 'Removed ' + c.duplicates_removed + ' duplicate hour(s).')); fixes++; }
    if (c.unsorted_fixed) { items.push(checkItem('fix', 'Put the rows in time order.')); fixes++; }
    if (c.dropped_bad_timestamp) { items.push(checkItem('fix', 'Skipped ' + c.dropped_bad_timestamp + ' row(s) with an unreadable time.')); fixes++; }
    if (c.gaps) { items.push(checkItem('fix', c.gaps + ' step(s) between rows are not exactly one hour.')); fixes++; }
    if (c.truncated) { items.push(checkItem('fix', 'Used the first ' + c.rows_used + ' rows and ignored ' + c.truncated + ' more.')); fixes++; }
    if (c.what_if_temp_delta_c) {
      const humid = state.config ? state.config.what_if.humidity_per_degree * c.what_if_temp_delta_c : 0;
      items.push(checkItem('fix', 'Stress test: every hour ' + (c.what_if_temp_delta_c > 0 ? '+' : '') + c.what_if_temp_delta_c + ' °C, humidity ' + (humid > 0 ? '+' : '') + humid.toFixed(1) + ' points.'));
    }
    if (!fixes) items.push(checkItem('ok', 'No missing, duplicate or out-of-range values.'));
    if (c.rows_used !== 24) items.push(checkItem('fix', 'A full forecast window is 24 hours; this one has ' + c.rows_used + '.'));
    $('dq').replaceChildren(...items);
  }

  function renderModel() {
    const m = state.result.model;
    const card = state.config ? state.config.model : null;
    const box = $('model-box');
    const grid = el('div', 'model-grid');
    [['RMSE', fmt1(m.rmse_kw) + ' kW'], ['Average miss', fmt1(m.mae_kw) + ' kW'], ['R²', m.r2.toFixed(3)]].forEach((p) => {
      const d = el('div');
      d.append(el('b', '', p[1]), el('span', '', p[0]));
      grid.append(d);
    });
    const last = m.status === 'live'
      ? 'Last forecast: live from the endpoint in ' + m.latency_ms + ' ms (' + (m.mode === 'batch' ? 'one request' : 'one request per hour') + ').'
      : 'Last forecast: a saved copy from ' + (m.saved_at || 'an earlier run') + ', because the endpoint could not be reached.';
    box.replaceChildren(
      grid,
      el('p', '', card ? card.name + '.' : 'Regression model on Zoho Catalyst QuickML.'),
      el('p', 'dim', 'Scores are QuickML’s own, measured on data held back during training. ' + (card ? 'Trained on ' + card.trained_on + '.' : '')),
      el('p', 'dim', last)
    );
  }

  async function loadRuns() {
    try {
      const data = await request('/api/runs');
      $('log-store').textContent = 'Stored in: ' + data.storage + '. Latest first.';
      $('log-body').replaceChildren(...data.runs.slice(0, 12).map((r) => {
        const tr = el('tr');
        const time = new Date(r.created_at);
        const delta = r.temp_delta_c ? ' ' + (r.temp_delta_c > 0 ? '+' : '') + r.temp_delta_c + '°' : '';
        tr.append(el('td', '', isNaN(time) ? r.created_at : time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })),
          el('td', '', String(r.source).replace('preset:', '') + delta),
          el('td', 'num', fmt0(r.peak_kw) + ' kW'),
          el('td', 'num', String(r.overload_hours)));
        return tr;
      }));
    } catch (e) { /* the log is optional */ }
  }

  /* --------------------------------------------------- inputs: presets etc --- */
  function presetById(id) {
    return state.config.presets.find((p) => p.id === id) || state.config.presets[0];
  }

  function sparkline(rows) {
    const w = 76, h = 34;
    const temps = rows.map((r) => r.ambient_temp_c);
    const lo = Math.min(...temps), hi = Math.max(...temps);
    const pts = temps.map((t, i) => [(w * i) / (temps.length - 1), h - 3 - ((t - lo) / Math.max(1, hi - lo)) * (h - 6)]);
    const s = svg('svg', { class: 'p-spark', viewBox: '0 0 ' + w + ' ' + h, 'aria-hidden': 'true' });
    svg('path', { d: pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ') }, s);
    return s;
  }

  function renderPresets() {
    const list = $('preset-list');
    list.replaceChildren(...state.config.presets.map((p) => {
      const label = el('label', 'preset');
      const input = document.createElement('input');
      input.type = 'radio';
      input.name = 'preset';
      input.value = p.id;
      input.checked = p.id === state.presetId;
      input.addEventListener('change', () => {
        state.presetId = p.id;
        state.manualDirty = false;
        fillManualFromPreset();
        run();
      });
      const when = new Date(p.date + 'T00:00:00Z').toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
      label.append(input, el('span', 'p-name', p.name), sparkline(p.rows), el('span', 'p-meta', when + ' · avg ' + p.mean_temp_c + ' °C · max ' + fmt1(p.max_temp_c) + ' °C'));
      return label;
    }));
  }

  function toLocalInput(ts) { return ts.slice(0, 16).replace(' ', 'T'); }

  function fillManualFromPreset() {
    if (state.manualDirty) return;
    const rows = presetById(state.presetId).rows;
    $('manual-start').value = toLocalInput(rows[0].timestamp);
    const body = $('manual-body');
    body.replaceChildren(...rows.map((r, i) => {
      const tr = el('tr');
      tr.append(el('td', '', ''));
      [['ambient_temp_c', r.ambient_temp_c], ['humidity_pct', r.humidity_pct], ['solar_irradiance_wm2', r.solar_irradiance_wm2]].forEach((f) => {
        const td = el('td');
        const input = document.createElement('input');
        input.type = 'number';
        input.step = 'any';
        input.value = f[1];
        input.dataset.f = f[0];
        input.setAttribute('aria-label', f[0].replace(/_/g, ' ') + ' for hour ' + i);
        input.addEventListener('input', () => { state.manualDirty = true; });
        td.append(input);
        tr.append(td);
      });
      return tr;
    }));
    labelManualHours();
  }

  function startMillis() {
    const v = $('manual-start').value;
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2})/.exec(v || '');
    return m ? Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4]) : null;
  }

  function labelManualHours() {
    const start = startMillis();
    document.querySelectorAll('#manual-body tr').forEach((tr, i) => {
      if (start === null) { tr.firstChild.textContent = String(i); return; }
      const d = new Date(start + i * 3600000);
      tr.firstChild.textContent = d.toISOString().slice(5, 10) + ' ' + d.toISOString().slice(11, 16);
    });
  }

  function manualRows() {
    const start = startMillis();
    return Array.from(document.querySelectorAll('#manual-body tr')).map((tr, i) => {
      const row = { timestamp: start === null ? '' : new Date(start + i * 3600000).toISOString().slice(0, 19).replace('T', ' ') };
      tr.querySelectorAll('input').forEach((input) => { row[input.dataset.f] = input.value === '' ? null : Number(input.value); });
      return row;
    });
  }

  function selectTab(name) {
    state.mode = name;
    ['preset', 'manual', 'csv'].forEach((t) => {
      const on = t === name;
      $('tab-' + t).setAttribute('aria-selected', String(on));
      $('tab-' + t).tabIndex = on ? 0 : -1;
      $('pane-' + t).hidden = !on;
    });
    if (name === 'manual') fillManualFromPreset();
  }

  function initTabs() {
    ['preset', 'manual', 'csv'].forEach((name, idx, all) => {
      const tab = $('tab-' + name);
      tab.addEventListener('click', () => { selectTab(name); if (name === 'preset') run(); });
      tab.addEventListener('keydown', (e) => {
        if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
        const next = all[(idx + (e.key === 'ArrowRight' ? 1 : all.length - 1)) % all.length];
        selectTab(next);
        $('tab-' + next).focus();
      });
    });
  }

  async function loadCsvFile(file) {
    if (!file) return;
    if (file.size > 900000) { showError(Object.assign(new Error('That file is larger than 1 MB.'), { kind: 'intake' })); return; }
    state.csvText = await file.text();
    state.csvName = file.name;
    const lines = state.csvText.split(/\r?\n/).filter((l) => l.trim()).length - 1;
    $('csv-title').textContent = file.name;
    $('csv-sub').textContent = lines + ' data row' + (lines === 1 ? '' : 's') + '. Choose another file to replace it.';
    run();
  }

  function initCsv() {
    const input = $('csv-file');
    input.addEventListener('change', () => loadCsvFile(input.files[0]));
    const drop = $('drop');
    ['dragenter', 'dragover'].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add('over'); }));
    ['dragleave', 'drop'].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove('over'); }));
    drop.addEventListener('drop', (ev) => loadCsvFile(ev.dataTransfer.files[0]));
    $('csv-template').addEventListener('click', () => {
      const rows = presetById(state.presetId).rows;
      const text = 'timestamp,ambient_temp_c,humidity_pct,solar_irradiance_wm2\n' + rows.map((r) => [r.timestamp, r.ambient_temp_c, r.humidity_pct, r.solar_irradiance_wm2].join(',')).join('\n') + '\n';
      download('peakshift_template.csv', text, 'text/csv');
    });
  }

  function initStress() {
    const slider = $('stress');
    let timer = null;
    const label = () => {
      const v = Number(slider.value);
      $('stress-out').textContent = (v > 0 ? '+' : '') + v + ' °C';
      const hum = (state.config ? state.config.what_if.humidity_per_degree : -1.19) * v;
      $('stress-hint').textContent = v === 0
        ? 'Moves every hour’s temperature, and humidity with it, then asks the model again.'
        : 'Every hour ' + (v > 0 ? 'warmer' : 'cooler') + ' by ' + Math.abs(v) + ' °C, humidity ' + (hum > 0 ? 'up ' : 'down ') + Math.abs(hum).toFixed(1) + ' points, as on real days in the training data.';
    };
    slider.addEventListener('input', label);
    slider.addEventListener('change', () => { clearTimeout(timer); timer = setTimeout(run, 150); });
    label();
  }

  function fillBattery() {
    const d = state.config.battery_defaults;
    $('b-capacity').value = d.capacity_kwh;
    $('b-initial').value = d.initial_soc_pct;
    $('b-pv').value = d.pv_kwp;
    $('b-reserve').value = d.reserve_pct;
    $('b-discharge').value = d.max_discharge_kw;
    $('b-charge').value = d.max_charge_kw;
  }

  /* -------------------------------------------------------------- start --- */
  function initButtons() {
    $('scenario').addEventListener('submit', (e) => { e.preventDefault(); run(); });
    $('play').addEventListener('click', togglePlay);
    $('scrub').addEventListener('input', (e) => { stop(); setCursor(Number(e.target.value)); });
    $('manual-start').addEventListener('input', () => { state.manualDirty = true; labelManualHours(); });
    $('dl-csv').addEventListener('click', () => { if (state.result) download('peakshift_schedule.csv', scheduleCsv(), 'text/csv'); });
    $('sig-json-toggle').addEventListener('click', (e) => {
      const pre = $('sig-json');
      pre.hidden = !pre.hidden;
      e.currentTarget.setAttribute('aria-expanded', String(!pre.hidden));
    });
    $('sig-copy').addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      if (!state.result) return;
      const text = JSON.stringify(state.result.signals, null, 2);
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = 'Copied';
      } catch (err) {
        const pre = $('sig-json');
        pre.hidden = false;
        const range = document.createRange();
        range.selectNodeContents(pre);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        btn.textContent = 'Press Ctrl+C';
      }
      setTimeout(() => { btn.textContent = 'Copy'; }, 1800);
    });
    $('hero-chart').addEventListener('keydown', (e) => {
      if (!state.result) return;
      const last = state.result.hours.length - 1;
      const keys = { ArrowRight: state.cursor + 1, ArrowLeft: state.cursor - 1, Home: 0, End: last };
      if (!(e.key in keys)) return;
      e.preventDefault();
      stop();
      setCursor(keys[e.key]);
    });
    window.addEventListener('scroll', hideTip, { passive: true });
    let frame = null;
    const redraw = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(() => { if (state.result) { renderHero(false); renderSoc(false); } }); };
    if ('ResizeObserver' in window) {
      const ro = new ResizeObserver(redraw);
      ro.observe($('hero-chart'));
      ro.observe($('soc-chart'));
    } else {
      window.addEventListener('resize', redraw);
    }
  }

  async function start() {
    initTheme();
    initTabs();
    initCsv();
    initStress();
    initButtons();
    try {
      state.config = await request('/api/config');
    } catch (err) {
      showError(err);
      return;
    }
    fillBattery();
    renderPresets();
    fillManualFromPreset();
    if (!state.config.model_credentials_configured) {
      setPill('down', 'Model credentials not set');
    }
    run();
  }

  start();
})();
