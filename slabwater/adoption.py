"""SQLite-backed seedbed adoption contracts and field hand-over ledger.

The registry is built on the same frozen :class:`~slabwater.catalog.Catalog` as
the plan publisher and never mutates it.  A contract binds to one catalog
version (and therefore one ``elevation_hash``); later terrain revisions leave
old contracts and their sunshine ledgers byte-for-byte intact, while a hand-over
may be evaluated against a newer observed terrain -- the record keeps both.

Idempotency mirrors the publisher:

* A sign request id is tied to the exact canonical input it was first seen
  with.  Replaying it returns the stored contract (or the stored rejection
  evidence); reusing the id with different input raises ``idempotency_conflict``.
* Hand-over requests carry their own id with the same guarantee.
* Rejected signings never create a contract row.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .catalog import Catalog
from .model import is_int, sha256_hex
from .sun import (
    MAX_BEATS,
    MAX_BEDS,
    MAX_TRAYS,
    AdoptionError,
    PlanSpec,
    SunEngine,
    normalize_occupancy,
    parse_spec,
    simulate_handover,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sign_outcomes (
    request_id  TEXT PRIMARY KEY,
    input_hash  TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('signed','rejected','error')),
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS contracts (
    contract_id     TEXT PRIMARY KEY,
    request_id      TEXT NOT NULL UNIQUE REFERENCES sign_outcomes(request_id),
    input_hash      TEXT NOT NULL,
    mode            TEXT NOT NULL,
    catalog_version INTEGER NOT NULL,
    elevation_hash  TEXT NOT NULL,
    spec            TEXT NOT NULL,
    artifact        TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS handover_outcomes (
    handover_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
    input_hash  TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('delivered','failed','error')),
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@dataclass
class SignResult:
    ok: bool
    request_id: str
    status: str
    contract_id: str | None
    artifact: dict[str, Any] | None
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "request_id": self.request_id,
            "status": self.status,
            "contract_id": self.contract_id,
            "replayed": self.replayed,
            "artifact": self.artifact,
        }


@dataclass
class HandoverResult:
    ok: bool
    handover_id: str
    contract_id: str
    status: str
    report: dict[str, Any] | None
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "handover_id": self.handover_id,
            "contract_id": self.contract_id,
            "status": self.status,
            "replayed": self.replayed,
            "report": self.report,
        }


class AdoptionRegistry:
    """Persist adoption contracts and same-day field hand-over records."""

    def __init__(self, path: str | Path, catalog: Catalog):
        self.path = str(path)
        self.catalog = catalog
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "AdoptionRegistry":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- hashing ----------------------------------------------------------- #
    def _sign_hash(self, request: Mapping[str, Any], elevation_hash: str | None) -> str:
        return sha256_hex(
            {
                "request": request,
                "elevation_hash": elevation_hash,
                "schema": "seedbed-adoption-v1",
            }
        )

    def _handover_hash(self, request: Mapping[str, Any], elevation_hash: str | None) -> str:
        return sha256_hex(
            {
                "request": request,
                "elevation_hash": elevation_hash,
                "schema": "seedbed-handover-v1",
            }
        )

    # -- signing ----------------------------------------------------------- #
    def sign(self, request: Mapping[str, Any]) -> SignResult:
        """Validate and, if robustly deliverable, sign one adoption contract."""

        request_id = request.get("id") if isinstance(request, Mapping) else None
        if not isinstance(request_id, str) or not request_id:
            raise AdoptionError("bad_request", "sign request id must be a non-empty string")
        source_revision = request.get("source_revision")
        if not is_int(source_revision):
            raise AdoptionError("bad_request", "source_revision must be an integer version")
        try:
            surface = self.catalog.get_slice(source_revision)
        except KeyError as exc:
            input_hash = self._sign_hash(request, None)
            replay = self._replay_sign(request_id, input_hash)
            if replay is not None:
                return replay
            payload = {
                "ok": False,
                "code": "unknown_source_revision",
                "message": str(exc),
                "details": {"source_revision": source_revision},
            }
            self._record_sign_outcome(request_id, input_hash, "error", payload)
            return SignResult(False, request_id, "error", None, None, False)
        input_hash = self._sign_hash(request, surface.elevation_hash)
        replay = self._replay_sign(request_id, input_hash)
        if replay is not None:
            return replay
        try:
            spec = parse_spec(request, surface)
            self._check_limits(spec)
        except AdoptionError as exc:
            self._record_sign_outcome(request_id, input_hash, "error", exc.to_dict())
            return SignResult(False, request_id, "error", None, None, False)
        engine = SunEngine(spec, surface)
        if not engine.feasible():
            payload = {
                "ok": False,
                "code": "rejected",
                "message": "no non-anticipative policy keeps every tray sunlit for the full run",
                "input": request,
                "elevation": {
                    "version": surface.version,
                    "elevation_hash": surface.elevation_hash,
                },
                "failure": engine.explain_failure(),
                "shade": [
                    result.to_dict()
                    for result in sorted(
                        engine._shade.values(), key=lambda r: (r.bed_id, r.beat)
                    )
                ],
            }
            self._record_sign_outcome(request_id, input_hash, "rejected", payload)
            return SignResult(False, request_id, "rejected", None, None, False)
        contract_id = "C-" + sha256_hex(
            {"request_id": request_id, "input_hash": input_hash,
             "elevation_hash": surface.elevation_hash, "mode": spec.mode}
        )[:16]
        artifact = {
            "contract_id": contract_id,
            "request_id": request_id,
            "mode": spec.mode,
            "catalog_version": surface.version,
            "elevation_hash": surface.elevation_hash,
            "elevation_source": surface.elevation_source(include_cells=False),
            "spec": spec.to_dict(),
            "policy": engine.policy_tree(),
            "shade": [
                result.to_dict()
                for result in sorted(engine._shade.values(), key=lambda r: (r.bed_id, r.beat))
            ],
            "artifact_hash": sha256_hex(
                {"spec": spec.to_dict(), "elevation_hash": surface.elevation_hash}
            ),
        }
        self._record_contract(contract_id, request_id, input_hash, spec, surface, artifact)
        return SignResult(True, request_id, "signed", contract_id, artifact, False)

    def _check_limits(self, spec: PlanSpec) -> None:
        if len(spec.beds) > MAX_BEDS:
            raise AdoptionError("limit_exceeded", f"at most {MAX_BEDS} beds per order")
        if spec.n_beats > MAX_BEATS:
            raise AdoptionError("limit_exceeded", f"at most {MAX_BEATS} beats per order")
        if len(spec.trays) > MAX_TRAYS:
            raise AdoptionError("limit_exceeded", f"at most {MAX_TRAYS} trays per order")

    # -- field hand-over --------------------------------------------------- #
    def handover(self, request: Mapping[str, Any]) -> HandoverResult:
        """Execute a signed contract against one realized day and current terrain."""

        handover_id = request.get("id") if isinstance(request, Mapping) else None
        if not isinstance(handover_id, str) or not handover_id:
            raise AdoptionError("bad_request", "handover id must be a non-empty string")
        contract_id = request.get("contract_id")
        if not isinstance(contract_id, str) or not contract_id:
            raise AdoptionError("bad_request", "handover needs a contract_id")
        row = self._conn.execute(
            "SELECT spec, artifact FROM contracts WHERE contract_id=?", (contract_id,)
        ).fetchone()
        if row is None:
            raise AdoptionError("unknown_contract", f"no signed contract: {contract_id}")
        spec = self._spec_from_json(json.loads(row[0]))
        pinned_artifact = json.loads(row[1])
        observed_revision = request.get("observed_revision", pinned_artifact["catalog_version"])
        if not is_int(observed_revision):
            raise AdoptionError("bad_request", "observed_revision must be an integer version")
        try:
            surface = self.catalog.get_slice(observed_revision)
        except KeyError as exc:
            raise AdoptionError(
                "unknown_source_revision", str(exc),
                {"observed_revision": observed_revision},
            ) from exc
        input_hash = self._handover_hash(request, surface.elevation_hash)
        replay = self._replay_handover(handover_id, input_hash)
        if replay is not None:
            return replay
        bed_ids = {bed.id for bed in spec.beds}
        occupancy = normalize_occupancy(request.get("occupied"), bed_ids, spec.n_beats)
        gardeners = request.get("gardeners", spec.gardeners)
        if not is_int(gardeners) or gardeners < 0:
            raise AdoptionError("bad_request", "gardeners must be a non-negative integer")
        weather_id = request.get("weather_id")
        weather = next((w for w in spec.weather if w.id == weather_id), None)
        if weather is None:
            raise AdoptionError(
                "unknown_weather",
                f"weather_id {weather_id!r} is not one of the contracted possibilities",
                {"weather_id": weather_id},
            )
        simulation = simulate_handover(
            spec, surface, weather.beats, occupancy, int(gardeners)
        )
        status = "delivered" if simulation.ok else "failed"
        report = {
            "handover_id": handover_id,
            "contract_id": contract_id,
            "status": status,
            "pinned_elevation": {
                "version": pinned_artifact["catalog_version"],
                "elevation_hash": pinned_artifact["elevation_hash"],
            },
            "observed_elevation": {
                "version": surface.version,
                "elevation_hash": surface.elevation_hash,
                "terrain_changed": surface.elevation_hash
                != pinned_artifact["elevation_hash"],
            },
            "weather_id": weather_id,
            "gardeners_on_duty": int(gardeners),
            "occupied": {
                str(beat): sorted(beds) for beat, beds in sorted(occupancy.items()) if beds
            },
            "simulation": simulation.to_dict(),
        }
        self._record_handover(handover_id, contract_id, input_hash, status, report)
        return HandoverResult(simulation.ok, handover_id, contract_id, status, report, False)

    def _spec_from_json(self, raw: Mapping[str, Any]) -> PlanSpec:
        # The geometry was fully validated against the frozen surface at sign
        # time; reopening only reconstructs the immutable value object.
        return PlanSpec.from_dict(raw)

    # -- read-back / reopen ------------------------------------------------ #
    def get_contract(self, contract_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT artifact FROM contracts WHERE contract_id=?", (contract_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no such contract: {contract_id}")
        return json.loads(row[0])

    def sign_outcome(self, request_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT status, payload FROM sign_outcomes WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no sign outcome for request: {request_id}")
        return {"status": row[0], "payload": json.loads(row[1])}

    def list_contracts(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT contract_id, request_id, mode, catalog_version, elevation_hash, created_at "
            "FROM contracts ORDER BY contract_id"
        ).fetchall()
        return [
            {
                "contract_id": r[0],
                "request_id": r[1],
                "mode": r[2],
                "catalog_version": r[3],
                "elevation_hash": r[4],
                "created_at": r[5],
            }
            for r in rows
        ]

    def list_handovers(self, contract_id: str | None = None) -> list[dict[str, Any]]:
        if contract_id is None:
            rows = self._conn.execute(
                "SELECT handover_id, contract_id, status, created_at "
                "FROM handover_outcomes ORDER BY handover_id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT handover_id, contract_id, status, created_at "
                "FROM handover_outcomes WHERE contract_id=? ORDER BY handover_id",
                (contract_id,),
            ).fetchall()
        return [
            {"handover_id": r[0], "contract_id": r[1], "status": r[2], "created_at": r[3]}
            for r in rows
        ]

    def get_handover(self, handover_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT payload FROM handover_outcomes WHERE handover_id=?", (handover_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no such hand-over: {handover_id}")
        return json.loads(row[0])

    def reopen(self, contract_id: str) -> SignResult:
        """Re-open a signed contract exactly as stored (no recomputation)."""

        artifact = self.get_contract(contract_id)
        return SignResult(
            ok=True,
            request_id=artifact["request_id"],
            status="signed",
            contract_id=contract_id,
            artifact=artifact,
            replayed=True,
        )

    def verify(self, contract_id: str) -> dict[str, Any]:
        """Recompute the robust policy from the pinned frozen terrain."""

        try:
            artifact = self.get_contract(contract_id)
        except KeyError:
            return {"ok": False, "contract_id": contract_id,
                    "checks": {"exists": False}, "messages": ["contract not found"]}
        checks: dict[str, bool] = {"exists": True}
        messages: list[str] = []
        version = artifact["catalog_version"]
        try:
            surface = self.catalog.get_slice(version)
        except KeyError:
            return {"ok": False, "contract_id": contract_id,
                    "checks": {**checks, "surface_available": False},
                    "messages": ["catalog version missing"], "artifact": artifact}
        checks["surface_available"] = True
        checks["elevation_hash_matches"] = surface.elevation_hash == artifact["elevation_hash"]
        if not checks["elevation_hash_matches"]:
            messages.append("frozen elevation hash does not match the contract")
        # The hash covers the spec *embedded in the artifact*, so tampering with
        # the stored artifact is detected rather than silently re-validated.
        embedded_spec = PlanSpec.from_dict(artifact["spec"])
        checks["artifact_hash_matches"] = artifact["artifact_hash"] == sha256_hex(
            {"spec": embedded_spec.to_dict(), "elevation_hash": surface.elevation_hash}
        )
        if not checks["artifact_hash_matches"]:
            messages.append("stored artifact hash does not reproduce")
        # Independently recompute the robust policy from the trusted spec column
        # against the pinned frozen terrain.
        spec = self._spec_from_json(
            json.loads(
                self._conn.execute(
                    "SELECT spec FROM contracts WHERE contract_id=?", (contract_id,)
                ).fetchone()[0]
            )
        )
        checks["spec_column_matches_artifact"] = spec.to_dict() == embedded_spec.to_dict()
        if not checks["spec_column_matches_artifact"]:
            messages.append("stored spec column diverges from the artifact spec")
        engine = SunEngine(spec, surface)
        checks["robust_policy_feasible"] = engine.feasible()
        if not checks["robust_policy_feasible"]:
            messages.append("the robust sunshine policy no longer reproduces on the frozen terrain")
        return {
            "ok": all(checks.values()),
            "contract_id": contract_id,
            "checks": checks,
            "messages": messages,
            "artifact": artifact,
        }

    # -- storage helpers --------------------------------------------------- #
    def _replay_sign(self, request_id: str, input_hash: str) -> SignResult | None:
        row = self._conn.execute(
            "SELECT input_hash, status, payload FROM sign_outcomes WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        if row[0] != input_hash:
            raise AdoptionError(
                "idempotency_conflict",
                f"sign request id {request_id} was already used with different input",
                {"request_id": request_id},
            )
        status, payload = row[1], json.loads(row[2])
        if status == "signed":
            plan = self._conn.execute(
                "SELECT contract_id, artifact FROM contracts WHERE request_id=?", (request_id,)
            ).fetchone()
            if plan is None:
                raise AdoptionError(
                    "inconsistent_store", f"signed outcome {request_id} has no contract row"
                )
            return SignResult(True, request_id, "signed", plan[0], json.loads(plan[1]), True)
        return SignResult(False, request_id, status, None, payload, True)

    def _record_sign_outcome(
        self, request_id: str, input_hash: str, status: str, payload: Mapping[str, Any]
    ) -> None:
        self._conn.execute(
            "INSERT INTO sign_outcomes(request_id,input_hash,status,payload) VALUES (?,?,?,?)",
            (request_id, input_hash, status, json.dumps(payload, sort_keys=True)),
        )

    def _record_contract(
        self,
        contract_id: str,
        request_id: str,
        input_hash: str,
        spec: PlanSpec,
        surface: Any,
        artifact: Mapping[str, Any],
    ) -> None:
        spec_json = json.dumps(spec.to_dict(), sort_keys=True)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT INTO sign_outcomes(request_id,input_hash,status,payload) VALUES (?,?,?,?)",
                (request_id, input_hash, "signed",
                 json.dumps({"ok": True, "contract_id": contract_id}, sort_keys=True)),
            )
            self._conn.execute(
                "INSERT INTO contracts(contract_id,request_id,input_hash,mode,"
                "catalog_version,elevation_hash,spec,artifact) VALUES (?,?,?,?,?,?,?,?)",
                (
                    contract_id, request_id, input_hash, spec.mode,
                    surface.version, surface.elevation_hash, spec_json,
                    json.dumps(artifact, sort_keys=True),
                ),
            )
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def _replay_handover(self, handover_id: str, input_hash: str) -> HandoverResult | None:
        row = self._conn.execute(
            "SELECT input_hash, status, payload, contract_id FROM handover_outcomes WHERE handover_id=?",
            (handover_id,),
        ).fetchone()
        if row is None:
            return None
        if row[0] != input_hash:
            raise AdoptionError(
                "idempotency_conflict",
                f"hand-over id {handover_id} was already used with different input",
                {"handover_id": handover_id},
            )
        report = json.loads(row[2])
        return HandoverResult(
            row[1] == "delivered", handover_id, row[3], row[1], report, True
        )

    def _record_handover(
        self,
        handover_id: str,
        contract_id: str,
        input_hash: str,
        status: str,
        report: Mapping[str, Any],
    ) -> None:
        self._conn.execute(
            "INSERT INTO handover_outcomes(handover_id,contract_id,input_hash,status,payload) "
            "VALUES (?,?,?,?,?)",
            (handover_id, contract_id, input_hash, status, json.dumps(report, sort_keys=True)),
        )
