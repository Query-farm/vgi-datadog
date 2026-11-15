r"""Translate DuckDB's pushed predicates into Datadog search syntax and time windows.

Logs, spans, events and audit events are searched with Datadog's query
language (``service:web status:error -env:staging``). Each scan maps some of
its SQL columns to a search attribute (``SEARCH_FIELDS``); predicates on them
become extra terms ANDed to the user's query:

==========================  ===============================
SQL                         Datadog search term
==========================  ===============================
``col = 'v'``               ``attr:"v"``
``col <> 'v'``              ``-attr:"v"``
``col IN ('a', 'b')``       ``attr:("a" OR "b")``
``col = 'a' OR col = 'b'``  ``attr:("a" OR "b")``
``col IS NOT NULL``         ``attr:*``
==========================  ===============================

Values are always double-quoted with ``\\`` and ``"`` escaped, so a value can
never change the query's structure. Predicates on the time column narrow the
request's ``from``/``to`` window instead. The scans set ``auto_apply_filters``,
so DuckDB re-applies every predicate to the rows that come back: anything not
translated (or only widened, such as ``ts > x`` sent as ``from = x``) still
holds.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any

from vgi.table_filter_pushdown import (
    AndFilter,
    ComparisonOp,
    ConstantFilter,
    Filter,
    InFilter,
    IsNotNullFilter,
    OrFilter,
    PushdownFilters,
)

from .convert import parse_ts

MAX_IN_VALUES = 100


def quote(value: str) -> str:
    r"""Double-quote a value for Datadog search syntax, escaping backslashes and quotes.

    Args:
        value: The literal value.

    Returns:
        The quoted value, e.g. ``"o\\"reilly"``.

    """
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _text(value: Any) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    return None


def _same_column_values(f: Filter) -> tuple[str, list[str]] | None:
    """``col = a OR col = b OR col IN (...)`` on one column: (column, values)."""
    if isinstance(f, ConstantFilter) and f.op == ComparisonOp.EQ:
        text = _text(f.value.as_py())
        return (f.column_name, [text]) if text is not None else None
    if isinstance(f, InFilter):
        items = [_text(v) for v in f.values.to_pylist() if v is not None]
        if not items or any(i is None for i in items) or len(items) > MAX_IN_VALUES:
            return None
        return f.column_name, [i for i in items if i is not None]
    if isinstance(f, OrFilter):
        column: str | None = None
        values: list[str] = []
        for child in f.children:
            sub = _same_column_values(child)
            if sub is None or (column is not None and sub[0] != column):
                return None
            column = sub[0]
            values.extend(sub[1])
        if column is None or len(values) > MAX_IN_VALUES:
            return None
        return column, values
    return None


def _term(f: Filter, fields: dict[str, str]) -> str | None:
    """Translate one filter into a search term, or None."""
    if isinstance(f, ConstantFilter) and f.op == ComparisonOp.NE:
        attr = fields.get(f.column_name)
        text = _text(f.value.as_py())
        return None if attr is None or text is None else f"-{attr}:{quote(text)}"
    if isinstance(f, IsNotNullFilter):
        attr = fields.get(f.column_name)
        return None if attr is None else f"{attr}:*"
    sub = _same_column_values(f)
    if sub is None:
        return None
    attr = fields.get(sub[0])
    if attr is None:
        return None
    values = list(dict.fromkeys(sub[1]))
    if len(values) == 1:
        return f"{attr}:{quote(values[0])}"
    return f"{attr}:(" + " OR ".join(quote(v) for v in values) + ")"


def _flatten(filters: PushdownFilters | None) -> list[Filter]:
    out: list[Filter] = []
    stack = list(filters.filters) if filters else []
    while stack:
        f = stack.pop(0)
        if isinstance(f, AndFilter):
            stack[0:0] = list(f.children)
        else:
            out.append(f)
    return out


def search_terms(filters: PushdownFilters | None, fields: dict[str, str]) -> list[str]:
    """Search terms for every translatable top-level predicate.

    Args:
        filters: The pushed-down filters (a conjunction), or None.
        fields: SQL column name to Datadog search attribute (``service``, ``@evt.name`` ...).

    Returns:
        Terms to AND onto the query.

    """
    terms = []
    for f in _flatten(filters):
        term = _term(f, fields)
        if term is not None and term not in terms:
            terms.append(term)
    return terms


def combine(query: str | None, terms: list[str]) -> str:
    """AND the pushed terms onto the user's query.

    Args:
        query: The user's search query ('' or '*' for everything).
        terms: Pushed terms.

    Returns:
        The final query string.

    """
    base = (query or "").strip()
    if base in ("", "*"):
        return " ".join(terms) if terms else "*"
    if not terms:
        return base
    return f"({base}) " + " ".join(terms)


@dataclass
class Window:
    """A time window: ``lo`` inclusive, ``hi`` inclusive-or-exclusive (Datadog re-checked by DuckDB)."""

    lo: dt.datetime | None = None
    hi: dt.datetime | None = None
    empty: bool = False
    pushed: list[str] = field(default_factory=list)

    def clamp(self, lo: dt.datetime | None, hi: dt.datetime | None) -> Window:
        """Intersect with another window."""
        new_lo = max(x for x in (self.lo, lo) if x is not None) if (self.lo or lo) else None
        new_hi = min(x for x in (self.hi, hi) if x is not None) if (self.hi or hi) else None
        empty = self.empty or (new_lo is not None and new_hi is not None and new_lo > new_hi)
        return Window(new_lo, new_hi, empty, self.pushed)


def time_window(filters: PushdownFilters | None, column: str) -> Window:
    """Compute the tightest ``[lo, hi]`` implied by top-level comparisons on ``column``.

    Args:
        filters: The pushed-down filters, or None.
        column: The timestamp column (``ts``).

    Returns:
        The window (unbounded sides are None).

    """
    w = Window()
    for f in _flatten(filters):
        if not isinstance(f, ConstantFilter) or f.column_name != column:
            continue
        value = parse_ts(f.value.as_py())
        if value is None:
            continue
        if f.op in (ComparisonOp.GE, ComparisonOp.GT, ComparisonOp.EQ):
            w = w.clamp(value, None)
        if f.op in (ComparisonOp.LE, ComparisonOp.LT, ComparisonOp.EQ):
            w = w.clamp(None, value)
        if f.op in (ComparisonOp.GE, ComparisonOp.GT, ComparisonOp.EQ, ComparisonOp.LE, ComparisonOp.LT):
            w.pushed.append(f.op.name)
    return w


def iso_ms(value: dt.datetime, *, ceil: bool = False) -> str:
    """Format as ``YYYY-MM-DDTHH:MM:SS.mmmZ`` in UTC, rounding down (or up) to the millisecond.

    Args:
        value: An aware datetime.
        ceil: Round up instead of down (for the upper bound).

    Returns:
        The ISO-8601 string.

    """
    utc = value.astimezone(dt.UTC)
    micro = utc.microsecond
    ms = micro // 1000
    if ceil and micro % 1000:
        ms += 1
    base = utc.replace(microsecond=0)
    if ms == 1000:
        base += dt.timedelta(seconds=1)
        ms = 0
    return base.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms:03d}Z"


def epoch_s(value: dt.datetime, *, ceil: bool = False) -> int:
    """Epoch seconds, rounded down (or up)."""
    ts = value.timestamp()
    return math.ceil(ts) if ceil else math.floor(ts)


def epoch_ms(value: dt.datetime, *, ceil: bool = False) -> int:
    """Epoch milliseconds, rounded down (or up)."""
    ts = value.timestamp() * 1000
    return math.ceil(ts) if ceil else math.floor(ts)
