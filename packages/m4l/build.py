"""Builds the MidiBakery Max for Live devices (.amxd) from the sources in src/.

    uv run python packages/m4l/build.py  # -> packages/m4l/dist/, with install.json
    uv run midibakery install            # builds them and installs everything into Live

Devices:
- MidiBakery Chat (MIDI Effect): chat with the MidiBakery agent in Live's device
  view (jweb shows the API's /ui page). MIDI passes through unchanged.
- MidiBakery Generator / MidiBakery Transform (MIDI Tools, Live 12.2+): the
  composition engine in the clip view's Generate / Transform panels.
- MidiBakery Reference Analyzer (Audio Effect): analyzes the reference song on its track
  (sections, kick, energy per bar), shows the structure and sets locators.

Patchers are generated here instead of hand-saved in Max, so they stay
reviewable in git and every device shares one node helper (src/midibakery-node.js).
An .amxd file is a small chunk header followed by the patcher JSON.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import shutil
import struct
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
DIST = HERE / "dist"
NODE_SCRIPT = "midibakery-node.js"
# node.script entry files per device kind; each sets its mode and loads NODE_SCRIPT.
NODE_ENTRIES = ("midibakery-chat.js", "midibakery-miditool.js")
REFERENCE_FILES = ("midibakery-reference.js", "reference-analysis.js")  # the Reference device's node.script
ABOUT_SCRIPT = "midibakery-about.js"  # the About button's version line, for both node.scripts
NODE_FILES = (NODE_SCRIPT, *NODE_ENTRIES, *REFERENCE_FILES, ABOUT_SCRIPT)
# MidiBakery's one version (midibakery.__version__), read as text: building needs no installed package.
VERSION = re.search(r'^__version__ = "([^"]+)"', (HERE.parent / "core/src/midibakery/__init__.py").read_text(encoding="utf-8"), re.M)[1]

# Four-character device type codes, as found in Live's own device templates.
DEVICE_TYPES = {"midi_effect": b"mmmm", "audio_effect": b"aaaa", "generator": b"nagg", "transformation": b"natt"}
INSTALL_DIRS = {
    "audio_effect": Path("Presets/Audio Effects/Max Audio Effect/MidiBakery"),
    "midi_effect": Path("Presets/MIDI Effects/Max MIDI Effect/MidiBakery"),
    "generator": Path("MIDI Tools/Max Generators"),
    "transformation": Path("MIDI Tools/Max Transformations"),
}

# live.* parameter types and unit styles (Max parameter attributes)
# Max parameter types. INT is "Int (0-255)": anything wider must be FLOAT with an integer unit style.
FLOAT, INT, ENUM = 0, 1, 2
UNIT_INT, UNIT_PERCENT = 0, 5
# Text in Live's control colour, as Ableton's own devices colour their comments: readable in every Live theme,
# where a fixed grey disappears on a light device background.
THEMED_TEXT: dict[str, Any] = {"saved_attribute_attributes": {"textcolor": {"expression": "themecolor.live_control_fg"}}}


class Patcher:
    """Collects boxes and patch cords; box ids are assigned in creation order."""

    def __init__(self) -> None:
        self.boxes: list[dict[str, Any]] = []
        self.lines: list[dict[str, Any]] = []
        self.parameters: dict[str, Any] = {}
        self._ids = itertools.count(1)

    def box(self, maxclass: str, rect: tuple[float, float, float, float], *, inlets: int, outlets: list[str],
            text: str | None = None, presentation: tuple[float, float, float, float] | None = None, **attrs: Any) -> str:
        box_id = f"obj-{next(self._ids)}"
        box: dict[str, Any] = {
            "id": box_id,
            "maxclass": maxclass,
            "numinlets": inlets,
            "numoutlets": len(outlets),
            "patching_rect": list(rect),
        }
        if outlets:
            box["outlettype"] = outlets
        if text is not None:
            box["text"] = text
        if presentation is not None:
            box["presentation"] = 1
            box["presentation_rect"] = list(presentation)
        box.update(attrs)
        self.boxes.append({"box": box})
        return box_id

    def obj(self, text: str, x: float, y: float, *, inlets: int = 1, outlets: list[str] | None = None, width: float = 120) -> str:
        return self.box("newobj", (x, y, width, 22), inlets=inlets, outlets=outlets if outlets is not None else [""], text=text)

    def message(self, text: str, x: float, y: float) -> str:
        return self.box("message", (x, y, 60, 22), inlets=2, outlets=[""], text=text)

    def comment(self, text: str, rect, presentation=None, **attrs: Any) -> str:
        return self.box("comment", rect, inlets=1, outlets=[], text=text, presentation=presentation, **attrs)

    def parameter(self, maxclass: str, rect, presentation, *, name: str, short: str, ptype: int,
                  inlets: int, outlets: list[str], varname: str | None = None, **valueof: Any) -> str:
        box_id = self.box(
            maxclass, rect, inlets=inlets, outlets=outlets, presentation=presentation,
            parameter_enable=1, varname=varname or name,
            saved_attribute_attributes={"valueof": {
                "parameter_longname": name, "parameter_shortname": short, "parameter_type": ptype, **valueof,
            }},
        )
        self.parameters[box_id] = [name, short, 0]
        return box_id

    def connect(self, source: str, outlet: int, destination: str, inlet: int = 0) -> None:
        self.lines.append({"patchline": {"source": [source, outlet], "destination": [destination, inlet]}})

    def to_json(self, *, kind: str, title: str, width: float, height: float) -> dict[str, Any]:
        return {"patcher": {
            "fileversion": 1,
            "title": title,
            "appversion": {"major": 9, "minor": 1, "revision": 0, "architecture": "x64", "modernui": 1},
            "classnamespace": "box",
            "rect": [100.0, 100.0, 900.0, 600.0],
            "openrect": [0.0, 0.0, width, height],
            "bglocked": 0,
            "openinpresentation": 1,
            "default_fontsize": 10.0,
            "default_fontface": 0,
            "default_fontname": "Arial Bold",
            "gridonopen": 1,
            "gridsize": [8.0, 8.0],
            "gridsnaponopen": 1,
            "objectsnaponopen": 1,
            "statusbarvisible": 2,
            "toolbarvisible": 1,
            "lefttoolbarpinned": 0,
            "toptoolbarpinned": 0,
            "righttoolbarpinned": 0,
            "bottomtoolbarpinned": 0,
            "toolbars_unpinned_last_save": 0,
            "tallnewobj": 0,
            "boxanimatetime": 500,
            "enablehscroll": 1,
            "enablevscroll": 1,
            "devicewidth": width,
            "description": f"MidiBakery {VERSION} by Klirrwerk - AI composition for Ableton Live",
            "digest": "",
            "tags": "",
            "style": "",
            "subpatcher_template": "",
            "assistshowspatchername": 0,
            "platform_compatibility": 0,
            "saved_attribute_attributes": {"default_plcolor": {"expression": ""}},
            "boxes": self.boxes,
            "lines": self.lines,
            "parameters": {**self.parameters, "parameterbanks": {}, "inherited_shortname": 1},
            "dependency_cache": [],
            "latency": 0,
            "is_mpe": 0,
            "minimum_live_version": "12.2",
            "minimum_max_version": "9.1",
            # Max dereferences every one of these keys when loading a device; a missing
            # "searchpath" crashes Live 12.4 (MaxPlugLib project_deserialize_searchpath).
            # Mirrors the project block of Live's own Max device templates.
            "project": {
                "version": 1,
                "creationdate": 3590052493,
                "modificationdate": 3590052493,
                "viewrect": [25.0, 124.0, 300.0, 500.0],
                "autoorganize": 1,
                "hideprojectwindow": 1,
                "showdependencies": 1,
                "autolocalize": 0,
                "contents": {"patchers": {}},
                "layout": {},
                "searchpath": {},
                "detailsvisible": 0,
                "amxdtype": int.from_bytes(DEVICE_TYPES[kind], "big"),
                "readonly": 0,
                "devpathtype": 0,
                "devpath": ".",
                "sortmode": 0,
                "viewmode": 0,
                "includepackages": 0,
            },
            "autosave": 0,
        }}


def write_amxd(path: Path, kind: str, patcher: dict[str, Any]) -> None:
    payload = json.dumps(patcher, indent=1).encode("utf-8") + b"\n\x00"
    header = (
        b"ampf" + struct.pack("<I", 4) + DEVICE_TYPES[kind]
        + b"meta" + struct.pack("<I", 4) + b"\x00\x00\x00\x00"
        + b"ptch" + struct.pack("<I", len(payload))
    )
    path.write_bytes(header + payload)


def read_amxd(path: Path) -> tuple[bytes, dict[str, Any]]:
    """(device type, patcher dict); the inverse of write_amxd, used by the tests."""
    data = path.read_bytes()
    if data[:4] != b"ampf" or data[12:16] != b"meta" or data[24:28] != b"ptch":
        raise ValueError(f"{path} is not an uncompressed .amxd")
    size = struct.unpack("<I", data[28:32])[0]
    return data[8:12], json.loads(data[32:32 + size].rstrip(b"\x00"))


# ----------------------------------------------------------------------------
# Devices
# ----------------------------------------------------------------------------


def _on_press(p: Patcher, button: str, message: str, target: str, x: float, y: float, inlet: int = 0) -> None:
    """Send `message` when a live.text button is pressed; it may output bang or 1/0 depending on mode."""
    route = p.obj("route bang", x, y, outlets=["", ""], width=70)
    sel = p.obj("sel 1", x + 80, y + 30, inlets=2, outlets=["bang", ""], width=50)
    msg = p.message(message, x, y + 60)
    p.connect(button, 0, route)
    p.connect(route, 0, msg)
    p.connect(route, 1, sel)
    p.connect(sel, 0, msg)
    p.connect(msg, 0, target, inlet)


def _about(p: Patcher, presentation, node: str, x: float, y: float) -> None:
    """The About button: "about <version>" to the device's node.script, which answers with a status line
    comparing this device's version with the API's and the Remote Script's (midibakery-about.js).
    Not a Live parameter: nothing to automate or store."""
    button = p.box("live.text", (x, y, 50, 15), inlets=1, outlets=["", ""], presentation=presentation,
                   parameter_enable=0, mode=0, text="About", texton="About", varname="about")
    _on_press(p, button, f"about {VERSION}", node, x, y + 30)


def chat_device() -> dict[str, Any]:
    p = Patcher()
    # Layout: chat (jweb) on the left, a column with Pop out, the text field and the status on the right.
    # Nothing sits below jweb: in Live it can render taller than its rect.
    width, height = 700.0, 169.0
    column_x, column_w = 506.0, 190.0
    # MIDI passes through untouched: the device sits on a MIDI track.
    midiin = p.obj("midiin", 20, 20, outlets=["int"], width=50)
    midiout = p.obj("midiout", 20, 60, outlets=[], width=50)
    p.connect(midiin, 0, midiout)

    node = p.obj("node.script midibakery-chat.js @autostart 1 @watch 0", 120, 20, outlets=["", ""], width=300)
    route = p.obj("route url status", 120, 60, inlets=1, outlets=["", "", ""])
    # Same size in patching and presentation: jweb may render at its patching size.
    web_rect = (0, 0, 500, height)
    web = p.box("jweb", (120, 200, web_rect[2], web_rect[3]), inlets=1, outlets=[""], presentation=web_rect, url="")
    # Live does not pass keystrokes to jweb, so typing happens in a native Max text field.
    # Enter sends "chat_input <words>" to the page (window.max.bindInlet), then clears the field.
    entry = p.box("textedit", (700, 200, 190, 96), inlets=1, outlets=["", "int", "", ""],
                  presentation=(column_x, 26, column_w, 96), keymode=1, lines=0, wordwrap=1, fontsize=12.0,
                  bgcolor=[0.165, 0.165, 0.165, 1.0], textcolor=[0.85, 0.85, 0.85, 1.0],
                  bordercolor=[0.3, 0.3, 0.3, 1.0], varname="chat_entry")
    on_text = p.obj("route text", 700, 240, outlets=["", ""], width=70)
    order = p.obj("t b l", 700, 270, outlets=["bang", ""], width=50)
    as_symbol = p.obj("tosymbol", 760, 300, width=60)
    to_page = p.obj("prepend chat_input", 760, 330, width=120)
    clear = p.message("clear", 700, 330)
    # "select" makes the field the keyboard target again; deferred until Live has handled the Enter key.
    refocus_delay = p.obj("deferlow", 700, 360, width=60)
    refocus = p.message("select", 700, 390)
    p.connect(entry, 0, on_text)
    p.connect(on_text, 0, order)
    p.connect(order, 1, as_symbol)
    p.connect(as_symbol, 0, to_page)
    p.connect(to_page, 0, web)
    p.connect(order, 0, clear)
    p.connect(clear, 0, entry)
    p.connect(order, 0, refocus_delay)
    p.connect(refocus_delay, 0, refocus)
    p.connect(refocus, 0, entry)
    set_url = p.obj("prepend url", 120, 100)
    p.connect(node, 0, route)
    p.connect(route, 0, set_url)
    p.connect(set_url, 0, web)
    # The page reports "loaded" through window.max; node.script stops re-sending the URL.
    loaded = p.obj("route loaded", 120, 400, outlets=["", ""], width=90)
    loaded_msg = p.message("loaded", 120, 440)
    p.connect(web, 0, loaded)
    p.connect(loaded, 0, loaded_msg)
    p.connect(loaded_msg, 0, node)
    # Everything else jweb reports goes to the device log (wired through the Model menu router below).
    other = p.obj("prepend jweb", 220, 440, width=90)
    p.connect(other, 0, node)
    set_status = p.obj("prepend set", 260, 100)
    p.comment("Enter sends", (700, 300, 100, 14), presentation=(column_x, 124, 110, 14), fontsize=9.0,
              **THEMED_TEXT)
    _about(p, (column_x + 114, 124, column_w - 114, 14), node, 440, 160)
    status = p.comment("Starting...", (260, 140, 190, 30), presentation=(column_x, 138, column_w, 30),
                       linecount=2, fontsize=9.0, **THEMED_TEXT)
    p.connect(route, 1, set_status)
    p.connect(set_status, 0, status)

    # Model menu: a native umenu (dropdowns inside jweb do not open). The page fills it from GET /models
    # (menu_clear / menu_append <label> / menu_select <n>) and keeps the choice; picks go back as model_index.
    model_menu = p.box("umenu", (1000, 120, 110, 22), inlets=1, outlets=["int", "", ""],
                       presentation=(column_x, 4, 110, 18), items=["Default"], fontsize=10.0, varname="model_menu")
    menu_msgs = p.obj("route menu_clear menu_append menu_select", 1000, 20, outlets=["", "", "", ""], width=260)
    menu_clear = p.message("clear", 1000, 60)
    menu_append = p.obj("prepend append", 1080, 60, width=90)
    menu_select = p.obj("prepend set", 1180, 60, width=80)
    to_page_model = p.obj("prepend model_index", 1000, 160, width=130)
    p.connect(loaded, 1, menu_msgs)
    p.connect(menu_msgs, 3, other)
    p.connect(menu_msgs, 0, menu_clear)
    p.connect(menu_msgs, 1, menu_append)
    p.connect(menu_msgs, 2, menu_select)
    for box in (menu_clear, menu_append, menu_select):
        p.connect(box, 0, model_menu)
    p.connect(model_menu, 0, to_page_model)
    p.connect(to_page_model, 0, web)
    popout = p.parameter("live.text", (440, 60, 70, 16), (column_x + 114, 4, column_w - 114, 18), name="Pop out", short="Pop out", ptype=ENUM,
                         inlets=1, outlets=["", ""], parameter_enum=["val1", "val2"], parameter_mmax=1)
    p.boxes[-1]["box"].update({"mode": 0, "text": "Pop out", "texton": "Pop out"})
    _on_press(p, popout, "popout", node, 440, 100)
    return p.to_json(kind="midi_effect", title="MidiBakery Chat", width=width, height=height)


def _miditool_core(p: Patcher, mode: str) -> tuple[str, str]:
    """live.miditool.in -> dict.join -> v8.codebox -> live.miditool.out, plus node.script.
    Returns the v8 and node.script box ids."""
    code = f'var MODE = "{mode}";\n' + (SRC / "miditool.js").read_text(encoding="utf-8")
    tool_in = p.obj("live.miditool.in", 20, 20, outlets=["", ""], width=110)
    join = p.obj("dict.join", 20, 60, inlets=2, outlets=["dictionary"], width=70)
    v8 = p.box("v8.codebox", (20, 100, 420, 300), inlets=2, outlets=["", "", "", "", ""], code=code, filename="none",
               fontface=0, fontname="<Monospaced>", fontsize=10.0)
    tool_out = p.obj("live.miditool.out", 20, 420, outlets=[], width=110)
    node = p.obj("node.script midibakery-miditool.js @autostart 1 @watch 0", 460, 420, outlets=["", ""], width=300)
    rebang = p.obj("t b", 460, 20, outlets=["bang"], width=40)
    # Context (right outlet) fires first and is stored; notes (left) trigger the join.
    p.connect(tool_in, 1, join, 1)
    p.connect(tool_in, 0, join, 0)
    p.connect(join, 0, v8, 0)
    p.connect(v8, 0, tool_out)
    p.connect(v8, 1, node)
    p.connect(v8, 2, rebang)
    p.connect(rebang, 0, tool_in)
    p.connect(node, 0, v8, 1)
    return v8, node


def _status_and_feedback(p: Patcher, v8: str, presentation) -> dict[str, str]:
    status = p.comment("Connecting...", (460, 120, 250, 36), presentation=presentation, linecount=3,
                       fontsize=9.0, **THEMED_TEXT)
    p.connect(v8, 3, status)
    feedback = p.obj("route seed", 460, 180, outlets=["", ""], width=70)
    thispatcher = p.obj("thispatcher", 560, 220, outlets=["", ""], width=80)
    p.connect(v8, 4, feedback)
    p.connect(feedback, 1, thispatcher)
    return {"seed_feedback": feedback}


def _param_to_v8(p: Patcher, box: str, name: str, v8: str, x: float, y: float) -> None:
    prepend = p.obj(f"prepend param {name}", x, y, width=130)
    p.connect(box, 0, prepend)
    p.connect(prepend, 0, v8, 1)


def _seed_controls(p: Patcher, v8: str, feedback: str, numbox_rect, dice_rect, label_rect) -> None:
    seed = p.parameter("live.numbox", (760, 300, 50, 15), numbox_rect, name="Seed", short="Seed", ptype=FLOAT,
                       inlets=1, outlets=["", "float"], parameter_mmin=1, parameter_mmax=9999, parameter_initial=[1],
                       parameter_initial_enable=1, parameter_unitstyle=UNIT_INT)
    _param_to_v8(p, seed, "seed", v8, 760, 330)
    p.connect(feedback, 0, seed)
    dice = p.parameter("live.text", (840, 300, 50, 15), dice_rect, name="New seed", short="Dice", ptype=ENUM,
                       inlets=1, outlets=["", ""], parameter_enum=["val1", "val2"], parameter_mmax=1)
    p.boxes[-1]["box"].update({"mode": 0, "text": "Dice", "texton": "Dice"})
    _on_press(p, dice, "dice", v8, 840, 330, inlet=1)
    p.comment("Seed", (760, 280, 40, 18), presentation=label_rect, fontsize=9.0)


def _menu(p: Patcher, rect, presentation, name: str, short: str, items: list[str], initial: int, **attrs: Any) -> str:
    return p.parameter("live.menu", rect, presentation, name=name, short=short, ptype=ENUM, inlets=1,
                       outlets=["", "", "float"], parameter_enum=items, parameter_mmax=len(items) - 1,
                       parameter_initial=[initial], parameter_initial_enable=1, **attrs)


def _dial(p: Patcher, rect, presentation, name: str, short: str, initial: float) -> str:
    return p.parameter("live.dial", rect, presentation, name=name, short=short, ptype=FLOAT, inlets=1,
                       outlets=["", "float"], parameter_mmin=0.0, parameter_mmax=100.0, parameter_initial=[initial],
                       parameter_initial_enable=1, parameter_unitstyle=UNIT_PERCENT)


# Live's clip-view MIDI Tool panel (Live 12.4): the size of its own Max MIDI Tool templates.
TOOL_WIDTH, TOOL_HEIGHT = 152.0, 147.0
ABOUT_RECT = (102, 96, 48, 16)  # beside the Generator's Replace toggle
TOOL_STATUS_RECT = (2, 116, 148, 30)  # below Replace and About


def generator_device() -> dict[str, Any]:
    """Layout (152 x 147): Part | Style menus; Density, Swing, Chance (drums only) dials; Seed row; Replace | About; status."""
    p = Patcher()
    v8, node = _miditool_core(p, "generate")
    ui = _status_and_feedback(p, v8, TOOL_STATUS_RECT)
    _about(p, ABOUT_RECT, node, 1020, 300)

    kind = _menu(p, (760, 20, 80, 15), (2, 2, 72, 16), "Part", "Part", ["Drums", "Bass", "Chords", "Melody"], 0)
    _param_to_v8(p, kind, "kind", v8, 760, 44)
    styles = [
        ("drums", "Drum Style", ["House", "Techno", "Hip-Hop", "Trap", "Rock", "Breakbeat"], 0),
        ("bass", "Bass Style", ["Sustained", "Pulse", "Offbeat", "Octave", "Walking", "Syncopated"], 5),
        ("chords", "Chord Rhythm", ["Sustain", "Pulse", "Offbeat", "Stabs"], 0),
    ]
    for i, (key, name, items, initial) in enumerate(styles):
        menu = _menu(p, (760, 80 + i * 50, 110, 15), (78, 2, 72, 16), name, name.split()[-1], items, initial,
                     varname=f"style_{key}")
        p.boxes[-1]["box"]["hidden"] = 0 if key == "drums" else 1
        _param_to_v8(p, menu, key, v8, 880, 80 + i * 50)
    density = _dial(p, (760, 240, 44, 48), (2, 24, 48, 48), "Density", "Density", 50.0)
    _param_to_v8(p, density, "density", v8, 760, 300)
    swing = _dial(p, (820, 240, 44, 48), (52, 24, 48, 48), "Swing", "Swing", 0.0)
    _param_to_v8(p, swing, "swing", v8, 820, 360)
    chance = _dial(p, (880, 240, 44, 48), (102, 24, 48, 48), "Chance", "Chance", 100.0)
    p.boxes[-1]["box"]["varname"] = "chance_dial"
    _param_to_v8(p, chance, "chance", v8, 880, 360)
    _seed_controls(p, v8, ui["seed_feedback"], numbox_rect=(52, 76, 46, 16), dice_rect=(102, 76, 48, 16),
                   label_rect=(2, 77, 46, 14))
    # Replace: clear the time selection before the new notes go in (a Generator can only add).
    replace = p.parameter("live.text", (940, 300, 60, 15), (2, 96, 60, 16), name="Replace", short="Replace", ptype=ENUM,
                          inlets=1, outlets=["", ""], parameter_enum=["off", "on"], parameter_mmax=1,
                          parameter_initial=[1], parameter_initial_enable=1)
    p.boxes[-1]["box"].update({"mode": 1, "text": "Replace", "texton": "Replace"})
    _param_to_v8(p, replace, "replace", v8, 940, 330)
    return p.to_json(kind="generator", title="MidiBakery Generator", width=TOOL_WIDTH, height=TOOL_HEIGHT)


def transform_device() -> dict[str, Any]:
    """Layout (152 x 147): Operation menu, Amount dial with Semis and Arp rate beside it, Seed row, About, status."""
    p = Patcher()
    v8, node = _miditool_core(p, "transform")
    ui = _status_and_feedback(p, v8, TOOL_STATUS_RECT)
    _about(p, ABOUT_RECT, node, 1020, 300)

    operation = _menu(p, (760, 20, 110, 15), (2, 2, 148, 16), "Operation", "Operation",
                      ["Humanize", "Quantize", "Invert", "Arpeggiate", "Variation", "Transpose"], 0)
    _param_to_v8(p, operation, "operation", v8, 760, 44)
    amount = _dial(p, (760, 80, 44, 48), (2, 24, 48, 48), "Amount", "Amount", 50.0)
    _param_to_v8(p, amount, "amount", v8, 760, 140)
    p.comment("Semis", (820, 60, 40, 18), presentation=(52, 24, 46, 14), fontsize=9.0)
    semis = p.parameter("live.numbox", (820, 80, 44, 15), (52, 40, 46, 16), name="Semitones", short="Semis", ptype=FLOAT,
                        inlets=1, outlets=["", "float"], parameter_mmin=-48, parameter_mmax=48, parameter_initial=[12],
                        parameter_initial_enable=1, parameter_unitstyle=UNIT_INT)
    _param_to_v8(p, semis, "semitones", v8, 820, 110)
    p.comment("Arp rate", (900, 60, 50, 18), presentation=(102, 24, 48, 14), fontsize=9.0)
    rate = _menu(p, (900, 80, 60, 15), (102, 40, 48, 16), "Arp Rate", "Rate", ["1/32", "1/16", "1/8", "1/4"], 1)
    _param_to_v8(p, rate, "rate", v8, 900, 110)
    _seed_controls(p, v8, ui["seed_feedback"], numbox_rect=(52, 76, 46, 16), dice_rect=(102, 76, 48, 16),
                   label_rect=(2, 77, 46, 14))
    return p.to_json(kind="transformation", title="MidiBakery Transform", width=TOOL_WIDTH, height=TOOL_HEIGHT)


REFERENCE_WIDTH, REFERENCE_HEIGHT = 800.0, 169.0
SENSITIVITY_BANDS = (("Kick", "kick"), ("Lows", "low"), ("Mids", "mid"), ("Hats", "high"))  # the view's lanes
# Where reference-view.js draws its lanes (HEADER + 2 + BLOCKS + 4, then LANE each, LANE - 2 high): each
# Sensitivity field sits level with its lane.
LANES_TOP, LANE_HEIGHT = 16 + 2 + 32 + 4, 22


def _row(k: int) -> float:
    """The reference device's control rows on the view's lane grid: 2..5 are the lanes, 0 and 1 above them
    (y 10 and 32), 6 below; a control 18 high sits at _row(k) + 1, centred like the fields."""
    return LANES_TOP + LANE_HEIGHT * (k - 2)


def reference_device() -> dict[str, Any]:
    """Layout (800 x 169): the view (sections and lanes) on the left, next to it the Sensitivity per lane (Kick,
    Lows, Mids, Hats) and Lock; Analyze, the locator buttons, Genre and About on the right.
    Audio passes through: the device sits on the reference song's own audio track."""
    p = Patcher()
    plugin = p.obj("plugin~", 20, 20, inlets=2, outlets=["signal", "signal"], width=60)
    plugout = p.obj("plugout~", 20, 60, inlets=2, outlets=["signal", "signal"], width=60)
    p.connect(plugin, 0, plugout, 0)
    p.connect(plugin, 1, plugout, 1)

    code = (SRC / "reference.js").read_text(encoding="utf-8")
    v8 = p.box("v8.codebox", (120, 140, 420, 300), inlets=1, outlets=["", "", ""], code=code, filename="none",
               fontface=0, fontname="<Monospaced>", fontsize=10.0)
    node = p.obj("node.script midibakery-reference.js @autostart 1 @watch 0", 120, 480, outlets=["", ""], width=320)
    p.connect(v8, 0, node)
    p.connect(node, 0, v8)
    # The Live API answers once the device is in place: init a moment after live.thisdevice.
    this_device = p.obj("live.thisdevice", 120, 20, outlets=["bang", "int", "int"], width=90)
    later = p.obj("deferlow", 120, 50, width=60)
    init = p.message("init", 120, 80)
    p.connect(this_device, 0, later)
    p.connect(later, 0, init)
    p.connect(init, 0, v8)

    view_rect = (0, 0, 600, REFERENCE_HEIGHT)
    view_code = (SRC / "reference-view.js").read_text(encoding="utf-8")
    view = p.box("v8ui", (560, 140, view_rect[2], view_rect[3]), inlets=1, outlets=[""], presentation=view_rect,
                 filename="none", parameter_enable=0,
                 textfile={"text": view_code, "filename": "none", "flags": 0, "embed": 1, "autowatch": 0})
    p.connect(v8, 1, view)

    # the right column in two blocks on the lane grid: Genre and Analyze on top (rows 0-1), the locator
    # buttons and About at the bottom (rows 4-6, About level with Lock)
    buttons = (("Analyze", "analyze", 1), ("Set Locators", "locators", 4), ("Remove Locators", "unlocators", 5))
    for i, (label, message, row) in enumerate(buttons):
        button = p.parameter("live.text", (300 + 90 * i, 20, 80, 18), (706, _row(row) + 1, 90, 18), name=label, short=label,
                             ptype=ENUM, inlets=1, outlets=["", ""], parameter_enum=["val1", "val2"], parameter_mmax=1)
        p.boxes[-1]["box"].update({"mode": 0, "text": label, "texton": label})
        _on_press(p, button, message, v8, 300 + 90 * i, 50)
    genre = _menu(p, (600, 20, 110, 15), (706, _row(0) + 1, 90, 18), "Genre", "Genre",
                  ["Auto", "House", "Melodic Techno", "Peak Time Techno", "Drum & Bass", "Trap"], 0)
    to_v8 = p.obj("prepend genre", 600, 50, width=90)
    p.connect(genre, 0, to_v8)
    p.connect(to_v8, 0, v8)
    # Sensitivity: the fields' mean sets how finely the song is split (100: every change; lower merges more,
    # the phrases stay in the analysis), their ratio how much each band's changes count; Lock moves them together
    p.comment("Sensitivity", (860, 100, 80, 14), presentation=(604, _row(1) + 3, 92, 14), fontsize=9.0, **THEMED_TEXT)
    route = p.obj("route " + " ".join(key for _, key in SENSITIVITY_BANDS), 860, 400,
                  outlets=[""] * (len(SENSITIVITY_BANDS) + 1), width=200)
    p.connect(v8, 2, route)
    for i, (label, key) in enumerate(SENSITIVITY_BANDS):
        x, y = 860 + 70 * i, 140
        lane_y = LANES_TOP + LANE_HEIGHT * i  # the lane is LANE_HEIGHT - 2 high: the 16 px field centred on it
        p.comment(label, (x, y, 40, 14), presentation=(604, lane_y + 3, 40, 14), fontsize=9.0, **THEMED_TEXT)
        field = p.parameter("live.numbox", (x, y + 20, 44, 15), (648, lane_y + 2, 48, 16), name=f"Sensitivity {label}",
                            short=label, ptype=FLOAT, inlets=1, outlets=["", "float"], parameter_mmin=0.0,
                            parameter_mmax=100.0, parameter_initial=[50.0], parameter_initial_enable=1,
                            parameter_unitstyle=UNIT_INT)
        to_v8 = p.obj(f"prepend band {key}", x, y + 50, width=110)
        p.connect(field, 0, to_v8)
        p.connect(to_v8, 0, v8)
        shown = p.obj("prepend set", x, 430, width=70)
        p.connect(route, i, shown)
        p.connect(shown, 0, field)
    lock = p.parameter("live.text", (860, 220, 50, 15), (604, _row(6) + 1, 92, 18), name="Sensitivity Lock", short="Lock",
                       ptype=ENUM, inlets=1, outlets=["", ""], parameter_enum=["val1", "val2"], parameter_mmax=1)
    p.boxes[-1]["box"].update({"mode": 1, "text": "Lock", "texton": "Locked"})
    lock_to_v8 = p.obj("prepend lock", 860, 250, width=80)
    p.connect(lock, 0, lock_to_v8)
    p.connect(lock_to_v8, 0, v8)
    _about(p, (706, _row(6) + 1, 90, 18), node, 760, 20)
    return p.to_json(kind="audio_effect", title="MidiBakery Reference Analyzer", width=REFERENCE_WIDTH, height=REFERENCE_HEIGHT)


DEVICES = {
    "MidiBakery Chat": ("midi_effect", chat_device),
    "MidiBakery Reference Analyzer": ("audio_effect", reference_device),
    "MidiBakery Generator": ("generator", generator_device),
    "MidiBakery Transform": ("transformation", transform_device),
}


INSTALL_LIST = "install.json"


def build(out: Path = DIST) -> list[Path]:
    """The devices and, in INSTALL_LIST, where each built file goes in Live's User Library: `midibakery install`
    follows that list, so it needs neither this script nor the device kinds."""
    # From scratch: a renamed device must not linger in dist (and from there in an install).
    for kind in INSTALL_DIRS:
        shutil.rmtree(out / kind, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    files = []
    for name, (kind, factory) in DEVICES.items():
        path = out / kind / f"{name}.amxd"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_amxd(path, kind, factory())
        for node_file in NODE_FILES:
            shutil.copy2(SRC / node_file, path.parent / node_file)
        paths.append(path)
        # node.script finds its files next to the device.
        for file in (path.name, *NODE_FILES):
            entry = {"source": f"{kind}/{file}", "target": (INSTALL_DIRS[kind] / file).as_posix()}
            if entry not in files:
                files.append(entry)
    (out / INSTALL_LIST).write_text(json.dumps({"version": VERSION, "files": files}, indent=1) + "\n", encoding="utf-8")
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args(argv)
    for path in build():
        print(f"built {path.relative_to(HERE)} ({VERSION})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
