/**
 * Reference analysis: a song's structure and energy per bar, from its samples and Live's beat grid.
 *
 * Pure functions (no Max, no files), shared by the reference device's node.script and the tests.
 *
 *   analyze(samples, sampleRate, beats, options) -> {bars, sections, key, ...}
 *
 * `samples` is mono audio, `beats` the sample position of every beat boundary in arrangement
 * order (beats.length - 1 beats), so bar n spans beats[4n] .. beats[4n + 4]. Live's warp markers
 * give that grid; this module never guesses the tempo itself.
 *
 * Bands are rough stand-ins for parts, not a separation: kick and sub share the lows, and a kick
 * counts as present where the lows rise at the song's own kick positions (a held bass does not).
 */

"use strict";

const BANDS = {
  low: [30, 120], // kick and sub
  bass: [120, 300],
  mid: [300, 4000], // chords, leads, vocals
  high: [6000, 11000], // hats, cymbals, noise
};
const FLOOR_DB = -60;
// Genres as plan_song knows them: the usual tempo (an unwarped clip's tempo search leans towards it,
// which settles half or double time, e.g. Drum & Bass at 174 rather than 87), the bar grid sections
// start on (step) and their shortest length (phrase), and section names where the genre has its own.
const TEMPO_RANGE = [70, 180];
const GENRES = {
  auto: { prior: 125, step: 4, phrase: 8, names: {} },
  house: { prior: 124, step: 4, phrase: 8, names: {} },
  melodic_techno: { prior: 124, step: 4, phrase: 8, names: { break: "Breakdown" } },
  peak_time_techno: { prior: 135, step: 4, phrase: 8, names: { build: "Lift", drop: "Peak", break: "Breakdown" } },
  drum_and_bass: { prior: 174, step: 8, phrase: 16, names: { break: "Breakdown" } },
  trap: { prior: 140, step: 4, phrase: 8, names: { groove: "Verse" } },
};

/** The profile for a genre; Auto takes Drum & Bass's longer phrases from 160 BPM on. */
function profile(genre, bpm) {
  if (GENRES[genre] && genre !== "auto") return { genre, ...GENRES[genre] };
  if (bpm >= 160) return { ...GENRES.drum_and_bass, genre: "auto", names: {} };
  return { genre: "auto", ...GENRES.auto };
}
const NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
// Krumhansl-Kessler key profiles
const MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88];
const MINOR = [6.33, 2.68, 3.52, 5.38, 2.6, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17];

function fftSize(sampleRate) {
  // ~46 ms: 1024 at 22.05 kHz, 2048 at 44.1/48 kHz
  let n = 256;
  while (n * 2 <= sampleRate * 0.0465 * 1.5) n *= 2;
  return n;
}

/** In-place radix-2 FFT of (re, im). */
function fft(re, im) {
  const n = re.length;
  for (let i = 1, j = 0; i < n; i++) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      [re[i], re[j]] = [re[j], re[i]];
      [im[i], im[j]] = [im[j], im[i]];
    }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const angle = (-2 * Math.PI) / len;
    const wr = Math.cos(angle);
    const wi = Math.sin(angle);
    for (let i = 0; i < n; i += len) {
      let cr = 1;
      let ci = 0;
      for (let k = 0; k < len / 2; k++) {
        const a = i + k;
        const b = a + len / 2;
        const tr = re[b] * cr - im[b] * ci;
        const ti = re[b] * ci + im[b] * cr;
        re[b] = re[a] - tr;
        im[b] = im[a] - ti;
        re[a] += tr;
        im[a] += ti;
        const next = cr * wr - ci * wi;
        ci = cr * wi + ci * wr;
        cr = next;
      }
    }
  }
}

/** Per FFT frame: band powers, total power, spectral centroid (Hz); plus the track's chroma. */
function frames(samples, sampleRate) {
  const n = fftSize(sampleRate);
  const hop = n / 2;
  const window = new Float64Array(n).map((_, i) => 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / n));
  const binHz = sampleRate / n;
  const ranges = Object.fromEntries(Object.entries(BANDS).map(([k, [lo, hi]]) => [k, [Math.max(1, Math.round(lo / binHz)), Math.min(n / 2, Math.round(hi / binHz))]]));
  const count = Math.max(0, Math.floor((samples.length - n) / hop) + 1);
  const out = { hop, n, count, center: new Float64Array(count), total: new Float64Array(count), centroid: new Float64Array(count) };
  for (const band of Object.keys(BANDS)) out[band] = new Float64Array(count);
  const re = new Float64Array(n);
  const im = new Float64Array(n);
  for (let f = 0; f < count; f++) {
    const start = f * hop;
    for (let i = 0; i < n; i++) {
      re[i] = samples[start + i] * window[i];
      im[i] = 0;
    }
    fft(re, im);
    let total = 0;
    let weighted = 0;
    for (let k = 1; k < n / 2; k++) {
      const power = re[k] * re[k] + im[k] * im[k];
      total += power;
      weighted += power * k * binHz;
    }
    for (const [band, [lo, hi]] of Object.entries(ranges)) {
      let sum = 0;
      for (let k = lo; k < hi; k++) sum += re[k] * re[k] + im[k] * im[k];
      out[band][f] = sum;
    }
    out.center[f] = start + n / 2;
    out.total[f] = total;
    out.centroid[f] = total > 0 ? weighted / total : 0;
  }
  return out;
}

const pitchClass = (hz) => ((Math.round(12 * Math.log2(hz / 440)) % 12) + 12 + 9) % 12;

/** Pitch-class profiles over the whole song: notes 80 Hz - 2 kHz, and the bass (35-160 Hz) that
 * usually carries the root. Long frames (~0.37 s) resolve the low notes; each frame counts alike. */
function chromas(samples, sampleRate) {
  let n = 1024;
  while (n * 2 <= sampleRate * 0.37 * 1.5) n *= 2;
  const window = new Float64Array(n).map((_, i) => 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / n));
  const binHz = sampleRate / n;
  const notes = new Float64Array(12);
  const bass = new Float64Array(12);
  const re = new Float64Array(n);
  const im = new Float64Array(n);
  const mags = new Float64Array(n / 2);
  for (let start = 0; start + n <= samples.length; start += n / 2) {
    for (let i = 0; i < n; i++) {
      re[i] = samples[start + i] * window[i];
      im[i] = 0;
    }
    fft(re, im);
    const c = new Float64Array(12);
    const b = new Float64Array(12);
    for (let k = 0; k < n / 2; k++) mags[k] = Math.sqrt(re[k] * re[k] + im[k] * im[k]);
    // Only spectral peaks count: tones are peaks, while noise, hats and the kick's thump spread over
    // every bin and would flatten the chroma until major and minor look alike.
    for (let k = 3; k < n / 2 - 3; k++) {
      const mag = mags[k];
      if (!(mag > mags[k - 1] && mag >= mags[k + 1] && mag > 2 * mags[k - 3] && mag > 2 * mags[k + 3])) continue;
      // the peak's true frequency between bins (parabolic interpolation on the magnitudes)
      const shift = (0.5 * (mags[k - 1] - mags[k + 1])) / (mags[k - 1] - 2 * mag + mags[k + 1] || 1);
      const hz = (k + shift) * binHz;
      if (hz >= 80 && hz <= 2000) c[pitchClass(hz)] += mag;
      else if (hz >= 35 && hz < 80) b[pitchClass(hz)] += mag;
      if (hz >= 80 && hz < 160) b[pitchClass(hz)] += mag;
    }
    const mc = Math.max(...c) || 1;
    const mb = Math.max(...b) || 1;
    for (let i = 0; i < 12; i++) {
      notes[i] += c[i] / mc;
      bass[i] += b[i] / mb;
    }
  }
  return { notes, bass };
}

const db = (power) => (power > 0 ? Math.max(FLOOR_DB, 10 * Math.log10(power)) : FLOOR_DB);
const mean = (values) => (values.length ? values.reduce((a, b) => a + b, 0) / values.length : 0);

const BASS_WEIGHT = 0.5;
const KEY_TIE = 0.15; // a runner-up this close: "C major / A minor", or "D# minor?" on the same root
// Dance music is mostly minor, and much of it barely plays the third that tells major from minor
// (root, fifth, drones): without it, minor.
const MINOR_BIAS = 0.15;

/** Krumhansl-Kessler profiles over the notes, plus a bonus for the bass's strongest pitch classes. */
function key({ notes, bass }) {
  const norm = (v) => {
    const m = mean(v);
    const d = Math.sqrt(v.reduce((s, x) => s + (x - m) ** 2, 0)) || 1;
    return v.map((x) => (x - m) / d);
  };
  const c = norm(Array.from(notes));
  const top = Math.max(...bass) || 1;
  const scores = [];
  for (const [mode, profile] of [["major", MAJOR], ["minor", MINOR]]) {
    for (let root = 0; root < 12; root++) {
      const p = norm(profile.map((_, i) => profile[(i - root + 12) % 12]));
      const score = c.reduce((s, x, i) => s + x * p[i], 0) + (BASS_WEIGHT * bass[root]) / top + (mode === "minor" ? MINOR_BIAS : 0);
      scores.push({ score, name: `${NOTE_NAMES[root]} ${mode}`, root, mode });
    }
  }
  scores.sort((a, b) => b.score - a.score);
  const [best, second] = scores;
  if (!second || best.score - second.score >= KEY_TIE) return best;
  // the same root either way: the third is missing, the mode a guess; relative keys share their notes
  return { ...best, name: second.root === best.root ? `${best.name}?` : `${best.name} / ${second.name}` };
}

/** Per beat: mean band powers and brightness. */
function perBeat(fr, beats) {
  const result = [];
  let f = 0;
  for (let b = 0; b + 1 < beats.length; b++) {
    const lo = beats[b];
    const hi = beats[b + 1];
    while (f < fr.count && fr.center[f] < lo) f++;
    const sums = { low: 0, bass: 0, mid: 0, high: 0, total: 0, centroid: 0 };
    let k = f;
    for (; k < fr.count && fr.center[k] < hi; k++) {
      for (const band of ["low", "bass", "mid", "high", "total"]) sums[band] += fr[band][k];
      sums.centroid += fr.centroid[k] * fr.total[k];
    }
    const frames_ = k - f;
    const beat = { frames: frames_ };
    for (const band of ["low", "bass", "mid", "high", "total"]) beat[band] = frames_ ? sums[band] / frames_ : 0;
    beat.centroid = sums.total > 0 ? sums.centroid / sums.total : 0;
    result.push(beat);
  }
  return result;
}

const KICK_BINS = 32; // positions per bar for the kick template (1/32 notes)
const KICK_SCORE_DB = 4; // a bar's lows rise this much more at the song's kick positions than elsewhere
const KICK_MIN_LOW_DB = -30; // lows relative to the loudest bar

/** How strongly the lows rise (dB, frame to frame) at each 1/32 of every bar. */
function lowOnsets(fr, beats, beatsPerBar) {
  const rise = new Float64Array(fr.count);
  for (let f = 1; f < fr.count; f++) rise[f] = Math.max(0, db(fr.low[f]) - db(fr.low[f - 1]));
  const bars = [];
  let f = 0;
  for (let i = 0; i + beatsPerBar < beats.length; i += beatsPerBar) {
    const start = beats[i];
    const length = beats[i + beatsPerBar] - start;
    const v = new Float64Array(KICK_BINS);
    while (f < fr.count && fr.center[f] < start) f++;
    for (let k = f; k < fr.count && fr.center[k] < start + length; k++) {
      const bin = Math.min(KICK_BINS - 1, Math.floor(((fr.center[k] - start) / length) * KICK_BINS));
      v[bin] = Math.max(v[bin], rise[k]);
    }
    bars.push(v);
  }
  return bars;
}

const KICK_WINDOW = 8; // bars each side: the template follows a slowly drifting grid

function template(pool) {
  const t = new Float64Array(KICK_BINS);
  for (let b = 0; b < KICK_BINS; b++) {
    const values = pool.map((v) => v[b]).sort((x, y) => x - y);
    t[b] = values[Math.floor(values.length / 2)];
  }
  return t;
}

/** Per bar 0-1: the lows rise at the usual kick positions of the loud bars around it. A held bass
 * or a pad swell rises anywhere, so it does not count; a kick pattern of any kind does. A position
 * counts with its neighbours (+-1/32), so a grid a little off still finds the kick. */
function kickPresence(onsets, loud) {
  const everywhere = onsets.filter((_, i) => loud[i]);
  if (!everywhere.length) return onsets.map(() => 0);
  return onsets.map((v, i) => {
    const near = onsets.slice(Math.max(0, i - KICK_WINDOW), i + KICK_WINDOW + 1).filter((_, j) => loud[Math.max(0, i - KICK_WINDOW) + j]);
    const t = template(near.length >= KICK_WINDOW ? near : everywhere);
    const peak = Math.max(...t);
    if (peak <= 0) return 0;
    const at = (b) => Math.max(v[b], v[(b + 1) % KICK_BINS], v[(b + KICK_BINS - 1) % KICK_BINS]);
    const hits = [];
    const rest = [];
    for (let b = 0; b < KICK_BINS; b++) (t[b] >= peak * 0.5 ? hits : rest).push(b);
    const far = rest.filter((b) => !hits.some((h) => Math.abs(h - b) <= 1 || Math.abs(h - b) >= KICK_BINS - 1));
    const score = mean(hits.map(at)) - mean((far.length ? far : rest).map((b) => v[b]));
    return Math.max(0, Math.min(1, score / (2 * KICK_SCORE_DB)));
  });
}

function perBar(beats, beatsPerBar) {
  const bars = [];
  for (let i = 0; i + beatsPerBar <= beats.length; i += beatsPerBar) {
    const group = beats.slice(i, i + beatsPerBar).filter((b) => b.frames > 0);
    const bar = { index: bars.length };
    for (const band of ["low", "bass", "mid", "high", "total"]) bar[band] = db(mean(group.map((b) => b[band])));
    const power = group.reduce((s, b) => s + b.total, 0);
    bar.centroid = power > 0 ? group.reduce((s, b) => s + b.centroid * b.total, 0) / power : 0;
    bars.push(bar);
  }
  // relative to the loudest bar per band: 0 dB = the song's peak
  for (const band of ["low", "bass", "mid", "high", "total"]) {
    const top = Math.max(...bars.map((b) => b[band]));
    for (const bar of bars) bar[band] = Math.max(FLOOR_DB, bar[band] - top);
  }
  return bars;
}

const FEATURES = ["low", "bass", "mid", "high", "total"];

// The device's Sensitivity fields, 0..100 each (default 50). Their mean is how finely the song is split:
// at 100 every change found is a section, lower merges more (MERGING). Their ratio is what counts as a
// change: each band weighs as much as its field against the highest one, the total level as their mean.
// The fields are the view's lanes: Kick, Lows (30-300 Hz: the low and bass bands), Mids, Hats (high).
const SENSITIVITY = ["kick", "low", "mid", "high"];
const DEFAULT_SENSITIVITY = 50;
const FULL_SENSITIVITY = [1, 1, 1, 1, 1, 1];

/** {values: the fields, level: their mean 0..1, shape: weights in vector()'s order (low, bass, mid, high,
 * total, kick), each field over the highest} from {kick, low, mid, high: 0..100}. */
function weights(sensitivity = {}) {
  const values = {};
  for (const band of SENSITIVITY) {
    const value = Number(sensitivity[band]);
    values[band] = Number.isFinite(value) ? Math.max(0, Math.min(100, Math.round(value))) : DEFAULT_SENSITIVITY;
  }
  const top = Math.max(...SENSITIVITY.map((band) => values[band]));
  const w = (band) => (top > 0 ? values[band] / top : 1);
  const shape = [w("low"), w("low"), w("mid"), w("high"), mean(SENSITIVITY.map(w)), w("kick")];
  return { values, level: mean(SENSITIVITY.map((band) => values[band])) / 100, shape };
}

function vector(bars, w = FULL_SENSITIVITY) {
  const v = FEATURES.map((band) => mean(bars.map((b) => Math.max(-40, b[band]))));
  v.push(mean(bars.map((b) => b.kick)) * 12); // a kick that comes or goes counts like 12 dB
  return v.map((x, i) => x * w[i]);
}

const distance = (a, b) => Math.sqrt(a.reduce((s, x, i) => s + (x - b[i]) ** 2, 0) / a.length);

/** How much the phrase after `b` differs from the one before: the largest change of one feature, so
 * a single element coming in or out (hats, a pad) counts as much as everything changing a little. */
function novelty(bars, b, phrase, w) {
  const before = vector(bars.slice(Math.max(0, b - phrase), b), w);
  const after = vector(bars.slice(b, b + phrase), w);
  return Math.max(...before.map((x, i) => Math.abs(after[i] - x)));
}

/** Section starts on the genre's bar grid: the strongest changes, each at least a phrase from the next. */
function boundaries(bars, threshold, { step, phrase }, w = FULL_SENSITIVITY) {
  const candidates = [];
  for (let b = step; b <= bars.length - step; b += step) candidates.push({ b, n: novelty(bars, b, phrase, w) });
  const strength = (b) => (candidates.find((c) => c.b === b) || { n: 0 }).n;
  const peaks = candidates.filter((c) => c.n >= threshold && c.n >= strength(c.b - step) && c.n >= strength(c.b + step));
  // strongest first; on a tie the 8 bar phrase wins
  peaks.sort((x, y) => y.n - x.n || (x.b % phrase) - (y.b % phrase));
  const starts = [0];
  for (const p of peaks) {
    if (p.b < step || bars.length - p.b < step) continue;
    if (starts.some((s) => s > 0 && Math.abs(s - p.b) < phrase)) continue;
    starts.push(p.b);
  }
  return starts.sort((x, y) => x - y);
}

function slope(values) {
  if (values.length < 2) return 0;
  const n = values.length;
  const mx = (n - 1) / 2;
  const my = mean(values);
  let num = 0;
  let den = 0;
  values.forEach((y, x) => {
    num += (x - mx) * (y - my);
    den += (x - mx) ** 2;
  });
  return num / den;
}

function describe(bars, start, end) {
  const part = bars.slice(start, end);
  const kick = mean(part.map((b) => (b.kick >= 0.5 ? 1 : 0))); // share of bars with a clear kick
  const elements = [];
  if (kick >= 0.5) elements.push("kick");
  if (mean(part.map((b) => b.bass)) > -18) elements.push("bass");
  if (mean(part.map((b) => b.mid)) > -18) elements.push("mids");
  if (mean(part.map((b) => b.high)) > -20) elements.push("hats");
  const bright = part.map((b) => b.centroid);
  const change = bright[0] > 0 ? (bright[bright.length - 1] - bright[0]) / bright[0] : 0;
  return {
    start_bar: start + 1,
    bars: end - start,
    energy: Math.round(mean(part.map((b) => b.total)) * 10) / 10,
    kick: Math.round(kick * 100) / 100,
    elements,
    brightness: change > 0.3 ? "opens" : change < -0.3 ? "closes" : "steady",
    rising: slope(part.map((b) => b.total)) > 0.15,
  };
}

/** Names from energy and the kick: Drop, Break, Build (rising into a drop), Intro, Outro, Groove. */
function label(sections, names = {}) {
  const loudest = Math.max(...sections.map((s) => s.energy));
  for (const s of sections) s.kind = s.kick >= 0.6 && s.energy >= loudest - 3 ? "drop" : s.kick < 0.3 ? "break" : "groove";
  sections.forEach((s, i) => {
    const next = sections[i + 1];
    if (s.kind !== "drop" && next && next.kind === "drop" && s.bars <= 16 && (s.rising || s.brightness === "opens")) s.kind = "build";
  });
  // A drop comes out of something quieter; full energy right after a drop is its groove going on.
  const loud = sections.map((s) => s.kind === "drop");
  sections.forEach((s, i) => {
    if (i > 1 && loud[i] && loud[i - 1]) s.kind = "groove"; // the first section becomes the intro below
  });
  // A DJ intro often has the kick and most of the energy already: the first section is the intro.
  // The last is the outro unless the song ends at full energy (a radio edit ending on its drop).
  if (sections.length > 1) {
    sections[0].kind = "intro";
    const last = sections[sections.length - 1];
    if (last.energy < loudest - 3 || last.kind !== "drop") last.kind = "outro";
  }
  return name(sections, names);
}

/** "Drop", "Drop 2", ... in the genre's words. */
function name(sections, names = {}) {
  const seen = {};
  for (const s of sections) {
    seen[s.kind] = (seen[s.kind] || 0) + 1;
    const title = names[s.kind] || s.kind[0].toUpperCase() + s.kind.slice(1);
    s.name = seen[s.kind] > 1 ? `${title} ${seen[s.kind]}` : title;
  }
  return sections;
}

// MERGING: neighbours merge, the most alike first, while their cost stays under a limit that rises from
// 0 (fields at 100: every change found is a section) to MAX_COST (fields at 0). The same kind next to
// itself costs how far apart the two sound (four breaks in a row are one long break by 50); a drop next
// to its groove costs DROP_GROOVE more (one drop below 50); other kinds, the intro and the outro never
// merge. The changes within a section stay as its phrases.
const MAX_COST = 24; // dB: phrases of one kind differ by up to ~8 dB (The Signal's breaks)
const DROP_GROOVE = 12;
const FULL = ["drop", "groove"];

function cost(bars, a, b, shape) {
  if (["intro", "outro"].includes(a.kind) || ["intro", "outro"].includes(b.kind)) return Infinity;
  const apart = (s) => bars.slice(s.start_bar - 1, s.start_bar - 1 + s.bars);
  const sound = distance(vector(apart(a), shape), vector(apart(b), shape));
  if (a.kind === b.kind) return sound;
  if (FULL.includes(a.kind) && FULL.includes(b.kind)) return DROP_GROOVE + sound / 2;
  return Infinity;
}

function merge(sections, bars, names = {}, w = weights()) {
  const limit = (1 - w.level) * MAX_COST;
  const out = sections.map((s) => ({ ...s, phrases: [{ start_bar: s.start_bar, bars: s.bars, kind: s.kind, energy: s.energy, kick: s.kick, elements: s.elements }] }));
  for (;;) {
    let best = -1;
    let lowest = limit;
    for (let i = 0; i + 1 < out.length; i++) {
      const c = cost(bars, out[i], out[i + 1], w.shape);
      if (c < lowest) [best, lowest] = [i, c];
    }
    if (best < 0) break;
    const [a, b] = out.slice(best, best + 2);
    const first = a.start_bar - 1;
    const together = { ...describe(bars, first, first + a.bars + b.bars), kind: a.kind === "drop" || b.kind === "drop" ? "drop" : a.kind,
      phrases: a.phrases.concat(b.phrases) };
    out.splice(best, 2, together);
  }
  return name(out.map(({ phrases, ...s }) => (phrases.length > 1 ? { ...s, phrases } : s)), names);
}

function analyze(samples, sampleRate, beats, { beatsPerBar = 4, threshold = 4, genre = "auto", bpm = 0, sensitivity = {} } = {}) {
  const genreProfile = profile(genre, bpm);
  const fr = frames(samples, sampleRate);
  const bars = perBar(perBeat(fr, beats), beatsPerBar);
  const kicks = kickPresence(lowOnsets(fr, beats, beatsPerBar), bars.map((b) => b.low > -12));
  // a kick's tail can rise out of near silence; a bar this quiet in the lows has no kick
  bars.forEach((b, i) => (b.kick = b.low > KICK_MIN_LOW_DB ? kicks[i] ?? 0 : 0));
  const w = weights(sensitivity);
  const starts = boundaries(bars, threshold, genreProfile, w.shape);
  const found = label(starts.map((s, i) => describe(bars, s, starts[i + 1] ?? bars.length)), genreProfile.names);
  const sections = merge(found, bars, genreProfile.names, w);
  const k = key(chromas(samples, sampleRate));
  const round = (x) => Math.round(x * 10) / 10;
  return {
    bars: bars.length,
    genre: genreProfile.genre,
    sensitivity: w.values,
    key: k.name,
    sections,
    lanes: {
      kick: bars.map((b) => Math.round(b.kick * 100) / 100),
      low: bars.map((b) => round(b.low)),
      bass: bars.map((b) => round(b.bass)),
      mid: bars.map((b) => round(b.mid)),
      high: bars.map((b) => round(b.high)),
      energy: bars.map((b) => round(b.total)),
    },
  };
}


/** How sharply the sound rises per frame: positive dB steps summed over the bands, minus the local mean. */
function onsetCurve(fr, bands = Object.keys(BANDS)) {
  const curve = new Float64Array(fr.count);
  for (let f = 1; f < fr.count; f++) {
    for (const band of bands) curve[f] += Math.max(0, db(fr[band][f]) - db(fr[band][f - 1]));
  }
  const reach = 20; // frames each side (~0.5 s)
  const out = new Float64Array(fr.count);
  let sum = 0;
  for (let f = 0; f < Math.min(reach, fr.count); f++) sum += curve[f];
  for (let f = 0; f < fr.count; f++) {
    if (f + reach < fr.count) sum += curve[f + reach];
    if (f - reach - 1 >= 0) sum -= curve[f - reach - 1];
    const width = Math.min(fr.count - 1, f + reach) - Math.max(0, f - reach) + 1;
    out[f] = Math.max(0, curve[f] - sum / width);
  }
  return out;
}

/** The onset curve's strength and phase at one tempo: one Fourier coefficient over the whole song. */
function pulse(curve, times, bpm) {
  const w = (2 * Math.PI * bpm) / 60;
  let re = 0;
  let im = 0;
  for (let f = 0; f < curve.length; f++) {
    if (!curve[f]) continue;
    re += curve[f] * Math.cos(w * times[f]);
    im -= curve[f] * Math.sin(w * times[f]);
  }
  return { bpm, strength: Math.hypot(re, im), phase: Math.atan2(im, re) };
}

/** Tempo and beat grid of a song with a steady tempo (an unwarped clip). Dance music pulses
 * strongest in 8th notes (offbeat hats and bass), so the tempo and the 8th grid come from that
 * pulse; the beats are the 8ths where the lows rise more (the kick).
 * Returns {bpm, beats (sample positions of samples[from, to), from the first sounding beat)}. */
function estimateGrid(samples, sampleRate, { from = 0, to = samples.length, genre = "auto" } = {}) {
  const center = (GENRES[genre] || GENRES.auto).prior;
  const fr = frames(samples.subarray(from, to), sampleRate);
  const curve = onsetCurve(fr);
  const times = Array.from(fr.center, (c) => c / sampleRate);
  const eighths = (bpm) => pulse(curve, times, bpm * 2);
  // a tempo an octave from the genre's usual one weighs about half (half or double time)
  const prior = (bpm) => Math.exp(-0.5 * (Math.log2(bpm / center) / 0.8) ** 2);
  let best = null;
  let bestScore = -1;
  for (let bpm = TEMPO_RANGE[0]; bpm <= TEMPO_RANGE[1]; bpm += 0.05) {
    const score = (eighths(bpm).strength + pulse(curve, times, bpm).strength) * prior(bpm);
    if (score > bestScore) [best, bestScore] = [bpm, score];
  }
  let fine = eighths(best);
  for (let bpm = best - 0.05; bpm <= best + 0.05; bpm += 0.002) {
    const p = eighths(bpm);
    if (p.strength > fine.strength) fine = p;
  }
  // most dance music is at a whole BPM: take it when it is about as good
  const whole = eighths(Math.round(fine.bpm / 2));
  if (whole.strength >= fine.strength * 0.95) fine = whole;
  const bpm = fine.bpm / 2;
  const half = 30 / bpm; // seconds per 8th
  // 8th onsets peak where w*t + phase = 0 (mod 2 pi)
  let first = ((-fine.phase / (2 * Math.PI)) * half) % half;
  if (first < 0) first += half;
  // on-beat or off-beat 8ths: where the lows rise more
  const lows = onsetCurve(fr, ["low"]);
  const hop = fr.hop / sampleRate;
  const rise = [0, 0];
  for (let i = 0, t = first; t < times[times.length - 1]; i++, t = first + i * half) {
    const f = Math.round((t - times[0]) / hop);
    rise[i % 2] += Math.max(lows[f - 1] || 0, lows[f] || 0, lows[f + 1] || 0);
  }
  if (rise[1] > rise[0]) first += half;
  const seconds = 2 * half;
  if (first - seconds > -0.05) first -= seconds; // a song that starts on its first kick: the grid lands a hair before 0
  const count = Math.floor((times[times.length - 1] - first) / seconds);
  const all = Array.from({ length: count + 1 }, (_, i) => Math.max(0, from + Math.round((first + i * seconds) * sampleRate)));
  // bar 1 starts at the first beat where the song sounds (DJ tracks start on their downbeat)
  const peak = Math.max(...fr.total);
  const sounding = fr.total.findIndex((p) => p > peak * 1e-4); // -40 dB
  const start = Math.max(0, Math.ceil((times[Math.max(0, sounding)] - first) / seconds - 0.25));
  return { bpm: Math.round(bpm * 100) / 100, beats: all.slice(start) };
}

/** Sample position of each arrangement beat from Live's warp markers ({beat_time, sample_time} in s). */
function beatGrid(markers, firstBeat, beatCount, sampleRate) {
  const sorted = [...markers].sort((a, b) => a.beat_time - b.beat_time);
  const at = (beat) => {
    let i = 0;
    while (i < sorted.length - 2 && sorted[i + 1].beat_time <= beat) i++;
    const a = sorted[i];
    const b = sorted[i + 1] || { beat_time: a.beat_time + 1, sample_time: a.sample_time + 0.5 };
    const seconds = a.sample_time + ((beat - a.beat_time) * (b.sample_time - a.sample_time)) / (b.beat_time - a.beat_time);
    return Math.round(seconds * sampleRate);
  };
  return Array.from({ length: beatCount + 1 }, (_, i) => at(firstBeat + i));
}

/** The file's tempo where Live warps it: beats per minute between the first and last marker. */
function markerTempo(markers) {
  if (markers.length < 2) return null;
  const sorted = [...markers].sort((a, b) => a.beat_time - b.beat_time);
  const a = sorted[0];
  const b = sorted[sorted.length - 1];
  return b.sample_time > a.sample_time ? Math.round(((b.beat_time - a.beat_time) / (b.sample_time - a.sample_time)) * 60 * 10) / 10 : null;
}

module.exports = { GENRES, SENSITIVITY, weights, analyze, merge, chromas, beatGrid, estimateGrid, markerTempo, fft, key, BANDS };
