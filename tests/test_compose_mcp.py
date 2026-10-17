"""Composition through the real Remote Script and the MCP server, against the fake LOM."""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from fake_song import DrumRackDevice, RackDevice, Chain
from midibakery.bridge import BridgeError
from midibakery.compose import DrumsSpec, apply_plan, build_plan
from midibakery.compose.theory import Scale
from midibakery.mcp import build_server

KIT = {"Kick 909": 36, "Clap": 39, "Snare": 40, "Closed HH": 44, "Open HH": 46}


def _payload(result):
    return json.loads(result.content[0].text)


def _add_kit(live, track=0, legacy=False, nested=False):
    def build():
        rack = DrumRackDevice(legacy=legacy)
        for name, note in KIT.items():
            rack.add_pad(name, note)
        device = RackDevice("Instrument Rack", [Chain("Chain", [rack])]) if nested else rack
        live.song.tracks[track].devices.append(device)

    live.on_main(build)


def _clip_notes(live, track, slot):
    return live.on_main(lambda: sorted(live.song.tracks[track].clip_slots[slot].clip.notes.values(), key=lambda n: (n["start_time"], n["pitch"])))


# --------------------------------------------------------------------------
# drum_rack.pads op
# --------------------------------------------------------------------------


@pytest.mark.parametrize("legacy,nested", [(False, False), (True, False), (False, True)], ids=["in_note", "drum_pads", "nested"])
async def test_drum_rack_pads(client, live, legacy, nested):
    _add_kit(live, legacy=legacy, nested=nested)
    result = await client.call("drum_rack.pads", {"track": 0})
    assert [(p["name"], p["note"]) for p in result["pads"]] == sorted(KIT.items(), key=lambda kv: kv[1])


async def test_drum_rack_pads_errors(client, live):
    with pytest.raises(BridgeError) as exc:
        await client.call("drum_rack.pads", {"track": 0})
    assert exc.value.code == "NOT_FOUND"
    await client.call("device.insert", {"track": 0, "name": "Drift"})
    with pytest.raises(BridgeError) as exc:
        await client.call("drum_rack.pads", {"track": 0, "device": 0})
    assert exc.value.code == "INVALID_PARAMS"


# --------------------------------------------------------------------------
# Plan / apply
# --------------------------------------------------------------------------


async def test_plan_does_not_touch_live_and_apply_is_one_undo_step(client, live):
    _add_kit(live)
    plan = await build_plan(client.call, [DrumsSpec(track=0, slot=0, bars=2, seed=7, style="house")])
    assert live.on_main(lambda: live.song.tracks[0].clip_slots[0].has_clip) is False
    preview = plan.preview()
    change = preview["changes"][0]
    assert change["action"] == "create clip" and change["drum_map"] == "drum_rack"
    assert change["pads"]["kick"] == 36 and change["pads"]["clap"] == 39 and change["pads"]["open_hat"] == 46

    steps_before = live.on_main(lambda: live.song.undo_steps)
    result = await apply_plan(client.call, plan)
    assert live.on_main(lambda: live.song.undo_steps) == steps_before + 1
    notes = _clip_notes(live, 0, 0)
    assert len(notes) == change["add"] == len(result["applied"][0]["added_note_ids"])
    assert {n["pitch"] for n in notes} <= set(KIT.values())
    assert all(n["start_time"] < 8 for n in notes)


async def test_same_seed_same_notes(client, live):
    plans = [await build_plan(client.call, [DrumsSpec(track=0, slot=s, seed=99, style="trap")]) for s in (0, 1)]
    a, b = (p.changes[0].add for p in plans)
    assert a == b


async def test_apply_detects_conflicts(client, live):
    plan = await build_plan(client.call, [DrumsSpec(track=0, slot=0, seed=1)])
    await client.call("clip.create", {"track": 0, "slot": 0, "length": 4})
    with pytest.raises(BridgeError) as exc:
        await apply_plan(client.call, plan)
    assert exc.value.code == "CONFLICT"


# --------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------


async def test_compose_tools_are_listed_and_annotated():
    tools = {t.name: t for t in await build_server().list_tools()}
    assert len(tools) <= 37  # the real guard is the character budget in test_agent (local models)
    for name in ("compose", "transform_notes", "apply_plan"):
        assert tools[name].annotations.read_only_hint is False
        assert tools[name].annotations.destructive_hint is True
    assert tools["plan_changes"].annotations.read_only_hint is True
    assert tools["get_drum_pads"].annotations.read_only_hint is True
    assert "kind" in tools["compose"].input_schema["required"] and "kind" not in tools["transform_notes"].input_schema["properties"]
    assert "preview" in tools["compose"].input_schema["properties"]


async def test_compose_checks_the_fields_of_its_kind(client):
    server = build_server(client)
    with pytest.raises(ToolError) as exc:
        await server.call_tool("compose", {"kind": "drums", "track": 0, "slot": 0, "rhythm": "stabs"})
    assert "rhythm not for kind 'drums'" in str(exc.value)
    with pytest.raises(ToolError) as exc:
        await server.call_tool("compose", {"kind": "bass", "track": 1, "slot": 0, "style": "breakbeat"})
    assert "style" in str(exc.value)  # a drum style for the bass
    chords = _payload(await server.call_tool("compose", {"kind": "chords", "track": 1, "slot": 0, "seed": 2, "low": 48, "high": 62, "voices": 4}))
    assert chords["changes"][0]["kind"] == "chords"
    notes = (await client.call("notes.get", {"track": 1, "slot": 0}))["notes"]
    assert notes and all(48 <= n["pitch"] <= 62 for n in notes)  # the kind's own fields reach its generator


async def test_plan_changes_schema_stays_small_but_steps_are_still_checked(client):
    server = build_server(client)
    tools = {t.name: t for t in await server.list_tools()}
    assert len(json.dumps(tools["plan_changes"].input_schema)) < 1500  # kind + free fields, not every compose schema
    with pytest.raises(ToolError) as exc:
        await server.call_tool("plan_changes", {"steps": [{"kind": "bass", "track": 0, "slot": 0, "styel": "walking"}]})
    assert "styel" in str(exc.value)


async def test_full_song_sketch_via_plan_changes(client, live):
    server = build_server(client)
    await client.call("song.set", {"root_note": 9, "scale_name": "Minor"})
    live.on_main(lambda: setattr(live.song, "scale_intervals", (0, 2, 3, 5, 7, 8, 10)))
    _add_kit(live)

    plan = _payload(await server.call_tool("plan_changes", {"steps": [
        {"kind": "drums", "track": 0, "slot": 0, "bars": 4, "seed": 1},
        {"kind": "chords", "track": 1, "slot": 0, "bars": 4, "seed": 2, "progression": "i VI III VII"},
        {"kind": "bass", "track": 1, "slot": 1, "bars": 4, "seed": 3, "follow_kick": {"track": 0, "slot": 0}},
        {"kind": "melody", "track": 1, "slot": 2, "bars": 4, "seed": 4},
    ]}))
    assert plan["scale"] == "A Minor"
    assert [c["action"] for c in plan["changes"]] == ["create clip"] * 4
    chords, bass, melody = plan["changes"][1:]
    assert chords["chords"] == bass["chords"] == melody["chords"]  # later steps share the chords
    assert bass["kick_pitch"] == 36 and bass["kick_hits"] >= 16
    assert live.on_main(lambda: live.song.tracks[1].clip_slots[0].has_clip) is False

    undo_before = live.on_main(lambda: live.song.undo_steps)
    applied = _payload(await server.call_tool("apply_plan", {"plan_id": plan["plan_id"]}))
    assert len(applied["applied"]) == 4
    assert live.on_main(lambda: live.song.undo_steps) == undo_before + 1

    scale = Scale(9, (0, 2, 3, 5, 7, 8, 10))
    for slot in (0, 1, 2):
        notes = _clip_notes(live, 1, slot)
        assert notes and all(scale.contains(n["pitch"]) for n in notes)
        assert all(0 <= n["start_time"] < 16 for n in notes)
    kicks = {n["start_time"] for n in _clip_notes(live, 0, 0) if n["pitch"] == 36}
    assert {n["start_time"] for n in _clip_notes(live, 1, 1)} <= kicks | {0.0, 4.0, 8.0, 12.0}

    again = _payload(await server.call_tool("apply_plan", {"plan_id": plan["plan_id"]}))  # applied once only
    assert again["already_applied"] and live.on_main(lambda: live.song.undo_steps) == undo_before + 1
    with pytest.raises(ToolError, match="NOT_FOUND"):
        await server.call_tool("apply_plan", {"plan_id": "unknown"})


async def test_compose_preview_then_apply_and_if_exists(client, live):
    server = build_server(client)
    preview = _payload(await server.call_tool("compose", {"kind": "chords", "track": 1, "slot": 0, "seed": 5, "preview": True}))
    assert live.on_main(lambda: live.song.tracks[1].clip_slots[0].has_clip) is False
    await server.call_tool("apply_plan", {"plan_id": preview["plan_id"]})
    assert len(_clip_notes(live, 1, 0)) == preview["changes"][0]["add"]

    with pytest.raises(ToolError, match="CONFLICT"):
        await server.call_tool("compose", {"kind": "chords", "track": 1, "slot": 0, "seed": 6})
    replaced = _payload(await server.call_tool("compose", {"kind": "melody", "track": 1, "slot": 0, "seed": 6, "if_exists": "replace"}))
    change = replaced["changes"][0]
    assert change["action"] == "replace notes" and change["remove"] == preview["changes"][0]["add"]
    assert len(_clip_notes(live, 1, 0)) == change["add"]


async def test_bass_follows_chords_from_existing_clip(client, live):
    server = build_server(client)
    await server.call_tool("compose", {"kind": "chords", "track": 1, "slot": 0, "seed": 1, "progression": "vi IV I V"})
    bass = _payload(await server.call_tool("compose", {"kind": "bass", 
        "track": 1, "slot": 1, "seed": 1, "style": "sustained", "chords_from": {"track": 1, "slot": 0},
    }))
    assert bass["changes"][0]["chords_source"] == "clip"
    assert [c.split(" ")[0] for c in bass["changes"][0]["chords"]] == ["Am", "F", "C", "G"]
    assert [n["pitch"] % 12 for n in _clip_notes(live, 1, 1)] == [9, 5, 0, 7]


async def test_transform_notes_is_a_diff(client, live):
    server = build_server(client)
    await client.call("clip.create", {"track": 1, "slot": 0, "length": 4})
    ids = (await client.call("notes.apply", {"track": 1, "slot": 0, "add": [
        {"pitch": 60, "start_time": 0.0, "duration": 1, "probability": 0.5},
        {"pitch": 64, "start_time": 1.07, "duration": 1},
        {"pitch": 67, "start_time": 2.0, "duration": 1},
    ]}))["added"]

    result = _payload(await server.call_tool("transform_notes", {"track": 1, "slot": 0, "operation": "quantize"}))
    change = result["changes"][0]
    assert (change["add"], change["modify"], change["remove"]) == (0, 1, 0)
    notes = _clip_notes(live, 1, 0)
    assert [n["start_time"] for n in notes] == [0.0, 1.0, 2.0]
    assert notes[0]["probability"] == 0.5  # untouched note kept its data
    assert set(live.on_main(lambda: live.song.tracks[1].clip_slots[0].clip.notes)) == set(ids)

    await server.call_tool("transform_notes", {"track": 1, "slot": 0, "operation": "transpose", "steps": 1, "note_ids": [ids[2]]})
    assert [n["pitch"] for n in _clip_notes(live, 1, 0)] == [60, 64, 69]

    with pytest.raises(ToolError, match="NOT_FOUND"):
        await server.call_tool("transform_notes", {"track": 1, "slot": 0, "operation": "invert", "note_ids": [9999]})


async def test_transform_semitones_is_chromatic(client, live):
    server = build_server(client)
    await client.call("clip.create", {"track": 1, "slot": 0, "length": 4})
    await client.call("notes.apply", {"track": 1, "slot": 0, "add": [
        {"pitch": 60, "start_time": 0.0, "duration": 1}, {"pitch": 64, "start_time": 1.0, "duration": 1},
    ]})
    await server.call_tool("transform_notes", {"track": 1, "slot": 0, "operation": "transpose", "semitones": 2})
    assert [n["pitch"] for n in _clip_notes(live, 1, 0)] == [62, 66]
    plan = _payload(await server.call_tool("plan_changes", {"steps": [
        {"kind": "transform", "track": 1, "slot": 0, "operation": "transpose", "semitones": -2},
    ]}))
    assert plan["changes"][0]["modify"] == 2
    with pytest.raises(ToolError, match="semitones"):
        await server.call_tool("transform_notes", {"track": 1, "slot": 0, "operation": "transpose"})


async def test_chords_from_rejects_a_drum_track(client, live):
    _add_kit(live, track=0)
    server = build_server(client)
    await server.call_tool("compose", {"kind": "drums", "track": 0, "slot": 0, "seed": 1})
    await server.call_tool("compose", {"kind": "chords", "track": 1, "slot": 0, "seed": 1})
    with pytest.raises(ToolError, match="drum track") as exc:
        await server.call_tool("compose", {"kind": "bass", "track": 1, "slot": 1, "chords_from": {"track": 0, "slot": 0}})
    assert "1 '2-MIDI'" in str(exc.value)  # points at the track that has a chord clip
    await server.call_tool("compose", {"kind": "bass", "track": 1, "slot": 1, "seed": 2, "chords_from": {"track": 1, "slot": 0}})


async def test_apply_detects_shifted_tracks(client, live):
    plan = await build_plan(client.call, [DrumsSpec(track=1, slot=0, seed=1)])
    # A new, empty track with the same name and kind takes index 1: only the track id differs.
    await client.call("track.create", {"kind": "midi", "index": 1, "name": "2-MIDI"})
    with pytest.raises(BridgeError) as exc:
        await apply_plan(client.call, plan)
    assert exc.value.code == "CONFLICT"
    await client.call("track.delete", {"track": 0})
    await client.call("track.delete", {"track": 0})
    with pytest.raises(BridgeError) as exc:  # index 1 no longer exists
        await apply_plan(client.call, plan)
    assert exc.value.code == "CONFLICT"


async def test_failed_batch_is_rolled_back(client, live):
    plan = await build_plan(client.call, [DrumsSpec(track=0, slot=0, seed=1), DrumsSpec(track=1, slot=0, seed=2)])
    await client.call("clip.create", {"track": 1, "slot": 0, "length": 4})  # second clip.create will now fail
    undo_before = live.on_main(lambda: live.song.undo_calls)
    with pytest.raises(BridgeError) as exc:
        await apply_plan(client.call, plan, verify=False)
    assert exc.value.code == "CONFLICT" and exc.value.details["rolled_back"] is True
    assert "undone" in exc.value.message
    assert live.on_main(lambda: live.song.undo_calls) == undo_before + 1


async def test_failure_on_first_op_does_not_undo(client, live):
    plan = await build_plan(client.call, [DrumsSpec(track=0, slot=0, seed=1)])
    await client.call("clip.create", {"track": 0, "slot": 0, "length": 4})
    undo_before = live.on_main(lambda: live.song.undo_calls)
    with pytest.raises(BridgeError) as exc:
        await apply_plan(client.call, plan, verify=False)
    assert "rolled_back" not in exc.value.details
    assert live.on_main(lambda: live.song.undo_calls) == undo_before


async def test_compose_without_slot_takes_the_first_free_one(client, live):
    server = build_server(client)
    await client.call("clip.create", {"track": 1, "slot": 0, "length": 4})
    await client.call("clip.create", {"track": 1, "slot": 2, "length": 4})
    result = _payload(await server.call_tool("compose", {"kind": "chords", "track": 1, "seed": 1}))
    assert result["changes"][0]["slot"] == 1
    plan = await build_plan(client.call, [DrumsSpec(track=1, seed=1), DrumsSpec(track=1, seed=2)])
    assert [c.slot for c in plan.changes] == [3, 4]
    for slot in range(3, 8):
        await client.call("clip.create", {"track": 1, "slot": slot, "length": 4})
    with pytest.raises(ToolError, match="no free clip slot"):
        await server.call_tool("compose", {"kind": "chords", "track": 1})


# --------------------------------------------------------------------------
# Arrangement clips (a section of a song)
# --------------------------------------------------------------------------


def _arrangement_notes(live, track, index):
    def read():
        clip = sorted(live.song.tracks[track].arrangement_clips, key=lambda c: c.start_time)[index]
        return sorted(clip.notes.values(), key=lambda n: (n["start_time"], n["pitch"]))
    return live.on_main(read)


async def test_compose_rewrites_an_arrangement_clip_over_its_length(client, live):
    # A song's bass: an 8 bar Drop at bar 1, a 24 bar Groove at bar 9 with a plain root note pattern
    server = build_server(client)
    await client.call("arrangement.edit", {"actions": [
        {"action": "add_clip", "track": 1, "start": 0, "length": 32}, {"action": "add_clip", "track": 1, "start": 32, "length": 96}]})
    await client.call("notes.apply", {"track": 1, "arrangement_clip": 1, "add": [
        {"pitch": 33, "start_time": float(b), "duration": 0.5} for b in range(0, 96, 4)]})
    before = _arrangement_notes(live, 1, 1)

    preview = _payload(await server.call_tool("compose", {"kind": "bass", "track": 1, "arrangement_clip": 1, "seed": 3,
                                                           "style": "syncopated", "density": 0.8, "preview": True}))
    change = preview["changes"][0]
    assert change["arrangement_clip"] == 1 and "slot" not in change and change["action"] == "replace notes"
    assert change["remove"] == len(before)
    assert _arrangement_notes(live, 1, 1) == before  # a preview changes nothing

    result = _payload(await server.call_tool("apply_plan", {"plan_id": preview["plan_id"]}))
    assert result["applied"][0]["arrangement_clip"] == 1
    after = _arrangement_notes(live, 1, 1)
    assert after != before and max(n["start_time"] for n in after) >= 88  # the whole 24 bars, not 4
    assert _arrangement_notes(live, 1, 0) == []  # the Drop is untouched


async def test_compose_merges_into_an_arrangement_clip_on_request(client, live):
    server = build_server(client)
    await client.call("arrangement.edit", {"actions": [{"action": "add_clip", "track": 1, "start": 0, "length": 16}]})
    await client.call("notes.apply", {"track": 1, "arrangement_clip": 0, "add": [{"pitch": 33, "start_time": 0.0, "duration": 1}]})
    merged = _payload(await server.call_tool("compose", {"kind": "bass", "track": 1, "arrangement_clip": 0, "seed": 1, "if_exists": "merge"}))
    assert merged["changes"][0]["remove"] == 0 and len(_arrangement_notes(live, 1, 0)) > 1


async def test_transform_notes_varies_an_arrangement_clip(client, live):
    server = build_server(client)
    await client.call("arrangement.edit", {"actions": [{"action": "add_clip", "track": 1, "start": 16, "length": 16}]})
    await client.call("notes.apply", {"track": 1, "arrangement_clip": 0, "add": [
        {"pitch": 33 + (b % 3) * 7, "start_time": b * 0.5, "duration": 0.25} for b in range(32)]})
    before = _arrangement_notes(live, 1, 0)
    result = _payload(await server.call_tool("transform_notes", {"track": 1, "arrangement_clip": 0, "operation": "variation",
                                                                 "amount": 0.8, "seed": 2}))
    change = result["changes"][0]
    assert change["arrangement_clip"] == 0 and change["add"] + change["modify"] + change["remove"] > 0
    assert _arrangement_notes(live, 1, 0) != before


async def test_an_arrangement_clip_by_name_and_a_plan_applied_twice(client, live):
    # the chat names the clip ("vary the Groove Bass"); the user applies the preview, then the model asks too
    server = build_server(client)
    await client.call("arrangement.edit", {"actions": [
        {"action": "add_clip", "track": 1, "start": 0, "length": 16, "name": "Drop Bass"},
        {"action": "add_clip", "track": 1, "start": 16, "length": 16, "name": "Groove Bass"}]})
    await client.call("notes.apply", {"track": 1, "arrangement_clip": 1, "add": [
        {"pitch": 33 + (b % 3) * 7, "start_time": b * 0.5, "duration": 0.25} for b in range(32)]})
    before = _arrangement_notes(live, 1, 1)
    preview = _payload(await server.call_tool("transform_notes", {"track": 1, "arrangement_clip": "groove bass",
                                                                  "operation": "variation", "amount": 0.8, "seed": 2, "preview": True}))
    assert preview["changes"][0]["arrangement_clip"] == 1
    assert _payload(await server.call_tool("apply_plan", {"plan_id": preview["plan_id"]}))["applied"]
    again = _payload(await server.call_tool("apply_plan", {"plan_id": preview["plan_id"]}))
    assert again["already_applied"] and "nothing more to do" in again["message"]
    assert _arrangement_notes(live, 1, 1) != before and _arrangement_notes(live, 1, 0) == []
    # a part of a name works when only one clip has it; otherwise the error lists the clips
    assert _payload(await server.call_tool("compose", {"kind": "bass", "track": 1, "arrangement_clip": "Drop", "seed": 1,
                                                        "preview": True}))["changes"][0]["arrangement_clip"] == 0
    with pytest.raises(ToolError, match="2 arrangement clips match 'Bass'.*0 'Drop Bass', 1 'Groove Bass'"):
        await server.call_tool("compose", {"kind": "bass", "track": 1, "arrangement_clip": "Bass"})
    with pytest.raises(ToolError, match="No arrangement clip named 'Outro'"):
        await server.call_tool("compose", {"kind": "bass", "track": 1, "arrangement_clip": "Outro"})


async def test_vary_the_second_half_and_hints_for_a_wrong_target(client, live):
    server = build_server(client)
    await client.call("arrangement.edit", {"actions": [{"action": "add_clip", "track": 1, "start": 0, "length": 32, "name": "Break Bass"}]})
    await client.call("notes.apply", {"track": 1, "arrangement_clip": 0, "add": [
        {"pitch": 33 + (b % 3) * 7, "start_time": b * 0.5, "duration": 0.25} for b in range(64)]})
    before = _arrangement_notes(live, 1, 0)
    result = _payload(await server.call_tool("transform_notes", {"track": 1, "arrangement_clip": "Break Bass", "operation": "variation",
                                                                 "amount": 1.0, "seed": 4, "from_bar": 5}))
    assert result["changes"][0]["notes_considered"] == 32  # bars 5-8 of the 8 bar clip
    after = _arrangement_notes(live, 1, 0)
    assert [n for n in after if n["start_time"] < 16] == [n for n in before if n["start_time"] < 16] and after != before
    with pytest.raises(ToolError, match="No notes start in bars 9-end"):
        await server.call_tool("transform_notes", {"track": 1, "arrangement_clip": 0, "operation": "variation", "from_bar": 9})
    # "vary the Break Bass" as a compose/bass step: the error points to transform_notes
    with pytest.raises(ToolError, match="operation changes an existing clip: use transform_notes"):
        await server.call_tool("plan_changes", {"steps": [{"kind": "bass", "track": 1, "arrangement_clip": 0, "operation": "variation"}]})
    # a new slot clip on a track with a song part says the Arrangement stays as it was
    new = _payload(await server.call_tool("compose", {"kind": "bass", "track": 1, "seed": 1, "preview": True}))
    assert "arrangement clips ('Break Bass') stay unchanged" in new["changes"][0]["note"]


async def test_arrangement_targets_are_checked(client, live):
    server = build_server(client)
    with pytest.raises(ToolError, match="either slot .* or arrangement_clip"):
        await server.call_tool("compose", {"kind": "bass", "track": 1, "slot": 0, "arrangement_clip": 0})
    with pytest.raises(ToolError, match="give slot .* or arrangement_clip"):
        await server.call_tool("transform_notes", {"track": 1, "operation": "humanize"})
    with pytest.raises(ToolError, match="No arrangement clip index 2 .*its clips: none.*get_arrangement"):
        await server.call_tool("compose", {"kind": "bass", "track": 1, "arrangement_clip": 2})
    # chords_from and follow_kick can name arrangement clips too
    await client.call("arrangement.edit", {"actions": [{"action": "add_clip", "track": 1, "start": 0, "length": 16}]})
    await client.call("notes.apply", {"track": 1, "arrangement_clip": 0, "add": [
        {"pitch": p, "start_time": float(bar * 4), "duration": 4} for bar, root in enumerate((57, 53, 48, 55)) for p in (root, root + 4, root + 7)]})
    bass = _payload(await server.call_tool("compose", {"kind": "bass", "track": 1, "slot": 0, "seed": 1, "style": "sustained",
                                                       "chords_from": {"track": 1, "arrangement_clip": 0}}))
    assert bass["changes"][0]["chords_source"] == "clip"


async def test_plan_changes_ignores_preview_in_a_step(client, live):
    server = build_server(client)
    plan = _payload(await server.call_tool("plan_changes", {"steps": [{"kind": "bass", "track": 1, "slot": 0, "preview": True}]}))
    assert plan["changes"][0]["kind"] == "bass"
