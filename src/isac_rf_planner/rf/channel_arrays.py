"""Compact encodings shared by large channel-analysis array products."""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np


LARGE_ARRAY_THRESHOLD_POINTS = 100_000


TERRAIN_STATE_LABELS: tuple[str, ...] = (
    "los",
    "terrain_shadow",
    "terrain_diffraction",
    "blocked",
    "free_space",
    "unknown",
)
_TERRAIN_STATE_CODE = {name: i for i, name in enumerate(TERRAIN_STATE_LABELS)}


def terrain_state_code(value: Any) -> int:
    state = str(value or "los").strip().lower()
    return int(_TERRAIN_STATE_CODE.get(state, _TERRAIN_STATE_CODE["unknown"]))


def terrain_state_label(code: Any) -> str:
    try:
        index = int(code)
    except Exception:
        return "unknown"
    if 0 <= index < len(TERRAIN_STATE_LABELS):
        return TERRAIN_STATE_LABELS[index]
    return "unknown"


def encode_terrain_states(values: Iterable[Any], *, count: int | None = None) -> np.ndarray:
    return np.fromiter((terrain_state_code(v) for v in values), dtype=np.uint8, count=-1 if count is None else count)


def terrain_state_encoding_metadata() -> dict[str, str]:
    return {str(i): label for i, label in enumerate(TERRAIN_STATE_LABELS)}
