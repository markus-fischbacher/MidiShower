"""MCP server exposing MidiBakery to Claude and other MCP clients.

Tools are generated from the op catalog so their schemas never drift from
what the Live bridge accepts. A few read tools combine ops to save round
trips for the model.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from importlib.resources import files
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.apps import Apps
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from mcp_types import CallToolResult, InputRequiredResult, TextContent
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from midibakery import __version__, license
from midibakery.bridge import BridgeClient, BridgeError
from midibakery.catalog import get_op
from midibakery.catalog.ops import ArrangementTool, ClipRef, Empty
from midibakery.compose import (
    ComposeRequest,
    PlanStore,
    Step,
    TransformSpec,
    apply_plan,
    build_plan,
)
from midibakery.compose.plan import Plan, new_seed
from midibakery.compose.references import find_reference, load_references
from midibakery.compose.song import SongPlan, SongRequest, apply_song, build_song
from midibakery.compose.takes import TakesRequest, build_takes
from midibakery.mcp.confirm import confirm, request_state

INSTRUCTIONS = """\
MidiBakery controls the Ableton Live 12 set that is open right now.
- Call get_session first (tempo, signature, scale with root_note 0=C..11=B, tracks with
  devices and clips). Tracks, slots, devices and scenes are zero-based indices that shift
  when tracks or scenes are created or deleted; users count from 1 ("track 1" = index 0).
- Times are beats (quarter notes); pitch 60 = C3. Prefer notes from the song scale.
- edit_notes and transform_notes are diffs by note_id from get_clip; never rewrite a whole
  clip to change a few notes. transform_notes from_bar/to_bar limit it to bars of the clip (its second
  half). Transpose: semitones are chromatic (12 = octave), steps are scale steps.
  run_batch makes several edits one undo step; undo takes back any write.
- Mixer: volume and sends in dB as Live shows them; routing by the names get_mixer(options=true)
  lists. Devices inside racks: chain_path [chain, device, ...].
- Arrangement: bar n starts at beat (n-1) * beats per bar. edit_arrangement never overwrites
  a clip; clip tools take arrangement_clip (from get_arrangement) instead of slot. compose and
  transform_notes too: to rewrite or vary one part of a song (the bass in its Groove), give the
  arrangement clip, by index or by its name ('Groove Bass'); compose then replaces its notes over
  its whole length.
- A whole song: plan_song, then apply_plan; say which tracks it used and created.
  compose_takes puts variations of a part on take lanes to comp in Live.
- compose writes one part into the track's first free slot (if_exists for occupied ones),
  reproducible by seed. Make parts fit: bass and melody chords_from the chord clip, the bass
  follow_kick the drum clip; or plan_changes, where later steps share the first step's chords.
- preview=true or plan_changes return a diff (a piano roll in MCP Apps clients) and a plan_id;
  apply_plan writes it as one undo step.
- get_reference: songs the MidiBakery Reference Analyzer device analyzed (sections, key, kick); plan_song(reference=name) follows one (warped; an unwarped one only at its own tempo).
- Deleting tracks or clips and replacing notes ask the user first; a declined change returns CANCELLED.
"""

PIANO_ROLL_URI = "ui://midibakery/piano-roll.html"
UI_META = {"ui": {"resourceUri": PIANO_ROLL_URI}}
# Ops that delete something the user made; edit_notes removals are explicit by note_id and not asked.
CONFIRM_OPS = {"track.delete", "clip.delete"}

Runner = Callable[[dict[str, Any], Context | None], Awaitable[Any]]

# (tool name, op, title); annotations come from the op spec.
TOOL_OPS: list[tuple[str, str, str]] = [
    ("set_song", "song.set", "Set tempo, signature, scale or loop"),
    ("transport", "transport.control", "Play / stop / stop all clips / record"),
    ("create_track", "track.create", "Create track"),
    ("update_track", "track.set", "Update track"),
    ("delete_track", "track.delete", "Delete track"),
    ("create_clip", "clip.create", "Create MIDI clip"),
    ("update_clip", "clip.set", "Update clip"),
    ("delete_clip", "clip.delete", "Delete clip"),
    ("launch_clip", "clip.launch", "Launch or stop clip"),
    ("edit_notes", "notes.apply", "Edit notes (diff)"),
    ("insert_device", "device.insert", "Insert native device"),
    ("find_browser_items", "browser.search", "Find presets, kits and devices"),
    ("load_browser_item", "browser.load", "Load preset, kit or device onto a track"),
    ("get_device_parameters", "device.params", "Get device parameters"),
    ("write_clip_envelope", "clip.envelope", "Write clip automation"),
    ("get_drum_pads", "drum_rack.pads", "Get Drum Rack pads"),
    ("set_device_parameter", "device.set_param", "Set device parameter"),
    ("get_mixer", "mixer.get", "Get mixer and routing"),
    ("update_mixer", "mixer.set", "Set volume, pan, sends or routing"),
    ("create_return_track", "return.create", "Create return track"),
    ("get_rack", "rack.get", "Get rack chains and macros"),
    ("update_rack", "rack.set", "Set macros, edit or add rack chains"),
    ("get_arrangement", "arrangement.get", "Get arrangement clips and locators"),
    ("create_scene", "scene.create", "Create scene"),
    ("fire_scene", "scene.fire", "Launch scene"),
    ("undo", "history.step", "Undo / redo"),
    ("run_batch", "batch", "Run several ops as one undo step"),
]


# (tool name, request model, title, description)
COMPOSE_TOOLS: list[tuple[str, type[BaseModel], str, str]] = [
    (
        "compose",
        ComposeRequest,
        "Compose a part",
        "Write one part as a new MIDI clip in the song scale and time signature. kind drums: pattern on the track's "
        "Drum Rack pads (found by name, General MIDI fallback); bass: follows the chords and can lock to or avoid a "
        "drum clip's kick (follow_kick); chords: voice-led, a functional progression unless given one; melody: motifs "
        "(A A' A B), chord tones on strong beats. Fields tagged (kind) are only for those kinds.",
    ),
    (
        "transform_notes",
        TransformSpec,
        "Transform notes",
        "Change a MIDI clip as a diff by note_id: humanize, quantize, invert (diatonic), "
        "arpeggiate, variation, or transpose (semitones chromatic, steps in key).",
    ),
]


class PlanStep(BaseModel):
    """One step: `kind` plus the fields of that compose tool. The schema stays small (it would
    otherwise repeat every compose tool); the full step is validated when the plan is built."""

    model_config = ConfigDict(extra="allow")

    kind: Literal["drums", "bass", "chords", "melody", "transform"] = Field(
        description="drums/bass/chords/melody take the compose fields, transform the transform_notes fields"
    )


STEP = TypeAdapter(Step)


class PlanChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[PlanStep] = Field(min_length=1, max_length=16, description="Composition steps, in order")

    def specs(self) -> list[Any]:
        # preview inside a step: models copy it from compose; plan_changes always previews
        return [STEP.validate_python({k: v for k, v in step.model_dump().items() if k != "preview"}) for step in self.steps]


class ApplyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(description="plan_id returned by plan_changes or a compose tool with preview=true")


PREVIEW_FIELD = Field(False, description="Only return the planned diff and a plan_id; nothing changes until apply_plan")


def _signature_from_model(model: type[BaseModel], skip: tuple[str, ...] = ()) -> inspect.Signature:
    params = []
    for name, field in model.model_fields.items():
        if name in skip:
            continue
        if field.is_required():
            default = inspect.Parameter.empty
        elif field.default_factory is not None:
            default = field.default_factory()
        else:
            default = field.default
        # Keep constraints and description, but let the signature own the default.
        annotation = Annotated[(field.annotation, Field(description=field.description), *field.metadata)]
        params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation))
    return inspect.Signature(params, return_annotation=dict[str, Any])


def _make_tool(
    model: type[BaseModel], run: Runner, skip: tuple[str, ...] = (), extra: dict[str, Any] | None = None,
    free: bool = False,
):
    """free: usable without a license (reading Live, undo); everything else needs the trial or a license."""

    async def tool(ctx: Context | None = None, **kwargs: Any) -> Any:
        if not free:
            try:
                license.require()
            except license.LicenseError as exc:
                raise ToolError(f"LICENSE_REQUIRED: {exc} Run `midibakery license` or use the Chat device.") from None
        try:
            result = await run(kwargs, ctx)
        except BridgeError as exc:
            hint = f" Details: {exc.details}" if exc.details else ""
            raise ToolError(f"{exc.code}: {exc.message}.{hint}") from None
        except ValidationError as exc:
            raise ToolError(f"INVALID_PARAMS: {_validation_message(exc, model)}") from None
        if isinstance(result, dict | CallToolResult | InputRequiredResult):
            return result
        return {"result": result}

    signature = _signature_from_model(model, skip)
    params = [inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Context | None)]
    params += list(signature.parameters.values())
    for name, (annotation, default) in (extra or {}).items():
        params.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation))
    signature = signature.replace(parameters=params)
    tool.__signature__ = signature  # type: ignore[attr-defined]
    tool.__annotations__ = {p.name: p.annotation for p in signature.parameters.values()} | {
        "return": signature.return_annotation
    }
    return tool


def _validation_message(exc: ValidationError, model: type[BaseModel]) -> str:
    """loc + msg only: echoing the full input costs context and buries the message. An unknown field
    names the fields the tool does take, so a model can fix the call instead of guessing again."""
    parts = []
    for e in exc.errors(include_url=False, include_context=False):
        where = ".".join(map(str, e["loc"])) or "arguments"
        if e["type"] == "extra_forbidden":
            parts.append(f"{where}: unknown field")
        else:
            parts.append(f"{where}: {e['msg']}")
    if any(e["type"] == "extra_forbidden" and len(e["loc"]) == 1 for e in exc.errors()):
        parts.append("fields of this tool: " + ", ".join(model.model_fields))
    if any(e["type"] == "extra_forbidden" and e["loc"][-1] == "operation" for e in exc.errors()):
        parts.append("operation changes an existing clip: use transform_notes (in plan_changes a step with kind 'transform'), "
                     "e.g. operation 'variation' with arrangement_clip")
    return "; ".join(parts)


def with_arrangement(session: dict[str, Any], arrangement: dict[str, Any]) -> dict[str, Any]:
    """The Arrangement in bars next to the Session View: locators, and per track its arrangement clips
    (the index compose and transform_notes take as arrangement_clip). A song from plan_song lives there;
    without this a model only sees the slots and writes a new clip instead of changing the section."""
    song = session["song"]
    bpb = song.get("signature_numerator", 4) * 4 / (song.get("signature_denominator", 4) or 4)
    bar = lambda beat: round(beat / bpb, 2) + 1  # noqa: E731
    clips = {t["index"]: t.get("clips", []) for t in arrangement.get("tracks", [])}
    tracks = [
        {**t, "arrangement_clips": [
            {"index": c["index"], "name": c.get("name", ""), "start_bar": bar(c["start"]), "bars": round((c["end"] - c["start"]) / bpb, 2)}
            for c in clips.get(t["index"], [])
        ]} if clips.get(t["index"]) else t
        for t in session.get("tracks", [])
    ]
    locators = [{"name": c["name"], "bar": bar(c["time"])} for c in arrangement.get("locators", [])]
    return {**session, "tracks": tracks, **({"locators": locators} if locators else {})}


def _clip_label(change: Any) -> str:
    """As the user counts: track 2 slot 1, track 2 arrangement clip 3."""
    if change.place.in_arrangement:
        return f"track {change.track + 1} arrangement clip {change.place.arrangement_clip + 1}"
    return f"track {change.track + 1} slot {change.slot + 1}"


def _with_roll(plan: Plan, payload: dict[str, Any], applied: bool) -> CallToolResult:
    """Text + structured result for the model, full before/after notes in _meta for the piano-roll view."""
    roll = plan.roll() | {"applied": applied}
    if applied:
        roll.pop("plan_id")
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, separators=(",", ":")))],
        structured_content=payload,
        _meta={"midibakery/roll": roll},
    )


def _piano_roll_html() -> str:
    return files("midibakery.mcp").joinpath("piano_roll.html").read_text(encoding="utf-8")


def build_server(client: BridgeClient | None = None) -> MCPServer:
    bridge = client or BridgeClient()
    plans = PlanStore()
    apps = Apps()
    apps.add_html_resource(
        PIANO_ROLL_URI,
        _piano_roll_html(),
        name="piano-roll",
        title="MidiBakery piano roll",
        description="Before/after piano roll of planned or applied note changes",
        prefers_border=True,
    )
    server = MCPServer(
        "midibakery",
        title="MidiBakery by Klirrwerk for Ableton Live",
        instructions=INSTRUCTIONS,
        version=__version__,
        extensions=[apps],
    )

    async def get_session(_: dict[str, Any], ctx: Context | None) -> dict[str, Any]:
        song, tracks = await asyncio.gather(bridge.call("song.get"), bridge.call("tracks.list"))
        session = {"song": song, **tracks}
        try:
            arrangement = await bridge.call("arrangement.get")
        except BridgeError:
            return session
        return with_arrangement(session, arrangement)

    async def get_clip(args: dict[str, Any], ctx: Context | None) -> dict[str, Any]:
        clip = await bridge.call("clip.get", args)
        if clip.get("is_midi"):
            clip["notes"] = (await bridge.call("notes.get", args))["notes"]
        return clip

    async def describe(op: str, params: dict[str, Any]) -> str:
        """Human wording for a destructive op, with track and clip names."""
        try:
            tracks = (await bridge.call("tracks.list"))["tracks"]
        except BridgeError:
            tracks = []
        track = tracks[params["track"]] if 0 <= params.get("track", -1) < len(tracks) else {}
        track_name = f"track {params.get('track', 0) + 1} '{track.get('name', '?')}'"
        if op == "track.delete":
            clips = len(track.get("clips", []))
            return f"Delete {track_name} with {clips} clip(s)?"
        if params.get("arrangement_clip") is not None:
            return f"Delete arrangement clip {params['arrangement_clip'] + 1} of {track_name}?"
        clip = next((c for c in track.get("clips", []) if c["slot"] == params.get("slot")), {})
        return f"Delete clip '{clip.get('name') or 'unnamed'}' in slot {params.get('slot', 0) + 1} of {track_name}?"

    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    server.add_tool(
        _make_tool(Empty, get_session, free=True),
        name="get_session",
        title="Get Live session",
        description="Song settings (tempo, signature, scale, transport) plus all tracks with devices and clips.",
        annotations=read_only,
    )
    server.add_tool(
        _make_tool(ClipRef, get_clip, free=True),
        name="get_clip",
        title="Get clip with notes",
        description="Clip properties and, for MIDI clips, all notes with note_id, probability and velocity deviation.",
        annotations=read_only,
    )

    async def get_reference(args: dict[str, Any], ctx: Context | None) -> dict[str, Any]:
        return read_references(ReferenceQuery.model_validate(args).name)

    server.add_tool(
        _make_tool(ReferenceQuery, get_reference, free=True),
        name="get_reference",
        title="Get reference song analysis",
        description="Sections (start bar, length, kick, elements), key and tempo of reference songs analyzed by the "
        "MidiBakery Reference Analyzer device in Live. " + inspect.cleandoc(ReferenceQuery.__doc__ or ""),
        annotations=read_only,
    )

    async def edit_arrangement(args: dict[str, Any], ctx: Context | None) -> dict[str, Any]:
        # One tool for the model; two ops for Live: locators need ticks of their own (see arrangement.locators).
        actions = ArrangementTool.model_validate(args).model_dump(exclude_none=True)["actions"]
        clips = [a for a in actions if a["action"] not in ("set_locator", "delete_locator")]
        locators = [a for a in actions if a["action"] in ("set_locator", "delete_locator")]
        result: dict[str, Any] = {}
        song = await bridge.call("song.get")
        if clips:
            result.update(await bridge.call("arrangement.edit", {"actions": clips}))
        if locators:
            result["locators"] = (await bridge.call("arrangement.locators", {"actions": locators}))["locators"]
        # Copying a session clip moves Live's play position to the copy's end; put it back where the user was.
        if not song["is_playing"] and (await bridge.call("song.get"))["current_song_time"] != song["current_song_time"]:
            await bridge.call("song.position", {"beat": song["current_song_time"]})
        return result

    server.add_tool(
        _make_tool(ArrangementTool, edit_arrangement),
        name="edit_arrangement",
        title="Add, copy or delete arrangement clips and locators",
        description="Add MIDI clips with notes, copy session clips, delete arrangement clips, set or delete locators. "
        + inspect.cleandoc(ArrangementTool.__doc__ or ""),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
    )

    for tool_name, op_name, title in TOOL_OPS:
        spec = get_op(op_name)

        async def run(args: dict[str, Any], ctx: Context | None, _op: str = op_name) -> Any:
            if _op in CONFIRM_OPS:
                asked = await confirm(ctx, await describe(_op, args))
                if asked is not None:
                    return asked
            elif _op == "batch":
                risky = [item for item in args.get("ops", []) if _item_op(item) in CONFIRM_OPS]
                if risky:
                    questions = [await describe(_item_op(i), _item_params(i)) for i in risky]
                    asked = await confirm(ctx, "Run this batch? It will " + " ".join(q[0].lower() + q[1:] for q in questions))
                    if asked is not None:
                        return asked
            return await bridge.call(_op, args)

        server.add_tool(
            _make_tool(spec.params, run, free=spec.read_only or op_name == "history.step"),
            name=tool_name,
            title=title,
            description=" ".join(filter(None, [spec.summary + ".", inspect.cleandoc(spec.params.__doc__ or "")])),
            annotations=ToolAnnotations(
                readOnlyHint=spec.read_only,
                destructiveHint=spec.destructive,
                idempotentHint=False,
                openWorldHint=False,
            ),
        )

    async def ask_before_replacing(plan: Plan, ctx: Context | None, state: dict[str, Any] | None = None):
        removed = plan.removes_existing
        if not removed:
            return None
        clips = ", ".join(_clip_label(c) for c in plan.changes if c.removes_existing)
        return await confirm(ctx, f"Replace {removed} existing note(s) in {clips}?", state)

    for tool_name, model, title, description in COMPOSE_TOOLS:

        async def compose(args: dict[str, Any], ctx: Context | None, _model: type[BaseModel] = model) -> Any:
            args = dict(args)
            preview = args.pop("preview", False)
            if args.get("seed") is None:
                # Keep the idea stable across a confirmation round trip.
                args["seed"] = request_state(ctx).get("seed", new_seed())
            spec = _model.model_validate(args)
            if isinstance(spec, ComposeRequest):
                spec = spec.spec()
            plan = await build_plan(bridge.call, [spec])
            if preview:
                plans.put(plan)
                return _with_roll(plan, {**plan.preview(), "next": "apply_plan(plan_id) writes this as one undo step"}, False)
            asked = await ask_before_replacing(plan, ctx, {"seed": args["seed"]})
            if asked is not None:
                return asked
            applied = await apply_plan(bridge.call, plan, verify=False)
            result = plan.preview()
            result.pop("plan_id")
            return _with_roll(plan, {**result, "applied": applied["applied"]}, True)

        server.add_tool(
            _make_tool(model, compose, skip=() if model is ComposeRequest else ("kind",), extra={"preview": (Annotated[bool, PREVIEW_FIELD], False)}),
            name=tool_name,
            title=title,
            description=description,
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
            meta=UI_META,
        )

    async def plan_changes(args: dict[str, Any], ctx: Context | None) -> Any:
        request = PlanChanges.model_validate(args)
        plan = await build_plan(bridge.call, request.specs())
        plans.put(plan)
        return _with_roll(plan, {**plan.preview(), "next": "apply_plan(plan_id) writes all steps as one undo step"}, False)

    async def plan_song(args: dict[str, Any], ctx: Context | None) -> Any:
        song = await build_song(bridge.call, SongRequest.model_validate(args))
        plans.put(song)  # type: ignore[arg-type]  # apply_plan tells songs and clip plans apart
        return song.preview()

    async def run_plan(args: dict[str, Any], ctx: Context | None) -> Any:
        plan = plans.get(args["plan_id"])
        if plan is None and plans.was_applied(args["plan_id"]):
            return {"ok": True, "already_applied": True,
                    "message": f"Plan {args['plan_id']} is already in Live (applied from its preview); nothing more to do."}
        if plan is None:
            plans.pop(args["plan_id"])  # raises NOT_FOUND
        assert plan is not None
        if isinstance(plan, SongPlan):
            plans.pop(plan.id)
            result = await apply_song(bridge.call, plan)
            plans.applied(plan.id)
            return result
        asked = await ask_before_replacing(plan, ctx)
        if asked is not None:
            return asked
        plans.pop(plan.id)
        try:
            result = await apply_plan(bridge.call, plan)
        except BridgeError as exc:
            if exc.code == "CONFLICT":
                plans.put(plan)
            raise
        plans.applied(plan.id)
        return result

    server.add_tool(
        _make_tool(PlanChanges, plan_changes),
        name="plan_changes",
        title="Plan composition changes",
        description="Plan several composition steps (drums, bass, chords, melody, transform) without changing Live. "
        "Later steps see earlier ones: bass can follow_kick a planned drum clip, and harmonic steps without chords "
        "share the first step's chords. Returns a diff preview and a plan_id.",
        annotations=read_only,
        meta=UI_META,
    )
    async def compose_takes(args: dict[str, Any], ctx: Context | None) -> Any:
        actions, summary = await build_takes(bridge.call, TakesRequest.model_validate(args))
        await bridge.call("arrangement.edit", {"actions": actions})
        return summary

    server.add_tool(
        _make_tool(TakesRequest, compose_takes),
        name="compose_takes",
        title="Compose takes to comp",
        description="Several variations of one part (hook, melody, bass, chords, drums) over the same chords, each on its "
        "own take lane of a track in the Arrangement View (Live 12.2+), to comp the best one in Live. One undo step.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
    )
    server.add_tool(
        _make_tool(SongRequest, plan_song),
        name="plan_song",
        title="Plan a whole song in the Arrangement View",
        description="Full song from a genre template (house, melodic_techno, peak_time_techno, drum_and_bass, trap): intro, builds, drops, breaks, outro "
        "with locators, on the drum/bass/chords/lead tracks (found by Drum Rack or name; missing ones are created). "
        "Length radio/standard/extended or minutes; starts after the last arrangement clip unless start_bar. "
        "Returns a preview and a plan_id for apply_plan; nothing changes before that.",
        annotations=read_only,
        meta=UI_META,
    )
    server.add_tool(
        _make_tool(ApplyPlan, run_plan),
        name="apply_plan",
        title="Apply planned changes",
        description="Write a plan from plan_changes (or preview=true) to Live as one undo step. "
        "Fails with CONFLICT if a target clip changed since planning.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
    )
    return server


class ReferenceQuery(BaseModel):
    """Without name: every analyzed reference, newest first. With name: the best match, with lanes
    (per bar: kick 0-1; low, bass, mid, high, energy in dB below the song's peak). A section the device
    merged from neighbours of the same kind lists its phrases (where the sound changes within it)."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(None, description="Part of the song, file or track name")


def read_references(name: str | None = None) -> dict[str, Any]:
    """The Reference device's analyses (<MidiBakery folder>/references/*.json)."""
    if name:
        found = find_reference(name)
        if found is None:
            raise ToolError(f"No analyzed reference matches '{name}'")
        return found
    found = load_references()
    brief = ("name", "track", "tempo", "warped", "key", "first_bar", "bars")

    def section(s: dict[str, Any]) -> dict[str, Any]:
        out = {k: s.get(k) for k in ("name", "start_bar", "bars", "kick", "elements")}
        if s.get("phrases"):  # a merged section: where its phrases start
            out["phrases"] = [p.get("start_bar") for p in s["phrases"]]
        return out

    return {"references": [{**{k: r.get(k) for k in brief}, "sections": [section(s) for s in r.get("sections", [])]}
                           for r in found]}


def _item_op(item: Any) -> str:
    return item.op if hasattr(item, "op") else item.get("op", "")


def _item_params(item: Any) -> dict[str, Any]:
    return item.params if hasattr(item, "params") else item.get("params", {})
