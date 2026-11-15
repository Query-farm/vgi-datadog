#!/usr/bin/env python3
"""A strict, offline stand-in for the Datadog API (v1 and v2), used by the tests.

It replays the documented response shapes from ``test/fixtures/datadog.json``
and is deliberately unforgiving, so a wrong request fails loudly:

* **Auth.** Every API request must send ``DD-API-KEY: fixture-api-key`` and
  ``DD-APPLICATION-KEY: fixture-app-key`` (403 ``{"errors": ["Forbidden"]}``
  otherwise).
* **Parameters.** Each route lists the query parameters it accepts; any other
  parameter is a 400. POST bodies are validated key by key (unknown keys,
  missing ``filter.from``/``filter.to``, non-ISO times, ``page.limit`` outside
  1..1000 are 400s).
* **Search.** ``filter.query`` of logs, spans, events and audit searches is
  *evaluated*: terms ``key:value``, ``key:"quoted \\" value"``, ``key:(a OR
  b)``, ``key:*``, ``-term``, ``AND`` / ``OR`` / parentheses and bare words
  (message contains). ``key`` is a top-level attribute, a tag key, or an
  ``@attribute.path``. The window is ``from`` inclusive, ``to`` exclusive. So a
  wrong pushdown returns wrong rows, not just a different log line.
* **Paging.** Search endpoints page with an opaque ``meta.page.after``
  cursor; list endpoints with their own page/offset parameters.
* **Rate limits.** ``MOCK_FLAKY=1`` answers the first request to every route
  with 429 and Datadog's ``X-RateLimit-*`` headers (``Reset: 0``); every other
  answer carries ``X-RateLimit-*`` headers too.

Test hooks: ``GET /__requests`` returns the request log (seq, method, path,
query string, body, search query, from, to, status); ``GET /__reset`` clears it,
and ``GET /__reset?routes=1`` also forgets which routes were seen, so the next
request to each route is answered with 429 again under ``MOCK_FLAKY=1``.

Usage::

    python3 test/mock_api/server.py --fixture test/fixtures/datadog.json   # prints PORT:<n>
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

API_KEY = "fixture-api-key"
APP_KEY = "fixture-app-key"
FLAKY = os.environ.get("MOCK_FLAKY") == "1"

FIX: dict[str, Any] = {}
LOG: list[dict[str, Any]] = []
SEEN_ROUTES: set[str] = set()
LOCK = threading.Lock()


class RejectError(Exception):
    """An error response."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ------------------------------------------------------------------------------ search syntax


def tokenize(q: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    while i < len(q):
        c = q[i]
        if c.isspace():
            i += 1
        elif c in "()":
            tokens.append(c)
            i += 1
        else:
            j = i
            buf = ""
            while j < len(q) and not q[j].isspace() and q[j] not in "()":
                if q[j] == '"':
                    k = j + 1
                    while k < len(q) and q[k] != '"':
                        k += 2 if q[k] == "\\" else 1
                    if k >= len(q):
                        raise RejectError(400, f"unterminated quote in query: {q!r}")
                    buf += q[j : k + 1]
                    j = k + 1
                else:
                    buf += q[j]
                    j += 1
            # key:( ... ) value group stays attached to its key
            if buf.endswith(":") and j < len(q) and q[j] == "(":
                depth, k = 0, j
                while k < len(q):
                    if q[k] == '"':
                        k += 1
                        while k < len(q) and q[k] != '"':
                            k += 2 if q[k] == "\\" else 1
                    elif q[k] == "(":
                        depth += 1
                    elif q[k] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    k += 1
                buf += q[j : k + 1]
                j = k + 1
            tokens.append(buf)
            i = j
    return tokens


def unquote(v: str) -> str:
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        out, i, body = "", 0, v[1:-1]
        while i < len(body):
            if body[i] == "\\" and i + 1 < len(body):
                out += body[i + 1]
                i += 2
            else:
                out += body[i]
                i += 1
        return out
    return v


def value_matcher(raw: str) -> Callable[[list[str]], bool]:
    if raw.startswith("(") and raw.endswith(")"):
        parts = re.split(r"\s+OR\s+", raw[1:-1].strip())
        matchers = [value_matcher(p.strip()) for p in parts]
        return lambda vals: any(m(vals) for m in matchers)
    if raw == "*":
        return lambda vals: bool(vals)
    for op in (">=", "<=", ">", "<"):
        if raw.startswith(op):
            bound = float(raw[len(op) :])
            cmp = {">=": float.__ge__, "<=": float.__le__, ">": float.__gt__, "<": float.__lt__}[op]

            def numeric(vals: list[str], b: float = bound, c: Any = cmp) -> bool:
                for v in vals:
                    try:
                        if c(float(v), b):
                            return True
                    except ValueError:
                        pass
                return False

            return numeric
    quoted = raw.startswith('"')
    text = unquote(raw)
    if not quoted and "*" in text:
        rx = re.compile("^" + ".*".join(re.escape(p) for p in text.split("*")) + "$")
        return lambda vals: any(rx.match(v) for v in vals)
    return lambda vals: text in vals


class Parser:
    def __init__(self, tokens: list[str], lookup: Callable[[dict[str, Any], str], list[str]]) -> None:
        self.t = tokens
        self.i = 0
        self.lookup = lookup

    def peek(self) -> str | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def expr(self) -> Callable[[dict[str, Any]], bool]:
        left = self.and_expr()
        while self.peek() == "OR":
            self.i += 1
            right = self.and_expr()
            left = (lambda a, b: lambda r: a(r) or b(r))(left, right)
        return left

    def and_expr(self) -> Callable[[dict[str, Any]], bool]:
        parts = [self.unary()]
        while self.peek() not in (None, ")", "OR"):
            if self.peek() == "AND":
                self.i += 1
            parts.append(self.unary())
        return lambda r: all(p(r) for p in parts)

    def unary(self) -> Callable[[dict[str, Any]], bool]:
        tok = self.peek()
        if tok is None:
            raise RejectError(400, "query ended unexpectedly")
        if tok == "NOT":
            self.i += 1
            inner = self.unary()
            return lambda r: not inner(r)
        if tok.startswith("-") and len(tok) > 1:
            self.t[self.i] = tok[1:]
            inner = self.unary()
            return lambda r: not inner(r)
        if tok == "(":
            self.i += 1
            inner = self.expr()
            if self.peek() != ")":
                raise RejectError(400, "unbalanced parentheses in query")
            self.i += 1
            return inner
        self.i += 1
        if tok == "*":
            return lambda r: True
        m = re.match(r"^(@?[A-Za-z_][\w.\-]*):(.+)$", tok)
        if m:
            key, raw = m.group(1), m.group(2)
            match = value_matcher(raw)
            return lambda r: match(self.lookup(r, key))
        word = unquote(tok).lower()
        return lambda r: word in str(dig_attr(r, "message") or "").lower()


def dig_attr(rec: dict[str, Any], *path: str) -> Any:
    obj: Any = rec.get("attributes") or {}
    for p in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(p)
    return obj


def flat(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [x for item in v for x in flat(item)]
    if isinstance(v, bool):
        return ["true" if v else "false"]
    return [str(v)]


def lookup(rec: dict[str, Any], key: str) -> list[str]:
    a = rec.get("attributes") or {}
    if key.startswith("@"):
        obj: Any = a.get("custom") if "custom" in a else a.get("attributes")
        for p in key[1:].split("."):
            obj = obj.get(p) if isinstance(obj, dict) else None
        return flat(obj)
    if key in a and key not in ("attributes", "tags", "custom"):
        return flat(a[key])
    custom = a.get("custom") or {}
    if key in custom:
        return flat(custom[key])
    inner = a.get("attributes") or {}
    if key in inner and isinstance(inner, dict) and key not in ("evt", "asset", "usr"):
        return flat(inner[key])
    if key == "host" and isinstance(inner, dict) and inner.get("hostname"):
        return flat(inner["hostname"])
    if key == "tags":
        return list(a.get("tags") or [])
    prefix = key + ":"
    return [t[len(prefix) :] for t in a.get("tags") or [] if isinstance(t, str) and t.startswith(prefix)]


def compile_query(q: str) -> Callable[[dict[str, Any]], bool]:
    tokens = tokenize(q)
    if not tokens:
        raise RejectError(400, "empty query")
    p = Parser(tokens, lookup)
    fn = p.expr()
    if p.peek() is not None:
        raise RejectError(400, f"unexpected token {p.peek()!r} in query {q!r}")
    return fn


# ------------------------------------------------------------------------------ validation


def parse_time(v: Any, name: str) -> dt.datetime:
    if not isinstance(v, str):
        raise RejectError(400, f"{name} must be an ISO-8601 string")
    try:
        t = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as err:
        raise RejectError(400, f"{name}: invalid time {v!r}") from err
    if t.tzinfo is None:
        raise RejectError(400, f"{name}: time needs a timezone")
    return t


def check_keys(obj: Any, allowed: dict[str, Any], where: str) -> None:
    if not isinstance(obj, dict):
        raise RejectError(400, f"{where} must be an object")
    for k, v in obj.items():
        if k not in allowed:
            raise RejectError(400, f"unknown field {where}.{k}")
        sub = allowed[k]
        if isinstance(sub, dict):
            check_keys(v, sub, f"{where}.{k}")


SEARCH_BODY = {
    "filter": {"query": None, "from": None, "to": None, "indexes": None, "storage_tier": None},
    "sort": None,
    "page": {"cursor": None, "limit": None},
    "options": {"timezone": None, "timeOffset": None},
}
AGG_BODY = {
    "compute": None,
    "filter": {"query": None, "from": None, "to": None, "indexes": None},
    "group_by": None,
    "page": {"cursor": None},
    "options": None,
}


def search_window(filt: dict[str, Any]) -> tuple[str, dt.datetime, dt.datetime]:
    if "from" not in filt or "to" not in filt:
        raise RejectError(400, "filter.from and filter.to are required")
    lo, hi = parse_time(filt["from"], "filter.from"), parse_time(filt["to"], "filter.to")
    if lo > hi:
        raise RejectError(400, "filter.from is after filter.to")
    q = filt.get("query", "*")
    if not isinstance(q, str):
        raise RejectError(400, "filter.query must be a string")
    return q, lo, hi


def rec_time(rec: dict[str, Any]) -> dt.datetime:
    a = rec["attributes"]
    return dt.datetime.fromisoformat((a.get("timestamp") or a.get("start_timestamp")).replace("Z", "+00:00"))


def run_search(records: list[dict[str, Any]], body: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    check_keys(body, SEARCH_BODY, "body")
    filt = body.get("filter") or {}
    q, lo, hi = search_window(filt)
    ctx.update(search=q, frm=filt["from"], to=filt["to"])
    page = body.get("page") or {}
    limit = page.get("limit", 10)
    if not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise RejectError(400, "page.limit must be between 1 and 1000")
    sort = body.get("sort", "timestamp")
    if sort not in ("timestamp", "-timestamp"):
        raise RejectError(400, f"invalid sort {sort!r}")
    if "storage_tier" in filt and filt["storage_tier"] not in ("indexes", "online-archives", "flex"):
        raise RejectError(400, "invalid storage_tier")
    match = compile_query(q)
    hits = [r for r in records if lo <= rec_time(r) < hi and match(r)]
    hits.sort(key=lambda r: (rec_time(r), r["id"]), reverse=sort == "-timestamp")
    start = 0
    if page.get("cursor"):
        try:
            start = int(base64.b64decode(page["cursor"]).decode().removeprefix("off:"))
        except Exception as err:
            raise RejectError(400, "invalid page.cursor") from err
    chunk = hits[start : start + limit]
    meta: dict[str, Any] = {"status": "done", "elapsed": 3, "request_id": "req-1"}
    if start + limit < len(hits):
        meta["page"] = {"after": base64.b64encode(f"off:{start + limit}".encode()).decode()}
    return {"data": chunk, "meta": meta, "links": {"next": "https://api.datadoghq.com/..."} if "page" in meta else {}}


def measure(rec: dict[str, Any], metric: str | None) -> float | None:
    if metric is None:
        return None
    vals = lookup(rec, metric)
    try:
        return float(vals[0]) if vals else None
    except ValueError:
        return None


def aggregate(values: list[float], agg: str, count: int) -> float | None:
    if agg == "count":
        return float(count)
    if not values:
        return None
    values = sorted(values)
    if agg == "sum":
        return sum(values)
    if agg == "min":
        return values[0]
    if agg == "max":
        return values[-1]
    if agg == "avg":
        return sum(values) / len(values)
    if agg == "cardinality":
        return float(len(set(values)))
    pct = 50 if agg == "median" else int(agg[2:])
    idx = max(0, min(len(values) - 1, -(-pct * len(values) // 100) - 1))
    return values[idx]


def run_aggregate(
    records: list[dict[str, Any]], body: dict[str, Any], ctx: dict[str, Any], *, spans: bool
) -> dict[str, Any]:
    check_keys(body, AGG_BODY, "body")
    q, lo, hi = search_window(body.get("filter") or {})
    ctx.update(search=q, frm=body["filter"]["from"], to=body["filter"]["to"])
    computes = body.get("compute") or [{"aggregation": "count"}]
    if not isinstance(computes, list) or len(computes) != 1:
        raise RejectError(400, "exactly one compute is supported by the mock")
    c = computes[0]
    check_keys(c, {"aggregation": None, "metric": None, "type": None, "interval": None}, "compute[0]")
    agg = c.get("aggregation")
    if agg not in (
        "count",
        "cardinality",
        "sum",
        "min",
        "max",
        "avg",
        "median",
        "pc75",
        "pc90",
        "pc95",
        "pc98",
        "pc99",
    ):
        raise RejectError(400, f"invalid aggregation {agg!r}")
    if agg != "count" and not c.get("metric"):
        raise RejectError(400, "compute.metric is required")
    match = compile_query(q)
    hits = [r for r in records if lo <= rec_time(r) < hi and match(r)]
    facets = []
    limit = 10
    for g in body.get("group_by") or []:
        check_keys(
            g,
            {
                "facet": None,
                "limit": None,
                "sort": {"aggregation": None, "order": None, "type": None, "metric": None},
                "total": None,
                "missing": None,
                "histogram": None,
            },
            "group_by[]",
        )
        facets.append(g["facet"])
        limit = g.get("limit", 10)
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for r in hits:
        key = tuple((lookup(r, f) or ["N/A"])[0] for f in facets)
        groups.setdefault(key, []).append(r)
    interval = None
    if c.get("type") == "timeseries":
        m = re.match(r"^(\d+)([smhd])$", c.get("interval") or "")
        if not m:
            raise RejectError(400, "timeseries compute needs an interval like 5m")
        interval = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    buckets = []
    for key, recs in groups.items():
        vals = [v for v in (measure(r, c.get("metric")) for r in recs) if v is not None]
        if interval is None:
            value: Any = aggregate(vals, agg, len(recs))
        else:
            series: dict[int, list[dict[str, Any]]] = {}
            for r in recs:
                b = int(rec_time(r).timestamp()) // interval * interval
                series.setdefault(b, []).append(r)
            value = []
            for b in sorted(series):
                bv = [v for v in (measure(r, c.get("metric")) for r in series[b]) if v is not None]
                value.append(
                    {
                        "time": dt.datetime.fromtimestamp(b, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "value": aggregate(bv, agg, len(series[b])),
                    }
                )
        buckets.append((key, value, aggregate(vals, agg, len(recs)) or 0.0))
    buckets.sort(key=lambda b: (-b[2], b[0]))
    buckets = buckets[:limit] if facets else buckets
    if spans:
        return {
            "data": [
                {
                    "type": "bucket",
                    "id": f"b{i}",
                    "attributes": {"by": dict(zip(facets, k, strict=True)), "compute": {"c0": v}},
                }
                for i, (k, v, _) in enumerate(buckets)
            ],
            "meta": {"status": "done"},
        }
    return {
        "data": {"buckets": [{"by": dict(zip(facets, k, strict=True)), "computes": {"c0": v}} for k, v, _ in buckets]},
        "meta": {"status": "done"},
    }


# ------------------------------------------------------------------------------ routes


def ints(params: dict[str, str], name: str, default: int, hi: int | None = None) -> int:
    raw = params.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError as err:
        raise RejectError(400, f"{name} must be an integer") from err
    if v < 0 or (hi is not None and v > hi):
        raise RejectError(400, f"{name} out of range")
    return v


def r_metrics_query(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    for k in ("from", "to", "query"):
        if k not in p:
            raise RejectError(400, f"missing parameter {k}")
    frm, to = ints(p, "from", 0), ints(p, "to", 0)
    if frm >= to:
        raise RejectError(400, "from must be before to")
    q = p["query"]
    ctx.update(search=q, frm=str(frm), to=str(to))
    if q.count("{") != q.count("}"):
        return 400, {"status": "error", "error": "Error parsing query: unbalanced braces"}
    out = []
    for s in FIX["metric_queries"].get(q, []):
        pts = [pt for pt in s["pointlist"] if frm * 1000 <= pt[0] <= to * 1000]
        out.append({**s, "pointlist": pts, "length": len(pts)})
    return {
        "status": "ok",
        "res_type": "time_series",
        "series": out,
        "from_date": frm * 1000,
        "to_date": to * 1000,
        "query": q,
    }


def r_metrics_list(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    items = FIX["metrics"]
    if "filter[metric_type]" in p:
        items = [i for i in items if (i.get("attributes") or {}).get("metric_type") == p["filter[metric_type]"]]
    size = ints(p, "page[size]", 10_000, 10_000)
    start = int(p["page[cursor]"].removeprefix("c")) if "page[cursor]" in p else 0
    chunk = items[start : start + size]
    nxt = f"c{start + size}" if start + size < len(items) else None
    return {"data": chunk, "meta": {"pagination": {"next_cursor": nxt}}, "links": {}}


def r_metric_meta(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    meta = FIX["metric_metadata"].get(m.group(1))
    if meta is None:
        raise RejectError(404, "Metric not found")
    return meta


def r_metric_tags(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    tags = FIX["metric_tags"].get(m.group(1))
    if tags is None:
        raise RejectError(404, "Metric not found")
    return {"data": {"type": "metrics", "id": m.group(1), "attributes": {"tags": tags}}}


def r_logs(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    if "indexes" not in (body or {}).get("filter", {}):
        raise RejectError(400, "filter.indexes expected")
    if body["filter"]["indexes"] == ["__no_index_org__"]:
        # What a live org with no index answers (verified 2026-11-15), for the worker's hint.
        raise RejectError(400, "invalid_argument(No valid indexes specified)")
    return run_search(FIX["logs"], body, ctx)


def r_spans(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    if not isinstance(body, dict) or set(body) != {"data"} or (body["data"] or {}).get("type") != "search_request":
        raise RejectError(400, "spans search body must be {data: {type: search_request, attributes: ...}}")
    check_keys(body["data"], {"type": None, "attributes": None}, "data")
    return run_search(FIX["spans"], body["data"]["attributes"], ctx)


def r_spans_agg(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    if not isinstance(body, dict) or (body.get("data") or {}).get("type") != "aggregate_request":
        raise RejectError(400, "spans aggregate body must be {data: {type: aggregate_request, ...}}")
    return run_aggregate(FIX["spans"], body["data"]["attributes"], ctx, spans=True)


def offset_page(items: list[Any], start: int, size: int) -> list[Any]:
    return items[start : start + size]


def r_monitors(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    items = FIX["monitors"]
    if "name" in p:
        items = [i for i in items if p["name"].lower() in i["name"].lower()]
    page, size = ints(p, "page", 0), ints(p, "page_size", 100, 1000)
    out = offset_page(items, page * size, size)
    if p.get("group_states") != "all":
        out = [{k: v for k, v in i.items() if k != "state"} for i in out]
    return out


def r_monitor(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    for i in FIX["monitors"]:
        if str(i["id"]) == m.group(1):
            return i if p.get("group_states") == "all" else {k: v for k, v in i.items() if k != "state"}
    raise RejectError(404, "Monitor not found")


def r_downtimes(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    items = FIX["downtimes"]
    if p.get("current_only") not in (None, "true", "false"):
        raise RejectError(400, "current_only must be true or false")
    if p.get("current_only") == "true":
        items = [i for i in items if i["attributes"]["status"] == "active"]
    start, size = ints(p, "page[offset]", 0), ints(p, "page[limit]", 30, 100)
    return {"data": offset_page(items, start, size), "meta": {"page": {"total_filtered_count": len(items)}}}


def r_slos(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    items = FIX["slos"]
    if "ids" in p:
        wanted = p["ids"].split(",")
        items = [i for i in items if i["id"] in wanted]
    start, size = ints(p, "offset", 0), ints(p, "limit", 1000, 1000)
    return {"data": offset_page(items, start, size), "metadata": {"page": {"total_count": len(items)}}}


def r_slo_history(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    h = FIX["slo_history"].get(m.group(1))
    if h is None:
        raise RejectError(404, "SLO not found")
    frm, to = ints(p, "from_ts", -1), ints(p, "to_ts", -1)
    if frm < 0 or to <= frm:
        raise RejectError(400, "from_ts and to_ts (epoch seconds) are required")
    ctx.update(frm=str(frm), to=str(to))
    return {"data": {**h, "from_ts": frm, "to_ts": to}}


def r_incidents(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    items = FIX["incidents"]
    start, size = ints(p, "page[offset]", 0), ints(p, "page[size]", 10, 100)
    chunk = offset_page(items, start, size)
    nxt = start + size if start + size < len(items) else None
    return {"data": chunk, "meta": {"pagination": {"offset": start, "next_offset": nxt, "size": len(chunk)}}}


def r_dashboards(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    start, count = ints(p, "start", 0), ints(p, "count", 100, 1000)
    return {"dashboards": offset_page(FIX["dashboards"], start, count)}


def r_dashboard(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    d = FIX["dashboard_definitions"].get(m.group(1))
    if d is None:
        raise RejectError(404, "Dashboard not found")
    return d


def r_synthetics(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    num, size = ints(p, "page_number", 0), ints(p, "page_size", 100, 100)
    return {"tests": offset_page(FIX["synthetics"], num * size, size)}


def r_synthetic(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    for t in FIX["synthetics"]:
        if t["public_id"] == m.group(1):
            return t
    raise RejectError(404, "Synthetics test not found")


def r_synthetic_results(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    browser, pid = m.group(1) is not None, m.group(2)
    test = r_synthetic(p, body, ctx, re.match(r"(.*)", pid))  # type: ignore[arg-type]
    if browser != (test["type"] == "browser"):
        raise RejectError(
            400,
            f"test {pid} is a {test['type']} test; use the "
            f"{'browser' if test['type'] == 'browser' else 'API'} "
            "results endpoint",
        )
    frm, to = ints(p, "from_ts", -1), ints(p, "to_ts", -1)
    if frm < 0 or to <= frm:
        raise RejectError(400, "from_ts and to_ts (epoch milliseconds) are required")
    if frm < 10_000_000_000:
        raise RejectError(400, "from_ts must be in milliseconds")
    ctx.update(frm=str(frm), to=str(to))
    res = [r for r in FIX["synthetics_results"].get(pid, []) if frm <= r["check_time"] <= to]
    return {"results": res, "last_timestamp_fetched": to}


def r_hosts(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    items = FIX["hosts"]
    if "filter" in p:
        items = [h for h in items if p["filter"] in h["host_name"]]
    start, count = ints(p, "start", 0), ints(p, "count", 100, 1000)
    chunk = offset_page(items, start, count)
    return {"host_list": chunk, "total_returned": len(chunk), "total_matching": len(items)}


def r_host_tags(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    return {"tags": FIX["host_tags"]}


def number_paged(key: str) -> Callable[..., Any]:
    def handler(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
        items = FIX[key]
        if key == "users" and "filter[status]" in p:
            wanted = p["filter[status]"].split(",")
            items = [u for u in items if u["attributes"]["status"] in wanted]
        if key == "services" and p.get("schema_version") not in (None, "v2", "v2.1", "v2.2"):
            raise RejectError(400, "invalid schema_version")
        num, size = ints(p, "page[number]", 0), ints(p, "page[size]", 10, 100)
        return {"data": offset_page(items, num * size, size), "meta": {"page": {"total_count": len(items)}}}

    return handler


def r_usage(p: dict[str, str], body: Any, ctx: dict[str, Any], m: re.Match[str]) -> Any:
    if "start_month" not in p or not re.match(r"^\d{4}-\d{2}$", p["start_month"]):
        raise RejectError(400, "start_month (YYYY-MM) is required")
    if "end_month" in p and not re.match(r"^\d{4}-\d{2}$", p["end_month"]):
        raise RejectError(400, "end_month must be YYYY-MM")
    if os.environ.get("MOCK_USAGE_FORBIDDEN") == "1":
        raise RejectError(403, "Forbidden: usage_read permission required")
    lo, hi = p["start_month"], p.get("end_month", "9999-12")
    return {"usage": [u for u in FIX["usage"] if lo <= u["date"][:7] <= hi]}


# (method, regex, allowed query params, handler)
ROUTES: list[tuple[str, str, set[str], Callable[..., Any]]] = [
    ("GET", r"/api/v1/query", {"from", "to", "query"}, r_metrics_query),
    (
        "GET",
        r"/api/v2/metrics",
        {
            "filter[configured]",
            "filter[tags_configured]",
            "filter[metric_type]",
            "filter[include_percentiles]",
            "filter[queried]",
            "filter[tags]",
            "window[seconds]",
            "page[size]",
            "page[cursor]",
        },
        r_metrics_list,
    ),
    ("GET", r"/api/v1/metrics/([^/]+)", set(), r_metric_meta),
    ("GET", r"/api/v2/metrics/([^/]+)/all-tags", set(), r_metric_tags),
    ("POST", r"/api/v2/logs/events/search", set(), r_logs),
    (
        "POST",
        r"/api/v2/logs/analytics/aggregate",
        set(),
        lambda p, b, c, m: run_aggregate(FIX["logs"], b, c, spans=False),
    ),
    ("POST", r"/api/v2/spans/events/search", set(), r_spans),
    ("POST", r"/api/v2/spans/analytics/aggregate", set(), r_spans_agg),
    ("POST", r"/api/v2/events/search", set(), lambda p, b, c, m: run_search(FIX["events"], b, c)),
    ("POST", r"/api/v2/audit/events/search", set(), lambda p, b, c, m: run_search(FIX["audit"], b, c)),
    (
        "GET",
        r"/api/v1/monitor",
        {"group_states", "name", "tags", "monitor_tags", "with_downtimes", "id_offset", "page", "page_size"},
        r_monitors,
    ),
    ("GET", r"/api/v1/monitor/(\d+)", {"group_states", "with_downtimes"}, r_monitor),
    ("GET", r"/api/v2/downtime", {"current_only", "include", "page[offset]", "page[limit]"}, r_downtimes),
    ("GET", r"/api/v1/slo", {"ids", "query", "tags_query", "metrics_query", "limit", "offset"}, r_slos),
    ("GET", r"/api/v1/slo/([^/]+)/history", {"from_ts", "to_ts", "target", "apply_correction"}, r_slo_history),
    ("GET", r"/api/v2/incidents", {"include", "page[size]", "page[offset]"}, r_incidents),
    ("GET", r"/api/v1/dashboard", {"filter[shared]", "filter[deleted]", "count", "start"}, r_dashboards),
    ("GET", r"/api/v1/dashboard/([^/]+)", set(), r_dashboard),
    ("GET", r"/api/v1/synthetics/tests", {"page_size", "page_number"}, r_synthetics),
    ("GET", r"/api/v1/synthetics/tests/([^/]+)", set(), r_synthetic),
    (
        "GET",
        r"/api/v1/synthetics/tests/(browser/)?([^/]+)/results",
        {"from_ts", "to_ts", "probe_dc", "device_id"},
        r_synthetic_results,
    ),
    (
        "GET",
        r"/api/v1/hosts",
        {
            "filter",
            "sort_field",
            "sort_dir",
            "start",
            "count",
            "from",
            "include_muted_hosts_data",
            "include_hosts_metadata",
        },
        r_hosts,
    ),
    ("GET", r"/api/v1/tags/hosts", {"source"}, r_host_tags),
    (
        "GET",
        r"/api/v2/users",
        {"page[size]", "page[number]", "sort", "sort_dir", "filter", "filter[status]"},
        number_paged("users"),
    ),
    (
        "GET",
        r"/api/v2/team",
        {"page[size]", "page[number]", "sort", "include", "filter[keyword]", "filter[me]", "fields[team]"},
        number_paged("teams"),
    ),
    ("GET", r"/api/v2/roles", {"page[size]", "page[number]", "sort", "filter", "filter[id]"}, number_paged("roles")),
    (
        "GET",
        r"/api/v2/services/definitions",
        {"page[size]", "page[number]", "schema_version"},
        number_paged("services"),
    ),
    (
        "GET",
        r"/api/v1/usage/summary",
        {"start_month", "end_month", "include_org_details", "include_connected_accounts"},
        r_usage,
    ),
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _reply(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        url = urlparse(self.path)
        if url.path == "/__requests":
            with LOCK:
                self._reply(200, list(LOG))
            return
        if url.path == "/__reset":
            with LOCK:
                LOG.clear()
                if "routes" in dict(parse_qsl(url.query)):
                    SEEN_ROUTES.clear()
            self._reply(200, {"ok": True})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        pairs = parse_qsl(url.query, keep_blank_values=True)
        params = dict(pairs)
        ctx: dict[str, Any] = {}
        entry: dict[str, Any] = {"method": method, "path": url.path, "query": url.query, "body": raw.decode() or None}
        status, payload, headers = (
            200,
            None,
            {
                "X-RateLimit-Limit": "100",
                "X-RateLimit-Period": "10",
                "X-RateLimit-Remaining": "99",
                "X-RateLimit-Reset": "5",
            },
        )
        try:
            if self.headers.get("DD-API-KEY") != API_KEY or self.headers.get("DD-APPLICATION-KEY") != APP_KEY:
                raise RejectError(403, "Forbidden")
            for m_, rx, allowed, handler in ROUTES:  # noqa: B007 (used after the loop)
                mt = re.fullmatch(rx, url.path)
                if m_ == method and mt:
                    break
            else:
                raise RejectError(404, f"no route for {method} {url.path}")
            headers["X-RateLimit-Name"] = rx
            if len(pairs) != len(params):
                raise RejectError(400, "repeated query parameter")
            for k in params:
                if k not in allowed:
                    raise RejectError(400, f"Unknown parameter: {k}")
            with LOCK:
                first = (method, rx) not in SEEN_ROUTES
                SEEN_ROUTES.add((method, rx))  # type: ignore[arg-type]
            if FLAKY and first:
                status = 429
                payload = {"errors": ["Rate limit of 100 requests in 10 seconds reached. Please try again later."]}
                headers.update({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"})
            else:
                body = None
                if method == "POST":
                    if self.headers.get("Content-Type") != "application/json":
                        raise RejectError(400, "Content-Type must be application/json")
                    try:
                        body = json.loads(raw)
                    except ValueError as err:
                        raise RejectError(400, "body is not JSON") from err
                result = handler(params, body, ctx, mt)
                if isinstance(result, tuple):
                    status, payload = result
                else:
                    payload = result
        except RejectError as err:
            status, payload = err.status, {"errors": [err.message]}
        entry.update(status=status, search=ctx.get("search"), frm=ctx.get("frm"), to=ctx.get("to"))
        with LOCK:
            entry["seq"] = len(LOG) + 1
            LOG.append(entry)
        self._reply(status, payload, headers)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", default=str(Path(__file__).resolve().parent.parent / "fixtures" / "datadog.json"))
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()
    FIX.update(json.loads(Path(args.fixture).read_text()))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"PORT:{server.server_address[1]}", flush=True)
    sys.stdout.flush()
    server.serve_forever()


if __name__ == "__main__":
    main()
