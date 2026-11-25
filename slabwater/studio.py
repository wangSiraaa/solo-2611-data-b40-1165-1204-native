"""Persistent studio: issue overprint orders, keep their frozen artifacts.

The studio sits on top of a :class:`~slabwater.catalog.Catalog` and evaluates
:mod:`slabwater.overprint` orders.  A *published* order is frozen together with
the two catalog versions and their elevation hashes; switching to a new plate
version afterwards never rewrites an already-printed first layer.  Old orders
can be re-opened byte-for-byte and carry the two plate version numbers, the
per-paper-cell colours and the touched source tiles.

Idempotency follows the catalog publisher: an order id is tied to the canonical
input it was first seen with, and re-using an id with different input is an
error rather than a silent overwrite.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .catalog import Catalog
from .model import Slice, sha256_hex
from .overprint import (
    Evaluation,
    OverprintError,
    Plate,
    evaluate,
    parse_plates,
    parse_registration,
    parse_shift_list,
    parse_sheets,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outcomes (
    order_id    TEXT PRIMARY KEY,
    input_hash  TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('printed','rejected','error')),
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS prints (
    print_id       TEXT PRIMARY KEY,
    order_id       TEXT NOT NULL UNIQUE REFERENCES outcomes(order_id),
    input_hash     TEXT NOT NULL,
    plate_versions TEXT NOT NULL,
    artifact       TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@dataclass
class StudioResult:
    ok: bool
    order_id: str
    status: str
    print_id: str | None
    replayed: bool
    artifact: dict[str, Any] | None
    evaluation: Evaluation | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "order_id": self.order_id,
            "status": self.status,
            "print_id": self.print_id,
            "replayed": self.replayed,
            "artifact": self.artifact,
            "evaluation": self.evaluation.to_dict() if self.evaluation else None,
        }


class Studio:
    def __init__(self, path: str | Path, catalog: Catalog):
        self.path = str(path)
        self.catalog = catalog
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Studio":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- request canonicalisation -----------------------------------------
    def _normalize(self, order: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(order, Mapping):
            raise OverprintError("bad_request", "order must be an object")
        order_id = order.get("id")
        if not isinstance(order_id, str) or not order_id:
            raise OverprintError("bad_request", "order id must be a non-empty string")
        plates = parse_plates(order)
        shifts = parse_shift_list(order)
        sheets = parse_sheets(order)
        registration = parse_registration(order)
        overlay = order.get("ink_overlay", [])
        required = order.get("required_colors", [])
        return {
            "id": order_id,
            "plates": [plate.to_dict() for plate in plates],
            "allowed_shifts": [list(s) for s in shifts],
            "ink_overlay": list(overlay),
            "required_colors": list(required),
            "process": order.get("process", "auto"),
            "require_full_edge": bool(order.get("require_full_edge", False)),
            "registration": None
            if registration is None
            else {
                "marks": [
                    {"cell": [cell[0], cell[1]], "ink": ink}
                    for cell, ink in registration.marks
                ],
                "true_shift": [registration.true_shift[0], registration.true_shift[1]],
                "clamp_cells": [
                    [c[0], c[1]] for c in sorted(registration.clamp_cells)
                ],
            },
            "sheets": [
                {
                    "id": sheet.sheet_id,
                    "paper_rejects": [
                        {"cell": [cell[0], cell[1]], "inks": sorted(inks)}
                        for cell, inks in sheet.paper_rejects
                    ],
                }
                for sheet in sheets
            ],
        }

    def _input_hash(self, norm: Mapping[str, Any], hashes: Sequence[str | None]) -> str:
        return sha256_hex({"order": norm, "plate_elevation_hashes": list(hashes)})

    # -- publishing --------------------------------------------------------
    def print_order(self, order: Mapping[str, Any]) -> StudioResult:
        norm = self._normalize(order)
        order_id = norm["id"]
        plates = parse_plates(order)

        layers: list[tuple[Plate, Slice]] = []
        versions: list[int] = []
        hashes: list[str | None] = []
        missing_version: int | None = None
        for plate in plates:
            versions.append(plate.version)
            try:
                surface = self.catalog.get_slice(plate.version)
            except KeyError:
                missing_version = plate.version
                hashes.append(None)
                continue
            layers.append((plate, surface))
            hashes.append(surface.elevation_hash)

        input_hash = self._input_hash(norm, hashes)
        replay = self._replay(order_id, input_hash)
        if replay is not None:
            return replay

        if missing_version is not None:
            payload = {
                "ok": False,
                "code": "unknown_plate_version",
                "message": f"catalog has no version {missing_version}",
                "details": {"version": missing_version},
            }
            self._record_outcome(order_id, input_hash, "error", payload)
            return StudioResult(False, order_id, "error", None, False, None, None)

        try:
            evaluation = evaluate(layers, order)
        except OverprintError as exc:
            self._record_outcome(order_id, input_hash, "error", exc.to_dict())
            return StudioResult(False, order_id, "error", None, False, None, None)

        if not evaluation.ok:
            payload = {
                "ok": False,
                "code": "rejected",
                "message": "no sheet at the allowed shifts satisfies the order",
                "input": norm,
                "plate_versions": versions,
                "elevation_hashes": hashes,
                "evaluation": evaluation.to_dict(),
            }
            self._record_outcome(order_id, input_hash, "rejected", payload)
            return StudioResult(False, order_id, "rejected", None, False, None, evaluation)

        assert evaluation.sheet is not None
        artifact = {
            "print_id": None,
            "order_id": order_id,
            "input": norm,
            "process": evaluation.process,
            "plate_versions": versions,
            "elevation_hashes": hashes,
            "shift": list(evaluation.shift) if evaluation.shift is not None else None,
            "chosen_sheet": evaluation.chosen_sheet,
            "sheet": evaluation.sheet.to_dict(),
            "registration": evaluation.registration,
            "artifact_hash": sha256_hex(
                {
                    "plate_versions": versions,
                    "elevation_hashes": hashes,
                    "shift": list(evaluation.shift) if evaluation.shift is not None else None,
                    "sheet": evaluation.sheet.to_dict(),
                }
            ),
        }
        print_id = "OP-" + sha256_hex(
            {
                "order_id": order_id,
                "input_hash": input_hash,
                "plate_versions": versions,
                "shift": list(evaluation.shift) if evaluation.shift is not None else None,
            }
        )[:16]
        artifact["print_id"] = print_id
        self._record_print(print_id, order_id, input_hash, versions, artifact)
        return StudioResult(True, order_id, "printed", print_id, False, artifact, evaluation)

    # -- storage -----------------------------------------------------------
    def _replay(self, order_id: str, input_hash: str) -> StudioResult | None:
        row = self._conn.execute(
            "SELECT input_hash, status, payload FROM outcomes WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if row is None:
            return None
        stored_hash, status, payload_json = row
        if stored_hash != input_hash:
            raise OverprintError(
                "idempotency_conflict",
                f"order id {order_id} was already used with different input",
                {"order_id": order_id},
            )
        if status == "printed":
            row2 = self._conn.execute(
                "SELECT print_id, artifact FROM prints WHERE order_id=?", (order_id,)
            ).fetchone()
            if row2 is None:
                raise OverprintError(
                    "inconsistent_store", f"printed outcome {order_id} has no print row"
                )
            return StudioResult(
                True, order_id, "printed", row2[0], True, json.loads(row2[1]), None
            )
        return StudioResult(
            False, order_id, status, None, True, json.loads(payload_json), None
        )

    def _record_outcome(
        self, order_id: str, input_hash: str, status: str, payload: Mapping[str, Any]
    ) -> None:
        self._conn.execute(
            "INSERT INTO outcomes(order_id,input_hash,status,payload) VALUES (?,?,?,?)",
            (order_id, input_hash, status, json.dumps(payload, sort_keys=True)),
        )

    def _record_print(
        self,
        print_id: str,
        order_id: str,
        input_hash: str,
        versions: Sequence[int],
        artifact: Mapping[str, Any],
    ) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT INTO outcomes(order_id,input_hash,status,payload) VALUES (?,?,?,?)",
                (order_id, input_hash, "printed", json.dumps({"ok": True, "print_id": print_id}, sort_keys=True)),
            )
            self._conn.execute(
                "INSERT INTO prints(print_id,order_id,input_hash,plate_versions,artifact) "
                "VALUES (?,?,?,?,?)",
                (
                    print_id,
                    order_id,
                    input_hash,
                    json.dumps(list(versions)),
                    json.dumps(artifact, sort_keys=True),
                ),
            )
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # -- retrieval / reopen ------------------------------------------------
    def get_print(self, print_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT artifact FROM prints WHERE print_id=?", (print_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no such print: {print_id}")
        return json.loads(row[0])

    def print_for_order(self, order_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT artifact FROM prints WHERE order_id=?", (order_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no printed order: {order_id}")
        return json.loads(row[0])

    def outcome_for_order(self, order_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT status, payload FROM outcomes WHERE order_id=?", (order_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no outcome for order: {order_id}")
        return {"status": row[0], "payload": json.loads(row[1])}

    def list_prints(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT print_id, order_id, plate_versions, created_at FROM prints ORDER BY print_id"
        ).fetchall()
        return [
            {
                "print_id": r[0],
                "order_id": r[1],
                "plate_versions": json.loads(r[2]),
                "created_at": r[3],
            }
            for r in rows
        ]

    def reopen(self, print_id: str) -> StudioResult:
        """Re-open a printed order exactly as stored (no recomputation)."""

        artifact = self.get_print(print_id)
        return StudioResult(
            True, artifact["order_id"], "printed", print_id, True, artifact, None
        )

    # -- verification ------------------------------------------------------
    def verify(self, print_id: str) -> dict[str, Any]:
        try:
            artifact = self.get_print(print_id)
        except KeyError:
            return {"ok": False, "print_id": print_id, "checks": {"exists": False},
                    "messages": ["print not found"], "artifact": None}
        checks: dict[str, Any] = {"exists": True}
        messages: list[str] = []
        versions = artifact["plate_versions"]
        hashes = artifact["elevation_hashes"]
        surfaces: list[Slice] = []
        for version, expected_hash in zip(versions, hashes):
            try:
                surface = self.catalog.get_slice(version)
            except KeyError:
                checks["versions_available"] = False
                messages.append(f"catalog version {version} is missing")
                return {"ok": False, "print_id": print_id, "checks": checks,
                        "messages": messages, "artifact": artifact}
            surfaces.append(surface)
            if surface.elevation_hash != expected_hash:
                checks["elevation_hashes_match"] = False
                messages.append(f"frozen elevation hash for version {version} changed")
        checks.setdefault("versions_available", True)
        checks.setdefault("elevation_hashes_match", True)
        # Recompute the stored sheet from the frozen plates at the stored shift.
        plates = [
            Plate(i + 1, versions[i], artifact["input"]["plates"][i]["ink"],
                  artifact["input"]["plates"][i]["orientation"],
                  artifact["input"]["plates"][i]["depth"])
            for i in range(2)
        ]
        recomputed = evaluate(
            list(zip(plates, surfaces)),
            {
                "plates": artifact["input"]["plates"],
                "allowed_shifts": [artifact["shift"]],
                "ink_overlay": artifact["input"]["ink_overlay"],
                "required_colors": artifact["input"]["required_colors"],
                "process": artifact["process"],
                "require_full_edge": artifact["input"]["require_full_edge"],
                "registration": artifact["input"].get("registration"),
                "sheets": artifact["input"]["sheets"],
            },
        )
        checks["reproducible"] = recomputed.ok
        if not recomputed.ok:
            messages.append("stored sheet does not reproduce from the frozen plates")
        elif recomputed.sheet is not None:
            checks["sheet_matches"] = recomputed.sheet.to_dict() == artifact["sheet"]
            if not checks["sheet_matches"]:
                messages.append("stored colours/provenance differ from recomputation")
        expected_hash = sha256_hex(
            {
                "plate_versions": versions,
                "elevation_hashes": hashes,
                "shift": artifact["shift"],
                "sheet": artifact["sheet"],
            }
        )
        checks["artifact_hash_matches"] = expected_hash == artifact["artifact_hash"]
        if not checks["artifact_hash_matches"]:
            messages.append("artifact hash mismatch (tampered)")
        return {
            "ok": all(checks.values()),
            "print_id": print_id,
            "checks": checks,
            "messages": messages,
            "artifact": artifact,
        }
