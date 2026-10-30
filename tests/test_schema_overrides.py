"""Schema overrides for GWT types the pinned SDK catalog lacks.

Regression: Lose It changed ``GoalsSummary`` (one BOOLEAN field removed),
changing its serialization hash to ``26794011``. The pinned SDK catalog
only knows the old hash, so every range response decoded partial and weight
history failed on every date. The override registers the new layout.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from lose_it.core._decoder import _catalog, _Schema, decode_response

from loseit_mcp import weight as W
from loseit_mcp.schema_overrides import (
    GOALS_SUMMARY_FIELDS,
    GOALS_SUMMARY_FQCN,
    register_schema_overrides,
)
from loseit_mcp.weight import get_weight_history

GS = GOALS_SUMMARY_FQCN
RW = "com.loseit.core.client.model.RecordedWeight/1309383152"
DD = "com.loseit.core.shared.model.DayDate/1611136587"
AL = "java.util.ArrayList/4159755760"

# The 22-field layout read from the live bundle's GoalsSummary deserializer.
EXPECTED_FIELDS: list[str] = [
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


@pytest.fixture(autouse=True)
def _restore_catalog():
    """The SDK catalog is process-global; leave it as we found it."""
    catalog = _catalog()
    snapshot = dict(catalog.schemas)
    yield
    catalog.schemas.clear()
    catalog.schemas.update(snapshot)


def _synthetic_response() -> str:
    """Minimal GWT-RPC response: an ArrayList of [GoalsSummary, RecordedWeight].

    Token array is the reverse of consumption order (the decoder pops
    right-to-left): the top-level type ref comes last. OBJECT fields are
    None (type ref 0), booleans 0, doubles 0.0. The RecordedWeight carries
    day_num 9433 and 201.5 lb. Contains no real user data.
    """
    table = [AL, GS, RW, DD]
    ref = {name: i + 1 for i, name in enumerate(table)}

    consume: list[Any] = [ref[AL], 2, ref[GS]]
    for kind in GOALS_SUMMARY_FIELDS:
        consume.append(0 if kind in ("OBJECT", "BOOLEAN") else 0.0)
    consume.append(ref[RW])
    consume.append(ref[DD])  # DayDate type ref (RecordedWeight f0)
    consume.append(0)  # DayDate f0 OBJECT -> None
    consume.append(9433)  # DayDate f1 RAW -> day_num
    consume.append(0)  # DayDate f2 RAW
    consume.append(0)  # RecordedWeight f1 BOOLEAN False
    consume.append("A")  # RecordedWeight f2 LONG -> 0
    consume.append(201.5)  # RecordedWeight f3 DOUBLE

    tokens = list(reversed(consume))
    data = ",".join(str(t) for t in tokens)
    table_str = ",".join(f'"{s}"' for s in table)
    return "//OK[" + data + ",[" + table_str + "]]"


def test_registers_exact_fields_and_is_idempotent() -> None:
    catalog = _catalog()
    catalog.schemas.pop(GS, None)

    register_schema_overrides()
    assert catalog.schemas[GS].fields == EXPECTED_FIELDS
    assert catalog.schemas[GS].is_enum is False

    first = catalog.schemas[GS]
    register_schema_overrides()
    assert catalog.schemas[GS] is first


def test_does_not_clobber_existing_schema() -> None:
    catalog = _catalog()
    sentinel = _Schema(fqcn=GS, fields=["STRING"], is_enum=False)
    catalog.schemas[GS] = sentinel

    register_schema_overrides()
    assert catalog.schemas[GS] is sentinel


def test_response_decodes_only_with_override() -> None:
    catalog = _catalog()
    catalog.schemas.pop(GS, None)
    payload = _synthetic_response()

    # Without the override the unknown type degrades to a partial decode.
    partial = decode_response(payload)
    assert isinstance(partial, dict) and partial.get("__partial__") is True
    with pytest.raises(KeyError):
        decode_response(payload, strict=True)

    register_schema_overrides()
    decoded = decode_response(payload, strict=True)
    items = decoded["items"]
    assert items[0]["__type__"] == GS
    assert items[1]["__type__"] == RW
    assert items[1]["f0"]["f1"] == 9433
    assert items[1]["f3"] == 201.5


def test_get_weight_history_registers_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog()
    catalog.schemas.pop(GS, None)
    monkeypatch.setattr(W, "_fetch_window", lambda http, s, e: ([], False))

    get_weight_history(None, date(2026, 10, 29), date(2026, 10, 29))
    assert GS in catalog.schemas
