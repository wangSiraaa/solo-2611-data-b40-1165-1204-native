"""Winter seedbed sun rules and the robust consecutive-sunshine policy engine.

This module is deliberately persistence-free, mirroring :mod:`slabwater.solver`:
it knows how to parse sun-direction cards and weather possibilities, resolve
shading from a *frozen* :class:`~slabwater.model.Slice` (keeping the source
blocks that cast every shadow), and search for an operating policy that keeps
every adopted tray in uninterrupted sunshine for the contracted number of
beats.  The SQLite-backed contracts and hand-over ledgers live in
:mod:`slabwater.adoption`.

Terminology (one day is divided into a fixed number of *beats* 拍):

* A :class:`RayCard` is one published sun-direction card: for one bed and one
  beat it lists the terrain cells the incoming ray travels through (the
  "along-line cells") and the ray height above each.  A mid-path cell whose
  real elevation *reaches* the ray (``height >= ray_height``) blocks that beat.
* A :class:`WeatherPattern` is one whole-day possibility: per-beat sunny flags.
  The same pattern applies to every bed that day -- the garden may never pick
  sunny beats from one day for bed A and from another day for bed B.
* A fixed (固定种植畦) operation never moves a tray: the policy only has to
  prove the contracted bed stays sunny through the required run under every
  possible day.
* A mobile (带轮苗床) operation may push a tray along a declared corridor to
  another bed it holds rights to.  A move needs a free corridor and an on-duty
  gardener, consumes the whole beat, and that beat can never count as sunshine.
  Decisions cannot use afternoon weather that has not been observed yet.

Two information schedules are supported for mobile policies:

``dawn``
    The day type is announced on the first beat's morning, so beat-one actions
    may already differ between weather possibilities.
``progressive``
    Only past beats are known when an action is chosen; the beat-one action is
    shared by every still-possible day and branching happens on observations.

Both are non-anticipative: at no point does a branch read a future beat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .model import Cell, Slice, SourceRef, is_int

MAX_BEDS = 4
MAX_BEATS = 4
MAX_TRAYS = 2


class AdoptionError(Exception):
    """A malformed adoption request (analogous to PublishError)."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "message": self.message, "details": self.details}


@dataclass(frozen=True)
class Bed:
    """A rentable seedbed anchored at one surface cell."""

    id: str
    cell: Cell

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "cell": [self.cell[0], self.cell[1]]}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Bed":
        return cls(id=raw["id"], cell=(int(raw["cell"][0]), int(raw["cell"][1])))


@dataclass(frozen=True)
class RayCard:
    """One sun-direction card for ``bed_id`` at ``beat`` (beats are 1-based)."""

    bed_id: str
    beat: int
    path: tuple[Cell, ...]
    ray_heights: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "bed_id": self.bed_id,
            "beat": self.beat,
            "path": [[r, c] for r, c in self.path],
            "ray_heights": list(self.ray_heights),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RayCard":
        return cls(
            bed_id=raw["bed_id"],
            beat=int(raw["beat"]),
            path=tuple((int(p[0]), int(p[1])) for p in raw["path"]),
            ray_heights=tuple(int(h) for h in raw["ray_heights"]),
        )


@dataclass(frozen=True)
class WeatherPattern:
    """One whole-day weather possibility, one sunny flag per beat."""

    id: str
    beats: tuple[bool, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "beats": [bool(b) for b in self.beats]}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WeatherPattern":
        return cls(id=raw["id"], beats=tuple(bool(b) for b in raw["beats"]))


@dataclass(frozen=True)
class TraySpec:
    """One adopted tray: start bed, usable beds, and the required sun run."""

    id: str
    initial: str
    rights: tuple[str, ...]
    sun_beats: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "initial": self.initial,
            "rights": list(self.rights),
            "sun_beats": self.sun_beats,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TraySpec":
        return cls(
            id=raw["id"],
            initial=raw["initial"],
            rights=tuple(raw["rights"]),
            sun_beats=int(raw["sun_beats"]),
        )


@dataclass(frozen=True)
class Blocker:
    """A mid-path cell that reaches the ray, with its frozen provenance."""

    cell: Cell
    elevation: int
    ray_height: int
    sources: tuple[SourceRef, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell": [self.cell[0], self.cell[1]],
            "elevation": self.elevation,
            "ray_height": self.ray_height,
            "sources": [ref.to_dict() for ref in self.sources],
        }


@dataclass(frozen=True)
class ShadeResult:
    bed_id: str
    beat: int
    blocked: bool
    blockers: tuple[Blocker, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "bed_id": self.bed_id,
            "beat": self.beat,
            "blocked": self.blocked,
            "blockers": [b.to_dict() for b in self.blockers],
        }


@dataclass(frozen=True)
class Move:
    """One tray's intent for one beat: stay put, or move along a corridor."""

    tray_id: str
    destination: str
    moved: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "tray": self.tray_id,
            "action": "move" if self.moved else "stay",
            "to": self.destination if self.moved else None,
            "uses_gardener": self.moved,
        }


@dataclass(frozen=True)
class PlanSpec:
    """Everything the policy engine needs, fully validated against a surface."""

    mode: str
    beds: tuple[Bed, ...]
    cards: tuple[RayCard, ...]
    weather: tuple[WeatherPattern, ...]
    trays: tuple[TraySpec, ...]
    corridors: frozenset[frozenset[str]]
    gardeners: int
    n_beats: int
    reveal: str

    def bed(self, bed_id: str) -> Bed:
        return next(b for b in self.beds if b.id == bed_id)

    def card(self, bed_id: str, beat: int) -> RayCard:
        return next(c for c in self.cards if c.bed_id == bed_id and c.beat == beat)

    def corridor_open(self, left: str, right: str) -> bool:
        return frozenset((left, right)) in self.corridors

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "beds": [b.to_dict() for b in self.beds],
            "cards": [c.to_dict() for c in self.cards],
            "weather": [w.to_dict() for w in self.weather],
            "trays": [t.to_dict() for t in self.trays],
            "corridors": sorted(sorted(pair) for pair in self.corridors),
            "gardeners": self.gardeners,
            "n_beats": self.n_beats,
            "reveal": self.reveal,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PlanSpec":
        """Rebuild a stored spec without a surface (geometry was validated at sign)."""

        return cls(
            mode=raw["mode"],
            beds=tuple(Bed.from_dict(item) for item in raw["beds"]),
            cards=tuple(RayCard.from_dict(item) for item in raw["cards"]),
            weather=tuple(WeatherPattern.from_dict(item) for item in raw["weather"]),
            trays=tuple(TraySpec.from_dict(item) for item in raw["trays"]),
            corridors=frozenset(frozenset(pair) for pair in raw["corridors"]),
            gardeners=int(raw["gardeners"]),
            n_beats=int(raw["n_beats"]),
            reveal=raw["reveal"],
        )


# --------------------------------------------------------------------------- #
# Parsing / validation
# --------------------------------------------------------------------------- #
def _require_cell(raw: Any, label: str, surface: Slice) -> Cell:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise AdoptionError("bad_request", f"{label} must be a [row, col] pair")
    r, c = raw
    if not is_int(r) or not is_int(c):
        raise AdoptionError("bad_request", f"{label} coordinates must be integers")
    if not (0 <= r < surface.height and 0 <= c < surface.width):
        raise AdoptionError(
            "cell_out_of_bounds", f"{label} ({r},{c}) is outside the surface", {"cell": [r, c]}
        )
    return (r, c)


def _parse_beds(raw: Any, surface: Slice) -> tuple[Bed, ...]:
    if not isinstance(raw, list) or not raw:
        raise AdoptionError("bad_request", "beds must be a non-empty list")
    if len(raw) > MAX_BEDS:
        raise AdoptionError(
            "limit_exceeded", f"at most {MAX_BEDS} beds per adoption order", {"beds": len(raw)}
        )
    beds: list[Bed] = []
    seen_ids: set[str] = set()
    seen_cells: set[Cell] = set()
    for item in raw:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str) or not item["id"]:
            raise AdoptionError("bad_request", "each bed needs a non-empty string id")
        bed_id = item["id"]
        if bed_id in seen_ids:
            raise AdoptionError("duplicate_bed", f"bed id {bed_id} is repeated")
        cell = _require_cell(item.get("cell"), f"bed {bed_id} cell", surface)
        if cell in seen_cells:
            raise AdoptionError("duplicate_bed", f"cell {cell} is used by two beds")
        seen_ids.add(bed_id)
        seen_cells.add(cell)
        beds.append(Bed(bed_id, cell))
    return tuple(sorted(beds, key=lambda b: b.id))


def _parse_weather(raw: Any) -> tuple[WeatherPattern, ...]:
    if not isinstance(raw, list) or not raw:
        raise AdoptionError("bad_request", "weather must list at least one whole-day possibility")
    patterns: list[WeatherPattern] = []
    seen: set[str] = set()
    width: int | None = None
    for item in raw:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str) or not item["id"]:
            raise AdoptionError("bad_request", "each weather possibility needs a string id")
        wid = item["id"]
        if wid in seen:
            raise AdoptionError("duplicate_weather", f"weather id {wid} is repeated")
        beats = item.get("beats")
        if not isinstance(beats, list) or not beats or not all(isinstance(b, bool) for b in beats):
            raise AdoptionError("bad_request", f"weather {wid} beats must be a non-empty bool list")
        if width is None:
            width = len(beats)
        elif len(beats) != width:
            raise AdoptionError("bad_request", "all weather possibilities must span the same beats")
        if len(beats) > MAX_BEATS:
            raise AdoptionError(
                "limit_exceeded", f"at most {MAX_BEATS} beats per adoption order", {"beats": len(beats)}
            )
        seen.add(wid)
        patterns.append(WeatherPattern(wid, tuple(beats)))
    return tuple(sorted(patterns, key=lambda w: w.id))


def _parse_cards(
    raw: Any, beds: tuple[Bed, ...], n_beats: int, surface: Slice
) -> tuple[RayCard, ...]:
    if not isinstance(raw, list) or not raw:
        raise AdoptionError("bad_request", "cards must be a non-empty list of sun-direction cards")
    by_cell = {bed.cell: bed.id for bed in beds}
    by_id = {bed.id: bed.id for bed in beds}
    cards: list[RayCard] = []
    seen: set[tuple[str, int]] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise AdoptionError("bad_request", f"cards[{index}] must be an object")
        bed_id = item.get("bed_id")
        if bed_id is None:
            bed_cell = _require_cell(item.get("bed"), f"cards[{index}] bed", surface)
            if bed_cell not in by_cell:
                raise AdoptionError(
                    "unknown_bed", f"cards[{index}] cell {bed_cell} is not a registered bed"
                )
            bed_id = by_cell[bed_cell]
        elif bed_id not in by_id:
            raise AdoptionError("unknown_bed", f"card references unknown bed {bed_id}")
        beat = item.get("beat")
        if not is_int(beat) or not 1 <= beat <= n_beats:
            raise AdoptionError(
                "bad_request", f"card beat must be an integer in 1..{n_beats}", {"beat": beat}
            )
        if (bed_id, beat) in seen:
            raise AdoptionError("duplicate_card", f"bed {bed_id} has two cards for beat {beat}")
        seen.add((bed_id, beat))
        raw_path = item.get("path")
        if not isinstance(raw_path, list) or not raw_path:
            raise AdoptionError("bad_request", f"card {bed_id}@{beat} needs a non-empty path")
        path = tuple(_require_cell(cell, f"card {bed_id}@{beat} path cell", surface) for cell in raw_path)
        scalar = item.get("ray_height")
        per_cell = item.get("ray_heights")
        if per_cell is not None:
            if not isinstance(per_cell, list) or len(per_cell) != len(path):
                raise AdoptionError(
                    "bad_request", f"card {bed_id}@{beat} ray_heights must align with its path"
                )
            if not all(is_int(h) for h in per_cell):
                raise AdoptionError("bad_request", f"card {bed_id}@{beat} ray heights must be integers")
            ray_heights = tuple(int(h) for h in per_cell)
        else:
            if not is_int(scalar):
                raise AdoptionError(
                    "bad_request", f"card {bed_id}@{beat} needs integer ray_height or aligned ray_heights"
                )
            ray_heights = tuple(int(scalar) for _ in path)
        cards.append(RayCard(bed_id, int(beat), path, ray_heights))
    # Every bed must be reachable by the sun at every beat, otherwise sunshine
    # is undefined rather than merely blocked.
    expected = {(bed.id, beat) for bed in beds for beat in range(1, n_beats + 1)}
    missing = sorted(expected - seen)
    if missing:
        raise AdoptionError(
            "card_gap",
            "every bed needs one sun-direction card per beat",
            {"missing": [{"bed_id": b, "beat": t} for b, t in missing]},
        )
    return tuple(sorted(cards, key=lambda c: (c.bed_id, c.beat)))


def _parse_corridors(raw: Any, bed_ids: set[str]) -> frozenset[frozenset[str]]:
    if raw is None:
        return frozenset()
    if not isinstance(raw, list):
        raise AdoptionError("bad_request", "corridors must be a list of [bed_a, bed_b] pairs")
    corridors: set[frozenset[str]] = set()
    for pair in raw:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise AdoptionError("bad_request", "each corridor must be a [bed_a, bed_b] pair")
        a, b = pair
        if a not in bed_ids or b not in bed_ids:
            raise AdoptionError("unknown_bed", f"corridor {pair} references an unknown bed")
        if a == b:
            raise AdoptionError("bad_request", "a corridor must connect two distinct beds")
        corridors.add(frozenset((a, b)))
    return frozenset(corridors)


def _parse_trays(raw: Any, bed_ids: set[str], n_beats: int) -> tuple[TraySpec, ...]:
    if not isinstance(raw, list) or not raw:
        raise AdoptionError("bad_request", "trays must be a non-empty list")
    if len(raw) > MAX_TRAYS:
        raise AdoptionError(
            "limit_exceeded", f"at most {MAX_TRAYS} trays per adoption order", {"trays": len(raw)}
        )
    trays: list[TraySpec] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str) or not item["id"]:
            raise AdoptionError("bad_request", "each tray needs a non-empty string id")
        tray_id = item["id"]
        if tray_id in seen:
            raise AdoptionError("duplicate_tray", f"tray id {tray_id} is repeated")
        seen.add(tray_id)
        initial = item.get("initial")
        if initial not in bed_ids:
            raise AdoptionError("unknown_bed", f"tray {tray_id} initial bed {initial!r} is unknown")
        rights = item.get("rights")
        if not isinstance(rights, list) or not rights or any(r not in bed_ids for r in rights):
            raise AdoptionError("bad_request", f"tray {tray_id} rights must be a non-empty list of bed ids")
        if len(set(rights)) != len(rights):
            raise AdoptionError("bad_request", f"tray {tray_id} rights contain a duplicate bed")
        if initial not in rights:
            raise AdoptionError("bad_request", f"tray {tray_id} initial bed must be inside its rights")
        sun_beats = item.get("sun_beats")
        if not is_int(sun_beats) or not 1 <= sun_beats <= n_beats:
            raise AdoptionError(
                "bad_request", f"tray {tray_id} sun_beats must be an integer in 1..{n_beats}"
            )
        trays.append(TraySpec(tray_id, initial, tuple(sorted(rights)), int(sun_beats)))
    return tuple(sorted(trays, key=lambda t: t.id))


def parse_spec(request: Mapping[str, Any], surface: Slice) -> PlanSpec:
    """Validate one sign request against a real frozen surface."""

    if not isinstance(request, Mapping):
        raise AdoptionError("bad_request", "request must be an object")
    mode = request.get("mode")
    if mode not in ("fixed", "mobile"):
        raise AdoptionError("bad_request", "mode must be 'fixed' or 'mobile'")
    reveal = request.get("reveal", "dawn")
    if reveal not in ("dawn", "progressive"):
        raise AdoptionError("bad_request", "reveal must be 'dawn' or 'progressive'")
    beds = _parse_beds(request.get("beds"), surface)
    weather = _parse_weather(request.get("weather"))
    n_beats = len(weather[0].beats)
    cards = _parse_cards(request.get("cards"), beds, n_beats, surface)
    bed_ids = {bed.id for bed in beds}
    corridors = _parse_corridors(request.get("corridors"), bed_ids)
    gardeners = request.get("gardeners", 0)
    if not is_int(gardeners) or gardeners < 0:
        raise AdoptionError("bad_request", "gardeners must be a non-negative integer")
    trays = _parse_trays(request.get("trays"), bed_ids, n_beats)
    if mode == "fixed":
        # A fixed tray is bound to one bed and may never hold mobile rights.
        for tray in trays:
            if len(tray.rights) != 1 or tray.initial != tray.rights[0]:
                raise AdoptionError(
                    "fixed_bed_mobile_rights",
                    f"fixed tray {tray.id} must hold exactly one bed and start on it",
                )
    else:
        for tray in trays:
            for bed_id in tray.rights:
                if bed_id != tray.initial and not any(
                    tray.initial in pair and bed_id in pair for pair in corridors
                ):
                    raise AdoptionError(
                        "no_corridor",
                        f"tray {tray.id} has right on {bed_id} but no corridor reaches it from {tray.initial}",
                    )
    return PlanSpec(
        mode=mode,
        beds=beds,
        cards=cards,
        weather=weather,
        trays=trays,
        corridors=corridors,
        gardeners=int(gardeners),
        n_beats=n_beats,
        reveal=reveal,
    )


# --------------------------------------------------------------------------- #
# Shading against a real frozen slice
# --------------------------------------------------------------------------- #
def shade_card(surface: Slice, card: RayCard) -> ShadeResult:
    """Resolve a card on a real version; every blocker keeps its source blocks."""

    blockers: list[Blocker] = []
    for cell, ray_height in zip(card.path, card.ray_heights):
        elevation = surface.at(cell[0], cell[1])
        if elevation >= ray_height:
            blockers.append(
                Blocker(
                    cell=cell,
                    elevation=elevation,
                    ray_height=ray_height,
                    sources=surface.sources.get(cell, ()),
                )
            )
    return ShadeResult(card.bed_id, card.beat, bool(blockers), tuple(blockers))


def evaluate_surface(spec: PlanSpec, surface: Slice) -> dict[tuple[str, int], ShadeResult]:
    """Shade verdict for every published card on one frozen surface."""

    return {(card.bed_id, card.beat): shade_card(surface, card) for card in spec.cards}


# --------------------------------------------------------------------------- #
# Policy engine
# --------------------------------------------------------------------------- #
def _observation_index(spec: PlanSpec, pattern_index: int, beat: int) -> Any:
    """What the operator can distinguish when choosing the action at ``beat``."""

    if spec.reveal == "dawn":
        # The day type is announced on the first beat's morning.
        return pattern_index
    # Progressive reveal: the current beat's weather is observed only as it
    # happens, so it is the newest flag available when choosing this action.
    return spec.weather[pattern_index].beats[beat - 1]


@dataclass(frozen=True)
class _TrayState:
    tray_id: str
    position: str
    streak: int
    done: bool


@dataclass
class BeatRecord:
    """One tray's realized beat, persisted into the hand-over ledger."""

    tray_id: str
    beat: int
    bed_id: str
    moved: bool
    weather_sunny: bool
    shaded: bool
    sun_counted: bool
    streak: int
    achieved: bool
    blockers: tuple[Blocker, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "tray": self.tray_id,
            "beat": self.beat,
            "bed": self.bed_id,
            "moved": self.moved,
            "weather_sunny": self.weather_sunny,
            "shaded": self.shaded,
            "sun_counted": self.sun_counted,
            "streak": self.streak,
            "achieved": self.achieved,
            "blockers": [b.to_dict() for b in self.blockers],
        }


class SunEngine:
    """Robust AND/OR search over weather uncertainty for one frozen surface."""

    def __init__(self, spec: PlanSpec, surface: Slice):
        self.spec = spec
        self.surface = surface
        self._shade = evaluate_surface(spec, surface)
        self._memo: dict[Any, bool] = {}

    # -- basics ------------------------------------------------------------ #
    def is_sunny(self, bed_id: str, beat: int, pattern: WeatherPattern) -> bool:
        shade = self._shade[(bed_id, beat)]
        return pattern.beats[beat - 1] and not shade.blocked

    def _joint_actions(self, positions: Mapping[str, str], gardener_cap: int) -> list[tuple[Move, ...]]:
        options: list[list[Move]] = []
        for tray in self.spec.trays:
            current = positions[tray.id]
            choices = [Move(tray.id, current, False)]
            if self.spec.mode == "mobile":
                for bed_id in sorted(tray.rights):
                    if bed_id != current and self.spec.corridor_open(current, bed_id):
                        choices.append(Move(tray.id, bed_id, True))
            options.append(choices)
        joint: list[tuple[Move, ...]] = [()]
        for choices in options:
            joint = [prefix + (choice,) for prefix in joint for choice in choices]
        # Two trays may never share a bed after a beat, and the gardener cap
        # bounds how many trays can be pushed at once.
        valid: list[tuple[Move, ...]] = []
        for moves in joint:
            destinations = [move.destination for move in moves]
            if len(set(destinations)) != len(destinations):
                continue
            if sum(move.moved for move in moves) > gardener_cap:
                continue
            valid.append(moves)
        # Deterministic order: stays first, then moves, lexicographic by tray.
        valid.sort(key=lambda moves: tuple((m.moved, m.destination) for m in moves))
        return valid

    def _apply(
        self,
        states: tuple[_TrayState, ...],
        moves: tuple[Move, ...],
        pattern: WeatherPattern,
        beat: int,
    ) -> tuple[_TrayState, ...]:
        destination = {move.tray_id: move.destination for move in moves}
        moved = {move.tray_id: move.moved for move in moves}
        next_states: list[_TrayState] = []
        for state in states:
            pos = destination[state.tray_id] if state.tray_id in destination else state.position
            if moved.get(state.tray_id):
                # Pushing consumes the whole beat: no sunshine may be recorded.
                sunny = False
            else:
                shade = self._shade[(pos, beat)]
                sunny = pattern.beats[beat - 1] and not shade.blocked
            streak = state.streak + 1 if sunny else 0
            tray = next(t for t in self.spec.trays if t.id == state.tray_id)
            # The run is locked only when a full sunny run is actually held; a
            # later break cannot retroactively undo an already completed order.
            done = state.done or (not state.done and streak >= tray.sun_beats)
            next_states.append(_TrayState(state.tray_id, pos, streak, done))
        return tuple(next_states)

    # -- robust search ----------------------------------------------------- #
    def _key(
        self, beat: int, states: tuple[_TrayState, ...], belief: frozenset[int]
    ) -> tuple[Any, ...]:
        return (
            beat,
            tuple((s.position, s.streak, s.done) for s in states),
            belief,
        )

    def _group_by_observation(
        self, belief: frozenset[int], beat: int
    ) -> dict[Any, frozenset[int]]:
        """Patterns sharing the observation available before this beat's action."""

        branches: dict[Any, set[int]] = {}
        for pattern_index in belief:
            obs = _observation_index(self.spec, pattern_index, beat)
            branches.setdefault(obs, set()).add(pattern_index)
        return {obs: frozenset(group) for obs, group in branches.items()}

    def _feasible(
        self, beat: int, states: tuple[_TrayState, ...], belief: frozenset[int]
    ) -> bool:
        if all(state.done for state in states):
            return True
        if beat > self.spec.n_beats:
            return False
        key = self._key(beat, states, belief)
        if key in self._memo:
            return self._memo[key]
        positions = {state.tray_id: state.position for state in states}
        branches = self._group_by_observation(belief, beat)
        for moves in self._joint_actions(positions, self.spec.gardeners):
            # Patterns in one observation group share the current beat's flag at
            # every occupied bed, so the action's transition is identical there.
            if all(
                self._feasible(
                    beat + 1,
                    self._apply(states, moves, self.spec.weather[next(iter(group))], beat),
                    group,
                )
                for group in branches.values()
            ):
                self._memo[key] = True
                return True
        self._memo[key] = False
        return False

    def feasible(self) -> bool:
        initial = tuple(
            _TrayState(tray.id, tray.initial, 0, False) for tray in self.spec.trays
        )
        return self._feasible(1, initial, frozenset(range(len(self.spec.weather))))

    # -- contingent policy tree for read-back ------------------------------ #
    def policy_tree(self) -> dict[str, Any] | None:
        if not self.feasible():
            return None
        return self._tree(
            1,
            tuple(_TrayState(tray.id, tray.initial, 0, False) for tray in self.spec.trays),
            frozenset(range(len(self.spec.weather))),
        )

    def _tree(
        self, beat: int, states: tuple[_TrayState, ...], belief: frozenset[int]
    ) -> dict[str, Any]:
        if all(state.done for state in states) or beat > self.spec.n_beats:
            return {"beat": beat, "done": True, "actions": [], "branches": []}
        positions = {state.tray_id: state.position for state in states}
        grouped = self._group_by_observation(belief, beat)
        chosen: tuple[Move, ...] | None = None
        for moves in self._joint_actions(positions, self.spec.gardeners):
            if all(
                self._feasible(
                    beat + 1,
                    self._apply(states, moves, self.spec.weather[next(iter(group))], beat),
                    group,
                )
                for group in grouped.values()
            ):
                chosen = moves
                break
        assert chosen is not None
        branch_nodes = []
        for obs, group in sorted(grouped.items(), key=lambda kv: repr(kv[0])):
            representative = self.spec.weather[next(iter(group))]
            branch_nodes.append(
                {
                    "observation": self._label_observation(beat, obs),
                    "weather_ids": [self.spec.weather[i].id for i in sorted(group)],
                    "then": self._tree(
                        beat + 1,
                        self._apply(states, chosen, representative, beat),
                        group,
                    ),
                }
            )
        return {
            "beat": beat,
            "done": False,
            "actions": [move.to_dict() for move in chosen],
            "branches": branch_nodes,
        }

    def _label_observation(self, beat: int, obs: Any) -> Any:
        if self.spec.reveal == "dawn":
            return {"weather_id": self.spec.weather[obs].id}
        return {"beat": beat, "sunny": bool(obs)}

    # -- failure witness --------------------------------------------------- #
    def explain_failure(self) -> list[dict[str, Any]]:
        """Best-effort per-pattern trace explaining whose sun run breaks where."""

        witnesses: list[dict[str, Any]] = []
        for pattern_index, pattern in enumerate(self.spec.weather):
            states = tuple(_TrayState(tray.id, tray.initial, 0, False) for tray in self.spec.trays)
            breaks: list[dict[str, Any]] = []
            for beat in range(1, self.spec.n_beats + 1):
                positions = {state.tray_id: state.position for state in states}
                moves = self._greedy(states, positions, pattern, beat)
                destinations = {m.tray_id: m.destination for m in moves}
                for state in states:
                    moved = next(m.moved for m in moves if m.tray_id == state.tray_id)
                    pos = destinations[state.tray_id]
                    shade = self._shade[(pos, beat)]
                    if moved:
                        sunny = False
                        cause = "move_beat"
                    elif shade.blocked:
                        sunny = False
                        cause = "shaded"
                    elif not pattern.beats[beat - 1]:
                        sunny = False
                        cause = "cloudy"
                    else:
                        sunny = True
                        cause = None
                    if not sunny:
                        breaks.append(
                            {
                                "tray": state.tray_id,
                                "beat": beat,
                                "bed": pos,
                                "cause": cause,
                                "blockers": [b.to_dict() for b in shade.blockers],
                            }
                        )
                states = self._apply(states, moves, pattern, beat)
            for tray, state in zip(self.spec.trays, states):
                if not state.done:
                    witnesses.append(
                        {
                            "weather_id": pattern.id,
                            "tray": tray.id,
                            "required_sun_beats": tray.sun_beats,
                            "longest_run": state.streak,
                            "breaks": [b for b in breaks if b["tray"] == tray.id],
                        }
                    )
        witnesses.sort(key=lambda w: (w["tray"], w["weather_id"]))
        return witnesses

    def _greedy(
        self,
        states: tuple[_TrayState, ...],
        positions: Mapping[str, str],
        pattern: WeatherPattern,
        beat: int,
    ) -> tuple[Move, ...]:
        """Pick the action maximizing immediate streaks under known weather."""

        best: tuple[tuple[int, ...], tuple[Move, ...]] | None = None
        for moves in self._joint_actions(dict(positions), self.spec.gardeners):
            applied = self._apply(states, moves, pattern, beat)
            score = tuple((s.done, s.streak) for s in applied)
            if best is None or score > best[0]:
                best = (score, moves)
        assert best is not None
        return best[1]


# --------------------------------------------------------------------------- #
# Hand-over realities (occupancy, actual gardeners, current terrain)
# --------------------------------------------------------------------------- #
def _as_beat(key: Any) -> int:
    if is_int(key):
        return int(key)
    if isinstance(key, str) and key.lstrip("-").isdigit():
        return int(key)
    raise AdoptionError("bad_request", "occupancy keys must be beat numbers")


def normalize_occupancy(
    raw: Any, bed_ids: set[str], n_beats: int
) -> dict[int, frozenset[str]]:
    """Accept ``["W"]`` (claimed all day) or ``{"2": ["N"]}`` (per-beat claims)."""

    empty = {beat: frozenset() for beat in range(1, n_beats + 1)}
    if raw is None:
        return empty
    if isinstance(raw, Mapping):
        occupied = dict(empty)
        for key, beds in raw.items():
            beat = _as_beat(key)
            if not 1 <= beat <= n_beats or not isinstance(beds, list):
                raise AdoptionError("bad_request", "occupancy beat out of range or not a list")
            unknown = [b for b in beds if b not in bed_ids]
            if unknown:
                raise AdoptionError("unknown_bed", f"occupancy references unknown beds {unknown}")
            occupied[beat] = frozenset(beds)
        return occupied
    if isinstance(raw, list):
        unknown = [b for b in raw if b not in bed_ids]
        if unknown:
            raise AdoptionError("unknown_bed", f"occupancy references unknown beds {unknown}")
        return {beat: frozenset(raw) for beat in range(1, n_beats + 1)}
    raise AdoptionError("bad_request", "occupied must be a bed list or a per-beat map")


@dataclass
class HandoverSimulation:
    ok: bool
    observations: tuple[bool, ...]
    records: list[BeatRecord] = field(default_factory=list)
    obstructions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "observations": list(self.observations),
            "records": [record.to_dict() for record in self.records],
            "obstructions": self.obstructions,
        }


def _reachable_beds(spec: PlanSpec, state: _TrayState) -> list[str]:
    tray = next(t for t in spec.trays if t.id == state.tray_id)
    return sorted(
        bed_id
        for bed_id in tray.rights
        if bed_id != state.position and spec.corridor_open(state.position, bed_id)
    )


def simulate_handover(
    spec: PlanSpec,
    surface: Slice,
    observations: Sequence[bool],
    occupancy: Mapping[int, frozenset[str]],
    gardeners: int,
) -> HandoverSimulation:
    """Execute one realized day online, honoring current terrain and realities.

    At each beat the engine keeps the first joint action that stays robust for
    every weather possibility matching the observations so far, restricted by
    corridors, on-duty gardeners and beds already claimed by other clients.
    A move consumes the beat and never counts as sunshine.  Every per-tray beat
    (including every blocker's source blocks) is recorded for later read-back.
    """

    if len(observations) != spec.n_beats or not all(isinstance(o, bool) for o in observations):
        raise AdoptionError(
            "bad_request", "observations must be one sunny bool per contracted beat"
        )
    if not is_int(gardeners) or gardeners < 0:
        raise AdoptionError("bad_request", "gardeners must be a non-negative integer")
    engine = SunEngine(spec, surface)
    states = tuple(_TrayState(tray.id, tray.initial, 0, False) for tray in spec.trays)
    belief = frozenset(range(len(spec.weather)))
    records: list[BeatRecord] = []
    obstructions: list[dict[str, Any]] = []
    for beat in range(1, spec.n_beats + 1):
        positions = {state.tray_id: state.position for state in states}
        claimed = occupancy.get(beat, frozenset())
        # What the signed contract would permit, versus the crew actually on duty.
        contracted = engine._joint_actions(positions, spec.gardeners)
        candidates = engine._joint_actions(positions, gardeners)
        contracted_moves = {
            (m.tray_id, m.destination)
            for moves in contracted for m in moves if m.moved
        }
        available_moves = {
            (m.tray_id, m.destination)
            for moves in candidates for m in moves if m.moved
        }
        unfinished = {state.tray_id: state for state in states if not state.done}
        for tray_id, destination in sorted(contracted_moves - available_moves):
            state = unfinished.get(tray_id)
            if state is None or destination in claimed:
                # Completed trays need no crew; claimed beds are reported below.
                continue
            here = engine._shade[(state.position, beat)]
            here_sunny = bool(observations[beat - 1]) and not here.blocked
            if here_sunny:
                continue  # staying already scores sunshine; a move is not needed
            obstructions.append(
                {"tray": tray_id, "beat": beat, "code": "no_gardener",
                 "detail": {"gardeners_on_duty": gardeners,
                            "contracted_gardeners": spec.gardeners, "to": destination}}
            )
        legal: list[tuple[Move, ...]] = []
        rejected: list[dict[str, Any]] = []
        for moves in candidates:
            reasons = [
                {"tray": move.tray_id, "beat": beat,
                 "code": "bed_occupied" if move.destination in claimed else "no_corridor",
                 "detail": ({"bed": move.destination} if move.destination in claimed
                            else {"from": positions[move.tray_id], "to": move.destination})}
                for move in moves
                if move.moved and (
                    move.destination in claimed
                    or not spec.corridor_open(positions[move.tray_id], move.destination)
                )
            ]
            if reasons:
                rejected.extend(reasons)
            else:
                legal.append(moves)
        if not legal:
            # Every on-duty option was a move into a claimed bed: keep position.
            for state in states:
                reachable = _reachable_beds(spec, state)
                blocked = [bed_id for bed_id in reachable if bed_id in claimed]
                if blocked:
                    obstructions.append(
                        {"tray": state.tray_id, "beat": beat, "code": "bed_occupied",
                         "detail": {"claimed": sorted(claimed)}}
                    )
            legal = [tuple(Move(state.tray_id, state.position, False) for state in states)]
        else:
            # Surface claimed-bed rejections even though a stay action stayed legal.
            obstructions.extend(r for r in rejected if r["code"] == "bed_occupied")
        chosen = _choose_robust(engine, spec, states, belief, beat, legal, bool(observations[beat - 1]))
        destinations = {m.tray_id: m.destination for m in chosen}
        moved_flags = {m.tray_id: m.moved for m in chosen}
        next_states: list[_TrayState] = []
        for state in states:
            tray = next(t for t in spec.trays if t.id == state.tray_id)
            pos = destinations[state.tray_id]
            moved = moved_flags[state.tray_id]
            shade = engine._shade[(pos, beat)]
            weather_sunny = bool(observations[beat - 1])
            # A bed promised to another client cannot serve this tray, whether it
            # is fixed in place or merely failed to relocate: the beat never counts.
            bed_claimed = pos in claimed
            sunny = (not moved) and weather_sunny and not shade.blocked and not bed_claimed
            streak = state.streak + 1 if sunny else 0
            done = state.done or (not state.done and streak >= tray.sun_beats)
            if not state.done and bed_claimed:
                obstructions.append(
                    {"tray": tray.id, "beat": beat, "code": "bed_occupied",
                     "detail": {"bed": pos}}
                )
            elif not state.done and not moved and weather_sunny and shade.blocked:
                obstructions.append(
                    {"tray": tray.id, "beat": beat, "code": "shaded",
                     "detail": {"bed": pos, "blockers": [b.to_dict() for b in shade.blockers]}}
                )
            records.append(
                BeatRecord(
                    tray_id=tray.id,
                    beat=beat,
                    bed_id=pos,
                    moved=moved,
                    weather_sunny=weather_sunny,
                    shaded=shade.blocked,
                    sun_counted=sunny,
                    streak=streak,
                    achieved=done,
                    blockers=shade.blockers,
                )
            )
            next_states.append(_TrayState(state.tray_id, pos, streak, done))
        states = tuple(next_states)
        # Restrict the belief to days matching what was just observed.
        refined = frozenset(
            i for i in belief if spec.weather[i].beats[beat - 1] == observations[beat - 1]
        )
        if refined:
            belief = refined
    ok = all(state.done for state in states)
    return HandoverSimulation(
        ok=ok,
        observations=tuple(bool(o) for o in observations),
        records=records,
        obstructions=obstructions,
    )


def _choose_robust(
    engine: SunEngine,
    spec: PlanSpec,
    states: tuple[_TrayState, ...],
    belief: frozenset[int],
    beat: int,
    legal: Sequence[tuple[Move, ...]],
    observed_sunny: bool,
) -> tuple[Move, ...]:
    """First legal action whose every observation branch stays feasible."""

    grouped = engine._group_by_observation(belief, beat)
    for moves in legal:
        if all(
            engine._feasible(
                beat + 1,
                engine._apply(states, moves, spec.weather[next(iter(group))], beat),
                group,
            )
            for group in grouped.values()
        ):
            return moves
    # Reality (occupancy, gardener shortage, a new terrain version) killed every
    # robust option: fall back to a greedy action so the ledger still records
    # exactly where each contracted sun run was lost.
    matching = [
        spec.weather[i]
        for i in belief
        if spec.weather[i].beats[beat - 1] == observed_sunny
    ]
    pattern = matching[0] if matching else spec.weather[next(iter(belief))]
    return engine._greedy(states, {s.tray_id: s.position for s in states}, pattern, beat)
