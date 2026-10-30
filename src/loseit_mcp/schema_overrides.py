"""Runtime schema overrides for GWT types the pinned SDK catalog lacks.

Lose It's web build changed ``GoalsSummary`` — one BOOLEAN field removed —
which changed its GWT serialization hash from ``907689655`` to ``26794011``.
The pinned ``lose-it`` SDK (0.8.5) catalog only knows the old hash, so every
``getDailyDetailsIncludingPendingForDateRange`` response decodes as partial
and weight history fails on every date, even though the response itself is
small and well-formed.

The replacement layout below was read from the live bundle's own
``GoalsSummary`` deserializer (permutation
``19AE9651665AF1B41A5040BB55DBF595``, deserializer ``nPg``). Diffed
against the old 23-field layout, the only change is the missing BOOLEAN at
position 13; the layout was verified by strict-decoding a live range
response and matching the extracted weights against Hevy.

Overrides are additive and idempotent: a type already present in the
catalog is left alone, so a future SDK release that ships the new hash
simply supersedes this module.
"""

from __future__ import annotations

from lose_it.core._decoder import _catalog, _Schema

GOALS_SUMMARY_FQCN = "com.loseit.core.client.model.GoalsSummary/26794011"

# Field kinds in wire order, from the live bundle's GoalsSummary deserializer.
GOALS_SUMMARY_FIELDS: list[str] = [
    "OBJECT",
    "OBJECT",
    "BOOLEAN",
    "DOUBLE",
    "OBJECT",
    "OBJECT",
    "DOUBLE",
    "OBJECT",
    "OBJECT",
    "OBJECT",
    "DOUBLE",
    "BOOLEAN",
    "DOUBLE",
    "OBJECT",
    "OBJECT",
    "OBJECT",
    "OBJECT",
    "OBJECT",
    "OBJECT",
    "OBJECT",
    "DOUBLE",
    "BOOLEAN",
]

_OVERRIDES: dict[str, list[str]] = {
    GOALS_SUMMARY_FQCN: GOALS_SUMMARY_FIELDS,
}


def register_schema_overrides() -> None:
    """Register missing GWT schemas into the SDK decoder catalog.

    Idempotent, and never overwrites a schema the catalog already has.
    """
    catalog = _catalog()
    for fqcn, fields in _OVERRIDES.items():
        if fqcn not in catalog.schemas:
            catalog.schemas[fqcn] = _Schema(
                fqcn=fqcn,
                fields=list(fields),
                is_enum=False,
            )
