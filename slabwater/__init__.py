"""Independent elevation catalog and opened-outlet storage plan verifier.

Synthetic discrete model only -- not for field hydraulic decisions.
"""

from .adoption import AdoptionRegistry, HandoverResult, SignResult
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
from .solver import BottleneckCache, SolveResult, SolverStats, enumerate_subsets, solve
from .sun import (
    AdoptionError,
    Bed,
    Blocker,
    HandoverSimulation,
    Move,
    PlanSpec,
    RayCard,
    ShadeResult,
    SunEngine,
    TraySpec,
    WeatherPattern,
    normalize_occupancy,
    parse_spec,
    shade_card,
    simulate_handover,
)

__all__ = [
    "AdoptionError",
    "AdoptionRegistry",
    "BatchError",
    "BatchResult",
    "Bed",
    "Blocker",
    "BottleneckCache",
    "Catalog",
    "Cell",
    "HandoverResult",
    "HandoverSimulation",
    "Move",
    "Outlet",
    "PlanSpec",
    "PublishError",
    "PublishResult",
    "Publisher",
    "RayCard",
    "ShadeResult",
    "SignResult",
    "Slice",
    "SolveResult",
    "SolverStats",
    "SourceRef",
    "SunEngine",
    "TileSnapshot",
    "TraySpec",
    "VerifyResult",
    "WaterResult",
    "WeatherPattern",
    "canonical_json",
    "enumerate_subsets",
    "normalize_occupancy",
    "parse_spec",
    "sha256_hex",
    "shade_card",
    "simulate_handover",
    "solve",
]
