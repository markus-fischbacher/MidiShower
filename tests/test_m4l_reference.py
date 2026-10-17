"""The Reference device's analysis (packages/m4l/src/reference-analysis.js) on a synthetic song, in Node."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from midibakery.mcp.server import read_references

ROOT = Path(__file__).resolve().parents[1]
MAX_NODE = Path("/Applications/Max.app/Contents/Resources/C74/packages/Node for Max/source/bin/osx/node/node")
NODE = str(MAX_NODE) if MAX_NODE.exists() else shutil.which("node")


needs_node = pytest.mark.skipif(NODE is None, reason="no Node.js available")


def _node(*args: str, genre: str = "auto", sensitivity: dict | None = None) -> str:
    env = {**os.environ, "GENRE": genre, "SENSITIVITY": json.dumps(sensitivity or {})}
    return subprocess.run([NODE, *args], capture_output=True, text=True, check=True, env=env).stdout


@needs_node
def test_sections_kick_and_key_of_a_synthetic_song():
    result = json.loads(_node(str(ROOT / "tests/reference_harness.js"), str(ROOT / "packages/m4l/src/reference-analysis.js")))
    assert result["tempo"] == 128 and result["grid"] == [0, 41344, 2646000]  # 4 beats at 22.05 kHz, 128 BPM
    assert result["bars"] == 64
    sections = [(s["name"], s["start_bar"], s["bars"]) for s in result["sections"]]
    assert sections == [("Intro", 1, 16), ("Break", 17, 16), ("Drop", 33, 32)], sections
    kick = result["lanes"]["kick"]
    assert min(kick[:16]) >= 0.5 and max(kick[16:32]) < 0.5 and min(kick[32:]) >= 0.5
    assert result["sections"][2]["elements"] == ["kick", "bass", "mids", "hats"]
    assert result["key"].startswith("A minor")


@needs_node
def test_band_sensitivity_weights_what_counts_as_a_change():
    harness = (str(ROOT / "tests/reference_harness.js"), str(ROOT / "packages/m4l/src/reference-analysis.js"))
    full = json.loads(_node(*harness))
    assert full["sensitivity"] == {"kick": 50, "low": 50, "mid": 50, "high": 50}
    # the kick alone still hears where it stops (bar 17) and comes back (bar 33)
    kick = json.loads(_node(*harness, sensitivity={"kick": 100, "low": 0, "mid": 0, "high": 0}))
    assert [s["start_bar"] for s in kick["sections"]] == [1, 17, 33] and kick["sensitivity"]["mid"] == 0
    # hearing only the hats (they stop for the break and come back): the same changes
    hats = json.loads(_node(*harness, sensitivity={"kick": 0, "low": 0, "mid": 0, "high": 100}))
    assert [s["start_bar"] for s in hats["sections"]] == [1, 17, 33]
    # hearing only the mids (the pad plays from the break on): the intro-to-break change is all there is
    mids = json.loads(_node(*harness, sensitivity={"kick": 0, "low": 0, "mid": 100, "high": 0}))
    assert [s["start_bar"] for s in mids["sections"]] == [1, 17]


LOCK = """
const vm = require("vm"), fs = require("fs");
const sent = [];
const ctx = { outlet(n, ...a) { if (n === 2) sent.push(a); }, post() {}, LiveAPI: function () {}, Task: function () {}, Dict: function () {} };
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
for (const b of ["kick", "low", "mid", "high"]) ctx.band(b, 100);  // the fields start at 50
sent.length = 0;
ctx.band("high", 60);           // unlocked: only High moves
ctx.lock(1);
ctx.band("kick", 80);           // locked: the others follow by -20, kept within 0..100
ctx.band("high", 10);           // -30 more: Low, Mid to 50, Kick to 50, High stays 10
ctx.lock(0);
ctx.band("mid", 90);
console.log(JSON.stringify({ sensitivity: ctx.sensitivity, sent }));
"""


@needs_node
def test_lock_moves_every_band_by_as_much():
    out = json.loads(_node("-e", LOCK, str(ROOT / "packages/m4l/src/reference.js")))
    assert out["sensitivity"] == {"kick": 50, "low": 50, "mid": 90, "high": 10}
    # the fields that followed are shown (outlet 2), the one turned by hand is not sent back
    assert out["sent"][:3] == [["low", 80], ["mid", 80], ["high", 40]]
    assert ["kick", 50] in out["sent"] and ["high", 10] not in out["sent"] and len(out["sent"]) == 6


RUN = """
const { run } = require(process.argv[1]);
run(JSON.parse(process.argv[2])).then((r) => console.log(JSON.stringify(r)), (e) => { console.error(e); process.exit(1); });
"""


@needs_node
def test_run_places_sections_in_the_arrangement(tmp_path):
    # The 64 bar song at 128 BPM, its clip at arrangement bar 9 (beat 32) in a 120 BPM Set; the start
    # marker skips the file's first 8 bars (sections snap to 8 bar steps from the clip's first bar).
    wav = tmp_path / "song.wav"
    _node(str(ROOT / "tests/reference_harness.js"), str(ROOT / "packages/m4l/src/reference-analysis.js"), str(wav))
    clip = {"file": str(wav), "name": "Synth Song", "track": "Ref", "start": 32, "end": 32 + 56 * 4, "start_marker": 32,
            "warping": 1, "tempo": 120, "warp_markers": [{"beat_time": 0, "sample_time": 0}, {"beat_time": 256, "sample_time": 120}]}
    result = json.loads(_node("-e", RUN, str(ROOT / "packages/m4l/src/midibakery-reference.js"), json.dumps(clip)))
    assert result["tempo"] == 128 and result["set_tempo"] == 120 and result["first_bar"] == 9 and result["bars"] == 56
    # the file's break at its bar 17 is the clip's bar 9, arrangement bar 17
    sections = [(s["name"], s["start_bar"], s["beat"]) for s in result["sections"]]
    assert sections == [("Intro", 9, 32), ("Break", 17, 64), ("Drop", 33, 128)], sections


def test_read_references_lists_and_finds(tmp_path, monkeypatch):
    monkeypatch.setenv("MIDIBAKERY_HOME", str(tmp_path))
    (tmp_path / "references").mkdir()
    for i, name in enumerate(["Old Song", "New Song"]):
        ref = {"name": name, "file": f"/x/{name}.mp3", "analyzed": f"2026-10-0{i + 1}", "key": "A minor", "bars": 64,
               "sections": [{"name": "Intro", "start_bar": 1, "bars": 16, "kick": 1, "energy": -3}], "lanes": {"kick": [1] * 64}}
        (tmp_path / "references" / f"{i}.json").write_text(json.dumps(ref))
    (tmp_path / "references" / "broken.json").write_text("{")
    listed = read_references()["references"]
    assert [r["name"] for r in listed] == ["New Song", "Old Song"]
    assert "lanes" not in listed[0] and listed[0]["sections"] == [{"name": "Intro", "start_bar": 1, "bars": 16, "kick": 1, "elements": None}]
    assert read_references("old")["lanes"]["kick"][0] == 1
    with pytest.raises(Exception, match="No analyzed reference"):
        read_references("missing")


@needs_node
def test_unwarped_clip_gets_its_own_tempo_and_real_time_locators(tmp_path):
    # Unwarped, the 128 BPM song plays in real time from arrangement beat 32 of a 120 BPM Set:
    # its bar 17 starts 30 s in, at arrangement beat 32 + 30 s * 2 beats/s.
    wav = tmp_path / "song.wav"
    _node(str(ROOT / "tests/reference_harness.js"), str(ROOT / "packages/m4l/src/reference-analysis.js"), str(wav))
    seconds = 64 * 4 * 60 / 128
    clip = {"file": str(wav), "name": "Synth Song", "track": "Ref", "start": 32, "end": 32 + seconds * 2, "start_marker": 0,
            "warping": 0, "tempo": 120, "warp_markers": []}
    result = json.loads(_node("-e", RUN, str(ROOT / "packages/m4l/src/midibakery-reference.js"), json.dumps(clip)))
    assert result["tempo"] == 128 and not result["warped"] and result["bar_grid"] == "song" and result["bars"] in (63, 64)
    sections = [(s["name"], s["start_bar"], round(s["beat"], 1)) for s in result["sections"]]
    assert sections == [("Intro", 1, 32.0), ("Break", 17, 92.0), ("Drop", 33, 152.0)], sections
    # what the device warps the clip with: the detected tempo, bar 1 at the file's start
    assert result["warp"]["bpm"] == 128 and result["warp"]["first"] < 0.05 and result["warp"]["offset"] == 0
    assert abs(result["warp"]["length"] - seconds) < 0.01


WARP_PLAN = """
const vm = require("vm"), fs = require("fs");
const ctx = { outlet() {}, post() {}, LiveAPI: function () {}, Task: function () {}, Dict: function () {} };
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
const [warp, start] = JSON.parse(process.argv[2]);
const plan = ctx.warpPlan(warp, start);
// Live's markers once warped as planned: one at the file's start, the end marker, the hidden last one
const spb = 60 / warp.bpm;
const markers = [{ beat_time: plan.beatAt(0), sample_time: 0 }, plan.end, { beat_time: plan.end.beat_time + 4, sample_time: plan.end.sample_time + 4 * spb }];
const off = [{ beat_time: plan.beatAt(0), sample_time: 0 }, { beat_time: plan.end.beat_time, sample_time: plan.end.sample_time + 0.05 }];
console.log(JSON.stringify({ down: plan.down, start: plan.start, end: plan.end, ok: ctx.warpedOk(markers, plan), off: ctx.warpedOk(off, plan),
  bar17: ctx.secondsAt(markers, plan.down + 64) }));
"""


@needs_node
def test_auto_warp_puts_bar_1_on_a_bar_line():
    script = str(ROOT / "packages/m4l/src/reference.js")
    # The Signal: 124 BPM from its first sample, the clip at the Arrangement's start
    plan = json.loads(_node("-e", WARP_PLAN, script, json.dumps([{"bpm": 124, "first": 0.006, "length": 305.85, "offset": 0}, 0])))
    assert plan["down"] == 0 and plan["start"] == 0 and plan["ok"] and not plan["off"]
    assert plan["end"]["beat_time"] == 628 and abs(plan["bar17"] - (0.006 + 64 * 60 / 124)) < 1e-6
    # 3 s of pad before the first kick, the clip at beat 6: bar 1 lands on clip beat 8, the start marker
    # keeps the pad and puts the downbeat on arrangement beat 6 + 8 - 2 = 12
    plan = json.loads(_node("-e", WARP_PLAN, script, json.dumps([{"bpm": 124, "first": 3, "length": 300, "offset": 0}, 6])))
    assert plan["down"] == 8 and plan["start"] == 2 and plan["ok"]


@needs_node
def test_genre_names_the_sections():
    harness = (str(ROOT / "tests/reference_harness.js"), str(ROOT / "packages/m4l/src/reference-analysis.js"))
    peak = json.loads(_node(*harness, genre="peak_time_techno"))
    assert peak["genre"] == "peak_time_techno"
    assert [s["name"] for s in peak["sections"]] == ["Intro", "Breakdown", "Peak"]
    # Drum & Bass phrases are 16 bars: no section shorter than that
    dnb = json.loads(_node(*harness, genre="drum_and_bass"))
    assert all(s["bars"] >= 16 for s in dnb["sections"]) and dnb["sections"][-1]["name"] == "Drop"


MERGE = """
const { merge, weights } = require(process.argv[1]);
// The Signal's sections as the analysis finds them (fine); bars: flat, the merge only re-describes them
const found = [["intro", 1, 16], ["drop", 17, 8], ["groove", 25, 24], ["break", 49, 8], ["break", 57, 8], ["break", 65, 8],
  ["break", 73, 20], ["drop", 93, 32], ["groove", 125, 24], ["outro", 149, 9]]
  .map(([kind, start_bar, bars]) => ({ kind, start_bar, bars, energy: -3, kick: 1, elements: [] }));
const bars = Array.from({ length: 157 }, () => ({ low: -3, bass: -3, mid: -3, high: -3, total: -3, centroid: 1000, kick: 1 }));
const out = {};
const all = (n) => weights({ kick: n, low: n, mid: n, high: n });
for (const [level, n] of [["coarse", 0], ["normal", 50], ["fine", 100]])
  out[level] = merge(found.map((s) => ({ ...s })), bars, { break: "Breakdown" }, all(n))
    .map((s) => [s.name, s.start_bar, s.bars, (s.phrases || []).map((p) => p.start_bar)]);
// two breaks 4 dB apart: they merge once the fields' mean is low enough (limit (1 - mean/100) * 24 dB)
const loud = bars.map((b, i) => (i >= 56 && i < 64 ? { ...b, low: 1, bass: 1, mid: 1, high: 1, total: 1 } : b));
out.fields = [100, 90, 85, 84, 50].map((n) => merge(found.slice(3, 5).map((s) => ({ ...s })), loud, {}, all(n)).length);
console.log(JSON.stringify(out));
"""


@needs_node
def test_neighbours_of_the_same_kind_merge_and_keep_their_phrases():
    out = json.loads(_node("-e", MERGE, str(ROOT / "packages/m4l/src/reference-analysis.js")))
    # all fields at 50 (default): the four breaks in a row are one breakdown of 44 bars; where they changed stays as phrases
    assert out["normal"] == [["Intro", 1, 16, []], ["Drop", 17, 8, []], ["Groove", 25, 24, []],
                             ["Breakdown", 49, 44, [49, 57, 65, 73]], ["Drop 2", 93, 32, []], ["Groove 2", 125, 24, []],
                             ["Outro", 149, 9, []]]
    # all fields at 0: a drop and its groove are one drop as well
    assert out["coarse"] == [["Intro", 1, 16, []], ["Drop", 17, 32, [17, 25]], ["Breakdown", 49, 44, [49, 57, 65, 73]],
                             ["Drop 2", 93, 56, [93, 125]], ["Outro", 149, 9, []]]
    # breaks 4 dB apart (the distance over five features plus the kick: ~3.65) merge from 84 down
    assert out["fields"] == [2, 2, 2, 1, 1]
    assert [(s[1], s[2], s[3]) for s in out["fine"]][3:7] == [(49, 8, []), (57, 8, []), (65, 8, []), (73, 20, [])]  # as found
