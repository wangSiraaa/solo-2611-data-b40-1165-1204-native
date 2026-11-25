"""Deterministic winter seedbed material generator.

Builds a small fully-covered terrain (a north wall plus seedbeds) and the
published sun-direction cards / whole-day weather possibilities used by the
adoption tests.  The generator is seeded so the material is reproducible.

The terrain is a 1 x W strip:

* the outer columns are north walls whose elevation the incoming rays cross;
* the inner columns are the rentable beds, one bed per cell.

A mid-path cell whose real elevation *reaches* the ray
(``elevation >= ray_height``) blocks that beat.  Cards are written so that:

* the east bed's rays clear its wall in the morning and are reached in the
  afternoon;
* the west bed's rays are reached by its wall in the morning and clear in the
  afternoon.

A published revision that raises a wall therefore changes shading without
touching any card, and every blocker still records the source tile revision.
"""

from __future__ import annotations

from typing import Any

WALL_HEIGHT = 1
RAISED_WALL_HEIGHT = 2
CLEAR_RAY = 3      # above both wall versions -> never blocked by that wall
BLOCK_RAY = 1      # reached by the original wall (height 1) -> shaded
WALL_SENSITIVE_RAY = 2  # clears wall height 1; a wall raised to 2 reaches it


def _bed_cards(bed_id: str, wall_cell: list[int], sunny_in_morning: bool,
               n_beats: int, sensitive_morning: bool = False) -> list[dict[str, Any]]:
    """Cards whose morning/afternoon rays open one half of the day on this bed.

    When ``sensitive_morning`` is set the opening morning rays travel at height
    2 rather than 3: the original height-1 wall still lets them through, but a
    published wall revision to height 2 blocks them, tying the verdict to a real
    frozen terrain version.
    """

    cards: list[dict[str, Any]] = []
    morning = n_beats // 2
    for beat in range(1, n_beats + 1):
        is_morning = beat <= morning
        opens = is_morning if sunny_in_morning else not is_morning
        if opens:
            ray = WALL_SENSITIVE_RAY if (sensitive_morning and is_morning) else CLEAR_RAY
        else:
            ray = BLOCK_RAY
        cards.append(
            {"bed_id": bed_id, "beat": beat, "path": [wall_cell], "ray_height": ray}
        )
    return cards


def make_winter_material(seed: int = 772, n_beats: int = 4) -> dict[str, Any]:
    """East bed opens in the morning, west bed in the afternoon; one north wall.

    The wall sits at column 0 with height 1.  East morning rays travel at height
    2 and clear it; a revised wall of height 2 reaches those rays and blocks them.
    West afternoon rays travel at height 3, above either wall version.
    """

    width = 4  # wall | E bed | W bed | spare
    wall_cell = [0, 0]
    east_cell = [0, 1]
    west_cell = [0, 2]
    initial_tile = {
        "tile_id": "NORTH",
        "revision": 1,
        "row": 0,
        "col": 0,
        "values": [[WALL_HEIGHT, 0, 0, 0]],
    }
    raised_wall = {
        "tile_id": "NORTH",
        "revision": 2,
        "row": 0,
        "col": 0,
        # Only the wall changes: it now reaches the height-2 east morning rays.
        "values": [[RAISED_WALL_HEIGHT, 0, 0, 0]],
    }
    cards = (
        # East morning rays are height 2: they pass the height-1 wall but a
        # raised height-2 wall reaches them -- the sign verdict then changes.
        _bed_cards("E", wall_cell, sunny_in_morning=True, n_beats=n_beats,
                   sensitive_morning=True)
        + _bed_cards("W", wall_cell, sunny_in_morning=False, n_beats=n_beats)
    )
    return {
        "seed": seed,
        "height": 1,
        "width": width,
        "ceiling": 6,
        "initial_tiles": [initial_tile],
        "raised_wall": raised_wall,
        "beds": [
            {"id": "E", "cell": east_cell},
            {"id": "W", "cell": west_cell},
        ],
        "cards": cards,
        "corridors": [["E", "W"]],
        "gardeners": 1,
        # Whole-day possibilities: a fully clear day and a cloudy morning that
        # leaves only the afternoon.  The same pattern applies to every bed.
        "weather": [
            {"id": "CLEAR", "beats": [True] * n_beats},
            {"id": "CLOUD_AM", "beats": [False] + [True] * (n_beats - 1)},
        ],
    }
