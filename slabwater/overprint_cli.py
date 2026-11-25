"""Command-line driver for overprint ("接印") materials.

Example::

    python3 -m slabwater overprint materials/overprint/input.json --db-dir /tmp/op
    python3 -m slabwater reopen-print /tmp/op <print_id>

The material describes a catalog (dimensions, plate tile batches) and the
overprint orders to run against specific plate versions.  The report lists, for
each order, whether a printable sheet exists or which concrete bleed cells make
it fail, plus the process and shift used.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .studio import Studio


def run_overprint_material(path: str | Path, db_dir: str | Path | None = None) -> dict[str, Any]:
    material = json.loads(Path(path).read_text(encoding="utf-8"))
    db_dir = Path(db_dir) if db_dir else Path(tempfile.mkdtemp(prefix="overprint-"))
    db_dir.mkdir(parents=True, exist_ok=True)
    catalog = Catalog(
        db_dir / "catalog.db", material["height"], material["width"], material["ceiling"]
    )
    studio = Studio(db_dir / "studio.db", catalog)
    report: dict[str, Any] = {
        "db_dir": str(db_dir),
        "batches": [],
        "orders": [],
        "prints": [],
    }
    try:
        for batch in material.get("batches", []):
            result = catalog.apply_batch(batch)
            report["batches"].append(
                {"version": result.version, "batch": result.to_dict()}
            )
        for order in material.get("orders", []):
            result = studio.print_order(order)
            entry: dict[str, Any] = {
                "order": order,
                "status": result.status,
                "replayed": result.replayed,
                "print_id": result.print_id,
                "artifact": result.artifact,
            }
            if result.status == "rejected":
                stored = studio.outcome_for_order(order["id"])
                entry["evidence"] = stored["payload"]
            elif result.status == "error":
                entry["error"] = studio.outcome_for_order(order["id"])["payload"]
            report["orders"].append(entry)
        report["prints"] = studio.list_prints()
        report["versions"] = catalog.list_versions()
    finally:
        studio.close()
        catalog.close()
    return report


def reopen_print(db_dir: str | Path, print_id: str) -> dict[str, Any]:
    db_dir = Path(db_dir)
    catalog = Catalog.open(db_dir / "catalog.db")
    studio = Studio(db_dir / "studio.db", catalog)
    try:
        artifact = studio.get_print(print_id)
        verification = studio.verify(print_id)
        return {"artifact": artifact, "verify": verification}
    finally:
        studio.close()
        catalog.close()
