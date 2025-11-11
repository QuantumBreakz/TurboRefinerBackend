from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any


@dataclass
class StageState:
    name: str
    status: str = "pending"  # pending | running | ok | warn | fail | skipped
    duration_ms: float = 0.0


@dataclass
class PassMetrics:
    change_pct: float = 0.0
    tension_pct: float = 0.0
    latency_ms_avg: float = 0.0
    scanner_risk: Optional[float] = None
    toggle_stats: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PassTexts:
    prev: Optional[str] = None
    final: Optional[str] = None


@dataclass
class PassState:
    index: int
    stages: Dict[str, StageState] = field(default_factory=dict)
    metrics: PassMetrics = field(default_factory=PassMetrics)
    texts: PassTexts = field(default_factory=PassTexts)


@dataclass
class RunResult:
    file_path: str
    pass_index: int
    success: bool
    error: Optional[str] = None
    error_code: Optional[str] = None
    local_path: Optional[str] = None
    doc_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AnnotationSpan:
    start: int
    end: int
    rationale: str
    category: str  # e.g., clarity|brevity|tone|structure



@dataclass
class StrategyPlan:
    """Priority slotting for downstream refinement steps.
    - primary_strategy: one of {clarity, persuasion, brevity, formality}
    - secondary_strategy: optional second focus
    - modulators: list of remaining signals that modulate intensity/order
    """
    primary_strategy: str
    secondary_strategy: Optional[str] = None
    modulators: List[str] = field(default_factory=list)





