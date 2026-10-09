/*
 * Houseboat charge-manager dashboard cards — custom:houseboat-price-card
 * and custom:houseboat-status-card.
 *
 * These are NOT the ESS Manager HACS integration's cards — they're a
 * standalone adaptation of them (same rendering technique: a real SVG
 * price gradient, the header "big price" layout, extrema badges, a
 * status block with a progress bar, a slim history timeline), rewritten
 * from scratch against sensor.ess_charge_manager's own attributes
 * (charge_plan_*, pending_plan_*, price_limit_at_window_start, etc. —
 * see charge_system_sensor.yaml) instead of the ESS Manager's
 * card_plans/card_entities/battery_action/spike_plan/negative_price_plan
 * schema, since the houseboat only ever charges (no sell/discharge, no
 * spike or negative-price handling).
 *
 * INSTALL — no HACS and no integration needed, just a Lovelace resource:
 *   1. Copy this file into Home Assistant's config/www/ folder, e.g. as
 *      config/www/houseboat_cards.js.
 *   2. Settings -> Dashboards -> (⋮ top right) -> Resources -> Add
 *      Resource -> URL: /local/houseboat_cards.js, type: JavaScript
 *      Module.
 *   3. Add a card with type: custom:houseboat-price-card or
 *      custom:houseboat-status-card, entity: sensor.ess_charge_manager
 *      (both also work from the "Add card" picker, searching "ESS
 *      charge manager").
 *
 * Config (all optional besides entity):
 *   houseboat-price-card:  title, height, show_extremes, legend,
 *                          cheap_color, mid_color, high_color, buy_color,
 *                          limit_color (all [r,g,b] or "#rrggbb")
 *   houseboat-status-card: title, automation (an entity_id to show as a
 *                          toggle row — omit if you don't have one),
 *                          show_history, hours, buy_color, limit_color
 */

// -- Shared helpers ----------------------------------------------------------------
const UNIT_MS = 15 * 60000;
const HOUR_MS = 3600000;
const TWO_DAY_UNITS = (48 * HOUR_MS) / UNIT_MS; // 192 — the price chart's fixed today+tomorrow horizon
const _formats = {};

const I18N = {
  en: {
    now: "now", per_kwh: "/kWh", cheapest: "cheapest", most_expensive: "most expensive",
    legend_level: "price level", legend_charge: "Charge planned", legend_limit: "Price limit",
    no_prices: "No prices yet", no_charge: "No charge planned", planned: "planned", running: "running",
    left: (d) => `${d} left`, done: "done", stop: "stop", h: "h", min: "min", ago: (h) => `-${h} h`,
    status_title: "Charge", departure: "Departure",
    auto_control: "Automatic control",
  },
  nl: {
    now: "nu", per_kwh: "/kWh", cheapest: "goedkoopst", most_expensive: "duurst",
    legend_level: "prijsniveau", legend_charge: "Laden gepland", legend_limit: "Prijslimiet",
    no_prices: "Nog geen prijzen", no_charge: "Geen laadactie gepland", planned: "gepland", running: "bezig",
    left: (d) => `nog ${d}`, done: "klaar", stop: "stop", h: "u", min: "min", ago: (h) => `-${h} u`,
    status_title: "Charge", departure: "Vertrek",
    auto_control: "Automatische aansturing",
  },
};
function hassLang(hass) {
  return (hass && ((hass.locale && hass.locale.language) || hass.language)) || "en";
}
function hassTimeZone(hass) {
  const tz = hass && hass.config && hass.config.time_zone;
  return !tz || tz === "default" || tz === "Etc/UTC" || tz === "UTC" ? Intl.DateTimeFormat().resolvedOptions().timeZone : tz;
}
function tr(hass, key) {
  const table = I18N[hassLang(hass).slice(0, 2)] || I18N.en;
  return key in table ? table[key] : I18N.en[key];
}
function formats(hass) {
  const lang = hassLang(hass);
  const tz = hassTimeZone(hass);
  const currency = (hass && hass.config && hass.config.currency) || "EUR";
  const key = `${lang}|${tz}|${currency}`;
  if (_formats[key]) return _formats[key];
  const make = (options) => {
    try {
      return new Intl.DateTimeFormat(lang, { ...options, timeZone: tz });
    } catch (err) {
      return new Intl.DateTimeFormat("en", options);
    }
  };
  const time = make({ hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const weekday = make({ weekday: "short" });
  const dayLabel = make({ weekday: "short", day: "numeric" });
  const clock = make({ hour: "numeric", minute: "numeric", hourCycle: "h23" });
  let money;
  try {
    money = new Intl.NumberFormat(lang, { style: "currency", currency, minimumFractionDigits: 2, maximumFractionDigits: 2 });
  } catch (err) {
    money = new Intl.NumberFormat("en", { style: "currency", currency: "EUR", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  const numbers = {};
  _formats[key] = {
    time: (ms) => time.format(ms),
    weekday: (ms) => weekday.format(ms),
    dayLabel: (ms) => dayLabel.format(ms),
    clock: (ms) => {
      const parts = clock.formatToParts(ms);
      const get = (type) => Number((parts.find((p) => p.type === type) || {}).value || 0);
      return [get("hour") % 24, get("minute")];
    },
    num: (value, digits) => {
      if (!numbers[digits]) numbers[digits] = new Intl.NumberFormat(lang, { minimumFractionDigits: digits, maximumFractionDigits: digits });
      return numbers[digits].format(value);
    },
    money: (value) => money.format(value),
  };
  return _formats[key];
}
function localMidnight(hass, ms) {
  const [hour, minute] = formats(hass).clock(ms);
  return Math.floor(ms / 60000) * 60000 - (hour * 60 + minute) * 60000;
}
function nextMidnight(hass, ms) {
  let t = ms + 24 * HOUR_MS;
  const [hour] = formats(hass).clock(t);
  if (hour === 23) t += HOUR_MS;
  else if (hour === 1) t -= HOUR_MS;
  return t;
}
const NAMED_COLORS = { green: "#008000", yellow: "#ffff00", red: "#ff0000", blue: "#0000ff", orange: "#ffa500", purple: "#800080" };
function hexToRgb(hex) {
  const text = String(hex || "").trim();
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(NAMED_COLORS[text.toLowerCase()] || text);
  return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : null;
}
function toHex(color) {
  if (Array.isArray(color) && color.length === 3) {
    return "#" + color.map((c) => Math.min(Math.max(Math.round(Number(c) || 0), 0), 255).toString(16).padStart(2, "0")).join("");
  }
  const rgb = hexToRgb(color);
  return rgb ? toHex(rgb) : null;
}
function colorOptions(config, defaults, keys) {
  const opt = { ...defaults, ...config };
  for (const key of keys) opt[key] = toHex(opt[key]) || toHex(defaults[key]);
  return opt;
}
function esc(text) {
  return String(text == null ? "" : text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function tint(hex, alpha) {
  const rgb = hexToRgb(hex) || [128, 128, 128];
  return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`;
}
const r1 = (v) => Math.round(v * 10) / 10;
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
const num = (v) => (v === null || v === undefined || v === "" || !Number.isFinite(Number(v)) ? null : Number(v));

const ICONS = {
  up: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  down: '<path d="M12 5v14M6 13l6 6 6-6"/>',
  bolt: '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
  check: '<path d="M5 12l5 5 9-10"/>',
  sail: '<path d="M6 20h12M12 20V4l6 9H12M8 13l4 7"/>',
  robot: '<rect x="5" y="8" width="14" height="11" rx="2"/><path d="M12 4v4M9 13h.01M15 13h.01M9 16h6"/>',
};
function icon(name, color, size, width) {
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="${color}" stroke-width="${width || 2}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="flex-shrink:0">${ICONS[name]}</svg>`;
}

// Fetch recorder history for one entity: [[ms, state], ...], the first
// entry being the state at startMs.
async function fetchHistory(hass, entityId, startMs, endMs) {
  const result = await hass.callWS({
    type: "history/history_during_period",
    start_time: new Date(startMs).toISOString(),
    end_time: new Date(endMs).toISOString(),
    entity_ids: [entityId],
    minimal_response: true,
    no_attributes: true,
  });
  return ((result && result[entityId]) || []).map((item) => [
    Math.max((Number(item.lu != null ? item.lu : item.lc) || startMs / 1000) * 1000, startMs),
    item.s,
  ]);
}

function isHouseboatSensor(stateObj) {
  const a = stateObj && stateObj.attributes;
  return !!(a && "all_price" in a && "charge_plan_start_unit" in a && "price_limit_at_window_start" in a);
}
function notSensorMessage(entity, stateObj) {
  return `<ha-card><div style="padding:16px">${esc(entity)} ${stateObj ? "is not the houseboat charge-manager sensor (sensor.ess_charge_manager)." : "not found."}</div></ha-card>`;
}

// Reads the live/locked charge window the same way the sensor itself
// resolves start_unit/duration: the locked session plan while charging,
// the locked pending plan once one's latched, otherwise the live search
// preview — see charge_plan_start_unit / pending_plan_start_unit /
// start_unit in charge_system_sensor.yaml.
function chargeWindow(attrs) {
  const startUnit = attrs.charge_plan_start_unit != null ? Number(attrs.charge_plan_start_unit)
    : attrs.pending_plan_start_unit != null ? Number(attrs.pending_plan_start_unit)
    : Number(attrs.start_unit) || 0;
  const durationUnits = attrs.charge_plan_duration_units != null ? Number(attrs.charge_plan_duration_units)
    : attrs.pending_plan_duration_units != null ? Number(attrs.pending_plan_duration_units)
    : Number(attrs.planned_charge_time_units) || 0;
  const windowPrice = attrs.pending_plan_average_price != null ? Number(attrs.pending_plan_average_price) : (Number(attrs.charge_window_average_price) || 0);
  const priceLimit = attrs.charge_plan_price_limit != null ? Number(attrs.charge_plan_price_limit) : (Number(attrs.price_limit_at_window_start) || 0);
  const favorable = windowPrice <= priceLimit || !!attrs.is_soc_critical_at_window_start;
  const targetSoc = attrs.charge_plan_target_soc != null ? Number(attrs.charge_plan_target_soc)
    : attrs.pending_plan_target_soc != null ? Number(attrs.pending_plan_target_soc)
    : num(attrs.projected_end_soc);
  const isIdle = !attrs.is_charging && attrs.charge_plan_start_unit == null && attrs.pending_plan_start_unit == null && !attrs.has_charge_need;
  return { startUnit, durationUnits, favorable, targetSoc, isIdle, priceLimit, windowPrice };
}

const BASE_CSS = `
  :host { display: block; }
  ha-card { padding: 16px; display: flex; flex-direction: column; gap: 14px; overflow: hidden; container-type: inline-size; }
  .dim { color: var(--secondary-text-color, #a0a0a0); }
  .chart { position: relative; touch-action: pan-y; user-select: none; -webkit-user-select: none; }
  .chart svg { display: block; position: absolute; left: 0; top: 0; overflow: visible; filter: saturate(1.35) contrast(1.08); }
  .chart text { fill: var(--secondary-text-color, #a0a0a0); font-size: 10px; font-family: inherit; }
  .plabel { position: absolute; font-size: 10px; white-space: nowrap; pointer-events: none; transform: translateX(-50%); }
  .tip { position: absolute; display: none; flex-direction: column; gap: 4px; font-size: 12px; min-width: 132px; padding: 8px 10px; border-radius: 8px;
         background: var(--ha-card-background, var(--card-background-color, #2c2c2c)); border: 1px solid rgba(127,127,127,.35); box-shadow: 0 4px 14px rgba(0,0,0,.35); pointer-events: none; z-index: 2; }
  .tip .row { display: flex; align-items: center; gap: 6px; }
  .tip .row span.k { flex-grow: 1; color: var(--secondary-text-color, #a0a0a0); }
  .axisrow { position: relative; height: 14px; font-size: 11px; color: var(--secondary-text-color, #a0a0a0); }
  .axisrow span { position: absolute; white-space: nowrap; }
  .legend { display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 11px; color: var(--secondary-text-color, #a0a0a0); }
  .legend span { display: flex; align-items: center; gap: 5px; }
  .legend i { display: inline-block; width: 10px; height: 8px; border-radius: 2px; }
  .now { display: flex; flex-direction: column; gap: 2px; flex-grow: 1; min-width: 0; }
  .big { font-size: 28px; font-weight: 700; line-height: 1.1; white-space: nowrap; }
  .big small { font-size: 13px; font-weight: 500; color: var(--secondary-text-color, #a0a0a0); }
  .top { display: flex; align-items: flex-start; gap: 10px; }
  .ext { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
  .xp { display: flex; align-items: center; gap: 6px; font-size: 12px; padding: 4px 10px; border-radius: 999px; white-space: nowrap; }
  .xl { flex-grow: 1; overflow: hidden; text-overflow: ellipsis; }
  @container (max-width: 420px) { .top { flex-direction: column; align-items: stretch; } }
  .top + div { margin-top: -8px; }
`;

// -- Price card ---------------------------------------------------------------------
const PRICE_DEFAULTS = {
  title: "", height: 170, show_extremes: true, legend: true,
  cheap_color: [63, 174, 106], mid_color: [224, 195, 65], high_color: [240, 112, 60],
  buy_color: [79, 163, 247], limit_color: [160, 160, 160],
};
const PRICE_COLOR_KEYS = ["cheap_color", "mid_color", "high_color", "buy_color", "limit_color"];

class HouseboatPriceCard extends HTMLElement {
  static getStubConfig() {
    return { entity: "sensor.ess_charge_manager" };
  }
  static get info() {
    return { name: "Houseboat - Prices", description: "Today's and tomorrow's price, coloured by level, with the planned charge window." };
  }
  setConfig(config) {
    if (!config || !config.entity) throw new Error("Choose the houseboat charge-manager sensor (entity)");
    this._config = config;
    this._renderKey = null;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._render();
  }
  set hass(hass) {
    this._hass = hass;
    this._render();
  }
  getCardSize() {
    return 4;
  }
  connectedCallback() {
    this._timer = setInterval(() => {
      this._renderKey = null;
      this._render();
    }, 60000);
    if (!this._resize && window.ResizeObserver) {
      this._resize = new ResizeObserver(() => {
        const width = Math.floor(this.clientWidth);
        if (width && width !== this._width) {
          this._width = width;
          this._renderKey = null;
          this._render();
        }
      });
    }
    if (this._resize) this._resize.observe(this);
  }
  disconnectedCallback() {
    clearInterval(this._timer);
    if (this._resize) this._resize.disconnect();
  }
  _t(key) {
    return tr(this._hass, key);
  }
  _chartWidth() {
    const width = this._width || Math.floor(this.clientWidth) || 400;
    return Math.max(width - 32, 160);
  }
  _wireTooltip(show) {
    const layer = this.shadowRoot.querySelector(".hit");
    if (!layer) return;
    const at = (ev) => clamp(ev.clientX - layer.getBoundingClientRect().left, 0, layer.clientWidth);
    layer.addEventListener("pointermove", (ev) => {
      if (ev.pointerType === "mouse") show(at(ev));
    });
    layer.addEventListener("pointerleave", (ev) => {
      if (ev.pointerType === "mouse") show(null);
    });
    layer.addEventListener("pointerdown", (ev) => {
      if (ev.pointerType === "mouse") return;
      const x = at(ev);
      show(this._tipX != null && Math.abs(this._tipX - x) < 14 ? null : x);
    });
    if (this._tipX != null) show(this._tipX);
  }

  _render() {
    if (!this._hass || !this._config || !this.shadowRoot) return;
    const stateObj = this._hass.states[this._config.entity];
    if (!stateObj || !isHouseboatSensor(stateObj)) {
      this.shadowRoot.innerHTML = notSensorMessage(this._config.entity, stateObj);
      return;
    }
    const attrs = stateObj.attributes;
    const width = this._chartWidth();
    const key = [stateObj, width, hassLang(this._hass)];
    if (this._renderKey && key.every((v, i) => v === this._renderKey[i])) return;
    this._renderKey = key;

    const opt = colorOptions(this._config, PRICE_DEFAULTS, PRICE_COLOR_KEYS);
    const f = formats(this._hass);
    const now = Date.now();
    const T0 = localMidnight(this._hass, now);
    const title = esc(opt.title || this._t("status_title"));
    const prices = (attrs.all_price || []).map(num);
    if (!prices.length) {
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}</style><ha-card><div class="top"><span class="title">${title}</span></div><div class="dim">${esc(this._t("no_prices"))}</div></ha-card>`;
      return;
    }
    const unitMs = UNIT_MS;
    // Chart always spans a fixed today+tomorrow (2×24h) window from local
    // midnight, regardless of how much of `all_price` is actually published
    // yet (tomorrow's prices typically land mid-afternoon) - pad the tail
    // with null so the axis/grid always reach 48h out, same as the real
    // ESS Manager price card, instead of the chart shrinking to whatever
    // is currently known and widening again once tomorrow is published.
    const n = TWO_DAY_UNITS;
    prices.length = n; // pad (sparse "empty" slots) or truncate to exactly 48h
    for (let i = 0; i < n; i++) if (prices[i] === undefined) prices[i] = null;
    const T1 = T0 + n * unitMs;
    const cur = clamp(Math.floor((now - T0) / unitMs), 0, n - 1);
    const W = width;
    const win = chargeWindow(attrs);
    const hasWindow = !win.isIdle;
    const windowColor = win.favorable ? opt.buy_color : "#ff5252";
    const X0 = 34;
    const top = hasWindow ? 18 : 8;
    const H = Math.max(clamp(Number(opt.height) || 170, 100, 600) - 18, 60);
    const bottom = top + H;
    const valid = prices.filter((v) => v !== null);
    const limitVal = num(win.priceLimit);
    // The price limit isn't actually constant over time - it's a function
    // of SoC (soc_floor_percentage/soc_ceiling_percentage), and SoC itself
    // drains over time (battery_drain_rate_percent_per_hour). Project the
    // whole curve forward from the live reading, same formula the sensor
    // itself uses for price_limit_at_window_start, just evaluated at every
    // interval instead of only the charge window's own start. Falls back
    // to a flat line at limitVal when the sensor doesn't expose the two
    // passthrough attributes this needs yet (current_soc/price_14d_average
    // - added 2026-10, older charge_system_sensor.yaml won't have them).
    const floorSoc = num(attrs.soc_floor_percentage);
    const ceilingSoc = num(attrs.soc_ceiling_percentage);
    const drainPerUnit = (num(attrs.battery_drain_rate_percent_per_hour) || 0) / 4;
    const price14d = num(attrs.price_14d_average);
    const socNow = num(attrs.current_soc);
    const canProject = floorSoc !== null && ceilingSoc !== null && ceilingSoc > floorSoc && price14d !== null && socNow !== null;
    let lo = Math.min(0, ...valid, limitVal !== null ? limitVal : 0);
    let hi = Math.max(...valid, limitVal !== null ? limitVal : 0, canProject ? price14d : 0);
    const steps = [0.01, 0.02, 0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 5];
    const step = steps.find((s) => (hi - lo) / s <= 5) || 10;
    lo = Math.floor(lo / step + 1e-9) * step;
    hi = Math.ceil(hi / step - 1e-9) * step;
    if (hi <= lo) hi = lo + step;
    const bw = (W - X0) / n;
    const x = (t) => X0 + ((t - T0) / (T1 - T0)) * (W - X0);
    const xi = (i) => X0 + i * bw;
    const y = (v) => top + ((hi - v) / (hi - lo)) * H;
    const base = y(0);
    this._geo = { W, X0, top, H, bottom, bw, xi, y, T0, unitMs, n };
    this._prices = prices;

    const pmin = Math.max(0, Math.min(...valid));
    const pmax = Math.max(...valid, pmin + 0.001);
    const span = pmax - pmin;
    const off = (v) => `${clamp(((v - lo) / (hi - lo)) * 100, 0, 100).toFixed(2)}%`;
    const grad = `<linearGradient id="lvl" gradientUnits="userSpaceOnUse" x1="0" y1="${r1(y(lo))}" x2="0" y2="${r1(y(hi))}">
      <stop offset="0%" stop-color="${opt.cheap_color}"/><stop offset="${off(pmin + span * 0.3)}" stop-color="${opt.cheap_color}"/>
      <stop offset="${off(pmin + span * 0.6)}" stop-color="${opt.mid_color}"/><stop offset="${off(pmin + span * 0.9)}" stop-color="${opt.high_color}"/>
      <stop offset="100%" stop-color="${opt.high_color}"/></linearGradient>`;
    const gap = bw > 2.5 ? 0.5 : 0;
    const bars = (indexes) => indexes
      .filter((i) => prices[i] !== null)
      .map((i) => `M${r1(xi(i))} ${r1(base)}V${r1(y(prices[i]))}H${r1(xi(i) + bw - gap)}V${r1(base)}Z`)
      .join("");

    const winStartMs = T0 + win.startUnit * unitMs;
    const winStopMs = T0 + (win.startUnit + win.durationUnits) * unitMs;
    const inWindow = (i) => hasWindow && T0 + i * unitMs + unitMs > winStartMs && T0 + i * unitMs < winStopMs;
    const all = [...Array(n).keys()];
    let under = "";
    let streaks = "";
    let labels = "";
    if (hasWindow && winStopMs > winStartMs && winStopMs > T0 && winStartMs < T1) {
      const x0 = x(Math.max(winStartMs, T0));
      const x1 = x(Math.min(winStopMs, T1));
      under = `<rect x="${r1(x0 - 1)}" y="${top - 4}" width="${r1(Math.max(x1 - x0 + 2, 2))}" height="${r1(H + 4)}" rx="3" fill="${windowColor}" fill-opacity="0.22" stroke="${windowColor}" stroke-opacity="0.55" stroke-width="1"/>`;
      streaks = `<rect x="${r1(x0 - 1)}" y="${r1(bottom + 3)}" width="${r1(Math.max(x1 - x0 + 2, 2))}" height="3" rx="1.5" fill="${windowColor}"/>`;
      const text = `${esc(this._t("legend_charge"))} ${f.time(winStartMs)}`;
      labels = `<span class="plabel" style="left:${r1(clamp((x0 + x1) / 2, X0 + 30, W - 30))}px;top:0;color:${tint(windowColor, 1)}">${text}</span>`;
    }
    const bg = "var(--ha-card-background, var(--card-background-color, #1c1e24))";
    // Tomorrow's prices usually aren't published until mid-afternoon - the
    // chart still reserves the full 48h (see TWO_DAY_UNITS above), so mark
    // whatever tail is still unknown instead of just leaving it blank.
    let lastKnown = n - 1;
    while (lastKnown >= 0 && prices[lastKnown] === null) lastKnown--;
    if (lastKnown < n - 1) {
      const gapX0 = clamp(xi(lastKnown + 1), X0, W);
      labels += `<span class="dim" style="position:absolute;left:${r1((gapX0 + W) / 2)}px;top:${r1(top + H / 2)}px;transform:translate(-50%,-50%);font-size:12px;white-space:nowrap;background:${bg};padding:2px 8px;border-radius:6px">${esc(this._t("no_prices"))}</span>`;
    }
    let axis = "";
    for (let v = lo; v <= hi + step / 2; v += step) {
      axis += `<text x="0" y="${r1(y(v) + 3)}">${esc(f.num(v, step < 0.1 ? 2 : 1))}</text>`;
      axis += `<line x1="${X0}" y1="${r1(y(v))}" x2="${W}" y2="${r1(y(v))}" stroke="rgba(127,127,127,${Math.abs(v) < step / 2 ? 0.35 : 0.12})"/>`;
    }
    const xNow = x(now);
    let limitLine = "";
    if (canProject) {
      let d = "";
      for (let i = cur; i < n; i++) {
        const unitsAhead = i - cur;
        const projSoc = Math.max(socNow - drainPerUnit * unitsAhead, floorSoc);
        const mult = clamp(1 - (projSoc - floorSoc) / (ceilingSoc - floorSoc), 0, 1);
        const v = price14d * mult;
        d += `${i === cur ? "M" : "L"}${r1(xi(i) + bw / 2)} ${r1(y(v))}`;
      }
      if (d) limitLine = `<path d="${d}" fill="none" style="stroke:${opt.limit_color}" stroke-width="1.5" stroke-dasharray="4 3"/>`;
    } else if (limitVal !== null) {
      limitLine = `<line x1="${X0}" y1="${r1(y(limitVal))}" x2="${W}" y2="${r1(y(limitVal))}" style="stroke:${opt.limit_color}" stroke-width="1.5" stroke-dasharray="4 3"/>`;
    }
    const dayLine = n * unitMs > 26 * HOUR_MS ? nextMidnight(this._hass, T0) : null;
    const svg = `<defs>${grad}</defs>${axis}${under}
      <path d="${bars(all)}" fill="url(#lvl)" fill-opacity="0.9"/>
      <path d="${bars(all.filter(inWindow))}" fill="url(#lvl)"/>
      ${streaks}${limitLine}
      ${dayLine ? `<line x1="${r1(x(dayLine))}" y1="${top - 4}" x2="${r1(x(dayLine))}" y2="${r1(bottom + 8)}" stroke="rgba(127,127,127,.45)"/>` : ""}
      <line x1="${r1(xNow)}" y1="${top - 4}" x2="${r1(xNow)}" y2="${r1(bottom)}" style="stroke:var(--primary-text-color, #e8e8e8)" stroke-width="1.2"/>
      ${prices[cur] !== null ? `<circle cx="${r1(xNow)}" cy="${r1(y(prices[cur]))}" r="4" style="fill:var(--primary-text-color, #e8e8e8);stroke:${bg}" stroke-width="2"/>` : ""}`;

    let hours = "";
    for (let h = 0; T0 + h * HOUR_MS < T1; h += 6) {
      const t = T0 + h * HOUR_MS;
      const isDay = dayLine && Math.abs(t - dayLine) < HOUR_MS;
      if (h === 0 && !isDay) {
        hours += `<span style="left:${r1(x(t))}px">00</span>`;
        continue;
      }
      if (x(t) > W - 14) break;
      hours += `<span style="left:${r1(x(t) - 6)}px${isDay ? ";color:var(--primary-text-color, #e8e8e8);font-weight:600" : ""}">${esc(isDay ? f.dayLabel(t) : String(f.clock(t)[0]).padStart(2, "0"))}</span>`;
    }

    const nowP = prices[cur];
    let extremes = "";
    if (opt.show_extremes !== false) {
      let iMin = null;
      let iMax = null;
      for (let i = cur; i < n; i++) {
        if (prices[i] === null) continue;
        if (iMin === null || prices[i] < prices[iMin]) iMin = i;
        if (iMax === null || prices[i] > prices[iMax]) iMax = i;
      }
      const when = (i) => `${f.weekday(T0 + i * unitMs)} ${f.time(T0 + i * unitMs)}`;
      if (iMin !== null) {
        extremes = `<div class="ext">
          <span class="xp" style="background:${tint(opt.cheap_color, 0.16)}">${icon("down", opt.cheap_color, 14)}<span class="xl">${esc(this._t("cheapest"))} ${esc(when(iMin))}</span><b>${esc(f.money(prices[iMin]))}</b></span>
          <span class="xp" style="background:${tint(opt.high_color, 0.16)}">${icon("up", opt.high_color, 14)}<span class="xl">${esc(this._t("most_expensive"))} ${esc(when(iMax))}</span><b>${esc(f.money(prices[iMax]))}</b></span>
        </div>`;
      }
    }
    const legend = opt.legend === false ? "" : `<div class="legend">
      <span><i style="width:36px;background:linear-gradient(90deg, ${opt.cheap_color}, ${opt.mid_color}, ${opt.high_color})"></i>${esc(this._t("legend_level"))}</span>
      <span><i style="background:${tint(opt.buy_color, 0.6)}"></i>${esc(this._t("legend_charge"))}</span>
      ${limitVal !== null ? `<span><i style="width:12px;height:0;border-top:1.5px dashed ${opt.limit_color};border-radius:0"></i>${esc(this._t("legend_limit"))}</span>` : ""}
    </div>`;

    this.shadowRoot.innerHTML = `<style>${BASE_CSS}</style>
    <ha-card>
      <div class="top">
        <div class="now"><span class="dim" style="font-size:13px">${title} · ${esc(this._t("now"))}</span>
          <span class="big">${nowP === null ? "–" : esc(f.money(nowP))}<small> ${esc(this._t("per_kwh"))}</small></span>
          ${limitVal !== null ? `<span class="dim" style="font-size:12px">${esc(this._t("legend_limit"))} ${esc(f.money(limitVal))}</span>` : ""}</div>
        ${extremes}
      </div>
      <div>
        <div class="chart" style="width:${W}px;height:${r1(bottom + 8)}px">
          <svg width="${W}" height="${r1(bottom + 8)}" viewBox="0 0 ${W} ${r1(bottom + 8)}">${svg}</svg>
          <svg class="cross" width="${W}" height="${r1(bottom + 8)}" viewBox="0 0 ${W} ${r1(bottom + 8)}"></svg>
          ${labels}
          <div class="tip"></div>
          <div class="hit" style="position:absolute;left:${X0}px;top:${top}px;width:${r1(W - X0)}px;height:${r1(H)}px"></div>
        </div>
        <div class="axisrow" style="width:${W}px;margin-top:2px">${hours}</div>
      </div>
      ${legend}
    </ha-card>`;
    this._wireTooltip((px) => this._showTip(px));
  }

  _showTip(px) {
    const cross = this.shadowRoot.querySelector(".cross");
    const tip = this.shadowRoot.querySelector(".tip");
    if (!cross || !tip || !this._geo) return;
    this._tipX = px;
    if (px === null || px === undefined) {
      cross.innerHTML = "";
      tip.style.display = "none";
      return;
    }
    const g = this._geo;
    const f = formats(this._hass);
    const i = clamp(Math.floor(px / g.bw), 0, g.n - 1);
    const t = g.T0 + i * g.unitMs;
    const p = this._prices[i];
    const xx = g.xi(i) + g.bw / 2;
    cross.innerHTML = `<line x1="${r1(xx)}" y1="${g.top}" x2="${r1(xx)}" y2="${g.bottom}" style="stroke:var(--primary-text-color, #e8e8e8)" stroke-opacity="0.7" stroke-dasharray="3 3"/>${
      p === null ? "" : `<circle cx="${r1(xx)}" cy="${r1(g.y(p))}" r="4" style="fill:var(--primary-text-color, #e8e8e8)" stroke="rgba(0,0,0,.5)" stroke-width="1.5"/>`
    }`;
    let html = `<div class="row"><b>${esc(f.dayLabel(t))} · ${esc(f.time(t))}–${esc(f.time(t + g.unitMs))}</b></div>`;
    if (p !== null) html += `<div class="row"><span class="k">${esc(this._t("per_kwh"))}</span><b>${esc(f.money(p))}</b></div>`;
    tip.innerHTML = html;
    tip.style.display = "flex";
    const tipW = tip.offsetWidth || 150;
    tip.style.left = `${r1(xx + 12 + tipW > g.W ? Math.max(xx - 12 - tipW, 0) : xx + 12)}px`;
    tip.style.top = "0px";
  }
}

// -- Status card --------------------------------------------------------------------
const STATUS_DEFAULTS = {
  title: "ESS charge manager", automation: "", hours: 24, show_history: true,
  buy_color: [79, 163, 247], limit_color: [160, 160, 160],
};
const STATUS_COLOR_KEYS = ["buy_color", "limit_color"];
const TIMELINE_COLORS = {
  // "Wait for start" / "Start charge" / "Stop charge" deliberately share the
  // Standby color (2026-10) - Timo didn't want those as visually distinct
  // segments in the history timeline/status dot, just folded into Standby.
  Standby: "#3b3b3b", "Wait for start": "#3b3b3b", "Start charge": "#3b3b3b", "Stop charge": "#3b3b3b",
  // Actief/Cooldown swapped (2026-10) so charging reads as red here too,
  // matching the red the CHARGE block/price chart already use for an
  // unfavorable/forced charge (see `"#ff5252"` below and at the
  // `_chargeBlock`/`windowColor` definitions) - "100% mode" gets that same
  // vivid red (brighter than Actief's muted one) since it's a charging
  // state as well, just needs to stay visually distinct from plain Actief.
  Actief: "#9c3a2a", Cooldown: "#24527f", "100% mode": "#ff5252",
};

class HouseboatStatusCard extends HTMLElement {
  static getStubConfig() {
    return { entity: "sensor.ess_charge_manager" };
  }
  static get info() {
    return { name: "Houseboat - Status", description: "Charge plan with progress, price limit, departure countdown and a timeline." };
  }
  setConfig(config) {
    if (!config || !config.entity) throw new Error("Choose the houseboat charge-manager sensor (entity)");
    this._config = config;
    this._history = null;
    this._historyFor = null;
    this._renderKey = null;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._render();
  }
  set hass(hass) {
    this._hass = hass;
    this._render();
  }
  getCardSize() {
    return 5;
  }
  connectedCallback() {
    this._timer = setInterval(() => {
      this._renderKey = null;
      this._render();
    }, 60000);
  }
  disconnectedCallback() {
    clearInterval(this._timer);
  }
  _t(key) {
    return tr(this._hass, key);
  }
  _opt() {
    return colorOptions(this._config, STATUS_DEFAULTS, STATUS_COLOR_KEYS);
  }
  _num(value, digits) {
    return formats(this._hass).num(value, digits);
  }
  _duration(minutes) {
    const m = Math.max(Math.round(minutes), 0);
    const h = Math.floor(m / 60);
    const rest = String(m % 60).padStart(2, "0");
    return h ? `${h} ${this._t("h")} ${rest} ${this._t("min")}` : `${m % 60} ${this._t("min")}`;
  }

  async _loadHistory(entityId, hours) {
    const key = `${entityId}|${hours}`;
    const now = Date.now();
    if (this._historyFor === key && this._historyAt && now - this._historyAt < 300000) return;
    if (this._historyLoading) return;
    this._historyLoading = true;
    const obj = this._hass.states[entityId];
    this._historySeen = obj ? obj.last_changed : null;
    try {
      const start = now - hours * HOUR_MS;
      const items = await fetchHistory(this._hass, entityId, start, now);
      const segments = [];
      items.forEach(([from, state], i) => {
        const to = items[i + 1] ? Math.min(items[i + 1][0], now) : now;
        if (to > from) segments.push({ state, seconds: (to - from) / 1000, from, to });
      });
      this._history = segments;
    } catch (err) {
      this._history = [];
    } finally {
      this._historyFor = key;
      this._historyAt = Date.now();
      this._historyLoading = false;
      this._renderKey = null;
      this._render();
    }
  }

  // Charge block, adapted from the ESS Manager status card's Buy block:
  // empty state when the sensor itself is idle, otherwise start/stop
  // clock times, a progress bar (filled live while charging, previewing
  // the planned duration beforehand), and a kWh -> target SoC stat row.
  _chargeBlock(attrs, opt) {
    const f = formats(this._hass);
    const win = chargeWindow(attrs);
    const color = win.favorable ? opt.buy_color : "#ff5252";
    const head = `<div class="head"><span class="name" style="color:${color}">${icon("bolt", color, 16, 2.2)}${esc(this._t("status_title"))}</span></div>`;
    if (win.isIdle) {
      return `<div class="block" style="border-color:transparent">${head}<div class="empty">${esc(this._t("no_charge"))}</div></div>`;
    }
    const T0 = localMidnight(this._hass, Date.now());
    const startMs = T0 + win.startUnit * UNIT_MS;
    const stopMs = T0 + (win.startUnit + win.durationUnits) * UNIT_MS;
    const now = Date.now();
    const isCharging = !!attrs.is_charging;
    const minutes = (stopMs - startMs) / 60000;
    const energy = num(attrs.planned_charge_kwh_gross) || 0;
    let inBar;
    let below;
    if (isCharging) {
      const remainMin = Math.max((stopMs - now) / 60000, 0);
      inBar = esc(this._t("left")(this._duration(remainMin)));
      below = `${esc(this._t("running"))}`;
    } else {
      inBar = esc(this._duration(minutes));
      below = `${esc(this._t("planned"))} · ${esc(f.weekday(startMs))}`;
    }
    const fill = isCharging ? clamp(((now - startMs) / (stopMs - startMs)) * 100, 0, 100) : 0;
    const soc = win.targetSoc;
    return `<div class="block" style="border-color:${isCharging ? color : "transparent"}">${head}
      <div class="times"><span>${esc(f.time(startMs))}</span><span>${esc(f.time(stopMs))}</span></div>
      <div class="bar" style="background:${tint(color, 0.22)}"><div class="fill" style="width:${fill.toFixed(1)}%;background:${color}"></div><div class="bartext">${inBar}</div></div>
      <div class="below"><span>${below}</span><span>${esc(this._t("stop"))}</span></div>
      <div class="stat">${icon("bolt", color, 16)}<b>${this._num(energy, 1)}</b><span class="dim">kWh</span><span class="dim arrow">→</span>${
        soc == null ? "" : `<b>${this._num(soc, 0)} %</b>`
      }</div>
    </div>`;
  }

  // Price-limit tile (now vs. projected to the window's own start) and,
  // only when a voyage countdown is actually running, a departure tile —
  // the houseboat equivalents of the ESS Manager card's spike/negative
  // price tiles.
  _limitTile(attrs, opt) {
    const f = formats(this._hass);
    const nowLimit = num(attrs.price_limit);
    const winLimit = num(attrs.price_limit_at_window_start);
    const sameValue = nowLimit !== null && winLimit !== null && Math.abs(nowLimit - winLimit) < 0.0005;
    const detail = nowLimit === null ? "–"
      : sameValue || winLimit === null ? esc(f.money(nowLimit))
      : `${esc(f.money(nowLimit))} → ${esc(f.money(winLimit))}`;
    return `<div class="tile" style="background:rgba(127,127,127,.12)">${icon("bolt", "#a0a0a0", 20)}
      <div class="col"><span class="tname">${esc(this._t("legend_limit"))}</span><span class="tdetail">${detail}</span></div></div>`;
  }
  _voyageTile(attrs) {
    const hours = num(attrs.start_voyage_hours);
    if (hours === null || !attrs.is_voyage_pending) return "";
    const f = formats(this._hass);
    // start_voyage_hours is a whole-hour figure anchored to the top of
    // the current clock hour, not to this exact minute (2026-10, Timo:
    // at 10:52 with a departure of 5, the departure is 15:00, not
    // 15:52) - matches charge_system_sensor.yaml's same convention for
    // the voyage deadline used in its own planning horizon.
    const hourStart = Math.floor(Date.now() / HOUR_MS) * HOUR_MS;
    const when = f.time(hourStart + hours * HOUR_MS);
    return `<div class="tile" style="background:${tint("#b39ddb", 0.16)}">${icon("sail", "#b39ddb", 20)}
      <div class="col"><span class="tname">${esc(this._t("departure"))}</span><span class="tdetail" style="color:#b39ddb">${this._num(hours, 1)} ${esc(this._t("h"))} · ${esc(when)}</span></div></div>`;
  }

  _render() {
    if (!this._hass || !this._config || !this.shadowRoot) return;
    const opt = this._opt();
    const stateObj = this._hass.states[this._config.entity];
    if (!stateObj || !isHouseboatSensor(stateObj)) {
      this.shadowRoot.innerHTML = notSensorMessage(this._config.entity, stateObj);
      return;
    }
    const attrs = stateObj.attributes;
    const toggleId = this._config.automation || null;
    const toggleObj = toggleId ? this._hass.states[toggleId] : null;
    const key = [stateObj, toggleObj, this._history, hassLang(this._hass)];
    if (this._renderKey && key.every((v, i) => v === this._renderKey[i])) return;
    this._renderKey = key;
    if (opt.show_history && this._historyFor !== `${this._config.entity}|${opt.hours}` && !this._historyLoading) {
      this._loadHistory(this._config.entity, opt.hours);
    } else if (opt.show_history && this._historyAt && stateObj.last_changed !== this._historySeen) {
      this._historyAt = 0;
      this._loadHistory(this._config.entity, opt.hours);
    }

    let toggle = "";
    if (toggleObj) {
      const on = toggleObj.state === "on";
      const label = toggleObj.attributes.friendly_name || toggleId;
      toggle = `<div class="toggle">${icon("robot", "#a0a0a0", 18)}<span class="tlabel">${esc(label)}</span>
        <button type="button" class="switch ${on ? "on" : ""}" aria-pressed="${on}" aria-label="${esc(label)}" style="background:${on ? opt.buy_color : "#4a4a4a"}"><span></span></button></div>`;
    }

    let timeline = "";
    if (opt.show_history) {
      const f = formats(this._hass);
      const segs = this._history || [];
      const total = segs.reduce((sum, seg) => sum + seg.seconds, 0);
      const parts = total
        ? segs.map((seg) => {
            // Native title attribute -> hovering a segment shows a plain
            // browser tooltip with the status name and the time range it
            // covers, e.g. "Cooldown · 14:05–14:20" (2026-10, Timo asked for
            // a hover-to-see-status on the timeline strip).
            const tip = seg.from != null && seg.to != null ? `${seg.state} · ${f.time(seg.from)}–${f.time(seg.to)}` : seg.state;
            return `<span title="${esc(tip)}" style="flex:${seg.seconds} 0 0;background:${TIMELINE_COLORS[seg.state] || "#2a2a2a"}"></span>`;
          }).join("")
        : `<span style="flex:1 0 0;background:#2a2a2a"></span>`;
      const hours = opt.hours;
      timeline = `<div class="timeline">${parts}</div>
        <div class="axis"><span>${esc(this._t("ago")(hours))}</span><span>${esc(this._t("ago")(Math.round(hours / 2)))}</span><span>${esc(this._t("now"))}</span></div>`;
    }

    const voyage = this._voyageTile(attrs);

    this.shadowRoot.innerHTML = `<style>
      ha-card { padding: 16px; display: flex; flex-direction: column; gap: 14px; container-type: inline-size; }
      .top { display: flex; align-items: center; gap: 10px; }
      .title { font-size: 17px; font-weight: 600; flex-grow: 1; }
      .status { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--secondary-text-color, #a0a0a0); }
      .status i { width: 8px; height: 8px; border-radius: 999px; display: inline-block; }
      .layout { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; align-items: stretch; }
      .side-col { display: flex; flex-direction: column; gap: 10px; }
      @container (max-width: 380px) { .layout { grid-template-columns: minmax(0, 1fr); } }
      .block { display: flex; flex-direction: column; gap: 10px; border: 2px solid transparent; border-radius: 12px; padding: 8px; background: rgba(127,127,127,.12); }
      .head { display: flex; align-items: center; gap: 6px; }
      .name { display: flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; flex-grow: 1; }
      .empty { font-size: 13px; color: var(--secondary-text-color, #a0a0a0); padding: 18px 0; text-align: center; }
      .times, .below { display: flex; justify-content: space-between; }
      .times { font-size: 13px; }
      .below { font-size: 11px; color: var(--secondary-text-color, #a0a0a0); }
      .bar { position: relative; height: 22px; border-radius: 6px; overflow: hidden; }
      .fill { position: absolute; left: 0; top: 0; bottom: 0; }
      .bartext { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; gap: 4px; font-size: 12px; font-weight: 600; color: #ffffff; text-shadow: 0 0 3px rgba(0,0,0,.6); }
      .stat { display: flex; align-items: center; gap: 6px; white-space: nowrap; background: rgba(127,127,127,.12); border-radius: 10px; padding: 7px 10px; }
      .stat b { font-size: 15px; }
      .dim { font-size: 12px; color: var(--secondary-text-color, #a0a0a0); }
      .arrow { font-size: 13px; padding: 0 2px; }
      .tile { border-radius: 10px; padding: 10px 12px; display: flex; align-items: center; gap: 10px; }
      .col { display: flex; flex-direction: column; min-width: 0; }
      .tname { font-size: 13px; font-weight: 600; }
      .tdetail { font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--secondary-text-color, #a0a0a0); }
      .toggle { display: flex; align-items: center; gap: 10px; border-top: 1px solid rgba(127,127,127,.2); padding-top: 10px; }
      .tlabel { font-size: 13px; flex-grow: 1; }
      .switch { width: 44px; height: 26px; border-radius: 999px; border: none; position: relative; padding: 0; cursor: pointer; }
      .switch span { position: absolute; top: 3px; left: 3px; width: 20px; height: 20px; border-radius: 999px; background: #ffffff; transition: left .15s; }
      .switch.on span { left: 21px; }
      .timeline { display: flex; height: 14px; border-radius: 999px; overflow: hidden; }
      .axis { display: flex; justify-content: space-between; font-size: 11px; color: var(--secondary-text-color, #a0a0a0); margin-top: -8px; }
    </style>
    <ha-card>
      <div class="top"><span class="title">${esc(opt.title)}</span><span class="status"><i style="background:${TIMELINE_COLORS[stateObj.state] || "#8a8a8a"}"></i>${esc(stateObj.state)}</span></div>
      <div class="layout">
        ${this._chargeBlock(attrs, opt)}
        <div class="side-col">${this._limitTile(attrs, opt)}${voyage}</div>
      </div>
      ${toggle}
      ${timeline}
    </ha-card>`;
    const button = this.shadowRoot.querySelector(".switch");
    if (button && toggleId) {
      button.addEventListener("click", () => this._hass.callService("homeassistant", "toggle", { entity_id: toggleId }));
    }
  }
}

// -- Registration -------------------------------------------------------------------
window.customCards = window.customCards || [];
for (const [type, cls] of [
  ["houseboat-price-card", HouseboatPriceCard],
  ["houseboat-status-card", HouseboatStatusCard],
]) {
  if (customElements.get(type)) continue;
  customElements.define(type, cls);
  window.customCards.push({ type, name: cls.info.name, description: cls.info.description, preview: true });
}
console.info("%c Houseboat charge-manager cards %c loaded ", "color:#fff;background:#4caf50", "");
