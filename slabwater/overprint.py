"""Studio overprint / registration ("接印") engine on a tiled-elevation catalog.

A *plate* ("版") is one frozen catalog version: its per-cell heights are the
plate's relief.  A two-pass relief print runs each plate through a pressure foot
("压力脚") and transfers ink onto a sheet of paper:

* **Foot + depth.**  A foot is a four-neighbour-connected set of plate cells;
  by default each plate is a single foot.  Within a foot the highest cell inks
  first, and any cell whose height is within that pass's press-in depth
  ("压入深度") of the foot maximum also inks: ``height >= max - depth``.
* **True orientation.**  Every contact point is mapped to paper under the
  plate's real orientation (identity / mirror / rotate) and *one* paper shift.
* **Rigid placement.**  A single pass uses one shift for all of its cells.  You
  may not choose a different position per cell -- the second layer's coverage is
  fixed by where the same sheet actually sat during that pass.
* **Ink overlay.**  When inks from several layers land on one paper cell, the
  ink later in the overlay table covers the earlier one ("黄盖蓝").  A cell that
  receives two or more distinct inks is a *bleed cell* ("串色格").
* **Sheet rejection.**  A deliverable sheet may refuse a given ink at given
  cells; a refused ink is simply not deposited there.

Two registration processes are supported:

``pin``
    Punch holes in the margin; both passes key to the same holes, so both use
    the same shift and the second pass is stable.  The punched area can no
    longer deliver a complete image edge.

``clamp``
    Keep the full paper edge; align the second plate from the registration dots
    left by the first pass.  Only the dots outside the clamp are visible, and
    the same visible dots must always yield the same adjustment.

All quantities are integers over a discrete grid; four-neighbour connectivity.
This is a synthetic verifier, not a model of any real press.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .model import Cell, Slice, is_int
WHITE = "white"

#: Allowed plate orientations (paper is observed from the printed side).
ORIENTATIONS = ("identity", "flip_h", "flip_v", "rot180")


class OverprintError(Exception):
    """A malformed overprint order."""

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "message": self.message, "details": self.details}


# ---------------------------------------------------------------------------
# Geometry primitives
# ---------------------------------------------------------------------------


def orient(row: int, col: int, height: int, width: int, orientation: str) -> Cell:
    """Apply a plate orientation to a plate-local cell."""

    if orientation == "identity":
        return row, col
    if orientation == "flip_h":
        return row, width - 1 - col
    if orientation == "flip_v":
        return height - 1 - row, col
    if orientation == "rot180":
        return height - 1 - row, width - 1 - col
    raise OverprintError("bad_orientation", f"unknown plate orientation {orientation!r}")


def project(
    cell: Cell,
    orientation: str,
    shift: tuple[int, int],
    height: int,
    width: int,
) -> Cell:
    """Map a plate-local contact cell to a paper cell (orientation, then shift)."""

    r, c = orient(cell[0], cell[1], height, width, orientation)
    return r + shift[0], c + shift[1]


def inked_cells(
    heights: Sequence[Sequence[int]],
    depth: int,
    feet: Sequence[Sequence[Cell]] | None = None,
) -> frozenset[Cell]:
    """Plate-local cells that take ink at the given press depth.

    ``feet`` defaults to a single foot covering the whole plate.  Each foot is
    handled independently: a cell inks iff its height is within ``depth`` of that
    foot's maximum height.
    """

    h = len(heights)
    w = len(heights[0]) if h else 0
    if feet is None:
        feet = [tuple((r, c) for r in range(h) for c in range(w))]
    result: set[Cell] = set()
    for foot in feet:
        if not foot:
            continue
        top = max(heights[r][c] for r, c in foot)
        threshold = top - depth
        for r, c in foot:
            if heights[r][c] >= threshold:
                result.add((r, c))
    return frozenset(result)


# ---------------------------------------------------------------------------
# Normalised order parts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Plate:
    """One actual plate in a pass.

    ``feet`` is a list of four-neighbour-connected feet, each a list of
    plate-local cells.  ``None`` means a single foot spanning the whole plate.
    """

    index: int
    version: int
    ink: str
    orientation: str = "identity"
    depth: int = 0
    feet: tuple[tuple[Cell, ...], ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "pass": self.index,
            "version": self.version,
            "ink": self.ink,
            "orientation": self.orientation,
            "depth": self.depth,
        }
        if self.feet is not None:
            data["feet"] = [[list(c) for c in foot] for foot in self.feet]
        return data


@dataclass(frozen=True)
class Registration:
    """First-pass registration dots for the clamp process.

    ``marks`` are given in the *first plate's* local coordinates together with
    the ink colour of the dot.  ``true_shift`` is where the sheet actually sat
    for the first pass.  ``clamp_cells`` are paper cells hidden by the clamp, so
    a dot landing there is invisible to the curator.
    """

    marks: tuple[tuple[Cell, str], ...]
    true_shift: tuple[int, int]
    clamp_cells: frozenset[Cell] = frozenset()


@dataclass(frozen=True)
class Sheet:
    """A candidate deliverable sheet with its own ink refusals."""

    sheet_id: str
    paper_rejects: tuple[tuple[Cell, frozenset[str]], ...] = ()

    def rejects(self) -> dict[Cell, frozenset[str]]:
        return {cell: inks for cell, inks in self.paper_rejects}


@dataclass
class PassResult:
    pass_no: int
    version: int
    ink: str
    orientation: str
    depth: int
    shift: tuple[int, int]
    inked: tuple[Cell, ...]
    deposits: dict[Cell, Cell]  # paper cell -> plate-local source cell
    provenance: dict[Cell, tuple[dict[str, Any], ...]]
    rejected: tuple[Cell, ...]
    offpaper: tuple[Cell, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "pass": self.pass_no,
            "version": self.version,
            "ink": self.ink,
            "orientation": self.orientation,
            "depth": self.depth,
            "shift": list(self.shift),
            "inked": [list(c) for c in self.inked],
            "deposits": {f"{p[0]},{p[1]}": list(src) for p, src in sorted(self.deposits.items())},
            "provenance": [
                {"paper": list(paper), "sources": list(refs)}
                for paper, refs in sorted(self.provenance.items())
            ],
            "rejected": [list(c) for c in self.rejected],
            "offpaper": [list(c) for c in self.offpaper],
        }


@dataclass
class SheetResult:
    """One sheet rendered for one plate pair at one shift."""

    sheet_id: str
    colors: tuple[tuple[str, ...], ...]
    paper_inks: tuple[tuple[tuple[str, ...], ...], ...]
    bleed_cells: tuple[Cell, ...]
    present_colors: tuple[str, ...]
    missing_colors: tuple[str, ...]
    passes: tuple[PassResult, ...]
    source_blocks: dict[int, tuple[str, ...]]

    @property
    def printable(self) -> bool:
        return not self.missing_colors and not self.bleed_cells

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet": self.sheet_id,
            "printable": self.printable,
            "colors": [list(row) for row in self.colors],
            "present_colors": list(self.present_colors),
            "missing_colors": list(self.missing_colors),
            "bleed_cells": [list(c) for c in self.bleed_cells],
            "passes": [p.to_dict() for p in self.passes],
            "source_blocks": {str(k): list(v) for k, v in self.source_blocks.items()},
        }


@dataclass
class Evaluation:
    ok: bool
    reason: str | None
    process: str | None
    required_colors: tuple[str, ...]
    chosen_sheet: str | None
    sheet: SheetResult | None
    shift: tuple[int, int] | None
    sheets: tuple[SheetResult, ...]
    registration: dict[str, Any] | None
    stats: dict[str, Any]

    def to_dict(self, include_attempts: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ok": self.ok,
            "reason": self.reason,
            "process": self.process,
            "required_colors": list(self.required_colors),
            "chosen_sheet": self.chosen_sheet,
            "shift": list(self.shift) if self.shift is not None else None,
            "registration": self.registration,
            "stats": self.stats,
            "sheet": self.sheet.to_dict() if self.sheet is not None else None,
        }
        if include_attempts:
            data["sheets"] = [s.to_dict() for s in self.sheets]
        return data


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _provenance(surface: Slice, cell: Cell) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "tile_id": ref.tile_id,
            "revision": ref.tile_revision,
            "local_row": ref.local_row,
            "local_col": ref.local_col,
        }
        for ref in surface.sources.get(cell, ())
    )


def _render_pass(
    plate: Plate,
    surface: Slice,
    shift: tuple[int, int],
    rejects: Mapping[Cell, frozenset[str]],
) -> PassResult:
    height, width = surface.height, surface.width
    inked = inked_cells(surface.heights, plate.depth, plate.feet)
    deposits: dict[Cell, Cell] = {}
    provenance: dict[Cell, tuple[dict[str, Any], ...]] = {}
    rejected: list[Cell] = []
    offpaper: list[Cell] = []
    for src in sorted(inked):
        paper = project(src, plate.orientation, shift, height, width)
        if not (0 <= paper[0] < height and 0 <= paper[1] < width):
            offpaper.append(paper)
            continue
        if plate.ink in rejects.get(paper, frozenset()):
            rejected.append(paper)
            continue
        deposits[paper] = src
        provenance[paper] = _provenance(surface, src)
    return PassResult(
        pass_no=plate.index,
        version=plate.version,
        ink=plate.ink,
        orientation=plate.orientation,
        depth=plate.depth,
        shift=shift,
        inked=tuple(sorted(inked)),
        deposits=deposits,
        provenance=provenance,
        rejected=tuple(sorted(set(rejected))),
        offpaper=tuple(sorted(set(offpaper))),
    )


def _compose(
    passes: Sequence[PassResult],
    overlay_rank: Mapping[str, int],
    height: int,
    width: int,
) -> tuple[
    tuple[tuple[str, ...], ...],
    tuple[tuple[tuple[str, ...], ...], ...],
    tuple[Cell, ...],
]:
    inks_at: dict[Cell, list[str]] = {}
    for result in passes:
        for paper in result.deposits:
            inks_at.setdefault(paper, []).append(result.ink)
    colors: list[tuple[str, ...]] = []
    bleed: list[Cell] = []
    for r in range(height):
        row: list[str] = []
        for c in range(width):
            inks = inks_at.get((r, c), [])
            if not inks:
                row.append(WHITE)
                continue
            best = max(inks, key=lambda ink: overlay_rank.get(ink, -1))
            row.append(best)
            if len(set(inks)) >= 2:
                bleed.append((r, c))
        colors.append(tuple(row))
    return (
        tuple(colors),
        tuple(tuple(tuple(inks_at.get((r, c), ())) for c in range(width)) for r in range(height)),
        tuple(bleed),
    )


def _source_blocks(passes: Sequence[PassResult]) -> dict[int, tuple[str, ...]]:
    """Tiles (as ``tile_id@revision``) touched by each layer's contact cells."""

    blocks: dict[int, tuple[str, ...]] = {}
    for result in passes:
        touched: set[str] = set()
        for refs in result.provenance.values():
            for ref in refs:
                touched.add(f"{ref['tile_id']}@{ref['revision']}")
        blocks[result.pass_no] = tuple(sorted(touched))
    return blocks


def _render_sheet(
    sheet: Sheet,
    layers: Sequence[tuple[Plate, Slice]],
    shift: tuple[int, int],
    overlay_rank: Mapping[str, int],
    required: Sequence[str],
) -> SheetResult:
    height, width = layers[0][1].height, layers[0][1].width
    rejects = sheet.rejects()
    passes = tuple(
        _render_pass(plate, surface, shift, rejects) for plate, surface in layers
    )
    colors, paper_inks, bleed = _compose(passes, overlay_rank, height, width)
    present = tuple(sorted({c for row in colors for c in row}))
    missing = tuple(c for c in required if c not in present)
    return SheetResult(
        sheet_id=sheet.sheet_id,
        colors=colors,
        paper_inks=paper_inks,
        bleed_cells=bleed,
        present_colors=present,
        missing_colors=missing,
        passes=passes,
        source_blocks=_source_blocks(passes),
    )


# ---------------------------------------------------------------------------
# Registration inference (clamp strip)
# ---------------------------------------------------------------------------


def infer_shift(
    registration: Registration,
    first_plate: Plate,
    allowed_shifts: Sequence[tuple[int, int]],
    height: int,
    width: int,
) -> dict[str, Any]:
    """Infer the sheet shift from the registration dots visible outside the clamp.

    The studio knows the plate's dot layout and the clamp region (fixed paper
    cells).  The curator only sees dots outside the clamp.  For a candidate
    shift ``s`` each plate dot projects to ``m + s``; dots landing in the clamp
    are hidden and unconstrained, the rest must reproduce exactly what was seen.
    The result is therefore a pure function of the *observation* and the clamp
    region -- never of where the sheet secretly sat.  A shift is committed only
    when exactly one allowed shift is consistent.
    """

    def projected_visible(shift: tuple[int, int]) -> set[tuple[Cell, str]] | None:
        visible: set[tuple[Cell, str]] = set()
        for cell, ink in registration.marks:
            paper = project(cell, first_plate.orientation, shift, height, width)
            if not (0 <= paper[0] < height and 0 <= paper[1] < width):
                return None
            if paper not in registration.clamp_cells:
                visible.add((paper, ink))
        return visible

    true_observed = projected_visible(registration.true_shift)
    assert true_observed is not None, "true_shift must keep every mark on paper"
    observed = true_observed

    consistent: list[tuple[int, int]] = []
    for shift in allowed_shifts:
        candidate = projected_visible(shift)
        if candidate is not None and candidate == observed:
            consistent.append(shift)

    sides: set[str] = set()
    for (r, c), _ in observed:
        if r == 0:
            sides.add("top")
        if r == height - 1:
            sides.add("bottom")
        if c == 0:
            sides.add("left")
        if c == width - 1:
            sides.add("right")
    committable = len(consistent) == 1
    return {
        "observed_marks": sorted([list(cell), ink] for cell, ink in observed),
        "hidden_marks": len(registration.marks) - len(observed),
        "candidate_shifts": [list(s) for s in consistent],
        "committable": committable,
        "committed_shift": list(consistent[0]) if committable else None,
        "clampable_edges": sorted(sides),
        "trial_shifts": len(allowed_shifts),
    }


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------


def _as_int(value: Any, what: str, *, minimum: int | None = None) -> int:
    if not is_int(value):
        raise OverprintError("bad_request", f"{what} must be an integer")
    if minimum is not None and value < minimum:
        raise OverprintError("bad_request", f"{what} must be >= {minimum}")
    return value


def _as_cell(raw: Any, what: str) -> Cell:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise OverprintError("bad_request", f"{what} must be a [row, col] pair")
    r, c = raw
    return _as_int(r, f"{what} row"), _as_int(c, f"{what} col")


def _as_shift(raw: Any, what: str) -> tuple[int, int]:
    return _as_cell(raw, what)


def parse_plates(request: Mapping[str, Any]) -> list[Plate]:
    raw = request.get("plates")
    if not isinstance(raw, list) or len(raw) != 2:
        raise OverprintError("bad_plates", "an overprint order needs exactly two plates")
    plates: list[Plate] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise OverprintError("bad_plates", f"plate {index} must be an object")
        version = _as_int(item.get("version"), f"plate {index} version", minimum=1)
        ink = item.get("ink")
        if not isinstance(ink, str) or not ink:
            raise OverprintError("bad_plates", f"plate {index} ink must be a non-empty string")
        orientation = item.get("orientation", "identity")
        if orientation not in ORIENTATIONS:
            raise OverprintError("bad_orientation", f"plate {index} has unknown orientation")
        depth = _as_int(item.get("depth", 0), f"plate {index} depth", minimum=0)
        feet = _parse_feet(item.get("feet"), f"plate {index}")
        plates.append(Plate(index, version, ink, orientation, depth, feet))
    return plates


def _parse_feet(raw: Any, what: str) -> tuple[tuple[Cell, ...], ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise OverprintError("bad_feet", f"{what} feet must be a non-empty list of feet")
    feet: list[tuple[Cell, ...]] = []
    for foot_index, foot in enumerate(raw):
        if not isinstance(foot, list) or not foot:
            raise OverprintError("bad_feet", f"{what} foot {foot_index} must be a non-empty list")
        cells = tuple(_as_cell(cell, f"{what} foot cell") for cell in foot)
        if len(set(cells)) != len(cells):
            raise OverprintError("bad_feet", f"{what} foot {foot_index} repeats a cell")
        if not _is_four_connected(set(cells)):
            raise OverprintError(
                "foot_not_connected",
                f"{what} foot {foot_index} is not four-neighbour connected",
            )
        feet.append(tuple(sorted(cells)))
    return tuple(feet)


def _is_four_connected(cells: set[Cell]) -> bool:
    """True when ``cells`` form one four-neighbour-connected region."""

    if not cells:
        return False
    remaining = set(cells)
    start = next(iter(remaining))
    stack = [start]
    remaining.discard(start)
    while stack:
        r, c = stack.pop()
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            neighbour = (r + dr, c + dc)
            if neighbour in remaining:
                remaining.discard(neighbour)
                stack.append(neighbour)
    return not remaining


def _validate_plate_feet(plate: Plate, height: int, width: int) -> None:
    """Validate programmatically built feet (when bypassing ``parse_plates``)."""

    if plate.feet is None:
        return
    for foot_index, foot in enumerate(plate.feet):
        if not foot:
            raise OverprintError("bad_feet", f"plate {plate.index} foot {foot_index} is empty")
        for r, c in foot:
            if not (0 <= r < height and 0 <= c < width):
                raise OverprintError(
                    "foot_out_of_bounds",
                    f"plate {plate.index} foot {foot_index} cell ({r},{c}) is off the plate",
                )
        if not _is_four_connected(set(foot)):
            raise OverprintError(
                "foot_not_connected",
                f"plate {plate.index} foot {foot_index} is not four-neighbour connected",
            )


def parse_shift_list(request: Mapping[str, Any]) -> list[tuple[int, int]]:
    raw = request.get("allowed_shifts", [[0, 0]])
    if not isinstance(raw, list) or not raw:
        raise OverprintError("bad_request", "allowed_shifts must be a non-empty list")
    return sorted({_as_shift(item, "allowed shift") for item in raw})


def parse_sheets(request: Mapping[str, Any]) -> list[Sheet]:
    raw = request.get("sheets")
    if raw is None:
        raw = [{"id": "sheet-1", "paper_rejects": request.get("paper_rejects", [])}]
    if not isinstance(raw, list) or not raw:
        raise OverprintError("bad_request", "sheets must be a non-empty list")
    if len(raw) > 2:
        raise OverprintError("too_many_sheets", "at most two sheets may be ordered")
    sheets: list[Sheet] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise OverprintError("bad_request", f"sheet {index} must be an object")
        sheet_id = item.get("id", f"sheet-{index}")
        if not isinstance(sheet_id, str) or not sheet_id:
            raise OverprintError("bad_request", f"sheet {index} id must be a non-empty string")
        rejects_raw = item.get("paper_rejects", [])
        if not isinstance(rejects_raw, list):
            raise OverprintError("bad_request", f"sheet {index} paper_rejects must be a list")
        rejects: list[tuple[Cell, frozenset[str]]] = []
        for entry in rejects_raw:
            if not isinstance(entry, Mapping):
                raise OverprintError("bad_request", "paper_rejects entries must be objects")
            cell = _as_cell(entry.get("cell"), "paper_reject cell")
            inks = entry.get("inks", [])
            if not isinstance(inks, list) or not all(isinstance(i, str) and i for i in inks):
                raise OverprintError("bad_request", "paper_reject inks must be strings")
            rejects.append((cell, frozenset(inks)))
        sheets.append(Sheet(sheet_id, tuple(rejects)))
    return sheets


def parse_registration(request: Mapping[str, Any]) -> Registration | None:
    raw = request.get("registration")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise OverprintError("bad_request", "registration must be an object")
    marks_raw = raw.get("marks", [])
    if not isinstance(marks_raw, list):
        raise OverprintError("bad_request", "registration marks must be a list")
    marks: list[tuple[Cell, str]] = []
    for entry in marks_raw:
        if not isinstance(entry, Mapping):
            raise OverprintError("bad_request", "registration mark must be an object")
        cell = _as_cell(entry.get("cell"), "registration mark cell")
        ink = entry.get("ink")
        if not isinstance(ink, str) or not ink:
            raise OverprintError("bad_request", "registration mark ink must be a string")
        marks.append((cell, ink))
    true_shift = _as_shift(raw.get("true_shift", [0, 0]), "registration true_shift")
    clamp_raw = raw.get("clamp_cells", [])
    if not isinstance(clamp_raw, list):
        raise OverprintError("bad_request", "registration clamp_cells must be a list")
    clamp_cells = frozenset(_as_cell(c, "clamp cell") for c in clamp_raw)
    return Registration(tuple(marks), true_shift, clamp_cells)


def choose_process(request: Mapping[str, Any]) -> str:
    """Resolve ``process`` against ``require_full_edge``.

    A full image edge cannot come from a punched margin, so a full-edge order
    must use the clamp process; pin holes are only allowed when the edge may be
    sacrificed.
    """

    require_full_edge = bool(request.get("require_full_edge", False))
    requested = request.get("process", "auto")
    if requested not in ("auto", "pin", "clamp"):
        raise OverprintError("bad_process", f"unknown process {requested!r}")
    if requested == "auto":
        return "clamp" if require_full_edge else "pin"
    if require_full_edge and requested == "pin":
        raise OverprintError(
            "full_edge_requires_clamp",
            "a full paper edge is required, but the pin process punches the margin",
        )
    return requested


# ---------------------------------------------------------------------------
# Top-level evaluation
# ---------------------------------------------------------------------------


def evaluate(
    layers: Sequence[tuple[Plate, Slice]],
    request: Mapping[str, Any],
) -> Evaluation:
    """Evaluate one overprint order against two frozen plate surfaces."""

    if len(layers) != 2:
        raise OverprintError("bad_plates", "an overprint order needs exactly two plates")
    height, width = layers[0][1].height, layers[0][1].width
    for _, surface in layers:
        if (surface.height, surface.width) != (height, width):
            raise OverprintError(
                "plate_size_mismatch", "both plates must share the same H x W surface"
            )
    if height > 4 or width > 4:
        raise OverprintError(
            "surface_too_large", "an overprint surface may have at most four cells per side"
        )
    for plate in (plate for plate, _ in layers):
        _validate_plate_feet(plate, height, width)

    plates = [plate for plate, _ in layers]
    surfaces = [surface for _, surface in layers]
    overlay = request.get("ink_overlay", [])
    if not isinstance(overlay, list) or not all(isinstance(i, str) and i for i in overlay):
        raise OverprintError("bad_request", "ink_overlay must be a list of ink names")
    if len(set(overlay)) != len(overlay):
        raise OverprintError("bad_request", "ink_overlay must not repeat an ink")
    overlay_rank = {ink: rank for rank, ink in enumerate(overlay)}
    for plate in plates:
        if plate.ink not in overlay_rank:
            raise OverprintError(
                "ink_not_in_overlay",
                f"plate ink {plate.ink!r} is missing from the ink overlay table",
            )

    required = request.get("required_colors", [])
    if not isinstance(required, list) or not all(isinstance(i, str) and i for i in required):
        raise OverprintError("bad_request", "required_colors must be a list of ink names")
    required = tuple(dict.fromkeys(required))
    allowed = parse_shift_list(request)
    sheets = parse_sheets(request)
    process = choose_process(request)

    stats: dict[str, Any] = {
        "allowed_shifts": [list(s) for s in allowed],
        "trial_shifts": len(allowed),
        "sheets": len(sheets),
    }
    registration_payload: dict[str, Any] | None = None

    # Resolve the shift(s) to try.  Both passes share one shift: pin holes key
    # both passes to one set of holes, and the clamp aligns pass two to pass
    # one's dots.  A single shift per order enforces "no per-cell placement".
    if process == "clamp" and request.get("registration") is not None:
        registration = parse_registration(request)
        assert registration is not None
        registration_payload = infer_shift(registration, plates[0], allowed, height, width)
        stats["trial_shifts"] = registration_payload["trial_shifts"]
        if not registration_payload["committable"]:
            return Evaluation(
                ok=False,
                reason="registration_ambiguous",
                process=process,
                required_colors=required,
                chosen_sheet=None,
                sheet=None,
                shift=None,
                sheets=(),
                registration=registration_payload,
                stats=stats,
            )
        candidate_shifts = [tuple(registration_payload["committed_shift"])]
    else:
        candidate_shifts = list(allowed)

    rendered: list[SheetResult] = []
    best: tuple[tuple[int, int, int, int], tuple[int, int], SheetResult] | None = None
    for shift in candidate_shifts:
        for sheet in sheets:
            result = _render_sheet(sheet, layers, shift, overlay_rank, required)
            rendered.append(result)
            if result.printable:
                score = (
                    len(result.bleed_cells),
                    len(result.missing_colors),
                    shift[0],
                    shift[1],
                )
                if best is None or score < best[0]:
                    best = (score, shift, result)

    stats["evaluated"] = len(rendered)
    if best is not None:
        _, shift, result = best
        return Evaluation(
            ok=True,
            reason=None,
            process=process,
            required_colors=required,
            chosen_sheet=result.sheet_id,
            sheet=result,
            shift=shift,
            sheets=tuple(rendered),
            registration=registration_payload,
            stats=stats,
        )

    # Nothing printable: choose the least-bad rendering to report concrete bleed
    # cells and missing colours.
    worst = min(
        rendered,
        key=lambda s: (len(s.missing_colors), len(s.bleed_cells), s.sheet_id),
    )
    return Evaluation(
        ok=False,
        reason="order_not_met",
        process=process,
        required_colors=required,
        chosen_sheet=worst.sheet_id,
        sheet=worst,
        shift=worst.passes[0].shift if worst.passes else None,
        sheets=tuple(rendered),
        registration=registration_payload,
        stats=stats,
    )
