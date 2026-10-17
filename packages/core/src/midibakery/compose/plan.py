"""Plan/Apply: turn composition requests into a reviewable diff, then run it as one undo step.

``build_plan`` only *reads* from Live (song, tracks, notes, drum pads) and
returns a :class:`Plan` holding the exact ``clip.create`` / ``notes.apply`` ops
it would run. ``apply_plan`` first checks that the target clips still look as
they did when the plan was made, then sends all ops as one ``batch``, which
Live records as a single undo step.

Steps in one plan see each other: a bass step can follow the kick of a drum
clip planned before it, and harmonic steps without explicit chords reuse the
chords of the first harmonic step.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import secrets
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from midibakery.bridge.client import BridgeError
from midibakery.compose import harmony as harmony_mod
from midibakery.compose.drums import DrumMap, kick_times, map_pads
from midibakery.compose.harmony import Harmony
from midibakery.compose.notes import Note, fit_to_length
from midibakery.compose.parts import generate_part
from midibakery.compose.rhythm import Meter
from midibakery.compose.specs import (
    BassSpec,
    ChordsSpec,
    ClipLocation,
    DrumsSpec,
    Generate,
    Harmonic,
    MelodySpec,
    Spec,
    TransformSpec,
)
from midibakery.compose.standalone import run_transform
from midibakery.compose.theory import Scale, pitch_name

Call = Callable[..., Awaitable[Any]]
MAX_BATCH_OPS = 256
PREVIEW_NOTES = 48


def new_seed() -> int:
    return secrets.randbelow(2**31)


@dataclass(frozen=True)
class Place:
    """A clip a step reads or writes: a Session View slot or an Arrangement clip (by index on its track)."""

    track: int
    slot: int | None = None
    arrangement_clip: int | None = None

    @classmethod
    def of(cls, spec: Any) -> Place:
        return cls(spec.track, spec.slot, getattr(spec, "arrangement_clip", None))

    @property
    def in_arrangement(self) -> bool:
        return self.arrangement_clip is not None

    def params(self) -> dict[str, Any]:
        if self.in_arrangement:
            return {"track": self.track, "arrangement_clip": self.arrangement_clip}
        return {"track": self.track, "slot": self.slot}

    def __str__(self) -> str:
        if self.in_arrangement:
            return f"arrangement clip index {self.arrangement_clip} on track index {self.track}"
        return f"slot index {self.slot} on track index {self.track}"


def _fingerprint(track: tuple, exists: bool, is_midi: bool, length: float | None, notes: list[dict] | None) -> str:
    payload = [track, exists, is_midi, length, sorted(tuple(sorted(n.items())) for n in notes or [])]
    return hashlib.sha256(json.dumps(payload, default=str).encode()).hexdigest()[:16]


@dataclass
class ClipState:
    exists: bool
    is_midi: bool = True
    length: float | None = None
    notes: list[dict] | None = None
    planned: bool = False
    # (id, name, kind) of the track: indices shift when tracks are added or deleted.
    track: tuple = ()

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.track, self.exists, self.is_midi, self.length, self.notes)


class Session:
    """Read-only view of the open Live set, cached for the lifetime of one plan."""

    def __init__(self, call: Call):
        self._call = call
        self.song: dict[str, Any] = {}
        self.tracks: list[dict[str, Any]] = []
        self.scale = Scale(0)
        self.meter = Meter()
        self.harmony: Harmony | None = None
        self.planned: dict[Place, ClipState] = {}
        self._clips: dict[Place, ClipState] = {}
        self._arrangement: dict[int, list[dict[str, Any]]] = {}
        self._pads: dict[tuple[int, int | None], list[dict] | None] = {}

    async def load(self) -> Session:
        self.song, tracks = await asyncio.gather(self._call("song.get", {}), self._call("tracks.list", {}))
        self.tracks = tracks["tracks"]
        self.scale = Scale.from_song(self.song)
        self.meter = Meter(int(self.song.get("signature_numerator", 4)), int(self.song.get("signature_denominator", 4)))
        return self

    def track(self, index: int) -> dict[str, Any]:
        if not 0 <= index < len(self.tracks):
            raise BridgeError("NOT_FOUND", f"No track index {index} (song has {len(self.tracks)})", {"track": index})
        return self.tracks[index]

    async def clip(self, place: Place, include_planned: bool = True) -> ClipState:
        key = place
        if include_planned and key in self.planned:
            return self.planned[key]
        if key in self._clips:
            return self._clips[key]
        if place.in_arrangement:
            state = await self._arrangement_clip(place)
            self._clips[key] = state
            return state
        track, slot = place.track, place.slot
        assert slot is not None
        info = self.track(track)
        scenes = int(self.song.get("scene_count", slot + 1))
        if slot >= scenes:
            raise BridgeError("NOT_FOUND", f"No clip slot index {slot} (song has {scenes} scenes; create a scene first)", {"slot": slot})
        found = next((c for c in info.get("clips", []) if c["slot"] == slot), None)
        identity = (info.get("id"), info.get("name"), info.get("kind"))
        if found is None:
            state = ClipState(False, track=identity)
        elif not found.get("is_midi"):
            state = ClipState(True, False, found.get("length"), track=identity)
        else:
            notes = (await self._call("notes.get", {"track": track, "slot": slot}))["notes"]
            state = ClipState(True, True, found.get("length"), notes, track=identity)
        self._clips[key] = state
        return state

    async def _arrangement_clips(self, track: int) -> list[dict[str, Any]]:
        if track not in self._arrangement:
            found = (await self._call("arrangement.get", {"track": track}))["tracks"]
            self._arrangement[track] = found[0]["clips"] if found else []
        return self._arrangement[track]

    async def arrangement_index(self, track: int, ref: int | str) -> int:
        """An arrangement clip given by index or by name: the exact name (any case), else the one clip whose
        name contains it."""
        if isinstance(ref, int):
            return ref
        if ref.strip().isdigit():
            return int(ref.strip())
        clips = await self._arrangement_clips(track)
        wanted = ref.strip().casefold()
        exact = [c for c in clips if str(c.get("name", "")).strip().casefold() == wanted]
        found = exact or [c for c in clips if wanted in str(c.get("name", "")).casefold()]
        if len(found) == 1 or (exact and found):
            return int(found[0]["index"])
        info = self.track(track)
        listed = ", ".join(f"{c['index']} '{c.get('name', '')}'" for c in clips[:16]) or "none"
        problem = f"{len(found)} arrangement clips match '{ref}'" if found else f"No arrangement clip named '{ref}'"
        raise BridgeError("NOT_FOUND", f"{problem} on track index {track} '{info.get('name')}' (its clips: {listed})",
                          {"track": track, "arrangement_clip": ref})

    async def _arrangement_clip(self, place: Place) -> ClipState:
        """An arrangement clip: it exists or the request is wrong (no new clips in the Arrangement here)."""
        info = self.track(place.track)
        identity = (info.get("id"), info.get("name"), info.get("kind"))
        clips = await self._arrangement_clips(place.track)
        index = place.arrangement_clip
        assert isinstance(index, int)
        if index >= len(clips):
            listed = ", ".join(f"{c['index']} '{c.get('name', '')}' at beat {c['start']:g}" for c in clips[:12]) or "none"
            raise BridgeError(
                "NOT_FOUND",
                f"No arrangement clip index {index} on track index {place.track} '{info.get('name')}' "
                f"(its clips: {listed}); get_arrangement lists them, edit_arrangement adds new ones",
                {"track": place.track, "arrangement_clip": index},
            )
        clip = clips[index]
        if not clip.get("is_midi"):
            return ClipState(True, False, clip["end"] - clip["start"], track=identity)
        read = await self._call("notes.get", place.params())
        length = read.get("clip_length") or clip["end"] - clip["start"]
        return ClipState(True, True, length, read["notes"], track=identity)

    def free_slot(self, track: int, taken: set[Place]) -> int:
        """The track's first slot without a clip and not targeted by this plan."""
        used = {c["slot"] for c in self.track(track).get("clips", [])} | {p.slot for p in taken if p.track == track and p.slot is not None}
        scenes = int(self.song.get("scene_count", 0))
        free = next((s for s in range(scenes) if s not in used), None)
        if free is None:
            raise BridgeError("CONFLICT", f"Track index {track} has no free clip slot; create a scene first or pick a slot", {"track": track})
        return free

    async def drum_pads(self, track: int, device: int | None = None) -> list[dict] | None:
        """Pads of the track's Drum Rack, or None (no rack, or an older Remote Script)."""
        key = (track, device)
        if key not in self._pads:
            params: dict[str, Any] = {"track": track}
            if device is not None:
                params["device"] = device
            try:
                self._pads[key] = (await self._call("drum_rack.pads", params))["pads"]
            except BridgeError as exc:
                if exc.code not in ("NOT_FOUND", "UNSUPPORTED", "UNKNOWN_OP", "INVALID_PARAMS"):
                    raise
                if device is not None and exc.code == "INVALID_PARAMS":
                    raise
                self._pads[key] = None
        return self._pads[key]


@dataclass
class ClipChange:
    place: Place
    kind: str
    seed: int | None = None
    create: dict[str, Any] | None = None
    add: list[Note] = field(default_factory=list)
    modify: list[dict[str, Any]] = field(default_factory=list)
    remove: list[int] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)
    length: float = 0.0  # clip length after the change, in beats
    existing: list[dict[str, Any]] = field(default_factory=list)  # notes in the clip before the change

    @property
    def track(self) -> int:
        return self.place.track

    @property
    def slot(self) -> int | None:
        return self.place.slot

    @property
    def removes_existing(self) -> bool:
        """True when user notes disappear without being re-created from them (replace, not arpeggiate)."""
        return bool(self.remove) and self.kind != "transform"

    def roll(self) -> dict[str, Any]:
        """Everything a piano-roll view needs to draw before and after, untruncated."""
        return {
            **self.place.params(),
            "kind": self.kind,
            "length": self.length,
            "existing": [
                [n["note_id"], n["pitch"], n["start_time"], n["duration"], n["velocity"], n.get("probability", 1.0)]
                for n in self.existing
            ],
            "add": [[n.pitch, n.start_time, n.duration, n.velocity, n.probability] for n in (a.clamped() for a in self.add)],
            "modify": self.modify,
            "remove": self.remove,
        }

    @property
    def edits_notes(self) -> bool:
        return bool(self.add or self.modify or self.remove)

    def ops(self) -> list[dict[str, Any]]:
        ops: list[dict[str, Any]] = []
        if self.create:
            ops.append({"op": "clip.create", "params": self.create})
        if self.edits_notes:
            params: dict[str, Any] = self.place.params()
            if self.add:
                params["add"] = [n.to_spec() for n in self.add]
            if self.modify:
                params["modify"] = self.modify
            if self.remove:
                params["remove"] = self.remove
            ops.append({"op": "notes.apply", "params": params})
        return ops

    def preview(self) -> dict[str, Any]:
        if self.create:
            action = "create clip"
        elif self.remove and self.add and self.kind != "transform":
            action = "replace notes"
        elif self.edits_notes:
            action = "edit notes"
        else:
            action = "no change"
        data: dict[str, Any] = {
            **self.place.params(),
            "kind": self.kind,
            "action": action,
            "add": len(self.add),
            "modify": len(self.modify),
            "remove": len(self.remove),
        }
        if self.seed is not None:
            data["seed"] = self.seed
        if self.create:
            data["clip_length"] = self.create["length"]
        if self.add:
            pitches = [n.pitch for n in self.add]
            data["pitch_range"] = [pitch_name(min(pitches)), pitch_name(max(pitches))]
            data["notes"] = [_compact(n) for n in self.add[:PREVIEW_NOTES]]
            if len(self.add) > PREVIEW_NOTES:
                data["notes_truncated"] = len(self.add) - PREVIEW_NOTES
        if self.modify:
            data["changes"] = self.modify[:PREVIEW_NOTES]
        data.update(self.info)
        return data


def _compact(note: Note) -> dict[str, Any]:
    n = note.clamped()
    data: dict[str, Any] = {"pitch": n.pitch, "start": n.start_time, "dur": n.duration, "vel": n.velocity}
    if n.probability < 1:
        data["prob"] = n.probability
    if n.velocity_deviation:
        data["vel_dev"] = n.velocity_deviation
    return data


@dataclass
class Plan:
    id: str
    changes: list[ClipChange]
    fingerprints: dict[Place, str]
    scale: str
    meter: str

    def ops(self) -> list[dict[str, Any]]:
        return [op for change in self.changes for op in change.ops()]

    @property
    def removes_existing(self) -> int:
        """Number of existing notes the plan deletes outright (clip replacements)."""
        return sum(len(c.remove) for c in self.changes if c.removes_existing)

    def roll(self) -> dict[str, Any]:
        return {"plan_id": self.id, "scale": self.scale, "time_signature": self.meter, "clips": [c.roll() for c in self.changes]}

    def preview(self) -> dict[str, Any]:
        return {
            "plan_id": self.id,
            "scale": self.scale,
            "time_signature": self.meter,
            "op_count": len(self.ops()),
            "changes": [c.preview() for c in self.changes],
        }


class PlanStore:
    """Recent plans kept in memory until applied (oldest dropped first)."""

    def __init__(self, capacity: int = 32):
        self._plans: OrderedDict[str, Plan] = OrderedDict()
        self._applied: OrderedDict[str, None] = OrderedDict()
        self._capacity = capacity

    def put(self, plan: Plan) -> None:
        self._applied.pop(plan.id, None)
        self._plans[plan.id] = plan
        while len(self._plans) > self._capacity:
            self._plans.popitem(last=False)

    def pop(self, plan_id: str) -> Plan:
        try:
            return self._plans.pop(plan_id)
        except KeyError:
            raise BridgeError("NOT_FOUND", f"Unknown or already applied plan '{plan_id}'; call plan_changes again") from None

    def get(self, plan_id: str) -> Plan | None:
        return self._plans.get(plan_id)

    def applied(self, plan_id: str) -> None:
        """Remember that the plan went into Live: applying it again (the user pressed Apply in the preview,
        then the model asks too) says so instead of failing."""
        self._applied[plan_id] = None
        while len(self._applied) > self._capacity:
            self._applied.popitem(last=False)

    def was_applied(self, plan_id: str) -> bool:
        return plan_id in self._applied


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


async def _target(session: Session, spec: Generate) -> tuple[ClipState, float, dict[str, Any] | None, list[int]]:
    """Existing state, clip length, clip.create params and note ids to remove for a generator target."""
    place = Place.of(spec)
    track = session.track(spec.track)
    if track.get("kind") != "midi":
        raise BridgeError("INVALID_PARAMS", f"Track index {spec.track} '{track.get('name')}' is not a MIDI track")
    state = await session.clip(place)
    assert spec.bars is not None  # build_plan fills it in
    length = session.meter.beats_per_bar * spec.bars
    if state.planned:
        raise BridgeError("INVALID_PARAMS", f"The clip in {place} is already written by an earlier step of this plan")
    if not state.exists:
        name = spec.clip_name or spec.kind.title()  # type: ignore[attr-defined]
        return state, length, {**place.params(), "length": length, "name": name}, []
    if not state.is_midi:
        raise BridgeError("INVALID_PARAMS", f"The clip in {place} is an audio clip")
    # The user names an arrangement clip to rewrite it: its notes are replaced (after a confirmation) unless merge.
    if_exists = spec.if_exists or ("replace" if place.in_arrangement else "error")
    if if_exists == "error":
        raise BridgeError(
            "CONFLICT",
            f"The {place} already has a clip; pass if_exists='replace' or 'merge', or pick an empty slot",
        )
    remove = [n["note_id"] for n in state.notes or []] if if_exists == "replace" else []
    return state, min(length, state.length or length), None, remove


def is_drum_track(track: dict[str, Any]) -> bool:
    """A track (as tracks.list lists it) with a Drum Rack."""
    return any(d.get("class_name") == "DrumGroupDevice" for d in track.get("devices", []))


def _not_drums(session: Session, index: int) -> None:
    """chords_from on a drum clip detects nonsense chords; small models pick it by mistake."""
    track = session.track(index)
    if not is_drum_track(track):
        return
    candidates = [
        f"{t['index']} '{t.get('name')}'" for t in session.tracks
        if t.get("kind") == "midi" and not is_drum_track(t) and any(c.get("is_midi") for c in t.get("clips", []))
    ]
    hint = f"; MIDI tracks with clips: {', '.join(candidates)}" if candidates else ""
    raise BridgeError(
        "INVALID_PARAMS",
        f"chords_from points to track index {index} '{track.get('name')}', a drum track (Drum Rack) without chords. "
        f"Use the chord clip's track and slot, or omit chords_from{hint}",
    )


async def _harmony(session: Session, spec: Harmonic, rng: random.Random) -> Harmony:
    meter, scale = session.meter, session.scale
    try:
        if spec.progression:
            result = harmony_mod.from_progression(spec.progression, scale, meter, spec.bars, spec.chords_per_bar)
        elif spec.chords_from is not None:
            _not_drums(session, spec.chords_from.track)
            where = Place.of(spec.chords_from)
            source = await session.clip(where)
            if not source.exists or not source.is_midi:
                raise BridgeError("NOT_FOUND", f"chords_from: no MIDI clip in {where}")
            source_bars = max(1, round((source.length or meter.beats_per_bar) / meter.beats_per_bar))
            result = harmony_mod.detect(source.notes or [], scale, meter, source_bars, spec.chords_per_bar)
            result = result.for_length(meter, spec.bars)
        elif session.harmony is not None:
            result = session.harmony.for_length(meter, spec.bars)
        else:
            sevenths = isinstance(spec, ChordsSpec) and spec.sevenths
            result = harmony_mod.auto(scale, meter, spec.bars, spec.chords_per_bar, rng, sevenths)
    except ValueError as exc:
        raise BridgeError("INVALID_PARAMS", str(exc)) from None
    if session.harmony is None:
        session.harmony = result
    return result


async def _kick(session: Session, location: ClipLocation, length: float) -> tuple[list[float], dict[str, Any]]:
    where = Place.of(location)
    source = await session.clip(where)
    if not source.exists or not source.is_midi:
        raise BridgeError("NOT_FOUND", f"follow_kick: no MIDI clip in {where}")
    drum_map = map_pads(await session.drum_pads(location.track))
    kick_pitch = drum_map.note("kick")
    assert kick_pitch is not None
    loop = source.length or length
    onsets = kick_times(source.notes or [], kick_pitch)
    times = sorted({round(t + k * loop, 6) for k in range(int(length // loop) + 1) for t in onsets if t + k * loop < length})
    return times, {"kick_pitch": kick_pitch, "kick_hits": len(times)}


def _finish(session: Session, spec: Generate, kind: str, seed: int, notes: list[Note], target: tuple, info: dict) -> ClipChange:
    state, length, create, remove = target
    notes = fit_to_length(notes, length)
    session.planned[Place.of(spec)] = ClipState(
        True, True, length if create else state.length, [_as_dict(n) for n in notes], planned=True
    )
    return ClipChange(
        Place.of(spec), kind, seed, create, notes, [], remove, info,
        length=length if create else (state.length or length), existing=list(state.notes or []),
    )


def _as_dict(note: Note) -> dict[str, Any]:
    return {
        "pitch": note.pitch,
        "start_time": note.start_time,
        "duration": note.duration,
        "velocity": note.velocity,
        "mute": note.mute,
        "probability": note.probability,
        "velocity_deviation": note.velocity_deviation,
    }


async def _plan_drums(session: Session, spec: DrumsSpec) -> ClipChange:
    target = await _target(session, spec)
    seed = spec.seed if spec.seed is not None else new_seed()
    pads = await session.drum_pads(spec.track, spec.device)
    drum_map: DrumMap = map_pads(pads)
    notes = generate_part(spec, None, session.scale, session.meter, spec.bars, random.Random(seed), drum_map)
    info = {
        "style": spec.style,
        "drum_map": drum_map.source,
        "pads": {role: drum_map.notes[role] for role in sorted({_role_of(drum_map, n.pitch) for n in notes} - {None})},
    }
    return _finish(session, spec, "drums", seed, notes, target, info)


def _role_of(drum_map: DrumMap, pitch: int) -> str | None:
    return next((role for role, note in drum_map.notes.items() if note == pitch), None)


async def _plan_bass(session: Session, spec: BassSpec) -> ClipChange:
    target = await _target(session, spec)
    seed = spec.seed if spec.seed is not None else new_seed()
    rng = random.Random(seed)
    harmony = await _harmony(session, spec, rng)
    kick, info = None, {}
    if spec.follow_kick is not None:
        kick, info = await _kick(session, spec.follow_kick, target[1])
        info["kick_relation"] = spec.kick_relation
    notes = generate_part(spec, harmony, session.scale, session.meter, spec.bars, rng, kick=kick)
    info = {"style": spec.style, "chords": harmony.describe(), "chords_source": harmony.source, **info}
    return _finish(session, spec, "bass", seed, notes, target, info)


async def _plan_chords(session: Session, spec: ChordsSpec) -> ClipChange:
    target = await _target(session, spec)
    seed = spec.seed if spec.seed is not None else new_seed()
    rng = random.Random(seed)
    harmony = await _harmony(session, spec, rng)
    try:
        notes = generate_part(spec, harmony, session.scale, session.meter, spec.bars, rng)
    except ValueError as exc:
        raise BridgeError("INVALID_PARAMS", str(exc)) from None
    info = {"rhythm": spec.rhythm, "chords": harmony.describe(), "chords_source": harmony.source}
    return _finish(session, spec, "chords", seed, notes, target, info)


async def _plan_melody(session: Session, spec: MelodySpec) -> ClipChange:
    target = await _target(session, spec)
    seed = spec.seed if spec.seed is not None else new_seed()
    rng = random.Random(seed)
    harmony = await _harmony(session, spec, rng)
    notes = generate_part(spec, harmony, session.scale, session.meter, spec.bars, rng)
    info = {"chords": harmony.describe(), "chords_source": harmony.source}
    return _finish(session, spec, "melody", seed, notes, target, info)


async def _plan_transform(session: Session, spec: TransformSpec) -> ClipChange:
    place = Place.of(spec)
    if place in session.planned:
        raise BridgeError("INVALID_PARAMS", "Cannot transform a clip written earlier in the same plan; apply first, then transform")
    state = await session.clip(place)
    if not state.exists or not state.is_midi:
        raise BridgeError("NOT_FOUND", f"No MIDI clip in {place}")
    notes = state.notes or []
    if spec.note_ids is not None:
        known = {n["note_id"] for n in notes}
        unknown = sorted(set(spec.note_ids) - known)
        if unknown:
            raise BridgeError("NOT_FOUND", "Unknown note_id(s) in this clip", {"note_ids": unknown})
        wanted = set(spec.note_ids)
        notes = [n for n in notes if n["note_id"] in wanted]
    seed = spec.seed if spec.seed is not None else new_seed()
    op = spec.operation
    try:
        diff = run_transform(spec, notes, session.scale, session.meter, state.length, seed)
    except ValueError as exc:
        raise BridgeError("INVALID_PARAMS", str(exc)) from None
    uses_seed = op in ("humanize", "variation", "arpeggiate")
    return ClipChange(
        place, "transform", seed if uses_seed else None, None, diff.add, diff.modify, diff.remove,
        {"operation": op, "notes_considered": len(notes)},
        length=state.length or 0.0,
        existing=list(state.notes or []),
    )


_PLANNERS = {
    "drums": _plan_drums,
    "bass": _plan_bass,
    "chords": _plan_chords,
    "melody": _plan_melody,
    "transform": _plan_transform,
}


async def _resolve(session: Session, step: Generate, targets: set[Place]) -> Generate:
    """The defaults that depend on Live: the first free slot, and the length (4 bars, or the arrangement clip's)."""
    update: dict[str, Any] = {}
    if step.slot is None and step.arrangement_clip is None:
        update["slot"] = session.free_slot(step.track, targets)
    if step.bars is None:
        bars = 4
        if step.arrangement_clip is not None:
            state = await session.clip(Place.of(step), include_planned=False)
            bpb = session.meter.beats_per_bar
            bars = max(1, math.ceil(round((state.length or bpb) / bpb, 6)))  # whole bars, rounded up
            if bars > 64:
                raise BridgeError("INVALID_PARAMS", f"The {Place.of(step)} is {bars} bars long; compose writes at most 64 (pass bars)")
        update["bars"] = bars
    return step.model_copy(update=update) if update else step


async def _by_index(session: Session, spec: Any) -> Any:
    """`spec` (and the clip locations in it, e.g. chords_from) with arrangement clips given by name as indexes."""
    update: dict[str, Any] = {}
    for name in type(spec).model_fields:
        value = getattr(spec, name)
        if isinstance(value, BaseModel):
            resolved = await _by_index(session, value)
            if resolved is not value:
                update[name] = resolved
    ref = getattr(spec, "arrangement_clip", None)
    if isinstance(ref, str):
        update["arrangement_clip"] = await session.arrangement_index(spec.track, ref)
    return spec.model_copy(update=update) if update else spec


async def build_plan(call: Call, steps: list[Spec]) -> Plan:
    if not steps:
        raise BridgeError("INVALID_PARAMS", "A plan needs at least one step")
    session = await Session(call).load()
    changes = []
    targets: set[Place] = set()
    for step in steps:
        step = await _by_index(session, step)
        if isinstance(step, Generate):
            step = await _resolve(session, step, targets)
        key = Place.of(step)
        if key in targets:
            raise BridgeError("INVALID_PARAMS", f"Two steps target {key}; use one step per clip")
        targets.add(key)
        changes.append(await _PLANNERS[step.kind](session, step))  # type: ignore[attr-defined]
    fingerprints = {}
    for key in targets:
        state = await session.clip(key, include_planned=False)
        fingerprints[key] = state.fingerprint
    plan = Plan(
        secrets.token_hex(4),
        changes,
        fingerprints,
        session.scale.describe(),
        f"{session.meter.numerator}/{session.meter.denominator}",
    )
    if len(plan.ops()) > MAX_BATCH_OPS:
        raise BridgeError("INVALID_PARAMS", f"Plan needs {len(plan.ops())} ops; split it (max {MAX_BATCH_OPS})")
    return plan


async def _roll_back(call: Call, plan: Plan, error: BridgeError) -> None:
    """A batch stops at its first failing op but keeps what already ran (one undo step).
    Undo that step so a plan is applied completely or not at all. Only when ops did
    run: undoing an empty batch would revert the user's previous action instead."""
    if not error.details.get("completed"):
        return
    try:
        await call("history.step", {"action": "undo"})
    except BridgeError as undo_error:
        error.message += f" (rolling back the {len(error.details['completed'])} completed op(s) failed: {undo_error.message})"
        return
    error.message += f"; the {len(error.details['completed'])} op(s) that had already run were undone, Live is unchanged"
    error.details["rolled_back"] = True


async def apply_plan(call: Call, plan: Plan, verify: bool = True) -> dict[str, Any]:
    """Run the plan as one batch (one undo step) after checking nothing changed underneath it."""
    if verify:
        session = await Session(call).load()

        async def current(place: Place) -> Any:
            try:
                return await session.clip(place, include_planned=False)
            except BridgeError as exc:
                if exc.code != "NOT_FOUND":
                    raise
                return None

        targets = list(plan.fingerprints.items())
        states = await asyncio.gather(*(current(place) for place, _ in targets))  # one round trip, not one per clip
        for (place, expected), state in zip(targets, states):
            if state is None or state.fingerprint != expected:
                raise BridgeError(
                    "CONFLICT",
                    f"The clip in {place} or its track changed since plan {plan.id} was made "
                    "(tracks added, deleted or edited); plan again",
                    place.params(),
                )
    ops = plan.ops()
    applied: list[dict[str, Any]] = []
    if ops:
        try:
            results = (await call("batch", {"ops": ops, "label": f"MidiBakery plan {plan.id}"}))["results"]
        except BridgeError as exc:
            await _roll_back(call, plan, exc)
            raise
        cursor = 0
        for change in plan.changes:
            entry: dict[str, Any] = {**change.place.params(), "kind": change.kind}
            for op in change.ops():
                result = results[cursor]
                cursor += 1
                if op["op"] == "notes.apply":
                    entry.update(added_note_ids=result["added"], modified=result["modified"], removed=result["removed"])
                else:
                    entry["created_clip"] = True
            if change.seed is not None:
                entry["seed"] = change.seed
            applied.append(entry)
    return {"plan_id": plan.id, "applied": applied, "op_count": len(ops), "undo": "one undo step reverts the whole plan"}
