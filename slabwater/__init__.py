"""Independent elevation catalog and opened-outlet storage plan verifier.

Synthetic discrete model only -- not for field hydraulic decisions.
"""

from .catalog import Catalog
from .model import (
    BatchError,
    BatchResult,
    Cell,
    Outlet,
    Slice,
    SourceRef,
    TileSnapshot,
    WaterResult,
    canonical_json,
    sha256_hex,
)
from .publisher import PublishError, PublishResult, Publisher, VerifyResult
from .overprint import (
    Evaluation,
    OverprintError,
    Plate,
    Registration,
    Sheet,
    SheetResult,
    evaluate,
    inked_cells,
    orient,
    project,
)
from .solver import BottleneckCache, SolveResult, SolverStats, enumerate_subsets, solve
from .studio import Studio, StudioResult

__all__ = [
    "BatchError",
    "BatchResult",
    "BottleneckCache",
    "Catalog",
    "Cell",
    "Evaluation",
    "Outlet",
    "OverprintError",
    "Plate",
    "PublishError",
    "PublishResult",
    "Publisher",
    "Registration",
    "Sheet",
    "SheetResult",
    "Slice",
    "SolveResult",
    "SolverStats",
    "SourceRef",
    "Studio",
    "StudioResult",
    "TileSnapshot",
    "VerifyResult",
    "WaterResult",
    "canonical_json",
    "enumerate_subsets",
    "evaluate",
    "inked_cells",
    "orient",
    "project",
    "sha256_hex",
    "solve",
]
