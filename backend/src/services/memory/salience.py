"""Shared normalized salience for every memory-graph reader."""

from __future__ import annotations

import math
from typing import Any

from . import graph_fields as F


def degree_damp(degree: int | float) -> float:
    """Dampen hubs so a large component cannot dominate a focused memory."""
    try:
        bounded_degree = max(0.0, float(degree))
    except (TypeError, ValueError):
        bounded_degree = 0.0
    return 1.0 / (1.0 + math.log1p(bounded_degree))


def normalized_graph_salience(data: dict[str, Any]) -> float:
    """Return ``weight * degree_damp * status_gate`` for one graph node."""
    status = str(data.get(F.STATUS, F.NODE_STATUS_ACTIVE))
    if status in {F.NODE_STATUS_COMPLETED, F.NODE_STATUS_ABANDONED}:
        return 0.0
    try:
        weight = max(0.0, float(data.get(F.WEIGHT, 0.0) or 0.0))
    except (TypeError, ValueError):
        weight = 0.0
    return weight * degree_damp(data.get(F.DEGREE, 0))
