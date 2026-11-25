"""Tests for the overprint ("接印") engine, studio store and CLI material.

Run with::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from slabwater.catalog import Catalog
from slabwater.overprint import (
    Evaluation,
    OverprintError,
    Plate,
    Registration,
    Sheet,
    evaluate,
    infer_shift,
    inked_cells,
    orient,
    parse_plates,
    parse_shift_list,
    parse_sheets,
    project,
)
from slabwater.studio import Studio

ROOT = Path(__file__).resolve().parent.parent
MATERIAL = ROOT / "materials" / "overprint" / "input.json"


def layers_of(catalog: Catalog, versions, inks, orientations=None, depths=None):
    orientations = orientations or ["identity", "identity"]
    depths = depths or [0, 0]
    return [
        (
            Plate(i + 1, versions[i], inks[i], orientations[i], depths[i]),
            catalog.get_slice(versions[i]),
        )
        for i in range(2)
    ]


def order(versions=(1, 2), inks=("blue", "yellow"), **overrides):
    base = {
        "id": "TEST-ORDER",
        "plates": [
            {"version": versions[0], "ink": inks[0]},
            {"version": versions[1], "ink": inks[1]},
        ],
        "allowed_shifts": [[0, 0]],
        "ink_overlay": ["blue", "yellow"],
        "required_colors": ["blue", "yellow", "white"],
        "process": "pin",
        "sheets": [{"id": "only-sheet", "paper_rejects": []}],
    }
    base.update(overrides)
    return base


class OverprintEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.catalog = Catalog(self.dir / "catalog.db", 1, 3, 9)
        self.catalog.apply_batch(
            [{"tile_id": "PLATE", "revision": 1, "row": 0, "col": 0, "values": [[2, 1, 0]]}]
        )
        self.catalog.apply_batch(
            [{"tile_id": "PLATE", "revision": 2, "row": 0, "col": 0, "values": [[0, 2, 0]]}]
        )
        self.catalog.apply_batch(
            [{"tile_id": "PLATE", "revision": 3, "row": 0, "col": 0, "values": [[2, 1, 0]]}]
        )

    def tearDown(self) -> None:
        self.catalog.close()
        self.tmp.cleanup()

    # -- inking rules ------------------------------------------------------
    def test_depth_zero_only_highest_cell(self):
        self.assertEqual(inked_cells([[2, 1, 0]], 0), frozenset({(0, 0)}))

    def test_depth_admits_cells_within_band(self):
        self.assertEqual(inked_cells([[2, 1, 0]], 1), frozenset({(0, 0), (0, 1)}))
        self.assertEqual(inked_cells([[2, 1, 0]], 9), frozenset({(0, 0), (0, 1), (0, 2)}))

    def test_feet_are_inked_independently(self):
        # Two feet: {(0,0)} and {(0,1),(0,2)}.  Each has its own max.
        feet = [((0, 0),), ((0, 1), (0, 2))]
        self.assertEqual(
            inked_cells([[2, 1, 0]], 0, feet), frozenset({(0, 0), (0, 1)})
        )

    def test_foot_must_be_four_connected(self):
        plates = parse_plates(order())
        disconnected = Plate(
            plates[0].index, plates[0].version, plates[0].ink,
            plates[0].orientation, plates[0].depth,
            feet=(((0, 0), (0, 2)),),
        )
        layers = [
            (disconnected, self.catalog.get_slice(1)),
            (plates[1], self.catalog.get_slice(2)),
        ]
        with self.assertRaises(OverprintError) as ctx:
            evaluate(layers, order())
        self.assertEqual(ctx.exception.code, "foot_not_connected")

    # -- orientation -------------------------------------------------------
    def test_orient_mirrors_and_rotates(self):
        self.assertEqual(orient(0, 0, 1, 3, "flip_h"), (0, 2))
        self.assertEqual(orient(0, 2, 1, 3, "flip_h"), (0, 0))
        self.assertEqual(orient(0, 0, 1, 3, "rot180"), (0, 2))
        self.assertEqual(project((0, 0), "flip_h", (0, 1), 1, 3), (0, 3))

    def test_true_orientation_maps_contact_to_paper(self):
        layers = layers_of(
            self.catalog, (1, 2), ("blue", "yellow"), orientations=["flip_h", "identity"]
        )
        result = evaluate(layers, order())
        self.assertTrue(result.ok, result.reason)
        # Blue plate [2,1,0] mirrored -> high cell lands on paper col 2.
        self.assertIn((0, 2), result.sheet.passes[0].deposits)

    # -- core two-colour scenarios ----------------------------------------
    def test_base_two_color_is_printable(self):
        layers = layers_of(self.catalog, (1, 2), ("blue", "yellow"))
        result = evaluate(layers, order())
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.sheet.colors, (("blue", "yellow", "white"),))
        self.assertEqual(result.sheet.bleed_cells, ())
        self.assertEqual(result.sheet.source_blocks, {1: ("PLATE@1",), 2: ("PLATE@2",)})

    def test_changed_second_plate_covers_blue(self):
        # Second plate becomes [2,1,0]: yellow lands on the blue cell -> bleed.
        layers = layers_of(self.catalog, (1, 3), ("blue", "yellow"))
        result = evaluate(layers, order(versions=(1, 3)))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "order_not_met")
        self.assertIn((0, 0), result.sheet.bleed_cells)
        self.assertIn("blue", result.sheet.missing_colors)

    def test_second_plate_restored_but_sheet_rejects_middle_yellow(self):
        layers = layers_of(self.catalog, (1, 2), ("blue", "yellow"))
        rejected = order(
            sheets=[
                {"id": "only-sheet", "paper_rejects": [{"cell": [0, 1], "inks": ["yellow"]}]}
            ]
        )
        result = evaluate(layers, rejected)
        self.assertFalse(result.ok)
        self.assertIn("yellow", result.sheet.missing_colors)

    def test_overlay_precedence_yellow_covers_blue(self):
        # Directly drive both inks onto the same paper cell via compositing.
        from slabwater.overprint import PassResult, _compose

        blue = PassResult(1, 1, "blue", "identity", 0, (0, 0), (), {(0, 0): (0, 0)}, {}, (), ())
        yellow = PassResult(2, 2, "yellow", "identity", 0, (0, 0), (), {(0, 0): (0, 0)}, {}, (), ())
        colors, _, bleed = _compose([blue, yellow], {"blue": 0, "yellow": 1}, 1, 1)
        self.assertEqual(colors, (("yellow",),))
        self.assertEqual(bleed, ((0, 0),))

    # -- rigid placement ---------------------------------------------------
    def test_single_pass_uses_one_shift_for_every_cell(self):
        layers = layers_of(self.catalog, (1, 2), ("blue", "yellow"))
        result = evaluate(layers, order(allowed_shifts=[[0, 1]]))
        self.assertTrue(result.ok, result.reason)
        # Every blue deposit shares the same shift (0,1) from plate col -> paper.
        deposit_cells = sorted(result.sheet.passes[0].deposits)
        self.assertEqual(deposit_cells, [(0, 1)])

    # -- process selection -------------------------------------------------
    def test_full_edge_forbids_pin_process(self):
        layers = layers_of(self.catalog, (1, 2), ("blue", "yellow"))
        with self.assertRaises(OverprintError) as ctx:
            evaluate(layers, order(process="pin", require_full_edge=True))
        self.assertEqual(ctx.exception.code, "full_edge_requires_clamp")

    def test_pin_keys_both_passes_to_same_shift(self):
        layers = layers_of(self.catalog, (1, 2), ("blue", "yellow"))
        result = evaluate(layers, order(process="pin", allowed_shifts=[[0, 0], [0, 1]]))
        self.assertTrue(result.ok)
        self.assertEqual(result.shift, (0, 0))

    # -- clamp registration ------------------------------------------------
    def _clamp_layers(self):
        # Fresh 1x3 plates picked so the printable shift is (0,1), not (0,0).
        self.catalog.apply_batch(
            [{"tile_id": "PLATE", "revision": 4, "row": 0, "col": 0, "values": [[0, 1, 0]]}]
        )
        self.catalog.apply_batch(
            [{"tile_id": "PLATE", "revision": 5, "row": 0, "col": 0, "values": [[1, 0, 1]]}]
        )
        return layers_of(self.catalog, (4, 5), ("blue", "yellow"))

    def _clamp_order(self, marks, true_shift, clamp_cells):
        return order(
            versions=(4, 5),
            process="clamp",
            require_full_edge=True,
            allowed_shifts=[[0, 0], [0, 1]],
            registration={
                "marks": [{"cell": list(c), "ink": ink} for c, ink in marks],
                "true_shift": list(true_shift),
                "clamp_cells": [list(c) for c in clamp_cells],
            },
        )

    def test_clamp_commits_when_one_dot_hidden(self):
        layers = self._clamp_layers()
        marks = [((0, 0), "A"), ((0, 1), "B")]
        result = evaluate(layers, self._clamp_order(marks, (0, 1), [(0, 2)]))
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.shift, (0, 1))
        self.assertTrue(result.registration["committable"])
        self.assertEqual(result.registration["hidden_marks"], 1)
        self.assertEqual(result.sheet.colors, (("white", "yellow", "blue"),))

    def test_clamp_ambiguous_when_all_dots_hidden(self):
        layers = self._clamp_layers()
        marks = [((0, 0), "A"), ((0, 1), "B")]
        result = evaluate(
            layers, self._clamp_order(marks, (0, 1), [(0, 0), (0, 1), (0, 2), (0, 3)])
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "registration_ambiguous")
        self.assertGreater(len(result.registration["candidate_shifts"]), 1)

    def test_same_visible_dots_yield_same_adjustment(self):
        # Two different true shifts collide on the same visible dots; the
        # inferred adjustment must be identical (a pure function of the sight).
        plate = Plate(1, 1, "blue", "identity", 0)
        allowed = [(0, 0), (0, 1), (0, 2)]
        marks = (((0, 0), "A"), ((0, 1), "A"), ((0, 2), "A"))
        clamp = frozenset({(0, 0), (0, 3)})
        a = infer_shift(Registration(marks, (0, 0), clamp), plate, allowed, 1, 5)
        b = infer_shift(Registration(marks, (0, 1), clamp), plate, allowed, 1, 5)
        self.assertEqual(a["observed_marks"], b["observed_marks"])
        self.assertEqual(a["candidate_shifts"], b["candidate_shifts"])
        self.assertEqual(a["committed_shift"], b["committed_shift"])

    def test_clamp_records_trial_shifts_and_edges(self):
        layers = self._clamp_layers()
        marks = [((0, 0), "A"), ((0, 1), "B")]
        result = evaluate(layers, self._clamp_order(marks, (0, 1), [(0, 2)]))
        self.assertEqual(result.registration["trial_shifts"], 2)
        self.assertIn("top", result.registration["clampable_edges"])

    # -- limits ------------------------------------------------------------
    def test_at_most_two_sheets(self):
        layers = layers_of(self.catalog, (1, 2), ("blue", "yellow"))
        with self.assertRaises(OverprintError) as ctx:
            evaluate(layers, order(sheets=[{"id": f"s{i}"} for i in range(3)]))
        self.assertEqual(ctx.exception.code, "too_many_sheets")

    def test_surface_at_most_four_cells_per_side(self):
        big = Catalog(self.dir / "big.db", 1, 5, 9)
        try:
            big.apply_batch(
                [{"tile_id": "B", "revision": 1, "row": 0, "col": 0, "values": [[0, 1, 0, 1, 0]]}]
            )
            big.apply_batch(
                [{"tile_id": "B", "revision": 2, "row": 0, "col": 0, "values": [[1, 0, 1, 0, 1]]}]
            )
            layers = layers_of(big, (1, 2), ("blue", "yellow"))
            with self.assertRaises(OverprintError) as ctx:
                evaluate(layers, order())
            self.assertEqual(ctx.exception.code, "surface_too_large")
        finally:
            big.close()


class StudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.catalog = Catalog(self.dir / "catalog.db", 1, 3, 9)
        for rev, values in ((1, [2, 1, 0]), (2, [0, 2, 0]), (3, [2, 1, 0])):
            self.catalog.apply_batch(
                [{"tile_id": "PLATE", "revision": rev, "row": 0, "col": 0, "values": [values]}]
            )
        self.studio = Studio(self.dir / "studio.db", self.catalog)

    def tearDown(self) -> None:
        self.studio.close()
        self.catalog.close()
        self.tmp.cleanup()

    def test_print_and_reopen_artifacts(self):
        result = self.studio.print_order(order())
        self.assertTrue(result.ok)
        self.assertEqual(result.artifact["plate_versions"], [1, 2])
        self.assertEqual(result.artifact["sheet"]["colors"], [["blue", "yellow", "white"]])
        self.assertEqual(result.artifact["sheet"]["source_blocks"], {"1": ["PLATE@1"], "2": ["PLATE@2"]})
        reopened = self.studio.reopen(result.print_id)
        self.assertEqual(reopened.artifact, result.artifact)

    def test_switch_version_does_not_rewrite_first_layer(self):
        printed = self.studio.print_order(order())
        original = self.studio.get_print(printed.print_id)
        # Add a new plate version after the print and re-open.
        self.catalog.apply_batch(
            [{"tile_id": "PLATE", "revision": 9, "row": 0, "col": 0, "values": [[9, 9, 9]]}]
        )
        self.assertEqual(self.studio.get_print(printed.print_id), original)
        self.assertEqual(self.studio.print_for_order(printed.order_id), original)

    def test_idempotent_replay(self):
        first = self.studio.print_order(order())
        second = self.studio.print_order(order())
        self.assertTrue(second.replayed)
        self.assertEqual(first.print_id, second.print_id)
        self.assertEqual(first.artifact, second.artifact)

    def test_idempotency_conflict(self):
        self.studio.print_order(order())
        changed = order()
        changed["required_colors"] = ["blue"]
        with self.assertRaises(OverprintError) as ctx:
            self.studio.print_order(changed)
        self.assertEqual(ctx.exception.code, "idempotency_conflict")

    def test_rejected_order_has_no_print_row(self):
        result = self.studio.print_order(order(versions=(1, 3)))
        self.assertEqual(result.status, "rejected")
        stored = self.studio.outcome_for_order(result.order_id)
        self.assertEqual(stored["status"], "rejected")
        self.assertNotIn(result.order_id, {p["order_id"] for p in self.studio.list_prints()})

    def test_unknown_plate_version(self):
        result = self.studio.print_order(order(versions=(1, 99)))
        self.assertEqual(result.status, "error")
        self.assertNotIn(result.order_id, {p["order_id"] for p in self.studio.list_prints()})

    def test_verify_reproduces_and_detects_tampering(self):
        printed = self.studio.print_order(order())
        good = self.studio.verify(printed.print_id)
        self.assertTrue(good["ok"], good["messages"])
        tampered = json.loads(json.dumps(printed.artifact))
        tampered["sheet"]["colors"][0][0] = "yellow"
        self.studio._conn.execute(
            "UPDATE prints SET artifact=? WHERE print_id=?",
            (json.dumps(tampered), printed.print_id),
        )
        bad = self.studio.verify(printed.print_id)
        self.assertFalse(bad["ok"])
        self.assertFalse(bad["checks"]["sheet_matches"])


class MaterialTests(unittest.TestCase):
    def test_material_runs_end_to_end(self):
        from slabwater.overprint_cli import run_overprint_material

        report = run_overprint_material(MATERIAL, Path(tempfile.mkdtemp()) / "op")
        statuses = {entry["order"]["id"]: entry["status"] for entry in report["orders"]}
        self.assertEqual(statuses["TWO-COLOR"], "printed")
        self.assertEqual(statuses["CHANGED-PLATE"], "rejected")
        self.assertEqual(statuses["REJECT-MIDDLE-YELLOW"], "rejected")
        self.assertEqual(statuses["CLAMP-FULL-EDGE"], "printed")
        prints = {p["order_id"] for p in report["prints"]}
        self.assertEqual(prints, {"TWO-COLOR", "CLAMP-FULL-EDGE"})


if __name__ == "__main__":
    unittest.main()
