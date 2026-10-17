"""The agent's tools are the MCP server's tools, called in-process.

Using the same MCPServer as Claude Desktop/Code keeps one tool surface: the
same schemas, validation, plan previews and confirmations. Confirmations
(input_required / elicitation) are routed to the frontend through `confirm`.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mcp import Client
from mcp.server import MCPServer
from mcp_types import ElicitResult

from midibakery.compose.plan import is_drum_track

Confirm = Callable[[str], Awaitable[bool]]

# For small local models: few, coarse tools work better than the full set.
REDUCED_TOOLS = frozenset({
    "get_session",
    "get_clip",
    "get_arrangement",
    "create_track",
    "create_clip",
    "edit_notes",
    "compose",
    "transform_notes",
    "plan_changes",
    "apply_plan",
    "transport",
    "launch_clip",
    "undo",
})

# For 8k-context models: plan_changes alone is ~3k tokens of schema.
SMALL_TOOLS = REDUCED_TOOLS - {"plan_changes"}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool = False


@dataclass
class ToolOutcome:
    """What a tool call produced: text for the model, structure and piano roll for the UI."""

    text: str
    is_error: bool = False
    structured: dict[str, Any] | None = None
    roll: dict[str, Any] | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class ToolHost:
    """An in-process MCP client session on the MidiBakery server."""

    def __init__(self, server: MCPServer, confirm: Confirm, allowed: frozenset[str] | None = None):
        self._server = server
        self._confirm = confirm
        self._allowed = allowed
        self._client: Client | None = None
        self.tools: list[ToolSpec] = []

    async def __aenter__(self) -> ToolHost:
        self._client = Client(self._server, elicitation_callback=self._elicit)
        await self._client.__aenter__()
        listed = (await self._client.list_tools()).tools
        self.tools = sorted(
            (
                ToolSpec(
                    t.name,
                    t.description or t.title or t.name,
                    t.input_schema,
                    bool(t.annotations and t.annotations.read_only_hint),
                )
                for t in listed
                if self._allowed is None or t.name in self._allowed
            ),
            key=lambda t: t.name,  # deterministic order keeps the prompt cache warm
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc)
            self._client = None

    def names(self) -> set[str]:
        return {t.name for t in self.tools}

    def _spec(self, name: str) -> ToolSpec:
        return next(t for t in self.tools if t.name == name)

    async def call(self, name: str, arguments: Any) -> ToolOutcome:
        if name not in self.names():
            return ToolOutcome(f"Unknown tool '{name}'. Available: {', '.join(sorted(self.names()))}", is_error=True)
        if not isinstance(arguments, dict):
            return ToolOutcome(f"Arguments for '{name}' must be a JSON object, got {json.dumps(arguments)}", is_error=True)
        unknown = _unknown_arguments(self._spec(name).input_schema, arguments)
        if unknown:
            return ToolOutcome(unknown, is_error=True)
        assert self._client is not None, "use ToolHost as an async context manager"
        result = await self._client.call_tool(name, arguments)
        text = "\n".join(c.text for c in result.content if getattr(c, "type", None) == "text")
        meta = dict(result.meta or {})
        return ToolOutcome(
            text=text or json.dumps(result.structured_content or {}),
            is_error=bool(result.is_error),
            structured=result.structured_content,
            roll=meta.get("midibakery/roll"),
            meta=meta,
        )

    async def _elicit(self, context: Any, params: Any) -> ElicitResult:
        accepted = await self._confirm(params.message)
        if accepted:
            return ElicitResult(action="accept", content={"confirm": True})
        return ElicitResult(action="decline")


def _unknown_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    """MCP drops unknown arguments silently; a misspelled 'semitones' would then
    run as a no-op that the model reports as done. Name them, with a guess."""
    known = schema.get("properties")
    if not isinstance(known, dict):
        return None
    unknown = [k for k in arguments if k not in known]
    if not unknown:
        return None
    hints = []
    for key in unknown:
        close = difflib.get_close_matches(key, known, n=1)
        hints.append(f"'{key}' (did you mean '{close[0]}'?)" if close else f"'{key}'")
    return f"INVALID_PARAMS: unknown parameter {', '.join(hints)}; nothing was done. Parameters: {', '.join(known)}"


# Schema keys that cost context but do not change what a valid call looks like.
# The discriminator mapping repeats what each variant's `const` already says.
_DROP_KEYS = frozenset({
    "title", "additionalProperties", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "examples",
    "discriminator", "minItems", "maxItems", "minLength", "maxLength",
})


def _first_sentence(text: str, limit: int) -> str:
    text = " ".join(text.split())
    for end in (". ", "; "):
        if end in text:
            text = text.split(end, 1)[0] + "."
            break
    return text if len(text) <= limit else text[: limit - 1] + "…"


def compact_schema(node: Any, limit: int = 90) -> Any:
    """Shrink an inlined JSON schema for small context windows.

    Drops titles, bounds and discriminator mappings, folds Optional (anyOf [X, null]) into X, removes
    null/empty defaults and cuts descriptions to their first sentence.
    """
    if isinstance(node, list):
        return [compact_schema(v, limit) for v in node]
    if not isinstance(node, dict):
        return node
    variants = node.get("anyOf")
    if isinstance(variants, list):
        non_null = [v for v in variants if v != {"type": "null"}]
        if len(non_null) == 1 and len(non_null) < len(variants):
            node = {**non_null[0], **{k: v for k, v in node.items() if k != "anyOf"}}
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROP_KEYS or (key == "default" and value in (None, [], {})):
            continue
        if key == "description" and isinstance(value, str):
            out[key] = _first_sentence(value, limit)
        elif key == "properties" and isinstance(value, dict):
            out[key] = {name: compact_schema(v, limit) for name, v in value.items()}  # property names are data, not schema keys
        else:
            out[key] = compact_schema(value, limit)
    return out


# Hidden from small models, which set them unasked (if_exists='replace' on occupied slots);
# the CONFLICT error names if_exists, so an explicit overwrite still works, with confirmation.
SMALL_HIDDEN_PARAMS = frozenset({"if_exists"})


def _hide(schema: dict[str, Any]) -> dict[str, Any]:
    props = schema.get("properties")
    if not isinstance(props, dict) or not SMALL_HIDDEN_PARAMS & props.keys():
        return schema
    return {**schema, "properties": {k: v for k, v in props.items() if k not in SMALL_HIDDEN_PARAMS}}


def compact_tools(tools: list[ToolSpec]) -> list[ToolSpec]:
    """Short descriptions and lean schemas, for local models with a small context."""
    from midibakery.agent.providers.ollama import inline_refs

    return [
        ToolSpec(t.name, _first_sentence(t.description, 160), _hide(compact_schema(inline_refs(t.input_schema))), t.read_only)
        for t in tools
    ]


_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def _num(value: Any) -> Any:
    return int(value) if isinstance(value, float) and value.is_integer() else value


def compact_session(result: dict[str, Any]) -> str:
    """get_session as a few lines of text for small context windows.

    Keeps what a small model needs to pick a track and an empty slot: tempo,
    signature, scale, and per track its index, name, kind, instrument and clips
    (slot, name, length in bars); drops ids, mixer state, colors and transport.
    """
    song = result.get("song", {})
    num, den = song.get("signature_numerator", 4), song.get("signature_denominator", 4)
    root = song.get("root_note")
    scale = f"{_NOTE_NAMES[root % 12]} {song.get('scale_name', '')}".strip() if isinstance(root, int) else "unknown"
    scenes = int(song.get("scene_count", 0))
    beats_per_bar = num * 4 / den if den else 4
    lines = [
        f"Song: {_num(round(song.get('tempo', 0), 2))} BPM, {num}/{den}, scale {scale}, {scenes} slots per track (0-{max(0, scenes - 1)}).",
        "Tracks (index 'name' [kind, devices]; free slots; clips as slot 'name' length):",
    ]
    for track in result.get("tracks", []):
        devices = [d.get("name", "?") for d in track.get("devices", [])]
        kind = "drums" if is_drum_track(track) else track.get("kind", "?")
        head = f"{track.get('index')} '{track.get('name')}' [{', '.join([kind, *devices])}]"
        clips = sorted(track.get("clips", []), key=lambda c: c["slot"])
        used = {c["slot"] for c in clips}
        parts = []
        for c in clips:
            bars = _num(round((c.get("length") or 0) / beats_per_bar, 2))
            audio = "" if c.get("is_midi", True) else " audio"
            parts.append(f"{c['slot']} '{c.get('name', '')}' {bars} bars{audio}")
        # Free slots first: small models otherwise pick an occupied slot from the clip list.
        free = [i for i in range(scenes) if i not in used]
        free_text = f"free slots {_ranges(free)}" if free else "no free slot"
        clip_text = f"clips {', '.join(parts)}" if parts else "no clips"
        line = f"{head}; {free_text}; {clip_text}"
        arranged = track.get("arrangement_clips") or []
        if arranged:
            line += "; arrangement clips " + ", ".join(
                f"{c['index']} '{c.get('name', '')}' bars {_num(c['start_bar'])}-{_num(round(c['start_bar'] + c['bars'] - 1, 2))}"
                for c in arranged)
        lines.append(line)
    if any(t.get("arrangement_clips") for t in result.get("tracks", [])):
        locators = ", ".join(f"{l['name']} bar {_num(l['bar'])}" for l in result.get("locators", []))
        if locators:
            lines.insert(2, f"Arrangement sections (locators): {locators}.")
        lines.append("A section of the song (e.g. the Groove's bass): its arrangement clip; change it with compose or "
                     "transform_notes and arrangement_clip=<index or name>, not a new slot clip.")
    return "\n".join(lines)


def _ranges(values: list[int]) -> str:
    """[0, 1, 2, 5] -> '0-2,5'."""
    runs: list[list[int]] = []
    for v in values:
        if runs and v == runs[-1][-1] + 1:
            runs[-1].append(v)
        else:
            runs.append([v])
    return ",".join(str(r[0]) if len(r) == 1 else f"{r[0]}-{r[-1]}" for r in runs)


# Small profile: tool results rewritten to save context (the UI still gets the structured result).
COMPACT_RESULTS: dict[str, Callable[[dict[str, Any]], str]] = {"get_session": compact_session}
