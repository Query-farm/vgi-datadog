"""Value conversion: Datadog JSON values to the Arrow types the tables declare."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable
from typing import Any

import pyarrow as pa

TSTZ = pa.timestamp("us", tz="UTC")
TAGS = pa.map_(pa.string(), pa.string())
STR_LIST = pa.list_(pa.string())
INT_LIST = pa.list_(pa.int64())
JSON = pa.json_()

# Epoch magnitudes: seconds < 1e11 (year 5138) <= milliseconds < 1e14 <= microseconds < 1e17 <= nanoseconds.
_MS_THRESHOLD = 100_000_000_000
_US_THRESHOLD = 100_000_000_000_000
_NS_THRESHOLD = 100_000_000_000_000_000


def parse_ts(value: Any) -> dt.datetime | None:
    """Parse an ISO-8601 string or an epoch number (s, ms, us or ns) as an aware UTC datetime.

    Args:
        value: The raw value.

    Returns:
        The datetime, or None when it cannot be read.

    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, int | float):
        n = float(value)
        if abs(n) >= _NS_THRESHOLD:
            n /= 1e9
        elif abs(n) >= _US_THRESHOLD:
            n /= 1e6
        elif abs(n) >= _MS_THRESHOLD:
            n /= 1e3
        try:
            return dt.datetime.fromtimestamp(n, dt.UTC)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.lstrip("-").replace(".", "", 1).isdigit():
        return parse_ts(float(text))
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def tag_map(tags: Iterable[Any] | None) -> list[tuple[str, str]] | None:
    """Turn Datadog ``key:value`` tags into MAP entries.

    A bare tag (no colon) maps to an empty string. A key that appears more than
    once keeps every value, joined with ``,`` in the order seen.

    Args:
        tags: Tag strings, or None.

    Returns:
        ``(key, value)`` pairs with unique keys, or None for None input.

    """
    if tags is None:
        return None
    out: dict[str, list[str]] = {}
    for tag in tags:
        if tag is None:
            continue
        key, _, value = str(tag).partition(":")
        values = out.setdefault(key, [])
        if value not in values:
            values.append(value)
    return [(k, ",".join(v)) for k, v in out.items()]


def tag_value(tags: Iterable[Any] | None, key: str) -> str | None:
    """Return the first value of tag ``key`` in a Datadog tag list, if present."""
    prefix = key + ":"
    for tag in tags or ():
        if isinstance(tag, str) and tag.startswith(prefix):
            return tag[len(prefix) :]
    return None


def json_text(value: Any) -> str | None:
    """Serialise a value as JSON text (None stays None)."""
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def dig(obj: Any, *path: str) -> Any:
    """Follow ``path`` through nested dicts, returning None at the first missing step."""
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def convert(value: Any, type_: pa.DataType) -> Any:
    """Convert one JSON value to a Python value of Arrow type ``type_``.

    Args:
        value: Value from a Datadog payload (already mapped by a row function).
        type_: Target Arrow type.

    Returns:
        A value ``pa.array`` accepts for that type, or None when it cannot be read.

    """
    if value is None:
        return None
    if getattr(type_, "extension_name", None) == "arrow.json":
        return value if isinstance(value, str) else json_text(value)
    if pa.types.is_timestamp(type_):
        return parse_ts(value)
    if pa.types.is_date(type_):
        if isinstance(value, dt.date):
            return value
        try:
            return dt.date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    if pa.types.is_boolean(type_):
        if isinstance(value, str):
            return value.lower() in ("true", "1", "t", "yes")
        return bool(value)
    if pa.types.is_integer(type_):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if pa.types.is_floating(type_):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if pa.types.is_map(type_):
        if isinstance(value, dict):
            return [(str(k), None if v is None else str(v)) for k, v in value.items()]
        return value if isinstance(value, list) else None
    if pa.types.is_list(type_):
        if not isinstance(value, list | tuple):
            return None
        return [convert(v, type_.value_type) for v in value]
    if pa.types.is_struct(type_):
        if not isinstance(value, dict):
            return None
        return {
            type_.field(i).name: convert(value.get(type_.field(i).name), type_.field(i).type)
            for i in range(type_.num_fields)
        }
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int | float):
        return str(value).lower() if isinstance(value, bool) else str(value)
    return json_text(value)


def to_batch(rows: list[dict[str, Any]], schema: pa.Schema) -> pa.RecordBatch:
    """Build a record batch of ``schema`` from row dicts keyed by column name.

    Args:
        rows: One dict per row; missing keys become NULL.
        schema: The (projected) output schema.

    Returns:
        The batch.

    """
    columns = {f.name: [convert(r.get(f.name), f.type) for r in rows] for f in schema}
    return pa.RecordBatch.from_pydict(columns, schema=schema)
