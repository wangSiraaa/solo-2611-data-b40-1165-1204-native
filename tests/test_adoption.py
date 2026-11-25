"""End-to-end tests for winter seedbed adoption and field hand-over.

Run with::

    python -m unittest discover -s tests -v

The scenarios mirror the task narrative:

* a fixed bed whose two incoming rays only cross the north wall -- it signs when
  the measured wall (1) sits below the rays (2), is rejected once a published
  revision raises the wall to 2, and cannot be handed over when its beat-2 slot
  is already promised to another client with no replacement bed;
* a mobile east/west order whose day type is revealed on the first beat's
  morning, where pushing the tray costs the beat, needs a free corridor and an
  on-duty gardener, and cannot read afternoon weather in advance;
* the four-bed / four-beat / two-tray limits, idempotency, version pinning and
  re-opening a contract to recover its bed rights and sunshine ledger.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from slabwater import AdoptionError, AdoptionRegistry, Catalog
from slabwater.sun import (
    MAX_BEATS,
    MAX_BEDS,
    MAX_TRAYS,
    SunEngine,
    parse_spec,
    shade_card,
)
from slabwater.winter import make_winter_material


class AdoptionBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.material = make_winter_material()
        self.catalog = Catalog(
            self.dir / "catalog.db",
            self.material["height"],
            self.material["width"],
            self.material["ceiling"],
        )
        first = self.catalog.apply_batch(self.material["initial_tiles"])
        self.assertEqual(first.version, 1)
        self.registry = AdoptionRegistry(self.dir / "adoption.db", self.catalog)

    def tearDown(self) -> None:
        self.registry.close()
        self.catalog.close()
        self.tmp.cleanup()

    def raise_wall(self) -> None:
        self.catalog.apply_batch([self.material["raised_wall"]])

    def surface(self, version: int = 1):
        return self.catalog.get_slice(version)

    def east_cards(self):
        return [card for card in self.material["cards"] if card["bed_id"] == "E"]


class ShadingTests(AdoptionBase):
    def test_wall_below_ray_clears_and_records_sources(self):
        surface = self.surface(1)
        card = self.east_cards()[0]  # east beat 1: ray height 2 over the wall
        result = shade_card(surface, parse_spec_card(surface, card))
        self.assertFalse(result.blocked)
        self.assertEqual(result.blockers, ())

    def test_raised_wall_reaches_ray_and_keeps_source_block(self):
        self.raise_wall()
        surface = self.surface(2)
        card = next(c for c in self.east_cards() if c["beat"] == 1)
        result = shade_card(surface, parse_spec_card(surface, card))
        self.assertTrue(result.blocked)
        blocker = result.blockers[0]
        self.assertEqual(blocker.cell, (0, 0))
        self.assertEqual(blocker.elevation, 2)
        self.assertEqual(blocker.ray_height, 2)
        # The blocker carries the real source tile revision that cast it.
        self.assertEqual(blocker.sources[0].tile_id, "NORTH")
        self.assertEqual(blocker.sources[0].tile_revision, 2)

    def test_shading_is_height_reach_not_strictly_above(self):
        # Ray height 2 vs wall height 2 must block (reach, not strictly above).
        self.raise_wall()
        surface = self.surface(2)
        card = next(c for c in self.east_cards() if c["beat"] == 1)
        self.assertTrue(shade_card(surface, parse_spec_card(surface, card)).blocked)


def parse_spec_card(surface, card):
    from slabwater.sun import RayCard

    return RayCard(
        bed_id=card["bed_id"],
        beat=card["beat"],
        path=tuple(tuple(cell) for cell in card["path"]),
        ray_heights=tuple(card["ray_height"] for _ in card["path"]),
    )


class FixedBedTests(AdoptionBase):
    def fixed_request(self, request_id: str, version: int = 1, sun_beats: int = 2):
        return {
            "id": request_id,
            "source_revision": version,
            "mode": "fixed",
            "beds": [{"id": "E", "cell": [0, 1]}],
            "weather": [{"id": "SUN", "beats": [True] * 4}],
            "cards": self.east_cards(),
            "gardeners": 0,
            "trays": [
                {"id": "M1", "initial": "E", "rights": ["E"], "sun_beats": sun_beats}
            ],
        }

    def test_signs_when_wall_sits_below_both_rays(self):
        result = self.registry.sign(self.fixed_request("FIX-OK"))
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "signed")
        # East morning beats 1 and 2 are the contracted sunny run.
        records_policy = result.artifact["policy"]
        self.assertEqual(records_policy["beat"], 1)

    def test_raised_wall_rejects_and_names_the_lost_beats(self):
        self.raise_wall()
        result = self.registry.sign(self.fixed_request("FIX-BLOCKED", version=2))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "rejected")
        payload = self.registry.sign_outcome("FIX-BLOCKED")["payload"]
        failure = payload["failure"]
        self.assertEqual(len(failure), 1)
        witness = failure[0]
        self.assertEqual(witness["tray"], "M1")
        # The two contracted morning beats are both shaded by the raised wall.
        shaded_beats = {b["beat"] for b in witness["breaks"] if b["cause"] == "shaded"}
        self.assertIn(1, shaded_beats)
        self.assertIn(2, shaded_beats)
        blocker = next(b for b in witness["breaks"] if b["beat"] == 1)["blockers"][0]
        self.assertEqual(blocker["sources"][0]["tile_revision"], 2)
        # A rejection never creates a contract row.
        self.assertEqual(self.registry.list_contracts(), [])

    def test_handover_against_new_terrain_fails_with_pinned_contract(self):
        contract = self.registry.sign(self.fixed_request("FIX-PIN"))
        self.raise_wall()
        handover = self.registry.handover(
            {
                "id": "HAND-RAISED",
                "contract_id": contract.contract_id,
                "weather_id": "SUN",
                "observed_revision": 2,
            }
        )
        self.assertFalse(handover.ok)
        self.assertEqual(handover.status, "failed")
        report = handover.report
        self.assertTrue(report["observed_elevation"]["terrain_changed"])
        self.assertEqual(report["pinned_elevation"]["version"], 1)
        self.assertEqual(report["observed_elevation"]["version"], 2)
        codes = {o["code"] for o in report["simulation"]["obstructions"]}
        self.assertIn("shaded", codes)

    def test_promised_beat_with_no_replacement_bed_cannot_hand_over(self):
        # Restore the original terrain but promise bed E's beat-2 slot to another
        # client.  A fixed tray cannot relocate, so the run breaks.
        contract = self.registry.sign(self.fixed_request("FIX-PROMISE"))
        handover = self.registry.handover(
            {
                "id": "HAND-OCC",
                "contract_id": contract.contract_id,
                "weather_id": "SUN",
                "occupied": {"2": ["E"]},
            }
        )
        self.assertFalse(handover.ok)
        self.assertEqual(handover.status, "failed")
        obstruction = next(
            o for o in handover.report["simulation"]["obstructions"]
            if o["code"] == "bed_occupied"
        )
        self.assertEqual(obstruction["detail"]["bed"], "E")

    def test_reject_when_one_weather_pattern_breaks_the_run(self):
        # The garden cannot pick a sunny day per bed: every whole-day possibility
        # must satisfy the contract.  A cloudy morning breaks the east run.
        request = self.fixed_request("FIX-WEATHER")
        request["weather"] = [
            {"id": "SUN", "beats": [True] * 4},
            {"id": "PATCHY", "beats": [False, True, True, True]},
        ]
        result = self.registry.sign(request)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "rejected")
        witness = self.registry.sign_outcome("FIX-WEATHER")["payload"]["failure"][0]
        self.assertEqual(witness["weather_id"], "PATCHY")


class MobileBedTests(AdoptionBase):
    def mobile_request(self, request_id: str, **overrides):
        request = {
            "id": request_id,
            "source_revision": 1,
            "mode": "mobile",
            "reveal": "dawn",
            "beds": self.material["beds"],
            "weather": self.material["weather"],
            "cards": self.material["cards"],
            "corridors": self.material["corridors"],
            "gardeners": 1,
            "trays": [
                {"id": "P1", "initial": "E", "rights": ["E", "W"], "sun_beats": 2}
            ],
        }
        request.update(overrides)
        return request

    def test_signs_and_policy_branches_on_dawn_reveal(self):
        result = self.registry.sign(self.mobile_request("MOB-OK"))
        self.assertTrue(result.ok)
        tree = result.artifact["policy"]
        # Dawn reveal: the day type is known before the beat-1 action, so the
        # root has one branch per weather possibility.
        observations = {
            branch["observation"]["weather_id"] for branch in tree["branches"]
        }
        self.assertEqual(observations, {"CLEAR", "CLOUD_AM"})

    def test_clear_day_stays_east_cloudy_morning_moves_west(self):
        contract = self.registry.sign(self.mobile_request("MOB-RUN")).contract_id
        clear = self.registry.handover(
            {"id": "H-CLEAR", "contract_id": contract, "weather_id": "CLEAR"}
        )
        self.assertTrue(clear.ok)
        clear_records = clear.report["simulation"]["records"]
        # The east morning window alone completes the run; no move needed.
        self.assertTrue(all(r["bed"] == "E" for r in clear_records))
        self.assertEqual([r["streak"] for r in clear_records[:2]], [1, 2])

        cloudy = self.registry.handover(
            {"id": "H-CLOUD", "contract_id": contract, "weather_id": "CLOUD_AM"}
        )
        self.assertTrue(cloudy.ok)
        records = cloudy.report["simulation"]["records"]
        moves = [(r["beat"], r["bed"], r["moved"], r["sun_counted"], r["streak"])
                 for r in records]
        # One beat is spent pushing E->W; that beat records no sunshine.
        move_beats = [beat for beat, _, moved, _, _ in moves if moved]
        self.assertEqual(len(move_beats), 1)
        moved_beat = move_beats[0]
        self.assertFalse(records[moved_beat - 1]["sun_counted"])
        # The west afternoon window then supplies the consecutive run.
        west_sun = [r for r in records if r["bed"] == "W" and r["sun_counted"]]
        self.assertEqual([r["streak"] for r in west_sun], [1, 2])

    def test_move_beat_never_counts_as_sunshine(self):
        contract = self.registry.sign(self.mobile_request("MOB-COST")).contract_id
        cloudy = self.registry.handover(
            {"id": "H-COST", "contract_id": contract, "weather_id": "CLOUD_AM"}
        )
        for record in cloudy.report["simulation"]["records"]:
            if record["moved"]:
                self.assertFalse(record["sun_counted"])
                self.assertEqual(record["streak"], 0)

    def test_zero_gardeners_rejects_signing_when_move_is_essential(self):
        result = self.registry.sign(self.mobile_request("MOB-NOG", gardeners=0))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "rejected")

    def test_handover_without_gardener_fails_and_records_obstruction(self):
        contract = self.registry.sign(self.mobile_request("MOB-CREW")).contract_id
        handover = self.registry.handover(
            {
                "id": "H-NOG",
                "contract_id": contract,
                "weather_id": "CLOUD_AM",
                "gardeners": 0,
            }
        )
        self.assertFalse(handover.ok)
        codes = {o["code"] for o in handover.report["simulation"]["obstructions"]}
        self.assertIn("no_gardener", codes)

    def test_occupied_target_bed_blocks_the_move(self):
        contract = self.registry.sign(self.mobile_request("MOB-OCC")).contract_id
        # The cloudy-morning policy pushes to W; claiming W on that beat strands
        # the tray at shaded E in the afternoon.
        handover = self.registry.handover(
            {
                "id": "H-OCC",
                "contract_id": contract,
                "weather_id": "CLOUD_AM",
                "occupied": {"2": ["W"]},
            }
        )
        self.assertFalse(handover.ok)
        codes = {o["code"] for o in handover.report["simulation"]["obstructions"]}
        self.assertIn("bed_occupied", codes)
        self.assertIn("shaded", codes)

    def test_missing_corridor_rejected_at_signing(self):
        request = self.mobile_request("MOB-NOCOR", corridors=[])
        result = self.registry.sign(request)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "error")
        self.assertEqual(
            self.registry.sign_outcome("MOB-NOCOR")["payload"]["code"], "no_corridor"
        )

    def test_progressive_reveal_shares_the_first_action(self):
        # Two day types that look identical on beat 1 (both sunny) but diverge
        # later: under progressive reveal the beat-1 action cannot distinguish
        # them, so the root holds a single shared observation branch.
        request = self.mobile_request(
            "MOB-PROG",
            reveal="progressive",
            weather=[
                {"id": "D1", "beats": [True, True, True, True]},
                {"id": "D2", "beats": [True, True, False, True]},
            ],
        )
        result = self.registry.sign(request)
        self.assertTrue(result.ok)
        root = result.artifact["policy"]
        self.assertEqual(len(root["branches"]), 1)
        self.assertEqual(root["branches"][0]["observation"], {"beat": 1, "sunny": True})


class TwoTrayTests(AdoptionBase):
    def test_two_trays_need_independent_runs_not_pooled_beats(self):
        # Tray Y is fixed on W and needs its run to start in the morning, but W is
        # terrain-shaded for the first two beats.  X's sunny east beats cannot be
        # pooled across trays to rescue Y.
        request = {
            "id": "TWO",
            "source_revision": 1,
            "mode": "fixed",
            "beds": [
                {"id": "E", "cell": [0, 1]},
                {"id": "W", "cell": [0, 2]},
            ],
            "weather": [{"id": "SUN", "beats": [True] * 4}],
            "cards": self.material["cards"],
            "gardeners": 0,
            "trays": [
                # X completes on the east morning window.
                {"id": "X", "initial": "E", "rights": ["E"], "sun_beats": 2},
                # Y can only use W; the required two-beat run must include beat 1,
                # which W's shaded morning cannot provide.
                {"id": "Y", "initial": "W", "rights": ["W"], "sun_beats": 4},
            ],
        }
        result = self.registry.sign(request)
        self.assertFalse(result.ok)
        trays = {w["tray"] for w in self.registry.sign_outcome("TWO")["payload"]["failure"]}
        self.assertIn("Y", trays)
        self.assertNotIn("X", trays)

    def test_two_mobile_trays_cannot_share_a_bed(self):
        request = {
            "id": "TWO-MOB",
            "source_revision": 1,
            "mode": "mobile",
            "reveal": "dawn",
            "beds": self.material["beds"],
            "weather": [{"id": "SUN", "beats": [True] * 4}],
            "cards": self.material["cards"],
            "corridors": self.material["corridors"],
            "gardeners": 2,
            "trays": [
                {"id": "X", "initial": "E", "rights": ["E", "W"], "sun_beats": 1},
                {"id": "Y", "initial": "W", "rights": ["E", "W"], "sun_beats": 1},
            ],
        }
        result = self.registry.sign(request)
        # Feasible only because they can stay on separate beds; the engine must
        # never emit a joint action landing both trays on one cell.
        self.assertTrue(result.ok)
        spec = parse_spec({k: v for k, v in request.items() if k != "id"}, self.surface(1))
        engine = SunEngine(spec, self.surface(1))
        for moves in engine._joint_actions({"X": "E", "Y": "W"}, 2):
            destinations = [m.destination for m in moves]
            self.assertEqual(len(destinations), len(set(destinations)))


class LimitTests(AdoptionBase):
    def _base(self):
        return {
            "source_revision": 1,
            "mode": "fixed",
            "weather": [{"id": "SUN", "beats": [True] * 4}],
            "gardeners": 0,
        }

    def test_more_than_four_beds_rejected(self):
        request = self._base()
        request["id"] = "LIMIT-BEDS"
        request["beds"] = [{"id": f"B{i}", "cell": [0, i % 4]} for i in range(MAX_BEDS + 1)]
        request["cards"] = [
            {"bed_id": f"B{i}", "beat": b, "path": [[0, 0]], "ray_height": 9}
            for i in range(MAX_BEDS + 1) for b in range(1, 5)
        ]
        request["trays"] = [{"id": "M", "initial": "B0", "rights": ["B0"], "sun_beats": 1}]
        result = self.registry.sign(request)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "error")
        self.assertEqual(
            self.registry.sign_outcome("LIMIT-BEDS")["payload"]["code"], "limit_exceeded"
        )

    def test_more_than_four_beats_rejected(self):
        request = self._base()
        request["id"] = "LIMIT-BEATS"
        request["beds"] = [{"id": "E", "cell": [0, 1]}]
        request["weather"] = [{"id": "SUN", "beats": [True] * (MAX_BEATS + 1)}]
        request["cards"] = [
            {"bed_id": "E", "beat": b, "path": [[0, 0]], "ray_height": 9}
            for b in range(1, MAX_BEATS + 2)
        ]
        request["trays"] = [{"id": "M", "initial": "E", "rights": ["E"], "sun_beats": 1}]
        result = self.registry.sign(request)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "error")
        self.assertEqual(
            self.registry.sign_outcome("LIMIT-BEATS")["payload"]["code"], "limit_exceeded"
        )

    def test_more_than_two_trays_rejected(self):
        request = self._base()
        request["id"] = "LIMIT-TRAYS"
        request["beds"] = [
            {"id": "E", "cell": [0, 1]},
            {"id": "W", "cell": [0, 2]},
        ]
        request["cards"] = self.material["cards"]
        request["trays"] = [
            {"id": f"M{i}", "initial": "E", "rights": ["E", "W"], "sun_beats": 1}
            for i in range(MAX_TRAYS + 1)
        ]
        result = self.registry.sign(request)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "error")
        self.assertEqual(
            self.registry.sign_outcome("LIMIT-TRAYS")["payload"]["code"], "limit_exceeded"
        )


class PersistenceTests(AdoptionBase):
    def mobile_request(self, request_id: str):
        return {
            "id": request_id,
            "source_revision": 1,
            "mode": "mobile",
            "reveal": "dawn",
            "beds": self.material["beds"],
            "weather": self.material["weather"],
            "cards": self.material["cards"],
            "corridors": self.material["corridors"],
            "gardeners": 1,
            "trays": [{"id": "P1", "initial": "E", "rights": ["E", "W"], "sun_beats": 2}],
        }

    def test_replay_same_request_returns_same_contract(self):
        first = self.registry.sign(self.mobile_request("IDEM"))
        second = self.registry.sign(self.mobile_request("IDEM"))
        self.assertTrue(second.replayed)
        self.assertEqual(first.contract_id, second.contract_id)
        self.assertEqual(first.artifact, second.artifact)

    def test_sign_idempotency_conflict(self):
        self.registry.sign(self.mobile_request("CONFLICT"))
        changed = self.mobile_request("CONFLICT")
        changed["gardeners"] = 0
        with self.assertRaises(AdoptionError) as ctx:
            self.registry.sign(changed)
        self.assertEqual(ctx.exception.code, "idempotency_conflict")

    def test_handover_idempotency_conflict(self):
        contract = self.registry.sign(self.mobile_request("H-IDEM")).contract_id
        payload = {"id": "HAND", "contract_id": contract, "weather_id": "CLEAR"}
        first = self.registry.handover(payload)
        with self.assertRaises(AdoptionError) as ctx:
            self.registry.handover({**payload, "weather_id": "CLOUD_AM"})
        self.assertEqual(ctx.exception.code, "idempotency_conflict")
        # The original result is replayed unchanged.
        replay = self.registry.handover(payload)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.status, first.status)

    def test_new_terrain_does_not_rewrite_contract_and_reopen_pins_rights(self):
        contract = self.registry.sign(self.mobile_request("PIN"))
        self.registry.handover(
            {"id": "H1", "contract_id": contract.contract_id,
             "weather_id": "CLOUD_AM", "observed_revision": 1}
        )
        self.raise_wall()
        # The stored contract still points at version 1 and its bed rights.
        reopened = self.registry.reopen(contract.contract_id)
        self.assertTrue(reopened.replayed)
        self.assertEqual(reopened.artifact["catalog_version"], 1)
        self.assertEqual(
            reopened.artifact["spec"]["trays"][0]["rights"], ["E", "W"]
        )
        self.assertEqual(self.registry.get_contract(contract.contract_id), reopened.artifact)

    def test_reopen_recovers_sunshine_records(self):
        contract = self.registry.sign(self.mobile_request("LEDGER")).contract_id
        self.registry.handover(
            {"id": "H-LEDGER", "contract_id": contract, "weather_id": "CLOUD_AM"}
        )
        stored = self.registry.get_handover("H-LEDGER")
        self.assertEqual(stored["contract_id"], contract)
        # Every recorded beat keeps its shading verdict and any source blocks.
        for record in stored["simulation"]["records"]:
            self.assertIn("sun_counted", record)
            for blocker in record["blockers"]:
                self.assertTrue(blocker["sources"])

    def test_verify_detects_tampering_and_passes_when_clean(self):
        contract = self.registry.sign(self.mobile_request("VERIFY")).contract_id
        self.assertTrue(self.registry.verify(contract)["ok"])
        artifact = self.registry.get_contract(contract)
        tampered = json.loads(json.dumps(artifact))
        tampered["spec"]["gardeners"] = 99
        self.registry._conn.execute(
            "UPDATE contracts SET artifact=? WHERE contract_id=?",
            (json.dumps(tampered), contract),
        )
        result = self.registry.verify(contract)
        self.assertFalse(result["ok"])
        self.assertFalse(result["checks"]["artifact_hash_matches"])

    def test_unknown_source_revision_is_an_error_without_contract(self):
        request = self.mobile_request("MISSING-REV")
        request["source_revision"] = 99
        result = self.registry.sign(request)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "error")
        self.assertEqual(self.registry.list_contracts(), [])

    def test_handover_unknown_weather_rejected(self):
        contract = self.registry.sign(self.mobile_request("BAD-WEATHER")).contract_id
        with self.assertRaises(AdoptionError) as ctx:
            self.registry.handover(
                {"id": "H-BAD", "contract_id": contract, "weather_id": "STORM"}
            )
        self.assertEqual(ctx.exception.code, "unknown_weather")


class HeadlineNarrativeTests(unittest.TestCase):
    """The minimal north-wall sequence stated verbatim in the task brief."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.catalog = Catalog(self.dir / "catalog.db", 1, 2, 6)
        self.registry = AdoptionRegistry(self.dir / "adoption.db", self.catalog)

    def tearDown(self) -> None:
        self.registry.close()
        self.catalog.close()
        self.tmp.cleanup()

    def _request(self, request_id: str, version: int):
        return {
            "id": request_id,
            "source_revision": version,
            "mode": "fixed",
            "beds": [{"id": "A", "cell": [0, 1]}],
            "weather": [{"id": "SUN", "beats": [True, True]}],
            "cards": [
                {"bed_id": "A", "beat": 1, "path": [[0, 0]], "ray_height": 2},
                {"bed_id": "A", "beat": 2, "path": [[0, 0]], "ray_height": 2},
            ],
            "gardeners": 0,
            "trays": [{"id": "M1", "initial": "A", "rights": ["A"], "sun_beats": 2}],
        }

    def test_wall_below_ray_then_raised_wall_then_promised_bed(self):
        self.catalog.apply_batch(
            [{"tile_id": "T", "revision": 1, "row": 0, "col": 0, "values": [[1, 0]]}]
        )
        self.assertTrue(self.registry.sign(self._request("N1", 1)).ok)

        # Only a new north-wall version (height 2) is published: both rays blocked.
        self.catalog.apply_batch(
            [{"tile_id": "T", "revision": 2, "row": 0, "col": 0, "values": [[2, 0]]}]
        )
        blocked = self.registry.sign(self._request("N2", 2))
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.status, "rejected")

        # Restore the original terrain; promise bed A's beat-2 slot away with no
        # spare bed: the same order cannot be handed over.
        self.catalog.apply_batch(
            [{"tile_id": "T", "revision": 3, "row": 0, "col": 0, "values": [[1, 0]]}]
        )
        contract_id = self.registry.sign(self._request("N3", 3)).contract_id
        handover = self.registry.handover(
            {
                "id": "HN",
                "contract_id": contract_id,
                "weather_id": "SUN",
                "occupied": {"2": ["A"]},
            }
        )
        self.assertFalse(handover.ok)
        obstruction = next(
            o for o in handover.report["simulation"]["obstructions"]
            if o["code"] == "bed_occupied"
        )
        self.assertEqual(obstruction["beat"], 2)
        # The promising beat breaks the streak: the run never reaches length 2.
        self.assertEqual(
            [r["streak"] for r in handover.report["simulation"]["records"]], [1, 0]
        )


class SameDayWeatherTests(AdoptionBase):
    def test_cannot_pick_a_different_day_per_bed(self):
        # With terrain shade removed, only the weather table distinguishes the
        # beds.  Each pattern leaves one tray without a 2-run, and the engine must
        # not combine sunny beats across the two whole-day possibilities.
        cards = (
            [{"bed_id": "E", "beat": b, "path": [[0, 1]], "ray_height": 9}
             for b in range(1, 5)]
            + [{"bed_id": "W", "beat": b, "path": [[0, 2]], "ray_height": 9}
               for b in range(1, 5)]
        )
        request = {
            "id": "SAME-DAY",
            "source_revision": 1,
            "mode": "fixed",
            "beds": [{"id": "E", "cell": [0, 1]}, {"id": "W", "cell": [0, 2]}],
            "weather": [
                {"id": "P1", "beats": [True, True, False, True]},
                {"id": "P2", "beats": [False, True, False, True]},
            ],
            "cards": cards,
            "gardeners": 0,
            "trays": [
                {"id": "A", "initial": "E", "rights": ["E"], "sun_beats": 2},
                {"id": "B", "initial": "W", "rights": ["W"], "sun_beats": 2},
            ],
        }
        result = self.registry.sign(request)
        self.assertFalse(result.ok)
        witnesses = self.registry.sign_outcome("SAME-DAY")["payload"]["failure"]
        # Every failing witness is tied to one whole-day pattern applied to a tray.
        self.assertTrue(all(w["longest_run"] < 2 for w in witnesses))
        common = dict(request)
        common["id"] = "COMMON"
        common["weather"] = [{"id": "ALL", "beats": [True] * 4}]
        self.assertTrue(self.registry.sign(common).ok)


if __name__ == "__main__":
    unittest.main()
