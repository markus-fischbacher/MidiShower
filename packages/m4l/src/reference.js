/*
 * MidiBakery Reference Analyzer device, v8 side: finds the reference clip on this track through the Live
 * API, hands it to node.script for the analysis, passes the result to the view and sets
 * Arrangement locators at its sections (or removes the locators within the clip). An unwarped clip
 * plays at its own tempo, so its bars drift away from the Set's: after its tempo is detected the
 * device warps it onto the Set's grid (bar 1 on a bar line) and analyzes it again.
 *
 * inlet 0:  init (live.thisdevice), analyze, locators, unlocators, genre <menu index>,
 *           band <kick|low|mid|high> <0..100>, lock <0|1>,
 *           result <json>,
 *           status <text>
 * outlet 0: to node.script (analyze / lookup / log <json>)
 * outlet 1: to the view (show <json>, status <text>)
 * outlet 2: to the Sensitivity fields (<band> <value>: shown without being sent back here)
 */

autowatch = 0;
inlets = 1;
outlets = 3;

var ready = false;
var current = null; // the last result
// the Genre menu's items, as plan_song names the genres
var GENRES = ["auto", "house", "melodic_techno", "peak_time_techno", "drum_and_bass", "trap"];
var chosen = "auto";
var settleTask = null;
var SETTLE_MS = 400; // a field dragged runs through many values: analyze once it rests
// the Sensitivity fields, 0..100 per band; with Lock on, changing one moves the others by as much
var BANDS = ["kick", "low", "mid", "high"]; // the view's lanes: Kick, Lows, Mids, Hats
var sensitivity = { kick: 50, low: 50, mid: 50, high: 50 };
var locked = false;
var job = null; // locator job: {queue, home, phase, ticks, task}
var LOCATOR_TICK_MS = 60;
var LOCATOR_TIMEOUT_TICKS = 40; // ~2.4 s per locator, then it is skipped
var clipApi = null; // the LiveAPI object of the clip referenceClip() found last
var warpJob = null; // auto-warp: {clip, plan, phase, ticks, task}
var WARP_TIMEOUT_TICKS = 100; // ~6 s for the whole job
var WARP_TOLERANCE = 0.01; // s: the warped grid must land this close to the detected one

function status(text) {
  outlet(1, "status", text);
}

function log(text) {
  outlet(0, "log", text);
}

/** LiveAPI get() returns a list; single values come first. */
function one(api, name) {
  var value = api.get(name);
  return value && value.length !== undefined ? value[0] : value;
}

function ids(list) {
  var out = [];
  for (var i = 0; list && i < list.length; i++) if (list[i] === "id") out.push(list[i + 1]);
  return out;
}

/** Live returns warp markers as a dict: a JSON string in a list, or an object. */
function warpMarkers(clip) {
  var raw = clip.get("warp_markers");
  try {
    var value = raw;
    if (raw && raw.length !== undefined && typeof raw !== "string") value = raw.join(" ");
    if (typeof value === "string") value = JSON.parse(value);
    var list = value && value.warp_markers ? value.warp_markers : value;
    var markers = [];
    for (var i = 0; list && i < list.length; i++) markers.push({ beat_time: Number(list[i].beat_time), sample_time: Number(list[i].sample_time) });
    return markers;
  } catch (e) {
    log("warp markers unreadable: " + String(raw).slice(0, 200));
    return [];
  }
}

/** The longest audio clip in this track's Arrangement. */
function referenceClip() {
  var track = new LiveAPI("this_device canonical_parent");
  if (!track || track.id == 0) return null;
  var best = null;
  var clips = ids(track.get("arrangement_clips"));
  for (var i = 0; i < clips.length; i++) {
    var clip = new LiveAPI("id " + clips[i]);
    if (!one(clip, "is_audio_clip")) continue;
    var start = one(clip, "start_time");
    var end = one(clip, "end_time");
    if (!best || end - start > best.end - best.start) best = { api: clip, start: start, end: end };
  }
  if (!best) return null;
  var clip = best.api;
  clipApi = clip; // kept out of the description: it goes to node.script as JSON
  return {
    file: String(one(clip, "file_path")),
    name: String(one(clip, "name")),
    track: String(one(track, "name")),
    start: best.start,
    end: best.end,
    start_marker: one(clip, "start_marker"),
    end_marker: one(clip, "end_marker"),
    warping: one(clip, "warping") ? 1 : 0,
    warp_markers: warpMarkers(clip),
    tempo: one(new LiveAPI("live_set"), "tempo"),
    genre: chosen,
    sensitivity: sensitivity,
  };
}

function genre(index) {
  chosen = GENRES[index] || "auto";
}

/** New sensitivities split the song again once the fields rest: the analysis takes a second. */
function reanalyzeLater() {
  if (!ready || !current) return;
  if (!settleTask) settleTask = new Task(settled, this);
  settleTask.cancel();
  settleTask.schedule(SETTLE_MS);
}

/** One band's sensitivity; with Lock on the others follow by the same amount (kept within 0..100). */
function band(name, value) {
  if (!sensitivity.hasOwnProperty(name)) return;
  value = Math.max(0, Math.min(100, Math.round(Number(value))));
  var delta = value - sensitivity[name];
  if (delta === 0) return;
  sensitivity[name] = value;
  if (locked) {
    for (var i = 0; i < BANDS.length; i++) {
      if (BANDS[i] === name) continue;
      sensitivity[BANDS[i]] = Math.max(0, Math.min(100, sensitivity[BANDS[i]] + delta));
      outlet(2, BANDS[i], sensitivity[BANDS[i]]);
    }
  }
  reanalyzeLater();
}

function lock(on) {
  locked = Number(on) > 0;
}

function settled() {
  if (job || warpJob) settleTask.schedule(SETTLE_MS);
  else analyze();
}

function init() {
  ready = true;
  var clip = referenceClip();
  if (clip) outlet(0, "lookup", JSON.stringify(clip));
  else status("Drop the reference song on this track's Arrangement, then press Analyze");
}

function analyze() {
  if (!ready || warpJob) return;
  var clip = referenceClip();
  if (!clip) {
    status("No audio clip in this track's Arrangement");
    return;
  }
  status("Analyzing " + clip.name + (clip.warping ? "..." : " (not warped: detecting its tempo)..."));
  fresh = true;
  outlet(0, "analyze", JSON.stringify(clip));
}

function result(json) {
  try {
    current = JSON.parse(json);
  } catch (e) {
    status("Unreadable result");
    return;
  }
  outlet(1, "show", json);
  if (!current.warped && current.warp && current.analyzed && fresh) autoWarp(current.warp);
  fresh = false;
}

// ---------------------------------------------------------------------------- auto-warp

var fresh = false; // the result comes from a press of Analyze (not a stored one): warping is wanted

/** Where the warp markers go: bar 1 (the detected first downbeat, `first` s into the file) on a bar
 * line of the Arrangement, a second marker a whole number of bars later near the file's end, both on
 * the detected tempo. The start marker keeps the part of the file the clip played before, moved by
 * less than two beats so that bar lines stay bar lines. */
function warpPlan(warp, clipStart) {
  var spb = 60 / warp.bpm; // seconds per beat
  var firstBeats = warp.first / spb;
  var down = 4 * Math.max(0, Math.ceil(firstBeats / 4 - 0.1)); // clip beat of bar 1: the audio before it stays
  var beatAt = function (seconds) { return down + (seconds - warp.first) / spb; };
  var bars = Math.floor((warp.length - warp.first) / (4 * spb)) - 1;
  if (bars < 8) return null;
  var r = ((clipStart % 4) + 4) % 4; // start marker = this, mod 4: the downbeat then sits on a bar line
  var start = 4 * Math.round((beatAt(warp.offset || 0) - r) / 4) + r;
  return {
    bpm: warp.bpm,
    down: down,
    first: warp.first,
    end: { beat_time: down + bars * 4, sample_time: warp.first + bars * 4 * spb },
    beatAt: beatAt,
    start: start,
  };
}

/** Seconds into the file at a clip beat, by Live's warp markers. */
function secondsAt(markers, beat) {
  var sorted = markers.slice().sort(function (a, b) { return a.beat_time - b.beat_time; });
  if (sorted.length < 2) return NaN;
  var i = 0;
  while (i < sorted.length - 2 && sorted[i + 1].beat_time <= beat) i++;
  var a = sorted[i];
  var b = sorted[i + 1];
  return a.sample_time + ((beat - a.beat_time) * (b.sample_time - a.sample_time)) / (b.beat_time - a.beat_time);
}

/** The warped clip plays the detected grid: bar 1 and the end marker where the plan put them. */
function warpedOk(markers, plan) {
  return Math.abs(secondsAt(markers, plan.down) - plan.first) < WARP_TOLERANCE &&
    Math.abs(secondsAt(markers, plan.end.beat_time) - plan.end.sample_time) < WARP_TOLERANCE;
}

function autoWarp(warp) {
  var found = referenceClip();
  if (!found || found.warping) return;
  var plan = warpPlan(warp, found.start);
  if (!plan) return;
  var song = new LiveAPI("live_set");
  // at the song's own tempo the unwarped clip spans exactly its bars; warped, it keeps them at any tempo
  warpJob = { clip: clipApi, song: song, plan: plan, tempo: one(song, "tempo"), phase: "tempo", ticks: 0 };
  warpJob.task = new Task(warpStep, this);
  warpJob.task.interval = LOCATOR_TICK_MS;
  warpJob.task.repeat(-1);
  status("Warping the clip at " + plan.bpm + " BPM...");
}

function warpDone(message, ok) {
  var job = warpJob;
  job.task.cancel();
  if (Math.abs(one(job.song, "tempo") - job.tempo) > 1e-6) job.song.set("tempo", job.tempo);
  warpJob = null;
  log("auto-warp: " + message);
  if (ok) {
    status(message + "; analyzing again...");
    analyze();
  } else {
    status(message + ": warp it by hand (1.1.1 on the first downbeat at " + job.plan.bpm + " BPM), then Analyze");
  }
}

function warpStep() {
  var job = warpJob;
  var clip = job.clip;
  var plan = job.plan;
  if (++job.ticks > WARP_TIMEOUT_TICKS) {
    warpDone("Auto-warp stopped (" + job.phase + ")", false);
    return;
  }
  try {
    if (job.phase === "tempo") {
      job.song.set("tempo", plan.bpm);
      job.phase = "tempo wait";
    } else if (job.phase === "tempo wait") {
      if (Math.abs(one(job.song, "tempo") - plan.bpm) < 0.001) {
        clip.set("warping", 1); // Live defers this one
        job.phase = "warp wait";
      }
    } else if (job.phase === "warp wait") {
      if (one(clip, "warping")) job.phase = "clean";
    } else if (job.phase === "clean") {
      // keep the first marker and Live's hidden last one; the ones between are Live's own guess
      var markers = warpMarkers(clip).sort(function (a, b) { return a.beat_time - b.beat_time; });
      for (var i = markers.length - 2; i >= 1; i--) clip.call("remove_warp_marker", markers[i].beat_time);
      job.phase = "move";
    } else if (job.phase === "move") {
      var first = warpMarkers(clip).sort(function (a, b) { return a.beat_time - b.beat_time; })[0];
      var target = plan.beatAt(first.sample_time);
      if (Math.abs(target - first.beat_time) > 1e-6) clip.call("move_warp_marker", first.beat_time, target - first.beat_time);
      job.phase = "add";
    } else if (job.phase === "add") {
      var marker = { beat_time: plan.end.beat_time, sample_time: plan.end.sample_time };
      try {
        clip.call("add_warp_marker", marker); // Max converts the object to a dictionary
      } catch (e) {
        log("add_warp_marker with an object: " + e);
      }
      if (!hasMarker(clip, plan.end.beat_time)) {
        var d = new Dict();
        d.set("beat_time", marker.beat_time);
        d.set("sample_time", marker.sample_time);
        clip.call("add_warp_marker", "dictionary", d.name);
      }
      job.phase = "start";
    } else if (job.phase === "start") {
      clip.set("start_marker", plan.start);
      job.phase = "check";
    } else if (job.phase === "check") {
      var now = warpMarkers(clip);
      if (warpedOk(now, plan)) warpDone("Warped at " + plan.bpm + " BPM, bar 1 on a bar line", true);
      else {
        log("auto-warp markers: " + JSON.stringify(now));
        warpDone("Auto-warp did not land on the detected grid", false);
      }
    }
  } catch (e) {
    warpDone("Auto-warp failed in " + job.phase + " (" + e + ")", false);
  }
}

function hasMarker(clip, beat) {
  var markers = warpMarkers(clip);
  for (var i = 0; i < markers.length; i++) if (Math.abs(markers[i].beat_time - beat) < 0.001) return true;
  return false;
}

function locators() {
  if (!ready) return;
  if (!current || !current.sections || !current.sections.length) {
    status("Analyze first");
    return;
  }
  var queue = [];
  // Live puts a new locator on the Arrangement's grid anyway: aim for whole bars
  for (var i = 0; i < current.sections.length; i++) queue.push({ time: Math.round(current.sections[i].beat / 4) * 4, name: current.sections[i].name });
  start("set", queue, "Setting " + queue.length + " locators...");
}

/** Removes every locator within the reference clip's range of the Arrangement. */
function unlocators() {
  if (!ready) return;
  var clip = referenceClip();
  if (!clip) {
    status("No audio clip in this track's Arrangement");
    return;
  }
  var queue = [];
  var all = cues(new LiveAPI("live_set"));
  for (var i = 0; i < all.length; i++) if (all[i].time >= clip.start - 0.001 && all[i].time < clip.end) queue.push({ id: all[i].id, time: all[i].time, name: all[i].name });
  if (!queue.length) {
    status("No locators within the clip");
    return;
  }
  start("remove", queue, "Removing " + queue.length + " locators...");
}

function start(kind, queue, message) {
  if (job) return;
  var song = new LiveAPI("live_set");
  if (one(song, "is_playing")) {
    status("Stop the transport first");
    return;
  }
  job = { kind: kind, song: song, queue: queue, home: one(song, "current_song_time"), phase: "move", ticks: 0, done: 0 };
  job.task = new Task(kind === "set" ? step : removeStep, this);
  job.task.interval = LOCATOR_TICK_MS;
  job.task.repeat(-1);
  status(message);
}

function finish(message) {
  job.song.set("current_song_time", job.home);
  job.task.cancel();
  status(message);
  job = null;
}

function cues(song) {
  var list = ids(song.get("cue_points"));
  var out = [];
  for (var i = 0; i < list.length; i++) {
    var cue = new LiveAPI("id " + list[i]);
    out.push({ id: list[i], api: cue, time: one(cue, "time"), name: String(one(cue, "name")) });
  }
  return out;
}

/** The locator a section already has: one with its name within 4 bars (Live snapped it to its grid
 * on an earlier press), else any locator within a bar, which gets renamed: toggling there with
 * set_or_delete_cue could delete it. */
function cueNear(song, time, name) {
  var all = cues(song);
  for (var i = 0; i < all.length; i++) if (all[i].name === name && Math.abs(all[i].time - time) <= 16) return all[i];
  for (var j = 0; j < all.length; j++) if (Math.abs(all[j].time - time) < 4) return all[j];
  return null;
}

/** One locator at a time: Live sets a locator only at the play position, which moves a tick later,
 * and snaps it to the Arrangement's grid; the new locator is the one whose id was not there before. */
function step() {
  var song = job.song;
  var item = job.queue[0];
  if (!item) {
    // Live puts a locator on the Arrangement's grid: zoomed out, that grid can be coarser than a bar
    finish(job.done + " locators set" + (job.snapped ? "; " + job.snapped + " off the bar: zoom in to 1 bar, Remove Locators, Set Locators" : ""));
    return;
  }
  job.ticks++;
  if (job.ticks > LOCATOR_TIMEOUT_TICKS) {
    log("locator skipped at beat " + item.time + " in phase " + job.phase);
    next();
    return;
  }
  if (job.phase === "move") {
    var existing = cueNear(song, item.time, item.name);
    if (existing) {
      if (existing.name !== item.name) existing.api.set("name", item.name);
      job.done++;
      next();
      return;
    }
    job.before = cues(song);
    song.set("current_song_time", item.time);
    job.phase = "wait";
  } else if (job.phase === "wait") {
    if (Math.abs(one(song, "current_song_time") - item.time) < 0.001) {
      song.call("set_or_delete_cue");
      job.phase = "name";
    }
  } else if (job.phase === "name") {
    var now = cues(song);
    if (now.length < job.before.length) {
      // the grid snapped onto another locator and the toggle deleted it: put it back with its name
      var left = {};
      for (var k = 0; k < now.length; k++) left[now[k].id] = true;
      for (var d = 0; d < job.before.length; d++) if (!left[job.before[d].id]) item.name = job.before[d].name;
      log("locator near beat " + item.time + " was toggled off; restoring it as " + item.name);
      job.before = now;
      song.call("set_or_delete_cue");
      return; // the next tick names the restored locator
    }
    var known = {};
    for (var i = 0; i < job.before.length; i++) known[job.before[i].id] = true;
    for (var j = 0; j < now.length; j++) {
      if (known[now[j].id]) continue;
      now[j].api.set("name", item.name);
      if (Math.abs(now[j].time - item.time) > 0.01) {
        log("locator " + item.name + " snapped from beat " + item.time + " to " + now[j].time);
        job.snapped = (job.snapped || 0) + 1;
      }
      job.done++;
      next();
      return;
    }
  }
}

/** One locator at a time: move the play position onto it and toggle it off. Live snaps the play
 * position to its grid first; if that toggled a new locator on instead, toggle once more. */
function removeStep() {
  var song = job.song;
  var item = job.queue[0];
  if (!item) {
    finish(job.done + " locators removed");
    return;
  }
  job.ticks++;
  if (job.ticks > LOCATOR_TIMEOUT_TICKS) {
    log("locator " + item.name + " at beat " + item.time + " not removed (phase " + job.phase + ")");
    next();
    return;
  }
  if (job.phase === "move") {
    job.before = cues(song);
    song.set("current_song_time", item.time);
    job.phase = "wait";
  } else if (job.phase === "wait") {
    if (Math.abs(one(song, "current_song_time") - item.time) < 0.001) {
      song.call("set_or_delete_cue");
      job.phase = "check";
    }
  } else if (job.phase === "check") {
    var now = cues(song);
    var gone = true;
    for (var i = 0; i < now.length; i++) if (now[i].id === item.id) gone = false;
    if (now.length > job.before.length) {
      log("removing " + item.name + " at beat " + item.time + " added a locator on the grid; taking it back");
      song.call("set_or_delete_cue");
      next();
    } else if (now.length < job.before.length) {
      if (!gone) log("removing " + item.name + " at beat " + item.time + " removed another locator");
      else job.done++;
      next();
    }
  }
}

function next() {
  job.queue.shift();
  job.phase = "move";
  job.ticks = 0;
}

function anything() {
  // node.script's status lines
  if (messagename === "status") status(arrayfromargs(arguments).join(" "));
}
