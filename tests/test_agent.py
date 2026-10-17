"""Agent loop, tool host and providers, without calling a real model."""

import json

import httpx2 as httpx
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from midibakery.agent import REDUCED_TOOLS, SMALL_SYSTEM_PROMPT, SMALL_TOOLS, SYSTEM_PROMPT, Agent, ToolHost
from midibakery.agent.providers.anthropic import FALLBACK_BETA, AnthropicProvider
from midibakery.agent.providers.base import Provider, ToolCall, Turn
from midibakery.agent.providers import make_provider
from midibakery.agent.providers.base import ToolResult
from midibakery.agent.providers.ollama import OllamaProvider, inline_refs
from midibakery.agent.providers.openai_compat import OpenAICompatProvider
from midibakery.agent.tools import ToolSpec, compact_schema, compact_session
from midibakery.mcp import build_server


class ScriptedProvider(Provider):
    """Plays back prepared turns and records what the loop sends."""

    name = "scripted"
    model = "script"

    def __init__(self, turns):
        self.turns = list(turns)
        self.users = []
        self.results = []
        self.system = None
        self.tools = []

    def configure(self, system, tools):
        self.system, self.tools = system, tools

    def add_user(self, text):
        self.users.append(text)

    def reset(self):
        self.users = []

    async def stream_turn(self, emit):
        turn = self.turns.pop(0)
        if turn.text:
            await emit({"type": "text", "text": turn.text})
        return turn

    def add_tool_results(self, results):
        self.results.append(results)


def tool_turn(*calls):
    return Turn("tool_use", [ToolCall(f"t{i}", name, args) for i, (name, args) in enumerate(calls)])


async def run(server, provider, text, confirm=None, allowed=None):
    events = []

    async def emit(event):
        events.append(event)

    async def default_confirm(message):
        events.append({"type": "confirm", "message": message})
        return True

    async with ToolHost(server, confirm or default_confirm, allowed) as host:
        await Agent(provider, host).send(text, emit)
    return events


# --------------------------------------------------------------------------
# Loop + tool host against the fake LOM
# --------------------------------------------------------------------------


async def test_loop_runs_tools_and_streams_events(client, live):
    provider = ScriptedProvider([
        tool_turn(("get_session", {})),
        tool_turn(("compose", {"kind": "drums", "track": 0, "slot": 0, "seed": 3, "bars": 2})),
        Turn("end_turn", text="Done: 2 bars of house drums on track 1."),
    ])
    events = await run(build_server(client), provider, "Make drums")
    kinds = [e["type"] for e in events]
    assert kinds == ["tool_call", "tool_result", "tool_call", "tool_result", "plan", "text", "done"]
    assert provider.users == ["Make drums"]
    assert "MidiBakery" in provider.system and "get_session" in {t.name for t in provider.tools}
    session_result = provider.results[0][0]
    assert not session_result.is_error and json.loads(session_result.content)["song"]["tempo"] == 120
    assert events[4]["roll"]["applied"] is True
    assert live.on_main(lambda: len(live.song.tracks[0].clip_slots[0].clip.notes)) > 0


async def test_tool_errors_go_back_to_the_model(client, live):
    provider = ScriptedProvider([
        tool_turn(("no_such_tool", {}), ("delete_track", "not an object"), ("delete_track", {"track": 99})),
        Turn("end_turn", text="Sorry."),
    ])
    await run(build_server(client), provider, "break things")
    errors = provider.results[0]
    assert [r.is_error for r in errors] == [True, True, True]
    assert "Unknown tool" in errors[0].content and "JSON object" in errors[1].content and "NOT_FOUND" in errors[2].content


async def test_unknown_arguments_are_named_not_dropped(client, live):
    await client.call("clip.create", {"track": 1, "slot": 0, "length": 4})
    await client.call("notes.apply", {"track": 1, "slot": 0, "add": [{"pitch": 60, "start_time": 0.0, "duration": 1}]})
    provider = ScriptedProvider([
        tool_turn(("transform_notes", {"track": 1, "slot": 0, "operation": "transpose", "sembitimes": 2})),
        tool_turn(("transform_notes", {"track": 1, "slot": 0, "operation": "transpose"})),
        Turn("end_turn", text="Sorry."),
    ])
    await run(build_server(client), provider, "2 semitones up")
    typo, missing = provider.results[0][0], provider.results[1][0]
    assert typo.is_error and "'sembitimes' (did you mean 'semitones'?)" in typo.content
    assert missing.is_error and "transpose needs either semitones" in missing.content and "'input'" not in missing.content
    assert live.on_main(lambda: [n["pitch"] for n in live.song.tracks[1].clip_slots[0].clip.notes.values()]) == [60]


async def test_confirmation_is_asked_through_the_frontend(client, live):
    asked = []

    async def decline(message):
        asked.append(message)
        return False

    provider = ScriptedProvider([tool_turn(("delete_track", {"track": 0})), Turn("end_turn", text="Kept it.")])
    await run(build_server(client), provider, "delete track 1", confirm=decline)
    assert asked == ["Delete track 1 '1-MIDI' with 0 clip(s)?"]
    assert provider.results[0][0].is_error and "CANCELLED" in provider.results[0][0].content
    assert live.on_main(lambda: len(live.song.tracks)) == 2


async def test_reduced_tool_set_and_step_limit(client, live):
    provider = ScriptedProvider([tool_turn(("get_session", {}))] * 30)
    events = []

    async def emit(event):
        events.append(event)

    async def yes(message):
        return True

    async with ToolHost(build_server(client), yes, REDUCED_TOOLS) as host:
        assert host.names() == set(REDUCED_TOOLS)
        await Agent(provider, host, max_steps=3).send("loop", emit)
    assert events[-1]["type"] == "error" and "3 tool rounds" in events[-1]["message"]


# --------------------------------------------------------------------------
# Anthropic provider with a stub SDK client
# --------------------------------------------------------------------------


class _Delta:
    def __init__(self, kind, text):
        self.type = kind
        if kind == "text_delta":
            self.text = text
        else:
            self.thinking = text


class _Event:
    def __init__(self, kind, text):
        self.type = "content_block_delta"
        self.delta = _Delta(kind, text)


class _Stream:
    def __init__(self, events, message):
        self._events, self._message = events, message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e
        return gen()

    async def get_final_message(self):
        return self._message


class StubAnthropic:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        outer = self

        class _Messages:
            def stream(self, **params):
                outer.requests.append(params)
                reply = outer.replies.pop(0)
                if isinstance(reply, Exception):
                    raise reply
                return _Stream(*reply)

        class _Beta:
            messages = _Messages()

        self.beta = _Beta()
        self.messages = _Messages()


def _message(content, stop):
    return Message.model_construct(
        id="msg_1", type="message", role="assistant", model="claude-sonnet-5-5", content=content,
        stop_reason=stop, stop_sequence=None,
        usage=Usage.model_construct(input_tokens=10, output_tokens=5, cache_read_input_tokens=7, cache_creation_input_tokens=0),
    )


TOOLS = [ToolSpec("b_tool", "B", {"type": "object", "properties": {}}), ToolSpec("a_tool", "A", {"type": "object", "properties": {}})]


async def test_anthropic_request_shape_streaming_and_history():
    tool_use = ToolUseBlock.model_construct(type="tool_use", id="tu_1", name="a_tool", input={"x": 1})
    stub = StubAnthropic([
        ([_Event("thinking_delta", "hmm"), _Event("text_delta", "Let me look.")],
         _message([TextBlock.model_construct(type="text", text="Let me look."), tool_use], "tool_use")),
    ])
    provider = AnthropicProvider(client=stub)
    provider.configure("SYSTEM", TOOLS)
    provider.add_user("hi")
    events = []

    async def emit(e):
        events.append(e)

    turn = await provider.stream_turn(emit)
    assert events == [{"type": "thinking", "text": "hmm"}, {"type": "text", "text": "Let me look."}]
    assert [(c.id, c.name, c.input) for c in turn.tool_calls] == [("tu_1", "a_tool", {"x": 1})]
    assert turn.usage["cache_read_input_tokens"] == 7

    request = stub.requests[0]
    assert request["model"] == "claude-sonnet-5-5"
    assert request["cache_control"] == {"type": "ephemeral"}
    assert request["thinking"]["type"] == "adaptive" and request["output_config"] == {"effort": "medium"}
    assert request["betas"] == [FALLBACK_BETA] and request["fallbacks"] == "default"
    assert request["system"] == [{"type": "text", "text": "SYSTEM"}]
    assert all(t["eager_input_streaming"] for t in request["tools"])
    assert provider.messages[-1]["role"] == "assistant" and provider.messages[-1]["content"][1] is tool_use

    from midibakery.agent.providers.base import ToolResult

    provider.add_tool_results([ToolResult("tu_1", "a_tool", "ok")])
    assert provider.messages[-1] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "ok", "is_error": False}]}


async def test_anthropic_truncated_tool_call_and_refusal_are_not_run():
    tool_use = ToolUseBlock.model_construct(type="tool_use", id="tu_1", name="a_tool", input={})
    stub = StubAnthropic([
        ([], _message([tool_use], "max_tokens")),
        ([], _message([], "refusal")),
    ])
    provider = AnthropicProvider(client=stub, fallbacks=False)
    provider.configure("S", TOOLS)
    provider.add_user("x")

    async def emit(e):
        pass

    cut = await provider.stream_turn(emit)
    assert cut.error and not cut.tool_calls
    assert provider.messages[-1]["role"] == "user"  # the truncated turn is not kept
    refused = await provider.stream_turn(emit)
    assert refused.error and refused.stop_reason == "refusal"
    assert "betas" not in stub.requests[0]


async def test_anthropic_retries_unparseable_tool_json():
    stub = StubAnthropic([ValueError("bad json"), ([], _message([TextBlock.model_construct(type="text", text="ok")], "end_turn"))])
    provider = AnthropicProvider(client=stub)
    provider.configure("S", TOOLS)
    provider.add_user("x")

    async def emit(e):
        pass

    turn = await provider.stream_turn(emit)
    assert turn.text == "ok" and len(stub.requests) == 2


# --------------------------------------------------------------------------
# Ollama provider against a mock transport
# --------------------------------------------------------------------------


def test_inline_refs():
    schema = {
        "type": "object",
        "properties": {"loc": {"$ref": "#/$defs/Loc", "description": "where"}, "items": {"type": "array", "items": {"$ref": "#/$defs/Loc"}}},
        "$defs": {"Loc": {"type": "object", "properties": {"track": {"type": "integer"}}}},
    }
    resolved = inline_refs(schema)
    assert "$defs" not in json.dumps(resolved) and "$ref" not in json.dumps(resolved)
    assert resolved["properties"]["loc"]["properties"]["track"] == {"type": "integer"}
    assert resolved["properties"]["loc"]["description"] == "where"


async def test_ollama_streams_text_and_tool_calls():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        lines = [
            {"message": {"role": "assistant", "content": "Okay"}, "done": False},
            {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "a_tool", "arguments": {"x": 2}}}]}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True, "prompt_eval_count": 12, "eval_count": 3},
        ]
        return httpx.Response(200, content="\n".join(json.dumps(l) for l in lines).encode())

    provider = OllamaProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), base_url="http://ollama.test")
    provider.configure("SYS", TOOLS)
    provider.add_user("hi")
    events = []

    async def emit(e):
        events.append(e)

    turn = await provider.stream_turn(emit)
    assert events == [{"type": "text", "text": "Okay"}]
    assert [(c.name, c.input) for c in turn.tool_calls] == [("a_tool", {"x": 2})]
    assert turn.usage == {"input_tokens": 12, "output_tokens": 3, "model": provider.model}
    body = seen[0]
    assert body["messages"][0] == {"role": "system", "content": "SYS"} and body["stream"] is True
    assert body["tools"][0]["function"]["name"] == "b_tool"

    from midibakery.agent.providers.base import ToolResult

    provider.add_tool_results([ToolResult(turn.tool_calls[0].id, "a_tool", "boom", is_error=True)])
    assert provider.messages[-2]["tool_calls"][0]["function"]["name"] == "a_tool"
    assert provider.messages[-1] == {"role": "tool", "content": "ERROR: boom", "tool_name": "a_tool"}


async def test_ollama_unreachable_is_a_turn_error():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    provider = OllamaProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), base_url="http://ollama.test")
    provider.configure("S", TOOLS)
    provider.add_user("x")

    async def emit(e):
        pass

    turn = await provider.stream_turn(emit)
    assert turn.error and "ollama serve" in turn.error


# --------------------------------------------------------------------------
# LM Studio (OpenAI-compatible) provider against a mock transport
# --------------------------------------------------------------------------


def sse(*chunks):
    return httpx.Response(200, content=("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode())


def lmstudio(handler):
    return OpenAICompatProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), base_url="http://lmstudio.test/v1")


async def test_lmstudio_streams_text_and_joins_tool_call_fragments():
    seen = []

    def handler(request):
        seen.append((str(request.url), json.loads(request.content)))
        delta = lambda **d: {"choices": [{"index": 0, "delta": d, "finish_reason": None}]}  # noqa: E731
        return sse(
            delta(role="assistant", content="Ok"),
            delta(reasoning_content="think"),
            delta(tool_calls=[{"index": 0, "id": "c1", "type": "function", "function": {"name": "a_tool", "arguments": ""}}]),
            delta(tool_calls=[{"index": 0, "function": {"arguments": '{"x":'}}]),
            delta(tool_calls=[{"index": 1, "id": "c2", "type": "function", "function": {"name": "b_tool", "arguments": "{}"}}]),
            delta(tool_calls=[{"index": 0, "function": {"arguments": " 2}"}}]),
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 9}},
        )

    provider = lmstudio(handler)
    provider.configure("SYS", TOOLS)
    provider.add_user("hi")
    events = []

    async def emit(e):
        events.append(e)

    turn = await provider.stream_turn(emit)
    assert events == [{"type": "text", "text": "Ok"}, {"type": "thinking", "text": "think"}]
    assert turn.stop_reason == "tool_use"
    assert [(c.id, c.name, c.input) for c in turn.tool_calls] == [("c1", "a_tool", {"x": 2}), ("c2", "b_tool", {})]
    assert turn.usage == {"input_tokens": 40, "output_tokens": 9, "model": provider.model}
    url, body = seen[0]
    assert url == "http://lmstudio.test/v1/chat/completions"
    assert body["stream"] is True and body["messages"][0] == {"role": "system", "content": "SYS"}
    assert body["tools"][0] == {"type": "function", "function": {"name": "b_tool", "description": "B", "parameters": {"type": "object", "properties": {}}}}

    provider.add_tool_results([ToolResult("c1", "a_tool", "boom", is_error=True), ToolResult("c2", "b_tool", "fine")])
    assert provider.messages[-3]["tool_calls"][0] == {"id": "c1", "type": "function", "function": {"name": "a_tool", "arguments": '{"x": 2}'}}
    assert provider.messages[-2:] == [
        {"role": "tool", "tool_call_id": "c1", "content": "ERROR: boom"},
        {"role": "tool", "tool_call_id": "c2", "content": "fine"},
    ]


async def test_lmstudio_plain_answer_and_http_error():
    replies = [
        sse({"choices": [{"index": 0, "delta": {"content": "Hallo"}, "finish_reason": "stop"}]}),
        httpx.Response(400, json={"error": "The number of tokens to keep from the initial prompt is greater than the context length."}),
    ]
    provider = lmstudio(lambda request: replies.pop(0))
    provider.configure("S", [])
    provider.add_user("x")

    async def emit(e):
        pass

    turn = await provider.stream_turn(emit)
    assert (turn.stop_reason, turn.text, turn.tool_calls) == ("end_turn", "Hallo", [])
    assert provider.messages[-1] == {"role": "assistant", "content": "Hallo"}
    turn = await provider.stream_turn(emit)
    assert turn.error and "400" in turn.error and "context length" in turn.error


async def test_lmstudio_unreachable_is_a_turn_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setenv("LMSTUDIO_HOST", "localhost:9999")
    provider = OpenAICompatProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert provider.base_url == "http://localhost:9999"
    provider.configure("S", TOOLS)
    provider.add_user("x")

    async def emit(e):
        pass

    turn = await provider.stream_turn(emit)
    assert turn.error and "localhost:9999" in turn.error and "lms server start" in turn.error


def test_make_provider_lmstudio():
    provider = make_provider("lmstudio", "google/gemma-4-e4b")
    assert (provider.name, provider.model) == ("lmstudio", "google/gemma-4-e4b")


def test_compact_schema():
    schema = {
        "type": "object",
        "title": "toolArguments",
        "properties": {
            "title": {"type": "string", "title": "Title", "default": None},
            "seed": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None, "description": "Same seed, same notes. Omit for a new idea."},
            "bars": {"type": "integer", "minimum": 1, "maximum": 64, "default": 4},
        },
    }
    assert compact_schema(schema) == {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "seed": {"type": "integer", "description": "Same seed, same notes."},
            "bars": {"type": "integer", "default": 4},
        },
    }


async def test_small_profile_fits_and_is_compact(client):
    provider = ScriptedProvider([Turn("end_turn", text="ok")])

    async def emit(e):
        pass

    async def no(message):
        return False

    async with ToolHost(build_server(client), no, SMALL_TOOLS) as host:
        Agent(provider, host, SMALL_SYSTEM_PROMPT, compact=True)
    assert "plan_changes" not in {t.name for t in provider.tools} and len(provider.tools) == len(SMALL_TOOLS)
    assert "launch_clip" in {t.name for t in provider.tools}  # transport alone cannot play a clip
    compact = json.dumps([t.input_schema for t in provider.tools])
    assert "$ref" not in compact and '"title"' not in compact and "if_exists" not in compact
    # Measured against Gemma 4 E4B in LM Studio: ~3.7k of 8192 tokens incl. descriptions. Keep it there.
    assert len(compact) + len(SMALL_SYSTEM_PROMPT) < 14000


SESSION = {
    "song": {"tempo": 120.0, "signature_numerator": 4, "signature_denominator": 4, "root_note": 9, "scale_name": "Minor",
             "scene_count": 8, "is_playing": False, "loop": True, "loop_start": 0.0, "loop_length": 16.0},
    "tracks": [
        {"index": 0, "id": 11, "name": "Drums", "kind": "midi", "color_index": 3, "mute": False, "solo": False, "arm": False,
         "volume": 0.85, "pan": 0.0, "devices": [{"index": 0, "name": "909 Core Kit", "class_name": "DrumGroupDevice"}],
         "clips": [{"slot": 0, "name": "Beat", "length": 8.0, "is_midi": True, "is_playing": False},
                   {"slot": 1, "name": "Fill", "length": 4.0, "is_midi": True, "is_playing": False}]},
        {"index": 1, "id": 12, "name": "Chord Stabs", "kind": "midi", "color_index": 5, "mute": False, "solo": False, "arm": False,
         "volume": 0.85, "pan": 0.0, "devices": [{"index": 0, "name": "Drift", "class_name": "Drift"}],
         "clips": [{"slot": 3, "name": "Stabs", "length": 16.0, "is_midi": True, "is_playing": True}]},
        {"index": 2, "id": 13, "name": "Vox", "kind": "audio", "color_index": 1, "mute": False, "solo": False, "arm": None,
         "volume": 0.85, "pan": 0.0, "devices": [], "clips": []},
    ],
}


def test_compact_session():
    text = compact_session(SESSION)
    assert text.splitlines() == [
        "Song: 120 BPM, 4/4, scale A Minor, 8 slots per track (0-7).",
        "Tracks (index 'name' [kind, devices]; free slots; clips as slot 'name' length):",
        "0 'Drums' [drums, 909 Core Kit]; free slots 2-7; clips 0 'Beat' 2 bars, 1 'Fill' 1 bars",
        "1 'Chord Stabs' [midi, Drift]; free slots 0-2,4-7; clips 3 'Stabs' 4 bars",
        "2 'Vox' [audio]; free slots 0-7; no clips",
    ]
    assert len(text) * 3 < len(json.dumps(SESSION))


async def test_small_profile_shortens_get_session(client, live):
    async def yes(message):
        return True

    async def emit(e):
        pass

    results = {}
    for compact in (False, True):
        provider = ScriptedProvider([tool_turn(("get_session", {})), Turn("end_turn", text="ok")])
        async with ToolHost(build_server(client), yes, SMALL_TOOLS) as host:
            await Agent(provider, host, SMALL_SYSTEM_PROMPT, compact=compact).send("what is in the set?", emit)
        results[compact] = provider.results[0][0].content
    assert json.loads(results[False])["song"]["tempo"] == 120  # full JSON for Claude
    assert results[True].startswith("Song: 120 BPM, 4/4") and "0 '1-MIDI' [midi]; free slots 0-7; no clips" in results[True]


async def test_full_tool_set_stays_within_the_local_budget(client):
    # Local models get every tool (compact). With a 32k context the prompt must leave room for the
    # conversation: ~33k characters for 30 tools was ~10k tokens with Gemma 4 E4B. Grow tools, not this.
    provider = ScriptedProvider([Turn("end_turn", text="ok")])

    async def no(message):
        return False

    async with ToolHost(build_server(client), no, None) as host:
        Agent(provider, host, SYSTEM_PROMPT, compact=True)
    payload = json.dumps([{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in provider.tools])
    assert {"get_mixer", "update_mixer", "create_return_track"} <= {t.name for t in provider.tools}
    assert len(payload) + len(SYSTEM_PROMPT) < 36000


async def test_a_model_repeating_a_failing_call_is_stopped(client, live):
    # A local model that keeps sending the same wrong call: stopped after 3, not after 24 rounds
    bad = ("compose", {"kind": "bass", "track": 1, "arrangement_clip": 7})
    provider = ScriptedProvider([tool_turn(bad)] * 30)
    events = await run(build_server(client), provider, "more bass variation in the groove")
    assert len(provider.results) == 3
    assert events[-1]["type"] == "error" and events[-1]["code"] == "STUCK"
    assert "compose failed 3 times with the same arguments" in events[-1]["message"]
    assert "No arrangement clip index 7" in events[-1]["message"]


async def test_a_model_failing_in_every_round_is_stopped(client, live):
    provider = ScriptedProvider([tool_turn(("compose", {"kind": "bass", "track": 1, "arrangement_clip": i})) for i in range(30)])
    events = await run(build_server(client), provider, "vary the bass")
    assert len(provider.results) == 6 and events[-1]["code"] == "STUCK" and "6 rounds in a row" in events[-1]["message"]


def test_compact_session_shows_the_arrangement():
    from midibakery.mcp.server import with_arrangement

    arrangement = {"locators": [{"index": 0, "name": "Drop", "time": 0.0}, {"index": 1, "name": "Groove", "time": 32.0}],
                   "tracks": [{"index": 1, "name": "Chord Stabs", "clips": [
                       {"index": 0, "name": "Drop Bass", "start": 0.0, "end": 32.0, "is_midi": True},
                       {"index": 1, "name": "Groove Bass", "start": 32.0, "end": 128.0, "is_midi": True}]}]}
    session = with_arrangement(SESSION, arrangement)
    assert session["tracks"][1]["arrangement_clips"][1] == {"index": 1, "name": "Groove Bass", "start_bar": 9, "bars": 24}
    assert session["locators"] == [{"name": "Drop", "bar": 1}, {"name": "Groove", "bar": 9}]
    lines = compact_session(session).splitlines()
    assert lines[2] == "Arrangement sections (locators): Drop bar 1, Groove bar 9."
    assert lines[4].endswith("; arrangement clips 0 'Drop Bass' bars 1-8, 1 'Groove Bass' bars 9-32")
    assert "arrangement_clip=<index or name>" in lines[-1]
    assert compact_session(SESSION).splitlines()[2].startswith("0 'Drums'")  # no arrangement: as before
