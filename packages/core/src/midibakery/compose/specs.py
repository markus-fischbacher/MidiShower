"""Request models for the composition tools; also used as MCP tool schemas."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from midibakery.compose.bass import BassStyle, KickRelation
from midibakery.compose.chords import ChordRhythm
from midibakery.compose.drums import DrumStyle
from midibakery.compose.transform import ArpDirection, Operation

TrackIndex = Annotated[int, Field(ge=0, description="Zero-based track index")]
SlotIndex = Annotated[int, Field(ge=0, description="Zero-based clip slot (= scene) index")]
ArrangementClip = Annotated[
    Annotated[int, Field(ge=0)] | Annotated[str, Field(min_length=1)] | None,
    Field(description="A clip in the Arrangement instead of slot, e.g. one section of a song: its zero-based "
          "index on the track (get_arrangement) or its name, e.g. 'Groove Bass'"),
]
Pitch = Annotated[int, Field(ge=0, le=127)]
Seed = Annotated[
    int | None,
    Field(description="Same seed + same inputs = same notes. Omit for a fresh idea; the used seed is returned."),
]
Amount = Annotated[float, Field(ge=0, le=1)]
Swing = Annotated[float, Field(ge=0, le=1, description="Delays every off-beat swing_unit; 0 straight, 1 full triplet feel")]
SwingUnit = Annotated[Literal["1/16", "1/8"], Field(description="Note value that swings: 1/16 (house, techno) or 1/8 (hip-hop, shuffle)")]
SWING_UNITS = {"1/16": 0.25, "1/8": 0.5}


class Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _one_place(spec: Any, required: bool) -> None:
    if spec.slot is not None and spec.arrangement_clip is not None:
        raise ValueError("give either slot (Session View) or arrangement_clip (Arrangement), not both")
    if required and spec.slot is None and spec.arrangement_clip is None:
        raise ValueError("give slot (Session View) or arrangement_clip (Arrangement, from get_arrangement)")


class ClipLocation(Spec):
    track: TrackIndex
    slot: SlotIndex | None = None
    arrangement_clip: ArrangementClip = None

    @model_validator(mode="after")
    def _place(self) -> ClipLocation:
        _one_place(self, required=True)
        return self


class Generate(Spec):
    track: TrackIndex
    slot: Annotated[
        int | None, Field(ge=0, description="Zero-based clip slot (= scene) index. Default: the track's first free slot")
    ] = None
    arrangement_clip: ArrangementClip = None
    bars: Annotated[
        int | None,
        Field(ge=1, le=64, description="Length in bars of the song's time signature; default 4, or the arrangement clip's length"),
    ] = None
    seed: Seed = None
    if_exists: Literal["error", "replace", "merge"] | None = Field(
        None,
        description="If the slot already holds a clip: 'error' (default), 'replace' its notes, or 'merge' (add on top). "
        "An arrangement clip always exists: its notes are replaced unless 'merge'",
    )
    clip_name: str | None = None

    @model_validator(mode="after")
    def _place(self) -> Generate:
        _one_place(self, required=False)
        return self


class Harmonic(Generate):
    progression: str | None = Field(
        None,
        description="Chords as roman numerals relative to the song scale ('i VI III VII', 'ii7 V7 I') or names ('Am F C G'). "
        "Default: chords_from, else the chords of an earlier step in the same plan, else a generated functional progression.",
    )
    chords_from: ClipLocation | None = Field(None, description="Detect the chords of an existing MIDI clip and follow them")
    chords_per_bar: Annotated[float, Field(gt=0, le=4, description="1 = a chord per bar, 2 = two, 0.5 = one per two bars")] = 1.0


class DrumsSpec(Generate):
    kind: Literal["drums"] = "drums"
    style: DrumStyle = "house"
    density: Amount = 0.5
    swing: Swing = 0.0
    swing_unit: SwingUnit = "1/16"
    fills: bool = Field(True, description="Fill at the end of each 4-bar phrase")
    chance: Amount = Field(
        1.0,
        description="Like Live's note Chance for ghost notes, some hats and extra kicks: 1 = every hit "
        "always plays (default), 0.5 = the style's natural variation, 0 = twice as much; backbeats always play",
    )
    device: Annotated[int | None, Field(ge=0, description="Drum Rack device index; default: first Drum Rack")] = None


class BassSpec(Harmonic):
    kind: Literal["bass"] = "bass"
    style: BassStyle = "syncopated"
    octave: Annotated[int, Field(ge=-1, le=4, description="Live octave of the root, C1 = 36")] = 1
    follow_kick: ClipLocation | None = Field(None, description="Drum clip whose kick the bass should lock to or avoid")
    kick_relation: KickRelation = "lock"
    density: Amount = 0.5
    swing: Swing = 0.0
    swing_unit: SwingUnit = "1/16"


class ChordsSpec(Harmonic):
    kind: Literal["chords"] = "chords"
    rhythm: ChordRhythm = "sustain"
    voices: Literal[3, 4] = 3
    low: Pitch = 52
    high: Pitch = 76
    sevenths: bool = Field(False, description="Use seventh chords for a generated progression")
    swing: Swing = 0.0
    swing_unit: SwingUnit = "1/16"

    @model_validator(mode="after")
    def _range(self) -> ChordsSpec:
        if self.high - self.low < 12:
            raise ValueError("chords need a range of at least one octave (high - low >= 12)")
        return self


class MelodySpec(Harmonic):
    kind: Literal["melody"] = "melody"
    low: Pitch = 60
    high: Pitch = 84
    density: Amount = 0.5
    swing: Swing = 0.0
    swing_unit: SwingUnit = "1/16"

    @model_validator(mode="after")
    def _range(self) -> MelodySpec:
        if self.high - self.low < 7:
            raise ValueError("melody range must span at least a fifth (high - low >= 7)")
        return self


class TransformSpec(Spec):
    kind: Literal["transform"] = "transform"
    track: TrackIndex
    slot: SlotIndex | None = None
    arrangement_clip: ArrangementClip = None
    operation: Operation
    note_ids: list[int] | None = Field(None, description="Only these notes (default: all notes in the clip)")
    amount: Amount = Field(0.5, description="humanize/variation intensity")
    grid: Annotated[float, Field(gt=0, le=4, description="quantize grid in beats (0.25 = 1/16)")] = 0.25
    strength: Amount = Field(1.0, description="quantize strength")
    swing: Swing = 0.0
    swing_unit: SwingUnit = "1/16"
    axis: Pitch | None = Field(None, description="invert: mirror pitch (default: middle of the notes' range)")
    semitones: int = Field(
        0, ge=-48, le=48,
        description="transpose: chromatic half steps (12 = one octave). Use this for 'n semitones / half steps / up an octave'",
    )
    steps: int = Field(
        0, ge=-24, le=24,
        description="transpose: diatonic scale steps in the song key (2 = up a third, not 2 semitones). Only for 'n scale steps'",
    )
    rate: Annotated[float, Field(gt=0, le=4, description="arpeggiate: step length in beats")] = 0.25
    direction: ArpDirection = "up"
    octaves: Annotated[int, Field(ge=1, le=3)] = 1
    seed: Seed = None

    @model_validator(mode="after")
    def _place(self) -> TransformSpec:
        _one_place(self, required=True)
        return self

    @model_validator(mode="after")
    def _transpose_amount(self) -> TransformSpec:
        if self.operation == "transpose" and bool(self.semitones) == bool(self.steps):
            raise ValueError("transpose needs either semitones (chromatic, 12 = octave) or steps (scale steps in key), not both")
        return self


PART_SPECS: dict[str, type[Generate]] = {"drums": DrumsSpec, "bass": BassSpec, "chords": ChordsSpec, "melody": MelodySpec}


class ComposeRequest(Generate):
    """The compose tool: one schema for every part, so the shared fields are listed once.
    A field tagged (drums), (bass) ... belongs to those kinds; `spec()` checks the
    request against the kind's own model, which also rejects another kind's fields."""

    kind: Literal["drums", "bass", "chords", "melody"]
    progression: str | None = Field(
        None,
        description="(bass, chords, melody) Chords as roman numerals in the song scale ('i VI III VII', 'ii7 V7 I') or names "
        "('Am F C G'). Default: chords_from, else a generated functional progression.",
    )
    chords_from: ClipLocation | None = Field(None, description="(bass, chords, melody) Follow the chords of an existing MIDI clip")
    chords_per_bar: Annotated[float, Field(gt=0, le=4, description="(bass, chords, melody) 1 = a chord per bar, 0.5 = one per two bars")] | None = None
    style: Literal[DrumStyle, BassStyle] | None = Field(  # type: ignore[valid-type]
        None,
        description="(drums) house, techno, hiphop, trap, rock, breakbeat; (bass) sustained, pulse, offbeat, octave, walking, syncopated",
    )
    density: Annotated[float, Field(ge=0, le=1, description="(drums, bass, melody) 0 sparse - 1 busy")] | None = None
    swing: Swing = 0.0
    swing_unit: SwingUnit = "1/16"
    fills: bool | None = Field(None, description="(drums) Fill at the end of each 4-bar phrase, default true")
    chance: Annotated[float, Field(ge=0, le=1, description="(drums) Note chance of ghost notes and some hats: 1 always (default), 0.5 natural")] | None = None
    device: Annotated[int, Field(ge=0, description="(drums) Drum Rack device index; default: first Drum Rack")] | None = None
    octave: Annotated[int, Field(ge=-1, le=4, description="(bass) Live octave of the root, C1 = 36; default 1")] | None = None
    follow_kick: ClipLocation | None = Field(None, description="(bass) Drum clip whose kick the bass locks to or avoids")
    kick_relation: KickRelation | None = Field(None, description="(bass) lock (default) or avoid the kick")
    rhythm: ChordRhythm | None = Field(None, description="(chords) sustain (default), pulse, offbeat, stabs")
    voices: Literal[3, 4] | None = Field(None, description="(chords) 3 (default) or 4")
    sevenths: bool | None = Field(None, description="(chords) Seventh chords for a generated progression")
    low: Pitch | None = Field(None, description="(chords, melody) Lowest pitch; default 52 chords, 60 melody")
    high: Pitch | None = Field(None, description="(chords, melody) Highest pitch; default 76 chords, 84 melody")

    @model_validator(mode="after")
    def _fields_of_the_kind(self) -> ComposeRequest:
        own = PART_SPECS[self.kind].model_fields
        foreign = [name for name in self.model_fields_set if name not in own and getattr(self, name) is not None]
        if foreign:
            raise ValueError(f"{', '.join(sorted(foreign))} not for kind '{self.kind}'")
        return self

    def spec(self) -> Generate:
        return PART_SPECS[self.kind].model_validate(self.model_dump(exclude_none=True))


Step = Annotated[DrumsSpec | BassSpec | ChordsSpec | MelodySpec | TransformSpec, Field(discriminator="kind")]
