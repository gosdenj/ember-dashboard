/* Ember - the browser side.
 *
 * One payload arrives over Server-Sent Events, and the whole dashboard is
 * rebuilt from it. There is no framework and no build step: this file is
 * served as it is.
 *
 * The three slides are Heating, Gas and Today. Each one names its source
 * on the panel itself, because a number without a source is a number
 * nobody can trust.
 */

'use strict';

const SLIDE_TITLES = {
  heating: 'Home · Hive thermostat',
  gas: 'Home · Octopus gas',
  today: 'Home · today',
};

const state = {
  payload: null,
  settings: null,
  index: 0,
  slides: ['heating', 'gas', 'today'],
  rotation: 20,
  timer: null,
  pinned: false,
  // Which view of the gas the panel is showing. The gas panel opens on
  // the week of days, because "how much" is what the gas slide is for.
  gasTab: 'week',
};

/* -- small helpers ------------------------------------------------------- */

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function num(value, digits) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return Number(value).toFixed(digits === undefined ? 1 : digits);
}

function money(value) {
  if (value === null || value === undefined) return '—';
  return '£' + Number(value).toFixed(2);
}

function hours(minutes) {
  if (minutes === null || minutes === undefined) return '—';
  const total = Math.round(minutes);
  const h = Math.floor(total / 60);
  const m = total % 60;
  return h + 'h ' + String(m).padStart(2, '0') + 'm';
}

function clockStamp(iso) {
  if (!iso) return '';
  const when = new Date(iso);
  const use24 = !state.settings || state.settings.clock_24h !== false;
  return when.toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', hour12: !use24,
  });
}

/* The clock: the day of the week, the date and the time.
 *
 * The LIVE pill says whether the dashboard is talking to the real
 * accounts. The time belongs in a clock of its own, and it wants the
 * date with it - "Fri 6 Nov · 08:53" answers "is this today?" without
 * anyone having to work it out.
 */
function clockFull(when) {
  if (!when) return '';
  const day = when.toLocaleDateString([], { weekday: 'short' });
  const date = when.toLocaleDateString([], { day: 'numeric', month: 'short' });
  return `${day} ${date} \u00b7 ${clockStamp(when.toISOString())}`;
}

function clockTime(value) {
  /* Hive gives a boost end as a timestamp; show it as a clock time. */
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'string' && /^\d{1,2}:\d{2}/.test(value)) return value;
  let when = null;
  if (typeof value === 'number') {
    /* Seconds or milliseconds - Hive has used both. */
    when = new Date(value > 1e11 ? value : value * 1000);
  } else {
    when = new Date(value);
  }
  if (isNaN(when.getTime())) return String(value);
  return when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}

function dayLabel(value) {
  if (!value) return '';
  const when = new Date(value + 'T00:00:00');
  return when.toLocaleDateString([], { weekday: 'short' });
}

/* -- shared pieces ------------------------------------------------------- */

function head(title) {
  const payload = state.payload || {};
  const live = payload.mode === 'live';
  const problems = (payload.errors || []).length;
  return `<div class="slide-head">
    <div class="mark"></div>
    <span class="brand">EMBER</span>
    <span class="sub">${esc(title)}</span>
    <span class="spacer"></span>
    ${live ? '' : '<span class="pill demo">DEMO</span>'}
    ${problems ? `<span class="pill warn">${problems} issue${problems > 1 ? 's' : ''}</span>` : ''}
    ${live ? '<span class="pill live"><i></i>LIVE</span>' : ''}
    <span class="pill clock" id="clock">${esc(clockFull(new Date()))}</span>
    <span class="hdots" id="dots"></span>
  </div>`;
}

function footerNote(text) {
  return `<div class="foot">${esc(text)}</div>`;
}

function problemsNote() {
  const errors = (state.payload && state.payload.errors) || [];
  if (!errors.length) return '';
  const first = errors[0];
  return `<div class="fnote">${esc(first.source)}: ${esc(first.message)}</div>`;
}

/** The problem with one source, if there is one.
 *
 * An empty panel with no explanation is the worst thing a dashboard can
 * do: it looks like there is nothing to report, when in fact something
 * went wrong. So the reason is shown in the panel that is missing it. */
function problemFor(source) {
  const errors = (state.payload && state.payload.errors) || [];
  for (const error of errors) {
    if (error.source === source) return error.message;
  }
  return '';
}

/** A panel that says what went wrong, instead of sitting empty. */
function trouble(text, hint) {
  if (!text) return '';
  return `<div class="trouble">
    <div class="t">${esc(text)}</div>
    ${hint ? `<div class="h">${esc(hint)}</div>` : ''}
  </div>`;
}

/* Today's gas, when the meter has not reported it yet.
 *
 * The meter is always a day or two behind, so "today" has no measured
 * gas in it. But today is not a mystery: the degree-day model says
 * what a day like today should use. That is an ESTIMATE, so it is
 * plum - the same colour as the forecast - and it always says so. It
 * never takes the place of a reading.
 *
 * Returns null when there is nothing to say: either the meter has
 * reported today, or there is no model yet. */
function todayEstimate(gas) {
  if (!gas || gas.today_reported) return null;
  const est = gas.today_estimate;
  if (!est || est.m3 === null || est.m3 === undefined) return null;
  return est;
}

/* What the chart says when the window holds nothing.
 *
 * An empty grid of bars is a lie: it looks like a day of no gas. The
 * meter has simply not reported, and the panel has to say which it is.
 */
function chartEmpty(gas, view) {
  if (view === 'day') {
    return 'The meter has not reported anything yet, so there is no ' +
      'shape of a day to show.';
  }
  const last = gas.last_reported;
  if (last && (last.label || last.date)) {
    const when = last.label || last.date;
    return `The meter has not reported since ${esc(when)}. ` +
      (gas.data_lag_hours !== null && gas.data_lag_hours !== undefined
        ? `It is ${Math.round(gas.data_lag_hours)} hours behind.`
        : '');
  }
  return 'No readings in this window — the meter has not reported.';
}

/* The line under an estimated figure.
 *
 * It says what the number is (an estimate), how sure it is (the
 * band), and what it would cost - and it says none of that is a
 * reading. */
function estimatedLine(gas, est) {
  const bits = ['estimated'];
  if (est.band !== null && est.band !== undefined) {
    bits.push('\u00b1' + num(est.band, 2) + ' ' + (gas.unit === 'kwh' ? 'kWh' : 'm³'));
  }
  if (est.cost !== null && est.cost !== undefined) {
    bits.push('about ' + money(est.cost));
  }
  // The separator is a real character, not an entity: the whole line
  // is escaped on the way out, and an entity would show as text.
  return esc(bits.join(' · '));
}

/* The last day the meter actually reported.
 *
 * When today has no reading - a new meter waiting to start - the
 * panels would be a row of dashes with nothing in them. This is the
 * most recent real number, labelled as what it is. It is never shown
 * as today's. */
function lastReportedLine(gas) {
  const last = gas && gas.last_reported;
  if (!last) return '';
  const bits = [];
  if (last.m3 !== null && last.m3 !== undefined) bits.push(num(last.m3, 2) + ' m³');
  if (last.kwh !== null && last.kwh !== undefined) bits.push(num(last.kwh, 1) + ' kWh');
  if (last.cost !== null && last.cost !== undefined) bits.push(money(last.cost));
  if (!bits.length) return '';
  return `Last reported &middot; ${esc(last.label)} &middot; ${bits.join(' &middot; ')}`;
}

/* A small flame, for the rows and for the pill. */
const FLAME_SMALL = '<svg class="flame" width="9" height="11" viewBox="0 0 24 24" '
  + 'fill="currentColor" aria-hidden="true"><path d="M12 2c1 4 5 6 5 11a5 5 0 0 1-10 0c0-2 1-3 2-5 0 2 1 3 2 3 1-2 0-5 1-9z"/></svg>';

/* -- slide 1: heating ---------------------------------------------------- */

/* The little symbols in the rings.
 *
 * Each one has to mean what the ring beside it is saying: a flame for
 * a boiler that is firing, a snowflake for a cold day outside, a sun
 * for a warm one. A drop where a flame belongs is a picture that
 * argues with the number next to it.
 */
const RING_ICONS = {
  flame: '<path d="M12 2c1 4 5 6 5 11a5 5 0 0 1-10 0c0-2 1-3 2-5 0 2 1 3 2 3 1-2 0-5 1-9z"/>',
  home: '<path d="M12 3.2 2.8 11h2.4v9.2h5.2v-5.6h3.2v5.6h5.2V11h2.4z"/>',
  thermometer: '<path d="M10 13.7V5a2 2 0 1 1 4 0v8.7a4 4 0 1 1-4 0z"/>',
  sun: '<circle cx="12" cy="12" r="4"/>'
    + '<path d="M12 1.6v2.4M12 20v2.4M1.6 12H4M20 12h2.4M4.6 4.6l1.7 1.7'
    + 'M17.7 17.7l1.7 1.7M19.4 4.6l-1.7 1.7M6.3 17.7l-1.7 1.7" '
    + 'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" fill="none"/>',
  snowflake: '<path d="M12 2.2v19.6M3.6 7.1l16.8 9.8M20.4 7.1L3.6 16.9" '
    + 'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" fill="none"/>'
    + '<path d="M8.6 4.4L12 6.4l3.4-2M8.6 19.6L12 17.6l3.4 2" '
    + 'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" fill="none"/>',
};

/* A symbol, placed by its centre. */
function ringIcon(name, x, y, size, colour) {
  const body = RING_ICONS[name];
  if (!body) return '';
  return `<g data-icon="${name}" transform="translate(${x - size / 2} ${y - size / 2}) `
    + `scale(${size / 24})" fill="${colour}" color="${colour}">${body}</g>`;
}

/* What the weather outside is doing.
 *
 * Cold enough to snow, warm enough for sun, otherwise the plain
 * fact of a temperature.
 */
function outsideIcon(temp) {
  if (temp === null || temp === undefined) return 'thermometer';
  if (temp <= 5) return 'snowflake';
  if (temp >= 20) return 'sun';
  return 'thermometer';
}

/* The weather symbols.
 *
 * A forecast that says "four degrees" but not "rain" is half a
 * forecast. These are the same handful a phone shows: sun, cloud,
 * rain, snow, fog, storm.
 */
const WEATHER_ICONS = {
  sun: '<circle cx="12" cy="12" r="4.2"/>'
    + '<path d="M12 2.6v2.6M12 18.8v2.6M2.6 12h2.6M18.8 12h2.6M5.4 5.4l1.9 1.9'
    + 'M16.7 16.7l1.9 1.9M18.6 5.4l-1.9 1.9M7.3 16.7l-1.9 1.9" stroke="currentColor" '
    + 'stroke-width="1.8" stroke-linecap="round" fill="none"/>',
  cloud: '<path d="M7 19a4.2 4.2 0 0 1 .4-8.4 5.6 5.6 0 0 1 10.7 1.7'
    + 'A3.9 3.9 0 0 1 17.6 19H7z"/>',
  'cloud-sun': '<circle cx="8" cy="7.5" r="3"/>'
    + '<path d="M8 1.8v1.4M1.8 7.5h1.4M3.5 3l1 1M13.5 3l-1 1" stroke="currentColor" '
    + 'stroke-width="1.5" stroke-linecap="round" fill="none"/>'
    + '<path d="M9.5 19.5a3.8 3.8 0 0 1 .3-7.6 5 5 0 0 1 9.6 1.5'
    + 'A3.5 3.5 0 0 1 19 19.5h-9.5z"/>',
  rain: '<path d="M7.4 14.5a4 4 0 0 1 .4-8 5.4 5.4 0 0 1 10.3 1.6'
    + 'A3.7 3.7 0 0 1 17.7 14.5H7.4z"/>'
    + '<path d="M8.6 17.4l-1 2.6M12 17.4l-1 2.6M15.4 17.4l-1 2.6" stroke="currentColor" '
    + 'stroke-width="1.8" stroke-linecap="round" fill="none"/>',
  snow: '<path d="M7.4 14.5a4 4 0 0 1 .4-8 5.4 5.4 0 0 1 10.3 1.6'
    + 'A3.7 3.7 0 0 1 17.7 14.5H7.4z"/>'
    + '<path d="M8.4 17.6v2.8M7.2 18.4l2.4 1.2M9.6 18.4l-2.4 1.2M14 17.6v2.8'
    + 'M12.8 18.4l2.4 1.2M15.2 18.4l-2.4 1.2" stroke="currentColor" stroke-width="1.4" '
    + 'stroke-linecap="round" fill="none"/>',
  fog: '<path d="M7.4 13a4 4 0 0 1 .4-8 5.4 5.4 0 0 1 10.3 1.6'
    + 'A3.7 3.7 0 0 1 17.7 13H7.4z"/>'
    + '<path d="M5 16.5h14M7 20h10" stroke="currentColor" stroke-width="1.8" '
    + 'stroke-linecap="round" fill="none"/>',
  storm: '<path d="M7.4 13.5a4 4 0 0 1 .4-8 5.4 5.4 0 0 1 10.3 1.6'
    + 'A3.7 3.7 0 0 1 17.7 13.5H7.4z"/>'
    + '<path d="M13 15l-3.4 4.4h2.6L11.4 23" stroke="currentColor" stroke-width="1.7" '
    + 'stroke-linecap="round" stroke-linejoin="round" fill="none"/>',
  thermometer: '<path d="M10 13.7V5a2 2 0 1 1 4 0v8.7a4 4 0 1 1-4 0z"/>',
};

/* A weather symbol, drawn. */
function weatherIcon(name, size) {
  const body = WEATHER_ICONS[name] || WEATHER_ICONS.thermometer;
  return `<svg class="wx-svg" viewBox="0 0 24 24" width="${size}" height="${size}" `
    + `fill="currentColor" aria-hidden="true">${body}</svg>`;
}

/* How long the boost has left, in minutes. */
function boostMinutes(until) {
  if (!until) return null;
  let when = null;
  if (typeof until === 'number') {
    when = new Date(until > 1e11 ? until : until * 1000);
  } else if (/^\d{1,2}:\d{2}/.test(String(until))) {
    // A clock time: the minutes are all that matter.
    const parts = String(until).split(':');
    return Number(parts[1]) || 0;
  } else {
    when = new Date(until);
  }
  if (isNaN(when.getTime())) return null;
  const minutes = Math.round((when.getTime() - Date.now()) / 60000);
  return minutes > 0 ? minutes : 0;
}

/* -- the home ring's scale and colours ------------------------------------ */

/* The scale the home ring is drawn on.
 *
 * It does not move. A gauge whose ends move with the weather cannot be
 * read at a glance, and the heating schedule alone would swing it by
 * fifteen degrees twice a day - which is what the ring used to do: it
 * measured the distance to a setpoint that was 7 degrees while the
 * heating was off and 21 while it was on, so the same house could look
 * full or three-quarters full with nothing having changed.
 */
const HEAT_MIN = 5;
const HEAT_MAX = 25;

/* The colours the ring takes as the house gets warmer.
 *
 * Named rather than written out, because the colours live in theme.css
 * with the rest of the palette.
 */
const HEAT_RAMP = ['--heat-1', '--heat-2', '--heat-3', '--heat-4', '--heat-5'];

/* Where a temperature sits on that scale, as a number from 0 to 1.
 *
 * Nothing in it, and it is not on the scale.
 */
function heatScale(temp) {
  if (temp === null || temp === undefined || temp === '') return null;
  const value = Number(temp);
  if (Number.isNaN(value)) return null;
  return Math.max(0, Math.min(1, (value - HEAT_MIN) / (HEAT_MAX - HEAT_MIN)));
}

/* The ramp, read from the stylesheet.
 *
 * The palette is the stylesheet's business, so it is read from there
 * rather than repeated here - the two cannot drift apart.
 */
let heatRampColours = null;

function readHeatRamp() {
  if (heatRampColours) return heatRampColours;
  const styles = getComputedStyle(document.documentElement);
  heatRampColours = HEAT_RAMP.map((name) => {
    const value = (styles.getPropertyValue(name) || '').trim();
    const hex = /^#([0-9a-f]{6})$/i.exec(value);
    if (!hex) return [239, 138, 74];
    return [
      parseInt(hex[1].slice(0, 2), 16),
      parseInt(hex[1].slice(2, 4), 16),
      parseInt(hex[1].slice(4, 6), 16),
    ];
  });
  return heatRampColours;
}

/* The colour for a point on the scale.
 *
 * The steps between the palette's colours are mixed, so the ring warms
 * up smoothly instead of jumping from one colour to the next.
 */
function heatColour(scale) {
  const ramp = readHeatRamp();
  const at = Math.max(0, Math.min(1, scale)) * (ramp.length - 1);
  const low = Math.floor(at);
  const high = Math.min(ramp.length - 1, low + 1);
  const mix = at - low;
  const rgb = ramp[low].map((value, index) =>
    Math.round(value + (ramp[high][index] - value) * mix));
  return 'rgb(' + rgb.join(',') + ')';
}

/* A mark on the ring for the target temperature.
 *
 * The ring's length is the house's own temperature, so the setpoint has
 * to be a mark on the same scale rather than more of the arc - and the
 * mark has to cross the ring, or it is lost in the colour behind it.
 */
function targetTick(scale, cx, cy, r) {
  const angle = (-90 + 360 * Math.max(0, Math.min(1, scale))) * Math.PI / 180;
  const inner = r - 9;
  const outer = r + 9;
  const at = (distance) => [
    (cx + distance * Math.cos(angle)).toFixed(2),
    (cy + distance * Math.sin(angle)).toFixed(2),
  ];
  const from = at(inner);
  const to = at(outer);
  return `<line data-tick="target" x1="${from[0]}" y1="${from[1]}" x2="${to[0]}" y2="${to[1]}"
          stroke="var(--bg)" stroke-width="5" stroke-linecap="round"/>
          <line data-tick="target" x1="${from[0]}" y1="${from[1]}" x2="${to[0]}" y2="${to[1]}"
          stroke="var(--ink)" stroke-width="2" stroke-linecap="round"/>`;
}

/* The rings.
 *
 * Home in the middle, because that is where the eye goes. The boiler
 * and the outside world in the corners, and the boost underneath the
 * outside world - not underneath the home ring, where it crowded the
 * temperature.
 *
 * Nothing joins them up. The dotted lines between the rings said
 * "these are connected", which the layout already says, and they ran
 * through the numbers.
 *
 * The rings are big enough for what they have to say: a word like
 * FIRING does not fit inside a circle that size without touching the
 * edge, so the text sits in the middle of the ring and the ring is
 * big enough to hold it with room to spare.
 */
function ringDiagram(heating) {
  const home = heating.home_temp;
  const target = heating.target_temp;
  const outside = heating.outside_temp;
  const firing = heating.boiler_on;

  /* The home ring.
   *
   * Its length is how warm the house is on a fixed cold-to-hot scale,
   * and its colour is that same scale's ramp - blue when the house is
   * cold, the boiler's orange when it is warm. Both come from one
   * number, so the ring cannot say one thing with its colour and
   * another with its length.
   *
   * The target is a mark on the same scale rather than more of the
   * arc: the setpoint still has to be visible, but it must not change
   * the length of the ring when the schedule moves.
   */
  const heat = heatScale(home);
  const heatInk = heat === null ? 'var(--dimmer)' : heatColour(heat);

  /* The outside ring is the same ramp, on the same scale.
   *
   * It stays a solid ring - it is not a gauge, and its length would only
   * compete with the home ring's. But its colour says how cold it is
   * out, from the weather's blue to the boiler's orange.
   *
   * The scale is the home ring's, deliberately: one scale and one ramp
   * for the whole panel, so the same temperature is the same colour
   * whether it is inside or outside, and a blue ring next to an orange
   * one means exactly what it looks like - cold out, warm in.
   */
  const outsideHeat = heatScale(outside);
  const outsideInk = outsideHeat === null ? 'var(--dimmer)' : heatColour(outsideHeat);

  const homeR = 64;
  const circumference = 2 * Math.PI * homeR;
  const dash = Math.round(circumference * (heat === null ? 0 : heat));
  const targetHeat = heatScale(target);

  // The boost that ends soonest, wherever it is.
  //
  // A boost can be on the boiler or on one room's valve, and they are
  // different things. The ring shows whichever ends soonest and says
  // which one it is - a ring that says "BOOST" and does not say what
  // is boosted is a puzzle.
  const boosts = Array.isArray(heating.boosts) ? heating.boosts : [];
  const timed = boosts
    .map((boost) => ({ boost: boost, minutes: boostMinutes(boost.until) }))
    .filter((entry) => entry.minutes !== null && entry.minutes > 0)
    .sort((a, b) => a.minutes - b.minutes);
  let boost = timed.length ? timed[0].boost : null;
  let boostLeft = timed.length ? timed[0].minutes : null;
  if (!boost && heating.boost_until) {
    // An older payload, or one that only knows about the boiler.
    boost = { room: 'Boiler', until: heating.boost_until, source: 'boiler' };
    boostLeft = boostMinutes(heating.boost_until);
  }
  const boostText = boostLeft === null ? '—' : (boostLeft + 'm');
  const boostName = boost
    ? (boost.source === 'boiler' ? 'BOILER' : String(boost.room || '').toUpperCase())
    : '';
  const boostLabel = boostName ? 'BOOST · ' + boostName : 'BOOST';

  const satR = 36;
  const boilerX = 58;
  const boilerY = 42;
  const outX = 406;
  const outY = 42;
  const homeX = 214;
  const homeY = 100;
  // The boost sits under the outside ring, with room to breathe.
  const boostX = 406;
  const boostY = 150;
  const boostR = 26;

  // The boiler's word, and the flame that goes with it.
  const boilerWord = firing === true ? 'FIRING' : (firing === false ? 'IDLE' : '—');
  const boilerColour = firing === true ? 'var(--ember)'
    : (firing === false ? 'var(--dim)' : 'var(--dimmer)');

  return `<svg viewBox="0 0 470 212" width="100%" height="100%" style="max-height:100%">
    <g data-ring="home">
      <circle cx="${homeX}" cy="${homeY}" r="${homeR}" fill="#11161d" stroke="rgba(255,255,255,.08)" stroke-width="7"/>
      ${heat !== null && heat > 0.005 ? `<circle cx="${homeX}" cy="${homeY}" r="${homeR}" fill="none" stroke="${heatInk}"
              stroke-width="7" stroke-linecap="round" transform="rotate(-90 ${homeX} ${homeY})"
              stroke-dasharray="${dash} ${circumference}"/>` : ''}
      ${targetHeat === null ? '' : targetTick(targetHeat, homeX, homeY, homeR)}
      ${ringIcon('home', homeX, homeY - 36, 22, heatInk)}
      <text x="${homeX}" y="${homeY + 10}" text-anchor="middle" fill="${heatInk}"
            font-family="JetBrains Mono,monospace" font-size="31" font-weight="600">${home === null ? '—' : num(home) + '°'}</text>
      <text x="${homeX}" y="${homeY + 34}" text-anchor="middle" fill="var(--dim)"
            font-family="Outfit,sans-serif" font-size="11.5" letter-spacing="1.8">HOME</text>
    </g>

    <g data-ring="boiler">
      <circle cx="${boilerX}" cy="${boilerY}" r="${satR}" fill="#11161d" stroke="${firing === true ? 'var(--ember)' : 'rgba(255,255,255,.2)'}" stroke-width="2.6"/>
      ${firing === true ? ringIcon('flame', boilerX, boilerY - 17, 20, 'var(--ember)') : ''}
      <text x="${boilerX}" y="${boilerY + (firing === true ? 20 : 5)}" text-anchor="middle" fill="${boilerColour}"
            font-family="JetBrains Mono,monospace" font-size="12" font-weight="600">${boilerWord}</text>
      <text x="${boilerX}" y="92" text-anchor="middle" fill="var(--dim)"
            font-family="Outfit,sans-serif" font-size="10.5" letter-spacing="1.4">BOILER</text>
    </g>

    <g data-ring="outside">
      <circle cx="${outX}" cy="${outY}" r="${satR}" fill="#11161d" stroke="${outsideInk}" stroke-width="2.6"/>
      ${ringIcon(outsideIcon(outside), outX, outY - 17, 20, outsideInk)}
      <text x="${outX}" y="${outY + 20}" text-anchor="middle" fill="${outsideInk}"
            font-family="JetBrains Mono,monospace" font-size="18" font-weight="600">${outside === null ? '—' : num(outside) + '°'}</text>
      <text x="${outX}" y="92" text-anchor="middle" fill="var(--dim)"
            font-family="Outfit,sans-serif" font-size="10.5" letter-spacing="1.4">OUTSIDE</text>
    </g>

    <g data-ring="boost">
      <circle cx="${boostX}" cy="${boostY}" r="${boostR}" fill="#11161d" stroke="${boostLeft ? 'var(--ember)' : 'rgba(255,255,255,.18)'}" stroke-width="2.6"/>
      <text x="${boostX}" y="${boostY + 5}" text-anchor="middle" fill="${boostLeft ? 'var(--ink)' : 'var(--dimmer)'}"
            font-family="JetBrains Mono,monospace" font-size="14" font-weight="600">${boostText}</text>
      <text x="${boostX}" y="198" text-anchor="middle" fill="var(--dim)"
            font-family="Outfit,sans-serif" font-size="10.5" letter-spacing="1.4">${boostLabel}</text>
    </g>
  </svg>`;
}

function slideHeating(payload) {
  const heating = payload.heating || {};
  const rooms = heating.rooms || [];
  const openCount = rooms.filter((room) => room.valve_open).length;
  const valveTotal = rooms.reduce((sum, room) => sum + (room.valve_count || 0), 0);

  const half = Math.ceil(rooms.length / 2);
  const roomRows = (list) => list.map((room) => {
    const valve = room.valve_open;
    const known = valve === true || valve === false;
    // Hive does not report the position of a valve, so the badge says
    // what we actually know: the room is asking for heat, or it is not.
    const label = known ? (valve ? 'on' : 'off') : '—';
    const cls = known ? (valve ? 'open' : 'shut') : 'unknown';
    const extra = room.valve_count > 1 ? `<span class="cnt">&times;${room.valve_count}</span>` : '';
    const low = typeof room.battery === 'number' && room.battery < 20
      ? `<span class="low" title="low battery">${Math.round(room.battery)}%</span>` : '';
    // A room asking for heat is the thing to notice, so it gets a
    // flame, a brighter name and a lit row - not just a small word.
    const flame = valve === true
      ? '<svg class="flame" width="9" height="11" viewBox="0 0 24 24" fill="currentColor" ' +
      'aria-label="asking for heat"><path d="M12 2c1 4 5 6 5 11a5 5 0 0 1-10 0c0-2 1-3 2-5 0 2 1 3 2 3 1-2 0-5 1-9z"/></svg>'
      : '';
    return `
    <div class="row roomrow ${valve === true ? 'heating' : ''}">
      <span class="nm">${flame}${esc(room.room)}${extra}${low}</span>
      <span class="t">${room.current === null || room.current === undefined ? '—' : num(room.current) + '°'}</span>
      <span class="t dimmer">&rarr; ${room.target === null || room.target === undefined ? '—' : num(room.target)}</span>
      <span class="valve ${cls}">${label}</span>
    </div>`;
  }).join('');

  const columns = [rooms.slice(0, half), rooms.slice(half)];
  const grid = columns.map((column, index) => `
    <div class="rows">
      ${roomRows(column)}
      ${index === columns.length - 1 ? `
        <div class="row summary">
          <span class="nm" style="color:var(--dim)">Rooms asking for heat</span>
          ${openCount > 0
        ? `<span class="pill heating">${FLAME_SMALL}${openCount} of ${rooms.length || 0}</span>`
        : `<span class="pill count">${openCount} of ${rooms.length || 0}</span>`}
          <span class="pill count">${valveTotal ? valveTotal + ' valves' : 'no valves'}</span>
        </div>` : ''}
    </div>`).join('');

  return `<div class="slide">
    ${head(SLIDE_TITLES.heating)}
    <div class="slide-body">
      <div class="panel">
        <div class="p-h"><h4>Heating now</h4><span class="src">from Hive</span></div>
        ${trouble(problemFor('hive'), 'Open the settings page (/settings) and sign in to Hive there. Ember picks the session up from then on.')}
        ${problemFor('hive') ? '' : `<div class="svgwrap">${ringDiagram(heating)}</div>`}
      </div>

      <div class="panel">
        <div class="p-h"><h4>Status</h4><span class="src">Ember's polls &middot; only while it was watching</span></div>
        ${problemFor('hive') ? trouble(problemFor('hive')) : `<div class="big">
          <div>
            <div class="kpi">${heating.home_temp === null || heating.home_temp === undefined ? '—' : num(heating.home_temp)}<span class="u">°C</span></div>
            <div class="dimmer" style="font-size:11.5px;margin-top:5px">target ${num(heating.target_temp)}°C &middot; inside</div>
          </div>
          <div class="side" style="text-align:right">
            <span class="state ${heating.boiler_on === true ? '' : 'off'}">
              ${heating.boiler_on === true
        ? '<svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2c1 4 5 6 5 11a5 5 0 0 1-10 0c0-2 1-3 2-5 0 2 1 3 2 3 1-2 0-5 1-9z"/></svg>Heating'
        : (heating.boiler_on === false ? 'Idle' : 'Unknown')}</span>
          </div>
        </div>
        <div class="rows" style="margin-top:12px">
          <div class="row"><span class="nm">Heating today</span><span class="t">${heating.heating_today_partial ? 'at least ' : ''}${hours(heating.heating_today_minutes)}</span></div>
          <div class="row"><span class="nm">Heating this week</span><span class="t">${heating.heating_week_partial ? 'at least ' : ''}${hours(heating.heating_week_minutes)}</span></div>
          <div class="row"><span class="nm">A day, on average</span><span class="t">${hours(heating.heating_daily_average_minutes)}</span></div>
        </div>
        `}
      </div>

      <div class="panel" style="grid-column:1/-1">
        <div class="p-h"><h4>Rooms &middot; radiator valves</h4><span class="src">from Hive</span></div>
        ${problemFor('hive')
      ? `<div class="trouble"><div class="h">The rooms appear once Hive is reachable.</div></div>`
      : `<div class="rooms-grid">${grid}</div>`}
      </div>
    </div>
  </div>`;
}

/* -- slide 2: gas -------------------------------------------------------- */

/* The three ways of looking at the gas.
 *
 * DAY is the shape of a day: each hour's average use over the last
 * week. WEEK is a week of days: one column per day. MONTH is the
 * month: a calendar, shaded by usage.
 *
 * They are here because "the last 24 hours" was none of them. Octopus
 * delivers gas a day or two late, so a rolling 24-hour window is
 * usually empty, and when it is not empty it is showing a day that
 * ended two days ago. */
const GAS_VIEWS = {
  day: { label: 'DAY', title: 'Gas &middot; the shape of a day' },
  week: { label: 'WEEK', title: 'Gas &middot; the last 7 days' },
  month: { label: 'MONTH', title: 'Gas &middot; the last 30 days' },
};

/* The shape of a day: each hour's average use across the week.
 *
 * Never empty while there is any history at all, and it answers the
 * question the old chart could not - when does this house use gas. */
function chartDayShape(gas, width, height) {
  const profile = (gas.hourly_profile || {}).hours || [];
  if (!profile.length || !profile.some((item) => (item.avg || 0) > 0)) return '';

  const values = profile.map((item) => item.avg || 0);
  const peak = Math.max.apply(null, values.concat([0.0001]));
  const peakIndex = values.indexOf(peak);
  const gap = 4;
  const barWidth = (width - gap * 23) / 24;
  const baseline = height - 16;
  const top = 16;
  const days = (gas.hourly_profile || {}).days || 0;

  const bars = profile.map((item, index) => {
    const value = item.avg || 0;
    const barHeight = value > 0 ? Math.max(3, (value / peak) * (baseline - top)) : 0;
    const x = index * (barWidth + gap);
    const y = baseline - barHeight;
    const isPeak = index === peakIndex && value > 0;
    // An hour averaged over fewer days is a weaker claim, so it is
    // drawn lighter rather than pretending to the same certainty.
    const opacity = item.days >= days && days > 0 ? 1 : 0.5;
    const colour = isPeak ? 'var(--ember)' : 'var(--teal)';
    if (!barHeight) {
      // No use in that hour. A flat line, not a zero-height bar that
      // would still claim the hour was measured.
      return `<line x1="${x.toFixed(1)}" y1="${baseline}" x2="${(x + barWidth).toFixed(1)}"
        y2="${baseline}" stroke="rgba(255,255,255,.10)" stroke-width="1"/>`;
    }
    return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(1)}"
      height="${barHeight.toFixed(1)}" rx="2.5" fill="${colour}" opacity="${opacity}"/>`;
  }).join('');

  const ticks = [0, 6, 12, 18, 23].map((hour) => {
    const x = hour * (barWidth + gap);
    const label = String(hour).padStart(2, '0') + ':00';
    const anchor = hour === 0 ? 'start' : (hour === 23 ? 'end' : 'middle');
    const dx = hour === 0 ? 0 : (hour === 23 ? 0 : barWidth / 2);
    return `<text x="${(x + dx).toFixed(1)}" y="${height - 3}" fill="var(--dimmer)"
      font-family="Outfit,sans-serif" font-size="10.5" text-anchor="${anchor}">${esc(label)}</text>`;
  }).join('');

  return `<svg viewBox="0 0 ${width} ${height}" width="100%" height="100%" style="max-height:100%">
    ${bars}
    <line x1="0" y1="${baseline}" x2="${width}" y2="${baseline}" stroke="rgba(255,255,255,.12)" stroke-width="1"/>
    ${ticks}
  </svg>`;
}

/* A week of days: one column per day.
 *
 * A day the meter has not finished reporting is a gap, never a zero -
 * zero means no gas was used, which would be a lie. Today is drawn in
 * plum when it is still an estimate, and the week's average runs across
 * as a line. */
function chartWeekOfDays(gas, width, height) {
  const week = gas.week || [];
  if (!week.length) return '';
  const est = todayEstimate(gas);
  const average = gas.average_m3;

  // Each column is one day. A finished day is the metered total. Today
  // is whatever the meter has reported so far, with the estimate for
  // the whole day drawn on top of it - because today is not over, and a
  // column that stopped at midnight would say nothing about the day.
  const columns = week.map((day, index) => {
    const isToday = index === week.length - 1;
    // A day the meter has not reported is estimated, in plum - the
    // same model, the same band, and clearly not a reading. It is
    // never drawn as a zero: zero means no gas was used.
    if (day.estimated) {
      // The estimate is for the whole day. If the meter sent part of
      // the day, that part is still real: it is drawn in teal under
      // the estimate, faded because it is only part.
      const measured = day.reported ? (day.value || 0) : 0;
      const estimate = day.m3 || 0;
      return {
        day, isToday, measured, estimate,
        band: day.band,
        total: Math.max(estimate, measured),
        known: true, estimated: true,
        partial: day.reported && day.complete === false,
      };
    }
    if (isToday) {
      const measured = gas.today_reported ? (gas.today_m3 || 0) : 0;
      const estimate = est ? est.m3 : null;
      return {
        day, isToday, measured, estimate,
        total: estimate !== null ? Math.max(estimate, measured) : measured,
        known: measured > 0 || estimate !== null,
      };
    }
    if (!day.reported) {
      // The meter sent nothing for this day and the model has nothing
      // to say about it either. A gap, not a zero.
      return { day, isToday, measured: 0, estimate: null, total: 0, known: false };
    }
    return {
      day, isToday, measured: day.value || 0, estimate: null,
      total: day.value || 0, known: true,
      // Reported, but not the whole day - the first day in the
      // store, say. The figure is real but it is a part.
      partial: day.complete === false
    };
  });

  // Nothing at all to draw: an empty grid of seven columns would look
  // like a week of no gas, which is not what happened.
  if (!columns.some((column) => column.known)) return '';

  const peak = Math.max.apply(null,
    columns.map((column) => column.total).concat([average || 0, 0.0001]));

  const gap = 14;
  const barWidth = (width - gap * (week.length - 1)) / week.length;
  const baseline = height - 20;
  const top = 30;
  // The tallest bar stops short of the top, so there is room for its
  // number and for the average line to be read against it.
  const usable = baseline - top;
  const scale = (value) => (value / peak) * usable;

  const bars = columns.map((column, index) => {
    const x = index * (barWidth + gap);
    const centre = x + barWidth / 2;
    let body = '';
    let label = '';
    if (!column.known) {
      body = `<line x1="${x.toFixed(1)}" y1="${baseline}" x2="${(x + barWidth).toFixed(1)}"
        y2="${baseline}" stroke="rgba(255,255,255,.16)" stroke-width="1.5"
        stroke-dasharray="3 3"/>`;
    } else {
      const measuredHeight = column.measured > 0 ? scale(column.measured) : 0;
      if (measuredHeight > 0) {
        body += `<rect x="${x.toFixed(1)}" y="${(baseline - measuredHeight).toFixed(1)}"
          width="${barWidth.toFixed(1)}" height="${measuredHeight.toFixed(1)}"
          rx="3" fill="var(--teal)"${column.partial ? ' opacity=".5"' : ''}/>`;
      }
      // The band, as a whisker over the top of the estimate. It says how
      // far off the estimate could be, so the number has to clear it: a
      // figure with a line through it reads as struck out.
      const band = column.estimate !== null
        ? (column.band !== undefined && column.band !== null
          ? column.band : (est ? est.band : null))
        : null;
      // Where the number sits: above the bar, and above the whisker
      // when there is one.
      let labelTop = baseline - scale(column.total);
      if (column.estimate !== null) {
        // The estimate sits on top of what has been measured, in plum -
        // the colour every estimate in Ember wears.
        const totalHeight = scale(column.total);
        const from = baseline - measuredHeight;
        const to = baseline - totalHeight;
        if (to < from) {
          body += `<rect x="${x.toFixed(1)}" y="${to.toFixed(1)}" width="${barWidth.toFixed(1)}"
            height="${(from - to).toFixed(1)}" rx="3" fill="var(--plum)" opacity=".85"/>`;
        }
        if (band) {
          const bandTop = baseline - scale(column.total + band);
          body += `<line x1="${x.toFixed(1)}" y1="${bandTop.toFixed(1)}"
            x2="${(x + barWidth).toFixed(1)}" y2="${bandTop.toFixed(1)}"
            stroke="var(--plum)" stroke-width="1" stroke-dasharray="2 2" opacity=".8"/>`;
          labelTop = Math.min(labelTop, bandTop);
        }
      }
      label = `<text x="${centre.toFixed(1)}" y="${Math.max(12, labelTop - 7).toFixed(1)}"
        fill="${column.estimate !== null ? 'var(--plum)' : 'var(--ink)'}"
        font-family="JetBrains Mono,monospace" font-size="11" text-anchor="middle"
        opacity=".9">${num(column.total, 2)}</text>`;
    }
    // Each column carries its date, so the chart can be asked what it
    // drew - by a test, or by anyone reading the page.
    return `<g data-day="${esc(column.day.date || '')}">` + body + label +
      `<text x="${centre.toFixed(1)}" y="${height - 5}"
      fill="${column.isToday ? 'var(--ember)' : 'var(--dimmer)'}" font-family="Outfit,sans-serif"
      font-size="11" text-anchor="middle">${esc(column.day.label || '')}</text></g>`;
  }).join('');

  let averageLine = '';
  if (average && average > 0 && peak > 0) {
    const y = baseline - scale(average);
    averageLine = `<line x1="0" y1="${y.toFixed(1)}" x2="${width}" y2="${y.toFixed(1)}"
      stroke="var(--ice)" stroke-width="1" stroke-dasharray="4 4" opacity=".65"/>`;
  }

  return `<svg viewBox="0 0 ${width} ${height}" width="100%" height="100%" style="max-height:100%">
    ${bars}
    ${averageLine}
    <line x1="0" y1="${baseline}" x2="${width}" y2="${baseline}" stroke="rgba(255,255,255,.12)" stroke-width="1"/>
  </svg>`;
}

/* The month: a calendar, shaded by usage.
 *
 * A day with no readings is an empty cell, not a white one - the meter
 * has not reported, which is different from reporting nothing. */
function chartMonthGrid(gas) {
  const month = gas.month || [];
  if (!month.length) return '';
  // A cell is filled when the meter sent something for that day. "Not
  // over yet" (today) and "not reported" (a gap after a meter
  // exchange) are different things, and the grid shows both.
  const values = month.map((day) => (day.reported ? (day.value || 0) : null));
  const knownValues = values.filter((v) => v !== null && v > 0);
  if (!knownValues.length) return '';
  const peak = Math.max.apply(null, knownValues.concat([0.0001]));

  // The first day of the window may not be a Monday, so the grid is
  // padded - otherwise every cell would be a day out of place.
  const padding = month.length ? (month[0].weekday || 0) : 0;
  const today = month.length ? month[month.length - 1].date : '';

  const cells = month.map((day, index) => {
    const value = values[index];
    const known = value !== null && value > 0;
    const shade = known ? 0.18 + 0.72 * (value / peak) : 0;
    const isToday = day.date === today;
    return `<div class="mcell ${known ? '' : 'none'} ${isToday ? 'today' : ''}"
      style="${known ? `background:rgba(47,184,154,${shade.toFixed(2)})` : ''}"
      title="${esc(day.date)}${known ? ' · ' + num(day.value, 2) : ' · not reported'}">
      <span>${day.day}</span>
    </div>`;
  }).join('');

  const blanks = Array.from({ length: padding },
    () => '<div class="mcell pad"></div>').join('');
  const weekdays = ['M', 'T', 'W', 'T', 'F', 'S', 'S'].map((label) =>
    `<div class="mh">${label}</div>`).join('');

  return `<div class="month">
    <div class="mgrid">${weekdays}${blanks}${cells}</div>
  </div>`;
}

/** How far behind the meter is.
 *
 * Octopus does not deliver readings as they happen. A day with no
 * reading is a day that has not been reported yet, not a day with no
 * gas in it, and the dashboard has to say which. */
function lagNote(gas) {
  if (!gas) return '';
  if (gas.today_reported) return '';
  const lag = gas.data_lag_hours;
  if (lag === null || lag === undefined) return 'No reading yet';
  if (lag < 48) return `${Math.round(lag)}h behind`;
  const days = Math.round(lag / 24);
  return `${days}d behind`;
}

/** What unit the meter actually reports in.
 *
 * A SMETS2 meter reports cubic metres and a SMETS1 meter reports kWh.
 * Octopus does not say which, so the dashboard has to follow whatever
 * the data says rather than assume metres. */
function gasUnit(gas) {
  if (!gas) return 'm³';
  if (gas.unit === 'kwh') return 'kWh';
  if (gas.unit === 'm3') return 'm³';
  /* Neither the meter nor the readings said, so the dashboard does not
     pick one. An empty label is honest; "m³" would not be. */
  return gas.unit_label || '';
}

/** The line under tomorrow's forecast.
 *
 * It can only say "about £x" when the unit is known, because the money
 * comes from the kWh. With no unit it says what it can. */
function forecastLine(gas, forecast) {
  const kwh = derivedKwh(gas, forecast.tomorrow_m3);
  const hasCost = forecast.tomorrow_cost !== null &&
    forecast.tomorrow_cost !== undefined;
  if (kwh !== null && forecast.tomorrow_kwh !== null &&
    forecast.tomorrow_kwh !== undefined) {
    if (hasCost) {
      return `${num(forecast.tomorrow_kwh, 1)} kWh &middot; about ${money(forecast.tomorrow_cost)}`;
    }
    // The kWh is there but the money is not. Say why, rather than
    // leaving "about -" hanging there.
    return `${num(forecast.tomorrow_kwh, 1)} kWh &middot; the cost cannot be worked out yet`;
  }
  if (!hasCost) {
    return gas && gas.unit === 'unknown'
      ? 'the unit is unknown, so there is no cost to work out'
      : 'the cost cannot be worked out yet';
  }
  return 'about ' + money(forecast.tomorrow_cost);
}

/** The derived kWh figure only means something when the meter reads m³. */
function derivedKwh(gas, volume) {
  if (!gas || gas.unit === 'kwh') return null;
  if (gas.unit === 'unknown') return null;
  return volume * (gas.calorific || 0);
}

/* The gas chart and its three views.
 *
 * The tabs are real: each one draws something different, and the panel
 * names what it is showing. */
function gasChartPanel(gas) {
  const view = GAS_VIEWS[state.gasTab] ? state.gasTab : 'week';
  const width = 470;
  const height = 210;

  let body = '';
  let legend = '';
  let note = '';
  if (view === 'day') {
    body = chartDayShape(gas, width, height);
    legend = '<span><i style="background:var(--teal)"></i>average use per hour</span>' +
      '<span><i style="background:var(--ember)"></i>busiest hour</span>';
    note = 'the average use in each hour, over the last ' +
      num((gas.hourly_profile || {}).days || 0, 0) + ' days';
  } else if (view === 'month') {
    body = chartMonthGrid(gas);
    legend = '<span><i style="background:rgba(47,184,154,.25)"></i>less</span>' +
      '<span><i style="background:rgba(47,184,154,.9)"></i>more</span>' +
      '<span><i style="background:transparent;border:1px dashed var(--line)"></i>not reported</span>';
    const known = (gas.month || []).filter((day) => day.complete !== false && (day.value || 0) > 0);
    note = known.length
      ? num(known.reduce((total, day) => total + day.value, 0), 2) + ' m³ over ' +
      known.length + ' reported days'
      : 'no days reported yet';
  } else {
    body = chartWeekOfDays(gas, width, height);
    legend = '<span><i style="background:var(--teal)"></i>metered</span>' +
      '<span><i style="background:var(--plum)"></i>estimated</span>' +
      '<span><i style="background:var(--ice)"></i>the week\'s average</span>' +
      '<span><i style="background:transparent;border:1px dashed var(--line)"></i>not reported</span>';
    const estimated = (gas.week || []).filter((day) => day.estimated).length;
    note = 'one column per day &middot; the dotted line is the week\'s average'
      + (estimated ? ' &middot; ' + estimated + ' not reported yet, so estimated' : '');
  }

  if (!body) {
    body = `<div class="fnote" style="padding:26px 8px">${chartEmpty(gas, view)}</div>`;
  }

  const tabs = Object.keys(GAS_VIEWS).map((key) =>
    `<span data-tab="${key}" class="${key === view ? 'on' : ''}">${GAS_VIEWS[key].label}</span>`).join('');

  return `<div class="panel">
    <div class="p-h">
      <h4>${GAS_VIEWS[view].title}</h4>
      <div class="tabs">${tabs}</div>
    </div>
    ${body}
    <div class="legend">${legend}</div>
    <div class="dimmer" style="font-size:10.5px;margin-top:3px">${note}</div>
  </div>`;
}

function slideGas(payload) {
  const gas = payload.gas || {};
  const unit = gasUnit(gas);
  const budget = payload.budget || {};
  const hourly = gas.hourly || [];
  // Today's estimate, when the meter has not reported today yet.
  const est = todayEstimate(gas);

  const chartWidth = 470;
  const chartHeight = 210;
  // A window with no readings at all has no peak hour. Without this
  // check the peak is midnight, which is a claim rather than an
  // absence - the meter reported nothing, it did not report midnight.
  const hasHours = (gas.hourly_count === undefined || gas.hourly_count === null)
    ? hourly.length > 0 : gas.hourly_count > 0;
  // The peak hour of a typical day.
  //
  // Octopus delivers gas a day or two late, so today's own readings
  // are usually not there - and a peak hour taken from them would be
  // the peak hour of nothing. The weekly profile is: the average use
  // in each hour of a day, across the last week. It is always there
  // once there is a week of history, and it is the honest question -
  // "when does this house use gas?"
  const profile = gas.hourly_profile || {};
  const profileHours = (profile.hours || [])
    .filter((entry) => entry && Number(entry.avg) > 0);
  let peak = profileHours.reduce(
    (best, entry) => (best === null || Number(entry.avg) > Number(best.avg)
      ? entry : best), null);
  let peakSource = 'avg day';
  if (!peak && hasHours) {
    // Today's own readings, when there are any.
    const value = Math.max.apply(null, hourly.map((i) => i.m3 || 0));
    const index = hourly.findIndex((i) => i.m3 === value);
    if (index >= 0) {
      peak = { hour: index, avg: value };
      peakSource = 'today';
    }
  }


  const tiles = [
    // A tile shows the estimate when the meter has not reported yet -
    // in plum, and saying so. It never pretends to be a reading.
    {
      k: 'Gas today', dot: est ? 'var(--plum)' : 'var(--teal)',
      v: gas.today_reported ? num(gas.today_m3, 2) : (est ? num(est.m3, 2) : '—'),
      // No number, no unit: "— m3" reads as "no gas, in cubic metres".
      unit: gas.today_reported || est ? unit : '',
      plum: !gas.today_reported && !!est,
      s: gas.today_reported
        ? (derivedKwh(gas, gas.today_m3) === null ? '' : num(gas.today_kwh, 1) + ' kWh')
        : (est ? 'estimated' : lagNote(gas))
    },
    {
      k: 'Cost', dot: est ? 'var(--plum)' : 'var(--teal)',
      v: est && est.cost !== null && est.cost !== undefined ? '~' + money(est.cost) : money(gas.cost_today),
      plum: !gas.today_reported && !!est,
      s: est ? 'estimated' : 'incl. standing'
    },
    {
      k: '7-day average', dot: 'var(--ice)', v: num(averageGas(payload), 2), unit: unit,
      s: derivedKwh(gas, averageGas(payload)) === null ? '' : num(averageGas(payload) * (gas.calorific || 0), 1) + ' kWh'
    },
    // "vs average" compares today with the average. When the meter
    // has not reported today, it compares the *estimate* - a whole
    // day against whole days - and says so.
    {
      k: 'vs average', dot: (est && !gas.today_reported) ? 'var(--plum)' : 'var(--ember)',
      v: comparison(payload), unit: comparison(payload) === '—' ? '' : '%',
      plum: !!(est && !gas.today_reported), s: comparisonWord(payload)
    },
    {
      k: 'Peak hour', dot: 'var(--dimmer)',
      v: peak ? String(peak.hour).padStart(2, '0') + ':00' : '—',
      s: peak ? num(peak.avg, 2) + ' ' + unit + ' ' + peakSource
        : (hasHours ? '' : 'nothing reported')
    },
    { k: 'Month budget', dot: 'var(--teal)', v: money(budget.spend), s: 'of ' + money(budget.budget) + ' · ' + (budget.percent === null || budget.percent === undefined ? '—' : budget.percent + '%'), bar: budget.percent },
  ];

  const tileHtml = tiles.map((tile) => `
    <div class="tile">
      <div class="k"><i style="background:${tile.dot}"></i>${esc(tile.k)}</div>
      <div class="v ${tile.plum ? 'plum' : ''}">${esc(tile.v)}${tile.unit ? `<small>${esc(tile.unit)}</small>` : ''}</div>
      <div class="s">${esc(tile.s)}</div>
      ${tile.bar !== undefined && tile.bar !== null ? `<div class="bar"><i style="background:var(--teal);width:${Math.max(2, Math.min(100, tile.bar))}%"></i></div>` : ''}
    </div>`).join('');

  return `<div class="slide">
    ${head(SLIDE_TITLES.gas)}
    <div class="slide-body s2">
      ${gasChartPanel(gas)}

      <div class="panel">
        <div class="p-h"><h4>Today</h4><span class="src">from your Octopus meter</span></div>
        ${trouble(problemFor('octopus'))}
        <div class="big">
          <div>
            <div class="kpi ${est ? 'plum' : ''}">${gas.today_reported ? num(gas.today_m3, 2) : (est ? num(est.m3, 2) : '—')}${gas.today_reported || est ? `<span class="u">${unit}</span>` : ''}</div>
            <div class="${est ? 'plum' : 'dimmer'}" style="font-size:11.5px;margin-top:5px">${gas.today_reported
      ? (gas.unit === 'unknown'
        ? 'the unit is unknown, so this is not converted'
        : (derivedKwh(gas, gas.today_m3) === null ? 'meter reports kWh' : `${num(gas.today_kwh, 1)} kWh &middot; at ${num(gas.calorific, 2)} kWh/m³`))
      : (est ? estimatedLine(gas, est) : (gas.today_note || 'today has not been reported yet'))}</div>
            ${!gas.today_reported && !est && gas.today_note ? `<div class="dimmer" style="font-size:11.5px;margin-top:4px">${esc(gas.today_note)}</div>` : ''}
            ${!gas.today_reported && est && gas.today_note ? `<div class="dimmer" style="font-size:11.5px;margin-top:4px">${esc(gas.today_note)}</div>` : ''}
            ${gas.meter_note ? `<div class="dimmer" style="font-size:11.5px;margin-top:4px">${esc(gas.meter_note)}</div>` : ''}
          </div>
        </div>
        <div class="rows" style="margin-top:10px">
          <div class="row"><span class="nm">Cost today</span><span class="t ${est ? 'plum' : ''}">${est && est.cost !== null && est.cost !== undefined ? '~' + money(est.cost) : money(gas.cost_today)}${est ? ' <small>est</small>' : (gas.cost_note ? ' <small>' + esc(gas.cost_note) + '</small>' : '')}</span></div>
          <div class="row"><span class="nm">Energy charge</span><span class="t ${est ? 'plum' : ''}">${est && gas.unit_rate_p ? '~' + money(est.m3 * (gas.calorific || 0) * gas.unit_rate_p / 100) : (gas.energy_p === null || gas.energy_p === undefined ? '—' : money(gas.energy_p / 100))}${est ? ' <small>est</small>' : ''}</span></div>
          <div class="row"><span class="nm">Standing charge</span><span class="t">${gas.standing_p === null || gas.standing_p === undefined ? '\u2014' : money(gas.standing_p / 100)}</span></div>
          <div class="row"><span class="nm">Unit rate</span><span class="t">${gas.unit_rate_p === null || gas.unit_rate_p === undefined ? '\u2014' : num(gas.unit_rate_p, 2) + 'p/kWh'}</span></div>
        </div>
        ${gas.unit_reason ? `<div class="foot">Unit: ${esc(gas.unit_label || gas.unit)} &mdash; ${esc(gas.unit_reason)}.</div>` : ''}
      </div>

      <div class="panel" style="grid-column:1/-1">
        <div class="p-h"><h4>Today at a glance</h4></div>
        <div class="tiles" style="grid-template-columns:repeat(6,1fr)">${tileHtml}</div>
        ${!gas.today_reported && lastReportedLine(gas)
      ? `<div class="dimmer" style="font-size:11.5px;margin-top:6px">${lastReportedLine(gas)}</div>`
      : ''}
        ${!gas.today_reported && gas.meter_note_detail
      ? `<div class="dimmer" style="font-size:11.5px;margin-top:2px">${esc(gas.meter_note_detail)}</div>`
      : ''}
      </div>
    </div>
  </div>`;
}

function averageGas(payload) {
  const gas = payload.gas || {};
  if (gas.average_m3 !== null && gas.average_m3 !== undefined) return gas.average_m3;
  const days = (payload.heating && payload.heating.days) || [];
  const values = days.map((day) => day.m3 || 0).filter((value) => value > 0);
  if (!values.length) return 0;
  return values.reduce((a, b) => a + b, 0) / values.length;
}

function comparison(payload) {
  const gas = payload.gas || {};
  if (gas.vs_average_percent === null || gas.vs_average_percent === undefined) return '—';
  const percent = Math.round(gas.vs_average_percent);
  return (percent > 0 ? '+' : '') + percent;
}

function comparisonWord(payload) {
  const gas = payload.gas || {};
  if (gas.vs_average_percent === null || gas.vs_average_percent === undefined) {
    // The reason matters: "not enough history" and "no reading for
    // today" are completely different problems.
    return gas.vs_average_note || 'not enough history yet';
  }
  const percent = gas.vs_average_percent;
  if (percent <= -8) return 'a mild day';
  if (percent >= 8) return 'a cold day';
  return 'about average';
}

/* -- slide 3: today ------------------------------------------------------ */

function slideToday(payload) {
  const gas = payload.gas || {};
  const unit = gasUnit(gas);
  const heating = payload.heating || {};
  const forecast = payload.forecast || {};
  const budget = payload.budget || {};
  const days = heating.days || [];
  const hourly = gas.hourly || [];
  // Today's estimate, when the meter has not reported today yet.
  const est = todayEstimate(gas);

  const width = 470;
  const height = 120;

  // The week, one tile per day.
  //
  // The big number is what the day COST, because that is what the
  // Today slide is about. The gas that produced it is underneath in
  // small type, and the heating hours with it - a day of gas and a day
  // of heat together is what says whether the gas was the heating.
  //
  // A day the meter has not reported yet is estimated, in plum, with a
  // tilde - and when the real reading arrives it takes the estimate's
  // place. An estimate never replaces a reading, and a missing day is
  // never drawn as a zero.
  const week = days.map((day, index) => {
    const isToday = index === days.length - 1;
    const estimated = day.estimated === true;
    const measured = day.status === 'measured';
    const hasCost = day.cost !== null && day.cost !== undefined;
    const big = hasCost ? (estimated ? '~' : '') + money(day.cost) : '—';
    let sub;
    if (!measured && !estimated) {
      sub = 'not reported';
    } else {
      sub = num(day.m3, 2) + (unit ? ' ' + unit : '') + ' gas'
        + (estimated ? ' est' : '');
    }
    return `<div class="tile" ${isToday ? 'style="border-color:rgba(239,138,74,.4)"' : ''}>
      <div class="k">${esc(day.label || '')}</div>
      <div class="v ${estimated ? 'plum' : (isToday ? 'ember' : '')}">${esc(big)}</div>
      <div class="s">${esc(sub)}</div>
      <div class="s" style="color:var(--ember)">${day.heat_known === false
        ? 'not recorded'
        : hours(day.heat_minutes) + (day.heat_partial ? '+' : '') + ' heating'}</div>
    </div>`;
  }).join('');

  // How many of the week are still waiting for the meter.
  const estimatedDays = days.filter((day) => day.estimated === true).length;
  const missingDays = days.filter((day) => day.status === 'none').length;
  const partialHeat = days.filter((day) => day.heat_partial === true).length;
  let weekNote = '';
  if (estimatedDays) weekNote += `${estimatedDays} estimated until the meter reports`;
  if (missingDays) weekNote += (weekNote ? ' \u00b7 ' : '') + `${missingDays} not reported`;
  if (partialHeat) {
    weekNote += (weekNote ? ' \u00b7 ' : '')
      + `${partialHeat} only partly watched, so those heating times are a lower bound`;
  }

  /* The week, as one number.
   *
   * The tiles below say what each day cost; this says what the week
   * cost altogether. It is the sum of the days that have a cost, and a
   * day with no cost is left out rather than guessed at - so the
   * number never claims to be more than it knows.
   *
   * When any part of it is an estimate the total says so with a tilde
   * and wears the same plum as the estimates themselves, because a
   * rounded-up guess dressed as a fact is the one thing a cost must
   * never be.
   */
  const costed = days.filter((day) => day.cost !== null && day.cost !== undefined);
  const weekTotal = costed.reduce((sum, day) => sum + Number(day.cost || 0), 0);
  const weekApprox = costed.length !== days.length || estimatedDays > 0;
  const weekPill = costed.length
    ? `<span class="pill cost${weekApprox ? ' approx' : ''}">${weekApprox ? '~' : ''}${money(weekTotal)}</span>`
    : '';

  /* What the strip is showing, and what is not settled.
   *
   * The heating hours are Ember's own count of the polls it saw the
   * boiler on, so they are the time the boiler was actually on. That
   * goes in the heading, where there is room for it, and the note
   * under the strip says what is not settled - the days still waiting
   * for the meter, and the days Ember did not watch all of.
   */
  const weekCaption = 'the heating hours are the time the boiler was actually on';

  const weather = payload.weather || {};
  const wxToday = weather.today || {};
  const wxTomorrow = weather.tomorrow || {};

  /* One day's weather, in the shape a phone shows it.

   * The temperature on the right, the symbol on the left, and the
   * condition in words between them. A day with nothing to say about
   * the weather says nothing - it does not draw a sunny sky it did not
   * read.
   */
  function weatherRow(day, label, temperature) {
    const range = (day.min !== null && day.min !== undefined
      && day.max !== null && day.max !== undefined)
      ? num(day.min, 0) + '\u00b0 \u2013 ' + num(day.max, 0) + '\u00b0' : '';
    return `<div class="wx-row">
      <div class="wx-ico">${weatherIcon(day.icon || 'thermometer', 30)}</div>
      <div class="wx-main">
        <div class="wx-day">${esc(label)}</div>
        <div class="wx-cond">${esc(day.label || '')}</div>
      </div>
      <div class="wx-right">
        <div class="wx-temp">${esc(temperature)}</div>
        ${range ? `<div class="wx-range">${esc(range)}</div>` : ''}
      </div>
    </div>`;
  }

  const currentTemp = (weather.current && weather.current.temp !== null
    && weather.current.temp !== undefined)
    ? num(weather.current.temp, 1) + '\u00b0' : '\u2014';
  const todayTemp = (wxToday.temp !== null && wxToday.temp !== undefined)
    ? num(wxToday.temp, 1) + '\u00b0' : currentTemp;
  const tomorrowTemp = (wxTomorrow.temp !== null && wxTomorrow.temp !== undefined)
    ? num(wxTomorrow.temp, 0) + '\u00b0' : '\u2014';

  const weatherBody = `
    <div class="wx">
      ${weatherRow(wxToday, 'Today', todayTemp)}
      ${weatherRow(wxTomorrow, 'Tomorrow', tomorrowTemp)}
    </div>
    <div class="wx-sep"></div>`;

  const forecastBody = forecast.available ? `
    <div class="kpi" style="font-size:35px;color:var(--plum)">${num(forecast.tomorrow_m3, 2)}${unit ? `<span class="u">${unit}</span>` : ''}</div>
    <div class="dimmer" style="font-size:11.5px;margin-top:5px">${forecastLine(gas, forecast)}</div>
    <div class="fnote" style="margin-top:10px">
      ${num(forecast.outside_mean, 1)}°C forecast &middot; ${num(forecast.degree_days, 1)} degree-days at a ${num(forecast.base_temperature, 0)}°C base<br>
      Fitted on ${forecast.model ? forecast.model.days_used : 0} days of your own readings.
    </div>` : `
    <div class="fnote" style="margin-top:10px">
      Not enough history yet to forecast.<br>
      ${esc(forecast.reason || '')}
    </div>`;

  return `<div class="slide">
    ${head(SLIDE_TITLES.today)}
    <div class="slide-body s3">
      <div class="panel">
        <div class="p-h"><h4>Today</h4><span class="src">gas from your Octopus meter &middot; heat from Hive</span></div>
        ${trouble(problemFor('octopus') || problemFor('hive'))}
        <div class="big">
          <div>
            <div class="kpi" style="font-size:45px${est ? ';color:var(--plum)' : ''}">${gas.today_reported ? num(gas.today_m3, 2) + `<span class="u">${unit}</span>` : (est ? num(est.m3, 2) + `<span class="u">${unit}</span>` : '—')}</div>
            <div class="${est ? 'plum' : 'dimmer'}" style="font-size:12px;margin-top:6px">${gas.today_reported
      ? `${money(gas.cost_today)} so far &middot; ${hours(heating.heating_today_minutes)} of heating`
      : (est ? `about ${money(est.cost)} estimated &middot; ${hours(heating.heating_today_minutes)} of heating`
        : `${lagNote(gas)} &middot; ${hours(heating.heating_today_minutes)} of heating`)}</div>
            ${gas.meter_note ? `<div class="dimmer" style="font-size:11.5px;margin-top:4px">${esc(gas.meter_note)}</div>` : ''}
          </div>
          <div class="side">
            <div class="k">vs 7-day average</div>
            <div class="v ember">${comparison(payload)}${comparison(payload) === '—' ? '' : '%'}</div>
            <div class="k" style="margin-top:11px">month budget</div>
            <div class="v">${money(budget.spend)}<span style="color:var(--dimmer);font-size:11px"> of ${money(budget.budget)}</span></div>
          </div>
        </div>
        <div class="svgwrap" style="margin-top:8px">
          ${chartDayShape(gas, width, height)
    || `<div class="fnote">${chartEmpty(gas, 'day')}</div>`}
        </div>
        <div class="dimmer" style="font-size:10.5px;margin-top:2px">
          the shape of a day &middot; the average use in each hour, over the last
          ${num((gas.hourly_profile || {}).days || 0, 0)} days
        </div>
      </div>

      <div class="panel">
        <div class="p-h"><h4>Today &amp; tomorrow</h4><span class="src">weather &middot; gas forecast</span></div>
        ${weatherBody}
        <div class="wx-gas-label">Tomorrow's gas</div>
        ${forecastBody}
      </div>

      <div class="panel" style="grid-column:1/-1">
        <div class="p-h"><h4>The week</h4>${weekPill}<span class="src">${weekCaption}</span></div>
        <div class="tiles weekstrip" style="grid-template-columns:repeat(7,1fr)">${week}</div>
        ${weekNote ? `<div class="dimmer" style="font-size:11.5px;margin-top:6px">${weekNote}</div>` : ''}
      </div>
    </div>
  </div>`;
}

/* -- the shell ----------------------------------------------------------- */

const RENDERERS = { heating: slideHeating, gas: slideGas, today: slideToday };

/* -- the slide transition ----------------------------------------------- */

/* The slide comes in from the right and the one leaving fades away
 * underneath it, the way a card is dealt onto a deck.
 *
 * The animation is started from here rather than from a rule in the
 * stylesheet, because a CSS animation runs once and then keeps its
 * final state: the rule for a slide stays matched while that slide is
 * showing, so it would not run again on the next change of slide.
 * Starting it here means every change gets one.
 *
 * Someone who has asked the browser for less movement should not be
 * given any.
 */
let SLIDE_ANIM = null;
let GHOST = null;
/* The slide that is on the screen now.  A change of slide is what sets
 * the transition off; a new reading for the same slide is not. */
let LAST_SLIDE = '';
/* Which way the slide was asked for: 1 for the next one, -1 for the one
 * before it.  The next slide comes in from the right, the one before
 * from the left. */
let SLIDE_WAY = 0;
/* The swipe in progress, if there is one. */
let SWIPE = null;

/* Ask for the next slide, or the one before it.
 *
 * The slide is shown straight away: a gesture that takes half a second
 * to answer does not feel like it was heard.
 */
function goSlide(step) {
  if (state.slides.length < 2) return;
  SLIDE_WAY = step;
  state.index = (state.index + step + state.slides.length) % state.slides.length;
  restartTimer();
  render();
}

function slideTransition(ghost, way) {
  const deck = document.getElementById('deck');
  const live = deck.querySelector('.slide:not(.ghost)');
  const still = window.matchMedia
              && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* The copy dissolves under the slide that is arriving.  It is a
   * little slower than the slide itself, so the old one is still
   * faintly there for a moment after the new one has landed. */
  if (ghost) {
    if (still || !ghost.animate) {
      ghost.remove();
      if (GHOST === ghost) GHOST = null;
    } else {
      const g = ghost.animate(
        [{opacity: 1, transform: 'translateX(0)'},
         {opacity: 0, transform: 'translateX(0)'}],
        {duration: 560, delay: 60, easing: 'ease-in-out'});
      g.onfinish = () => {
        ghost.remove();
        if (GHOST === ghost) GHOST = null;
      };
    }
  }

  if (!live || !live.animate || still) return;
  if (SLIDE_ANIM) { SLIDE_ANIM.cancel(); SLIDE_ANIM = null; }
  live.classList.add('dealing');
  const from = (way < 0) ? '-100%' : '100%';
  SLIDE_ANIM = live.animate(
    [{transform: 'translateX(' + from + ')', opacity: .8},
     {transform: 'translateX(0)', opacity: 1}],
    {duration: 420, easing: 'cubic-bezier(.22,.61,.36,1)'});
  SLIDE_ANIM.onfinish = () => live.classList.remove('dealing');
}

function render() {
  const deck = document.getElementById('deck');
  const head = document.getElementById('head');
  if (!state.payload) {
    deck.innerHTML = `<div class="slide"><div class="slide-body">
      <div class="panel" style="grid-column:1/-1"><div class="fnote">Waiting for the first reading…</div></div>
    </div></div>`;
    return;
  }
  const name = state.slides[state.index] || 'heating';
  const renderer = RENDERERS[name] || slideHeating;

  /* The slide that is on its way out is copied before the new one is put
   * in place, so that the copy still holds the old slide.  It fades away
   * under the slide that is arriving.
   *
   * Only a change of slide does this.  The dashboard is rebuilt on every
   * reading - every two minutes - and sliding it every time would be
   * more movement than anyone wants. */
  const changed = Boolean(LAST_SLIDE) && name !== LAST_SLIDE;
  let ghost = null;
  if (changed) {
    const leaving = deck.querySelector('.slide:not(.ghost)');
    if (leaving) {
      if (GHOST) { GHOST.remove(); GHOST = null; }
      ghost = leaving.cloneNode(true);
      /* The copy has the same element ids in it as the live slide, and
       * the live one has to be the one the browser finds when the code
       * goes looking for the clock or the dots. */
      ghost.querySelectorAll('[id]').forEach((el) => el.removeAttribute('id'));
      /* The header does not belong to the slide - it lives above it -
       * so the copy must not carry one either. */
      ghost.querySelectorAll('.slide-head').forEach((el) => el.remove());
      ghost.classList.add('ghost');
    }
  }
  LAST_SLIDE = name;

  deck.innerHTML = renderer(state.payload);

  /* The header does not change with the slide, so it is moved out of it
   * and into the one above, where it stays put while the slide under it
   * is dealt in. */
  const live = deck.querySelector('.slide:not(.ghost)');
  const fresh = live.querySelector('.slide-head');
  if (fresh) {
    head.replaceChildren(...Array.from(fresh.childNodes));
    fresh.remove();
  }
  if (ghost) deck.appendChild(ghost);

  live.querySelectorAll('.tabs [data-tab]').forEach((tab) => {
    tab.addEventListener('click', () => {
      state.gasTab = tab.dataset.tab;
      render();
    });
  });

  const dots = head.querySelector('#dots');
  if (dots) {
    dots.innerHTML = state.slides.map((name, index) =>
      `<button class="${index === state.index ? 'on' : ''}" data-index="${index}"
        aria-label="${esc(SLIDE_TITLES[name] || name)}"></button>`).join('');
    dots.querySelectorAll('button').forEach((button) => {
      button.addEventListener('click', () => {
        const next = Number(button.dataset.index);
        /* A slide further along comes in from the right, one further
         * back from the left. */
        SLIDE_WAY = next > state.index ? 1 : (next < state.index ? -1 : 0);
        state.index = next;
        restartTimer();
        render();
      });
    });
  }

  if (changed) {
    slideTransition(ghost, SLIDE_WAY);
    /* The direction has been used, so the next change - the rotation's
     * own - comes in from the right as usual. */
    SLIDE_WAY = 0;
  }
}

function fit() {
  const wide = window.innerWidth >= 800;
  const stage = document.getElementById('stage');
  if (!wide) {
    stage.style.setProperty('--k', '1');
    return;
  }
  const scale = Math.min(window.innerWidth / 800, window.innerHeight / 480);
  stage.style.setProperty('--k', String(Math.max(0.2, scale)));
}

function restartTimer() {
  if (state.timer) clearInterval(state.timer);
  if (state.pinned) return;
  if (state.slides.length < 2 || state.rotation <= 0) return;
  state.timer = setInterval(() => {
    state.index = (state.index + 1) % state.slides.length;
    /* The rotation always goes forwards, so the slide always comes in
     * from the right. */
    SLIDE_WAY = 1;
    render();
  }, state.rotation * 1000);
}

function apply(payload) {
  state.payload = payload;
  render();
}

/* Keep the clock ticking.
 *
 * The dashboard is rebuilt on every poll, which is every two minutes.
 * A clock that jumped every two minutes would not be a clock, so the
 * clock is painted on its own, on a shorter interval.
 */
function paintClock() {
  const element = document.getElementById('clock');
  if (!element) return;
  element.innerHTML = esc(clockFull(new Date()));
}

async function boot() {
  // A deep link pins one slide: /?slide=gas shows the gas slide and
  // stops rotating, which is handy for a bookmark and for a screenshot.
  const params = new URLSearchParams(window.location.search);
  const wanted = params.get('slide');
  if (wanted) {
    state.pinned = true;
  }

  try {
    const response = await fetch('/api/settings');
    const settings = await response.json();
    state.settings = settings;
    if (Array.isArray(settings.slides) && settings.slides.length) {
      state.slides = settings.slides;
    }
    state.rotation = Number(settings.rotation_seconds) || 20;
  } catch (error) {
    /* the defaults will do */
  }

  if (wanted) {
    const index = state.slides.indexOf(wanted);
    if (index >= 0) state.index = index;
  }

  try {
    const response = await fetch('/api/state');
    const payload = await response.json();
    if (payload && Object.keys(payload).length) apply(payload);
  } catch (error) {
    /* the socket will bring it */
  }

  const source = new EventSource('/api/events');
  source.onmessage = (event) => {
    try {
      apply(JSON.parse(event.data));
    } catch (error) {
      /* ignore a malformed frame */
    }
  };
  source.onerror = () => {
    /* EventSource reconnects on its own */
  };

  fit();
  window.addEventListener('resize', fit);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowRight') {
      state.index = (state.index + 1) % state.slides.length;
      SLIDE_WAY = 1;
      render();
    }
    if (event.key === 'ArrowLeft') {
      state.index = (state.index + state.slides.length - 1) % state.slides.length;
      /* Going back, the slide comes in from the left. */
      SLIDE_WAY = -1;
      render();
    }
  });

  /* -- swiping ----------------------------------------------------------- */

  /* A drag across the panel moves to the next slide or the one before,
   * the same as the arrow keys.  The panel is on a touch screen, so
   * this is the way it is actually driven.
   *
   * The gesture is measured rather than assumed: it has to be longer
   * than SWIPE_DISTANCE, and clearly across rather than down - a drag
   * down the screen is somebody trying to scroll, not to change slide.
   */
  const SWIPE_DISTANCE = 60;
  const SWIPE_SLOP = 1.4;

  function swipeIsOne(g) {
    return Math.abs(g.dx) >= SWIPE_DISTANCE
        && Math.abs(g.dx) > Math.abs(g.dy) * SWIPE_SLOP;
  }

  function swipeStart(e) {
    /* A mouse only swipes with its left button, and a second finger on
     * a touch screen is not a swipe either. */
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    if (!e.isPrimary) return;
    /* Buttons, links and the like are left to themselves. */
    const target = e.target;
    if (target && target.closest
        && target.closest('a,button,input,select,textarea,label')) return;
    SWIPE = {id: e.pointerId, x: e.clientX, y: e.clientY, dx: 0, dy: 0};
    /* The finger keeps the gesture even if it wanders off the element
     * it started on. */
    try { e.target.setPointerCapture(e.pointerId); } catch (_) {}
  }

  function swipeMove(e) {
    if (!SWIPE || e.pointerId !== SWIPE.id) return;
    SWIPE.dx = e.clientX - SWIPE.x;
    SWIPE.dy = e.clientY - SWIPE.y;
    /* Once it is clear that this is a swipe, the browser is told to keep
     * its hands off - otherwise it tries to scroll or to select the
     * text. */
    if (swipeIsOne(SWIPE) && e.cancelable) e.preventDefault();
  }

  function swipeEnd(e) {
    if (!SWIPE || e.pointerId !== SWIPE.id) return;
    const dx = SWIPE.dx, dy = SWIPE.dy;
    SWIPE = null;
    if (!swipeIsOne({dx: dx, dy: dy})) return;
    /* Left is the next slide, right the one before. */
    goSlide(dx < 0 ? 1 : -1);
  }

  window.addEventListener('pointerdown', swipeStart, {passive: true});
  window.addEventListener('pointermove', swipeMove, {passive: false});
  window.addEventListener('pointerup', swipeEnd);
  window.addEventListener('pointercancel', () => { SWIPE = null; });

  restartTimer();
  render();
  setInterval(paintClock, 10000);
}

boot();
