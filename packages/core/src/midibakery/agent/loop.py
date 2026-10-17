"""The tool loop shared by every frontend (terminal chat, Live chat device, API)."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any

from midibakery.agent.providers.base import Emit, Provider, ToolCall, ToolResult
from midibakery.agent.tools import COMPACT_RESULTS, ToolHost, ToolOutcome, compact_tools
from midibakery.mcp.server import INSTRUCTIONS

SYSTEM_PROMPT = (
    "You are MidiBakery, a music production assistant inside Ableton Live 12. "
    "You compose and edit MIDI and control the Live session through tools. "
    "Always answer in the language of the user's latest message and keep replies short: say what you changed or propose, "
    "with names of tracks, clips, chords and seeds where useful.\n\n"
    "Work like a producer: read the session before writing, keep parts in the song's scale and "
    "time signature, and make parts fit together (bass follows the kick and the chords, melody "
    "follows the chords). For more than one part, or when the user wants to hear options first, "
    "use plan_changes and let them apply the preview; otherwise write directly with the compose "
    "tools. If a tool fails, read the error and fix the call instead of repeating it.\n\n"
    "Tool guide:\n" + INSTRUCTIONS
)

# For local models with a small context (e.g. 8k): the essentials of SYSTEM_PROMPT and INSTRUCTIONS.
SMALL_SYSTEM_PROMPT = (
    "You are MidiBakery, a music assistant controlling the open Ableton Live 12 set through tools. "
    "Always answer in the language of the user's latest message, briefly.\n"
    "- Call get_session first. Tracks and clip slots are zero-based indices; "
    "users count from 1, so \"track 1\" is index 0.\n"
    "- Times are in beats (quarter notes); pitch 60 = C3. Stay in the song scale.\n"
    "- compose (kind drums/bass/chords/melody) writes a new clip into a free slot (omit slot for the first free one). "
    "Give bass and melody chords_from = the chord clip (never a drums track), the bass follow_kick = the drum clip. "
    "Never overwrite a clip unless the user asks for it.\n"
    "- A song in the Arrangement: get_arrangement lists each track's clips (arrangement_clip index, start beat, name). "
    "To change one part of it, give compose or transform_notes arrangement_clip (its index or its name, "
    "e.g. 'Groove Bass') instead of slot.\n"
    "- edit_notes and transform_notes change existing notes by note_id from get_clip. "
    "Transpose by half steps/semitones/octaves with semitones (12 = octave); steps means scale steps.\n"
    "- To play a clip, launch it with launch_clip; transport only starts or stops the whole song.\n"
    "- Every write can be undone with undo. If a tool fails, read the error, fix the call and retry it yourself. "
    "If the user declined a change (CANCELLED), nothing was written: say so, never claim it was done."
)

log = logging.getLogger("midibakery.agent")

MAX_STEPS = 24
# Small local models drift into another language over a long tool chain: each message reminds them.
LANGUAGE_REMINDER = "\n\n(Reply in the language of this message.)"
# A model that keeps failing gets stopped early instead of burning all MAX_STEPS rounds:
SAME_FAILURE_LIMIT = 3  # the same call (tool and arguments) failed this often
FAILED_ROUNDS_LIMIT = 6  # this many rounds in a row in which every call failed
# Tools the chat page runs itself (Apply on a plan preview, the Undo button).
UI_TOOLS = frozenset({"apply_plan", "undo"})


class Agent:
    """One conversation: a provider (the model) plus a tool host (Live via MCP)."""

    def __init__(self, provider: Provider, host: ToolHost, system: str = SYSTEM_PROMPT, max_steps: int = MAX_STEPS, compact: bool = False):
        """`compact` sends short descriptions and lean schemas (see compact_tools) and shorter
        results (COMPACT_RESULTS) for small local models."""
        self.provider = provider
        self.host = host
        self.max_steps = max_steps
        self.compact = compact
        provider.configure(system, compact_tools(host.tools) if compact else host.tools)
        self._lock = asyncio.Lock()
        self._notes: list[str] = []  # what the user did through the UI, told to the model with the next message
        self._ui_ids = itertools.count(1)

    def reset(self) -> None:
        self.provider.reset()
        self._notes.clear()

    async def run_tool(self, name: str, arguments: dict[str, Any], emit: Emit) -> None:
        """A tool the user started from the UI (Apply, Undo): the same events as a model's call,
        and the model hears about it with the next message so it does not apply or undo twice."""
        async with self._lock:
            if name not in UI_TOOLS:
                await emit({"type": "error", "message": f"{name} cannot be run from the chat"})
                return
            outcome = await self._call(ToolCall(f"ui{next(self._ui_ids)}", name, arguments), emit, source="user")
            status = "failed: " + outcome.text[:200] if outcome.is_error else "done"
            self._notes.append(f"[The user ran {name} {json.dumps(arguments)} from the chat buttons: {status}]")
            await emit({"type": "done", "stop_reason": "user_tool", "usage": {}})

    async def _call(self, call: ToolCall, emit: Emit, source: str = "model") -> ToolOutcome:
        await emit({"type": "tool_call", "id": call.id, "name": call.name, "input": call.input, "source": source})
        outcome = await self.host.call(call.name, call.input)
        log.info("%s tool %s %s -> %s", source, call.name, describe_input(call.input, 400),
                 "error: " + _first_line(outcome.text) if outcome.is_error else "ok")
        await emit({
            "type": "tool_result",
            "id": call.id,
            "name": call.name,
            "source": source,
            "is_error": outcome.is_error,
            "result": outcome.structured if outcome.structured is not None else outcome.text,
        })
        if outcome.roll:
            await emit({"type": "plan", "id": call.id, "name": call.name, "roll": outcome.roll})
        return outcome

    async def send(self, text: str, emit: Emit) -> None:
        """Handle one user message until the model stops calling tools. Events go to `emit`."""
        async with self._lock:
            log.info("user %s", " ".join(text.split())[:300])
            if self._notes:
                text = "\n".join(self._notes) + "\n\n" + text
                self._notes.clear()
            self.provider.add_user(text + LANGUAGE_REMINDER if self.compact else text)
            failures: dict[str, int] = {}
            failed_rounds = 0
            for _ in range(self.max_steps):
                try:
                    turn = await self.provider.stream_turn(emit)
                except Exception as exc:  # network, auth, rate limits: report, keep the session
                    await emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
                    return
                if turn.error:
                    await emit({"type": "error", "message": turn.error, "stop_reason": turn.stop_reason})
                    return
                if not turn.tool_calls:
                    await emit({"type": "done", "stop_reason": turn.stop_reason, "usage": turn.usage})
                    return
                results = []
                stuck = None
                # Sequential on purpose: Live ops depend on each other's order (create, then fill).
                for call in turn.tool_calls:
                    outcome = await self._call(call, emit)
                    text = outcome.text
                    if self.compact and not outcome.is_error and outcome.structured and call.name in COMPACT_RESULTS:
                        text = COMPACT_RESULTS[call.name](outcome.structured)
                    results.append(ToolResult(call.id, call.name, text, outcome.is_error))
                    if outcome.is_error:
                        key = call.name + json.dumps(call.input, sort_keys=True, default=str)
                        failures[key] = failures.get(key, 0) + 1
                        if failures[key] >= SAME_FAILURE_LIMIT:
                            stuck = f"{call.name} failed {failures[key]} times with the same arguments"
                self.provider.add_tool_results(results)
                failed_rounds = failed_rounds + 1 if all(r.is_error for r in results) else 0
                if stuck is None and failed_rounds >= FAILED_ROUNDS_LIMIT:
                    stuck = f"every tool call failed {failed_rounds} rounds in a row"
                if stuck is not None:
                    last = next(r.content for r in reversed(results) if r.is_error)
                    await emit({"type": "error", "code": "STUCK", "message": (
                        f"Stopped: {stuck}. Last error: {_first_line(last)} "
                        "Nothing more was changed. Rephrase the request (name the track and section), or try a larger model."
                    )})
                    return
            await emit({"type": "error", "message": f"Stopped after {self.max_steps} tool rounds without an answer."})


def _first_line(text: str, limit: int = 300) -> str:
    line = " ".join(text.strip().split("\n", 1)[0].split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def describe_input(value: Any, limit: int = 160) -> str:
    """Short one-line rendering of tool input for terminal output."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= limit else text[: limit - 1] + "…"
