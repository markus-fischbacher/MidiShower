/**
 * Node for Max side of the MidiBakery Reference Analyzer device: decodes the reference clip's file,
 * analyzes it on Live's beat grid (reference-analysis.js) and keeps the result.
 *
 *   analyze <json>   the clip as the device's v8 code found it (file, arrangement start/end,
 *                    start marker, warping, warp markers, Set tempo) -> "result <json>"
 *   lookup <json>    the same clip: a stored result for it, if any -> "result <json>"
 *   about <version>  the About button: the versions of the device, API and Remote Script -> "status <text>"
 *
 * Results land in <MidiBakery folder>/references/<hash>.json, one per file and placement, where
 * MidiBakery's MCP server reads them. Decoding uses macOS's afconvert (MP3, AAC, AIFF, WAV, FLAC
 * become 22.05 kHz mono); without it only WAV files can be read.
 */

"use strict";

const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFile } = require("child_process");
const { analyze, beatGrid, estimateGrid, markerTempo } = require("./reference-analysis.js");
const { about } = require("./midibakery-about.js");

const SAMPLE_RATE = 22050;
const BEATS_PER_BAR = 4;

function configDir() {
  if (process.env.MIDIBAKERY_HOME) return process.env.MIDIBAKERY_HOME;
  if (process.platform === "darwin") return path.join(os.homedir(), "Library", "Application Support", "MidiBakery");
  if (process.platform === "win32") return path.join(process.env.APPDATA || os.homedir(), "MidiBakery");
  return path.join(os.homedir(), ".config", "midibakery");
}

const REFERENCES = path.join(configDir(), "references");
const LOG_FILE = path.join(configDir(), "device-reference.log");

function log(...parts) {
  try {
    fs.mkdirSync(configDir(), { recursive: true });
    const text = parts.map((p) => (typeof p === "string" ? p : JSON.stringify(p))).join(" ");
    if (fs.existsSync(LOG_FILE) && fs.statSync(LOG_FILE).size > 256 * 1024) fs.renameSync(LOG_FILE, `${LOG_FILE}.1`);
    fs.appendFileSync(LOG_FILE, `${new Date().toISOString()} ${text}\n`);
  } catch (_) {
    // logging must never break the device
  }
}

/** Mono float samples of a WAV file (PCM 16/24/32 bit or float 32). */
function readWav(file) {
  const b = fs.readFileSync(file);
  if (b.toString("ascii", 0, 4) !== "RIFF" || b.toString("ascii", 8, 12) !== "WAVE") throw new Error("not a WAV file");
  let offset = 12;
  let fmt = null;
  let data = null;
  while (offset + 8 <= b.length) {
    const id = b.toString("ascii", offset, offset + 4);
    const size = b.readUInt32LE(offset + 4);
    if (id === "fmt ") {
      fmt = { format: b.readUInt16LE(offset + 8), channels: b.readUInt16LE(offset + 10), rate: b.readUInt32LE(offset + 12), bits: b.readUInt16LE(offset + 22) };
      if (fmt.format === 0xfffe) fmt.format = b.readUInt16LE(offset + 32); // WAVE_FORMAT_EXTENSIBLE: the sub format
    }
    if (id === "data") data = b.subarray(offset + 8, Math.min(b.length, offset + 8 + size));
    offset += 8 + size + (size % 2);
  }
  if (!fmt || !data) throw new Error("WAV without fmt or data chunk");
  const bytes = fmt.bits / 8;
  const frames = Math.floor(data.length / (bytes * fmt.channels));
  const read = fmt.format === 3 && fmt.bits === 32 ? (o) => data.readFloatLE(o)
    : fmt.bits === 16 ? (o) => data.readInt16LE(o) / 32768
    : fmt.bits === 24 ? (o) => data.readIntLE(o, 3) / 8388608
    : fmt.bits === 32 ? (o) => data.readInt32LE(o) / 2147483648
    : null;
  if (!read) throw new Error(`unsupported WAV format ${fmt.format}/${fmt.bits} bit`);
  const samples = new Float32Array(frames);
  for (let f = 0; f < frames; f++) {
    let sum = 0;
    for (let c = 0; c < fmt.channels; c++) sum += read((f * fmt.channels + c) * bytes);
    samples[f] = sum / fmt.channels;
  }
  return { samples, rate: fmt.rate };
}

function decode(file) {
  return new Promise((resolve, reject) => {
    if (process.platform !== "darwin") {
      try {
        resolve(readWav(file));
      } catch (exc) {
        reject(new Error(`${exc.message}: on this system the reference must be a WAV file`));
      }
      return;
    }
    const out = path.join(os.tmpdir(), `midibakery-reference-${process.pid}.wav`);
    execFile("/usr/bin/afconvert", [file, out, "-f", "WAVE", "-d", `LEF32@${SAMPLE_RATE}`, "-c", "1"], { timeout: 120000 }, (error) => {
      if (error) {
        reject(new Error(`afconvert could not decode the file (${error.message.split("\n")[0]})`));
        return;
      }
      try {
        resolve(readWav(out));
      } catch (exc) {
        reject(exc);
      } finally {
        fs.rm(out, { force: true }, () => {});
      }
    });
  });
}

/** Live's warp markers of a warped clip, or null: then the song's own tempo is detected. */
function markers(clip) {
  const given = (clip.warp_markers || []).filter((m) => Number.isFinite(m.beat_time) && Number.isFinite(m.sample_time));
  return clip.warping && given.length >= 2 ? given : null;
}

function storeFile(clip) {
  const id = crypto.createHash("sha1").update(`${clip.file}|${clip.start}|${clip.start_marker}`).digest("hex").slice(0, 16);
  return path.join(REFERENCES, `${id}.json`);
}

/** Sections and their phrases, each placed by `place` (bar number and arrangement beat). */
function placed(sections, place) {
  return sections.map((s) => (s.phrases ? { ...place(s), phrases: s.phrases.map(place) } : place(s)));
}

/** Whole arrangement bars the clip covers, analyzed; bar numbers and beats are the arrangement's. */
function runWarped(clip, warp, samples, rate) {
  const firstBar = Math.ceil(clip.start / BEATS_PER_BAR - 1e-6);
  const lastBar = Math.floor(clip.end / BEATS_PER_BAR + 1e-6);
  if (lastBar - firstBar < 8) throw new Error("the clip covers less than 8 bars of the Arrangement");
  // arrangement beat -> clip beat (Live's warp marker beats count from the clip's own 1.1.1)
  const firstClipBeat = clip.start_marker + firstBar * BEATS_PER_BAR - clip.start;
  const beats = beatGrid(warp, firstClipBeat, (lastBar - firstBar) * BEATS_PER_BAR, rate)
    .map((s) => Math.max(0, Math.min(samples.length, s)));
  const result = analyze(samples, rate, beats, { beatsPerBar: BEATS_PER_BAR, genre: clip.genre, bpm: markerTempo(warp), sensitivity: clip.sensitivity });
  const place = (s) => ({ ...s, start_bar: firstBar + s.start_bar, beat: (firstBar + s.start_bar - 1) * BEATS_PER_BAR });
  const sections = placed(result.sections, place);
  return { ...result, sections, tempo: markerTempo(warp), first_bar: firstBar + 1, bar_grid: "arrangement" };
}

/** An unwarped clip plays the file in real time: the song's own tempo and bars, detected; the
 * sections' arrangement beats follow from their time at the Set tempo. */
function runUnwarped(clip, samples, rate) {
  // an unwarped clip's markers are in seconds
  const offset = clip.start_marker || 0;
  const seconds = ((clip.end - clip.start) * 60) / clip.tempo;
  const from = Math.max(0, Math.round(offset * rate));
  const to = Math.min(samples.length, Math.round((offset + seconds) * rate));
  const grid = estimateGrid(samples, rate, { from, to, genre: clip.genre });
  if (grid.beats.length < 8 * BEATS_PER_BAR) throw new Error("the clip is shorter than 8 bars");
  const result = analyze(samples, rate, grid.beats, { beatsPerBar: BEATS_PER_BAR, genre: clip.genre, bpm: grid.bpm, sensitivity: clip.sensitivity });
  const beatAt = (sample) => Math.round((clip.start + ((sample / rate - offset) * clip.tempo) / 60) * 1000) / 1000;
  const sections = placed(result.sections, (s) => ({ ...s, beat: beatAt(grid.beats[(s.start_bar - 1) * BEATS_PER_BAR]) }));
  // what the device needs to warp the clip onto this grid: the tempo, where bar 1 starts and the file's length (s)
  const warp = { bpm: grid.bpm, first: grid.beats[0] / rate, length: samples.length / rate, offset };
  return { ...result, sections, tempo: grid.bpm, first_bar: 1, bar_grid: "song", warp };
}

async function run(clip) {
  const { samples, rate } = await decode(clip.file);
  const warp = markers(clip);
  const result = warp ? runWarped(clip, warp, samples, rate) : runUnwarped(clip, samples, rate);
  return {
    name: clip.name || path.basename(clip.file).replace(/\.[^.]+$/, ""),
    file: clip.file,
    track: clip.track,
    analyzed: new Date().toISOString(),
    tempo: result.tempo,
    set_tempo: clip.tempo,
    warped: Boolean(warp),
    bar_grid: result.bar_grid, // "arrangement": bars are the Arrangement's; "song": the song's own bars from its start
    genre: result.genre, // as chosen in the device; "auto" guesses only the phrase length (D&B from 160 BPM)
    sensitivity: result.sensitivity, // 0..100 per band (kick, low, mid, high): how much its changes count; lower merges more
    key: result.key,
    first_bar: result.first_bar,
    bars: result.bars,
    sections: result.sections,
    lanes: result.lanes,
    ...(result.warp ? { warp: result.warp } : {}),
  };
}

function store(clip, result) {
  fs.mkdirSync(REFERENCES, { recursive: true });
  fs.writeFileSync(storeFile(clip), JSON.stringify(result, null, 1));
}

async function onAnalyze(Max, json) {
  let clip;
  try {
    clip = JSON.parse(json);
    log("analyze", { ...clip, warp_markers: (clip.warp_markers || []).length });
    await Max.outlet("status", "Analyzing...");
    const started = Date.now();
    const result = await run(clip);
    store(clip, result);
    log("done", result.name, `${result.bars} bars`, `${result.sections.length} sections`, `${Date.now() - started} ms`);
    await Max.outlet("result", JSON.stringify(result));
  } catch (exc) {
    log("error", exc.stack || String(exc));
    await Max.outlet("status", `Analysis failed: ${exc.message}`);
  }
}

async function onLookup(Max, json) {
  try {
    const clip = JSON.parse(json);
    const file = storeFile(clip);
    if (fs.existsSync(file)) await Max.outlet("result", fs.readFileSync(file, "utf8"));
    else await Max.outlet("status", "Press Analyze");
  } catch (exc) {
    log("lookup error", exc.stack || String(exc));
  }
}

// max-api only exists inside Max (Node for Max runs this file through its own runner);
// tests require the file for readWav and run.
let Max = null;
try {
  Max = require("max-api");
} catch (_) {
  Max = null;
}
if (Max) {
  Max.addHandler("analyze", (...atoms) => onAnalyze(Max, atoms.join(" ")));
  Max.addHandler("lookup", (...atoms) => onLookup(Max, atoms.join(" ")));
  Max.addHandler("log", (...atoms) => log("device", atoms.join(" ")));
  Max.addHandler("about", async (version) => Max.outlet("status", await about(version)));
  log("started", process.version);
}

module.exports = { readWav, run, markers, storeFile };
