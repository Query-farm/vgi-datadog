"""The ``main`` schema: Datadog telemetry (metrics, logs, spans, events, audit trail).

* ``metrics_query`` (GET /api/v1/query) returns long rows ``(metric, tags MAP,
  ts, value)`` that UNION with other metric workers' long format.
* ``logs``, ``spans``, ``events`` and ``audit_events`` page through the v2
  search endpoints with the opaque ``meta.page.after`` cursor. SQL predicates on
  their hoisted columns become search terms (``search.py``) and predicates on
  ``ts`` narrow the request window.
* ``logs_aggregate`` / ``spans_aggregate`` run the analytics endpoints and
  return long rows ``(groups MAP, ts, value)``.
"""

from __future__ import annotations

import datetime as dt
import urllib.parse
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.metadata import FunctionExample
from vgi.table_filter_pushdown import ComparisonOp, ConstantFilter
from vgi.table_function import ProcessParams

from .base import PagedScan
from .client import DatadogClient, DatadogError
from .convert import JSON, STR_LIST, TAGS, TSTZ, dig, json_text, parse_ts, tag_map, tag_value
from .meta import column
from .search import combine, epoch_s, iso_ms, search_terms, time_window

Q = "datadog.main"
AGGREGATIONS = ("count", "cardinality", "sum", "min", "max", "avg", "median", "pc75", "pc90", "pc95", "pc98", "pc99")
SEARCH_PAGE_CAP = 1000


def _ts_arg(position: int, doc: str) -> Any:
    return Arg(position, doc=doc, arrow_type=TSTZ)


# ---------------------------------------------------------------------------- metrics_query


@dataclass(slots=True, frozen=True, kw_only=True)
class MetricsQueryArgs:
    """Arguments for ``metrics_query(query, from_time, to_time)``."""

    query: Annotated[
        str,
        Arg(0, doc="A Datadog metric query, e.g. 'avg:system.cpu.user{env:prod} by {host}'. Comma-separate several."),
    ]
    from_time: Annotated[dt.datetime, _ts_arg(1, "Start of the time window, inclusive, e.g. 2026-11-01 00:00 UTC.")]
    to_time: Annotated[dt.datetime, _ts_arg(2, "End of the time window.")]


METRICS_QUERY_SCHEMA = pa.schema(
    [
        column("metric", pa.string(), "Metric name of the series, e.g. 'system.cpu.user'.", nullable=False),
        column("tags", TAGS, "Group-by tags of the series as a MAP (host -> 'web-1'); empty for an ungrouped query."),
        column("ts", TSTZ, "Point timestamp (start of the rollup interval).", nullable=False),
        column("value", pa.float64(), "Point value; NULL where Datadog reports a gap."),
        column("scope", pa.string(), "The series scope as Datadog prints it, e.g. 'env:prod,host:web-1'."),
        column("expression", pa.string(), "The expanded query expression that produced the series."),
        column("query_index", pa.int32(), "0-based index of the comma-separated query that produced the series."),
        column("aggr", pa.string(), "Space aggregation: avg, sum, min or max (NULL for raw)."),
        column("unit", pa.string(), "Unit name of the metric, e.g. 'percent' or 'byte'."),
        column("interval_seconds", pa.int64(), "Rollup interval of the points, in seconds."),
    ]
)


class MetricsQuery(PagedScan):
    """``metrics_query(query, from_time, to_time)``: timeseries points as long rows."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = METRICS_QUERY_SCHEMA
    FunctionArguments = MetricsQueryArgs
    EXAMPLES: ClassVar[list[FunctionExample]] = [
        FunctionExample(
            sql=(
                f"SELECT tags['host'] AS host, avg(value) AS avg_cpu FROM {Q}.metrics_query("
                "'avg:system.cpu.user{env:prod} by {host}', TIMESTAMPTZ '2026-11-01 00:00:00+00', "
                "TIMESTAMPTZ '2026-11-01 01:00:00+00') GROUP BY host ORDER BY avg_cpu DESC"
            ),
            description="Average CPU per production host over one hour",
        ),
        FunctionExample(
            sql=(
                f"SELECT date_trunc('minute', ts) AS minute, sum(value) AS requests FROM {Q}.metrics_query("
                "'sum:trace.http.request.hits{service:web}.as_count()', TIMESTAMPTZ '2026-11-01 00:00:00+00', "
                "TIMESTAMPTZ '2026-11-01 01:00:00+00') GROUP BY minute ORDER BY minute"
            ),
            description="Request counts per minute for one service",
        ),
    ]

    class Meta:
        """Function metadata."""

        name = "metrics_query"
        description = "Query Datadog metrics over a time window and return one row per series point"
        tags: ClassVar[dict[str, str]] = {}
        examples: ClassVar[list[Any]] = []

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One GET /api/v1/query for the whole window."""
        args: MetricsQueryArgs = params.args
        lo, hi = parse_ts(args.from_time), parse_ts(args.to_time)
        if lo is None or hi is None or lo >= hi:
            return [], None
        body = client.get(
            "/api/v1/query",
            [("from", epoch_s(lo)), ("to", epoch_s(hi, ceil=True)), ("query", args.query)],
        )
        if isinstance(body, dict) and body.get("status") == "error":
            raise DatadogError(f"metrics_query: {body.get('error') or 'query failed'}")
        rows: list[dict[str, Any]] = []
        for series in (body or {}).get("series") or []:
            units = series.get("unit") or []
            unit = units[0].get("name") if units and isinstance(units[0], dict) else None
            base = {
                "metric": series.get("metric"),
                "tags": tag_map(series.get("tag_set") or []),
                "scope": series.get("scope"),
                "expression": series.get("expression"),
                "query_index": series.get("query_index"),
                "aggr": series.get("aggr"),
                "unit": unit,
                "interval_seconds": series.get("interval"),
            }
            for point in series.get("pointlist") or []:
                if not isinstance(point, list | tuple) or len(point) < 2 or point[0] is None:
                    continue
                rows.append({**base, "ts": point[0], "value": point[1]})
        return rows, None


# ---------------------------------------------------------------------------- metrics (table)


class Metrics(PagedScan):
    """Rows for ``datadog.main.metrics`` (GET /api/v2/metrics)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("metric", pa.string(), "Metric name, e.g. 'system.cpu.user'.", nullable=False),
            column(
                "has_tag_configuration",
                pa.bool_(),
                "True when the metric has a Metrics without Limits tag configuration.",
                nullable=False,
            ),
            column("metric_type", pa.string(), "Type from the tag configuration: gauge, count, rate or distribution."),
            column("configured_tags", STR_LIST, "Tag keys kept by the tag configuration."),
            column("include_percentiles", pa.bool_(), "Distribution metrics: whether percentiles are enabled."),
            column("created_at", TSTZ, "When the tag configuration was created."),
            column("modified_at", TSTZ, "When the tag configuration was last changed."),
        ]
    )
    EXAMPLES: ClassVar[list[tuple[str, str]]] = [
        (
            "Metric names by namespace",
            "SELECT split_part(metric, '.', "
            "1) AS namespace, count(*) AS "
            f"metrics FROM {Q}.metrics GROUP BY "
            "namespace ORDER BY metrics DESC",
        ),
        (
            "Distribution metrics with percentiles",
            "SELECT metric, configured_tags FROM "
            f"{Q}.metrics WHERE metric_type "
            "= 'distribution' AND include_percentiles "
            "ORDER BY metric",
        ),
    ]

    class Meta:
        """Table metadata."""

        name = "metrics_scan"
        description = "Scan behind the metrics table"
        filter_pushdown = True
        auto_apply_filters = True

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page of the v2 metric list, cursor-paged."""
        query: list[tuple[str, Any]] = [("page[size]", client.config.page_size)]
        pushed = params.current_pushdown_filters
        for f in pushed.filters if pushed else ():
            if isinstance(f, ConstantFilter) and f.column_name == "metric_type" and f.op == ComparisonOp.EQ:
                query.append(("filter[metric_type]", str(f.value.as_py())))
        if cursor:
            query.append(("page[cursor]", cursor))
        body = client.get("/api/v2/metrics", query) or {}
        rows = []
        for item in body.get("data") or []:
            attrs = item.get("attributes") or {}
            rows.append(
                {
                    "metric": item.get("id"),
                    "has_tag_configuration": item.get("type") == "manage_tags",
                    "metric_type": attrs.get("metric_type"),
                    "configured_tags": attrs.get("tags"),
                    "include_percentiles": attrs.get("include_percentiles"),
                    "created_at": attrs.get("created_at"),
                    "modified_at": attrs.get("modified_at"),
                }
            )
        nxt = dig(body, "meta", "pagination", "next_cursor")
        return rows, (str(nxt) if nxt and rows else None)


# ---------------------------------------------------------------------------- metric_metadata / metric_tags


@dataclass(slots=True, frozen=True, kw_only=True)
class MetricArg:
    """One metric name."""

    metric: Annotated[str, Arg(0, doc="Metric name, e.g. 'system.cpu.user'.")]


def _metric_path(metric: str, suffix: str = "") -> str:
    if not metric.strip():
        raise DatadogError("metric name must not be empty")
    return "/api/v1/metrics/" + urllib.parse.quote(metric, safe="") + suffix


class MetricMetadata(PagedScan):
    """``metric_metadata(metric)``: type, unit and description of one metric."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("metric", pa.string(), "Metric name.", nullable=False),
            column("type", pa.string(), "Metric type: gauge, count, rate or distribution."),
            column("description", pa.string(), "Description set on the metric."),
            column("short_name", pa.string(), "Short display name."),
            column("unit", pa.string(), "Unit, e.g. 'byte'."),
            column("per_unit", pa.string(), "Per-unit, e.g. 'second' for bytes per second."),
            column("statsd_interval", pa.int64(), "Flush interval in seconds for DogStatsD metrics."),
            column("integration", pa.string(), "Integration that reports the metric, e.g. 'system'."),
        ]
    )
    FunctionArguments = MetricArg
    EXAMPLES: ClassVar[list[FunctionExample]] = [
        FunctionExample(
            sql=f"SELECT type, unit, per_unit FROM {Q}.metric_metadata('system.net.bytes_rcvd')",
            description="Type and unit of one metric",
        )
    ]

    class Meta:
        """Function metadata."""

        name = "metric_metadata"
        description = "Type, unit, description and integration of one Datadog metric"
        tags: ClassVar[dict[str, str]] = {}
        examples: ClassVar[list[Any]] = []

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /api/v1/metrics/{metric}."""
        metric = params.args.metric
        body = client.get(_metric_path(metric)) or {}
        return [{**body, "metric": metric}], None


class MetricTags(PagedScan):
    """``metric_tags(metric)``: every indexed tag of one metric (GET /api/v2/metrics/{metric}/all-tags)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("metric", pa.string(), "Metric name.", nullable=False),
            column("tag", pa.string(), "The tag as Datadog stores it, 'key:value'.", nullable=False),
            column("key", pa.string(), "Tag key (text before the first colon).", nullable=False),
            column("value", pa.string(), "Tag value (text after the first colon; '' for a bare tag).", nullable=False),
        ]
    )
    FunctionArguments = MetricArg
    EXAMPLES: ClassVar[list[FunctionExample]] = [
        FunctionExample(
            sql="SELECT key, count(*) AS "
            "distinct_values FROM "
            f"{Q}.metric_tags('system.cpu.user') "
            "GROUP "
            "BY key ORDER BY distinct_values DESC",
            description="Tag cardinality per key for one metric",
        )
    ]

    class Meta:
        """Function metadata."""

        name = "metric_tags"
        description = "Every tag value reported for one Datadog metric, split into key and value"
        tags: ClassVar[dict[str, str]] = {}
        examples: ClassVar[list[Any]] = []

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /api/v2/metrics/{metric}/all-tags."""
        metric = params.args.metric
        path = "/api/v2/metrics/" + urllib.parse.quote(metric, safe="") + "/all-tags"
        body = client.get(path) or {}
        tags = dig(body, "data", "attributes", "tags") or []
        rows = []
        for tag in sorted({str(t) for t in tags if t is not None}):
            key, _, value = tag.partition(":")
            rows.append({"metric": metric, "tag": tag, "key": key, "value": value})
        return rows, None


# ---------------------------------------------------------------------------- search scans


@dataclass(slots=True, frozen=True, kw_only=True)
class SearchArgs:
    """Arguments shared by ``logs``, ``spans``, ``events`` and ``audit_events``."""

    query: Annotated[str, Arg(0, doc="Datadog search query, e.g. 'service:web status:error'; '*' matches everything.")]
    from_time: Annotated[dt.datetime, _ts_arg(1, "Start of the time window, inclusive, e.g. 2026-11-01 00:00 UTC.")]
    to_time: Annotated[dt.datetime, _ts_arg(2, "End of the time window.")]
    max_rows: Annotated[
        int | None, Arg("max_rows", default=None, doc="Stop after this many rows; NULL reads the whole window.", ge=1)
    ]
    newest_first: Annotated[
        bool,
        Arg("newest_first", default=False, doc="Page from the newest record backwards (with max_rows: the latest N)."),
    ]


@dataclass(slots=True, frozen=True, kw_only=True)
class LogsArgs(SearchArgs):
    """``logs`` adds the indexes and storage tier to search."""

    indexes: Annotated[
        str | None,
        Arg("indexes", default=None, doc="Comma-separated log index names to search; NULL searches every index ('*')."),
    ]
    storage_tier: Annotated[
        str,
        Arg(
            "storage_tier",
            default="indexes",
            doc="Storage tier: 'indexes' (default), 'online-archives' or 'flex'.",
            choices=("indexes", "online-archives", "flex"),
        ),
    ]


class SearchScan(PagedScan):
    """Base for the v2 search endpoints (POST, cursor in ``meta.page.after``)."""

    PATH: ClassVar[str] = ""
    WRAP: ClassVar[str | None] = None  # spans wrap the request in {"data": {"type": ..., "attributes": ...}}
    SEARCH_FIELDS: ClassVar[dict[str, str]] = {}

    class Meta:
        """Overridden by every concrete scan."""

        name = "search_scan"
        description = "Base class for Datadog search scans"

    @classmethod
    def row(cls, item: dict[str, Any]) -> dict[str, Any]:
        """Map one result object to column values."""
        raise NotImplementedError

    @classmethod
    def extra_filter(cls, params: ProcessParams[Any]) -> dict[str, Any]:
        """Endpoint-specific ``filter`` fields (log indexes, storage tier)."""
        return {}

    @classmethod
    def max_rows(cls, params: ProcessParams[Any]) -> int | None:
        """Return the ``max_rows`` argument."""
        value: int | None = params.args.max_rows
        return value

    @classmethod
    def request_body(cls, params: ProcessParams[Any], cursor: str, limit: int) -> dict[str, Any] | None:
        """Build the search request for one page, or None when the window is empty."""
        args: SearchArgs = params.args
        pushed = params.current_pushdown_filters
        window = time_window(pushed, "ts")
        if window.hi is not None:
            # Datadog's `to` is exclusive; widen a pushed `ts <= x` by 1 ms (DuckDB re-checks the predicate).
            window.hi += dt.timedelta(milliseconds=1)
        window = window.clamp(parse_ts(args.from_time), parse_ts(args.to_time))
        if window.empty or window.lo is None or window.hi is None or window.lo > window.hi:
            return None
        attributes: dict[str, Any] = {
            "filter": {
                "query": combine(args.query, search_terms(pushed, cls.SEARCH_FIELDS)),
                "from": iso_ms(window.lo),
                "to": iso_ms(window.hi, ceil=True),
                **cls.extra_filter(params),
            },
            "sort": "-timestamp" if args.newest_first else "timestamp",
            "page": {"limit": limit, **({"cursor": cursor} if cursor else {})},
        }
        if cls.WRAP:
            return {"data": {"type": cls.WRAP, "attributes": attributes}}
        return attributes

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """POST one search page."""
        limit = min(client.config.page_size, SEARCH_PAGE_CAP)
        cap = cls.max_rows(params)
        body = cls.request_body(params, cursor, limit if cap is None else min(limit, cap))
        if body is None:
            return [], None
        resp = client.post(cls.PATH, body) or {}
        rows = [cls.row(item) for item in resp.get("data") or [] if isinstance(item, dict)]
        after = dig(resp, "meta", "page", "after")
        return rows, (str(after) if after and rows else None)


# ---------------------------------------------------------------------------- logs

LOG_FIELDS = {"service": "service", "status": "status", "host": "host", "env": "env", "source": "source"}


class Logs(SearchScan):
    """``logs(query, from_time, to_time, ...)``: POST /api/v2/logs/events/search."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("id", pa.string(), "Log event id (unique, opaque).", nullable=False),
            column("ts", TSTZ, "Log timestamp.", nullable=False),
            column("status", pa.string(), "Log status (level): emergency .. debug, e.g. 'error', 'warn', 'info'."),
            column("service", pa.string(), "Service that emitted the log."),
            column("host", pa.string(), "Host that emitted the log."),
            column("env", pa.string(), "The env tag (env:prod -> 'prod'), hoisted from tags."),
            column("source", pa.string(), "The source tag (integration / language), hoisted from tags."),
            column("message", pa.string(), "Log message."),
            column("trace_id", pa.string(), "Correlated APM trace id (dd.trace_id attribute), if any."),
            column("tags", TAGS, "Every tag on the log as a MAP (env -> 'prod'); repeated keys join values with ','."),
            column("attributes", JSON, "The log's custom attributes (facets referenced as @path in queries), JSON."),
        ]
    )
    FunctionArguments = LogsArgs
    PATH: ClassVar[str] = "/api/v2/logs/events/search"
    SEARCH_FIELDS: ClassVar[dict[str, str]] = LOG_FIELDS
    EXAMPLES: ClassVar[list[FunctionExample]] = [
        FunctionExample(
            sql=(
                f"SELECT service, count(*) AS errors FROM {Q}.logs('*', TIMESTAMPTZ '2026-11-01 00:00:00+00', "
                "TIMESTAMPTZ '2026-11-02 00:00:00+00') WHERE status = 'error' AND env = 'prod' "
                "GROUP BY service ORDER BY errors DESC"
            ),
            description="Production error logs per service for one day (status and env run in Datadog)",
        ),
        FunctionExample(
            sql=(
                f"SELECT ts, host, message, attributes->>'$.http.status_code' AS http_status FROM {Q}.logs("
                "'service:web @http.status_code:>=500', TIMESTAMPTZ '2026-11-01 00:00:00+00', "
                "TIMESTAMPTZ '2026-11-02 00:00:00+00', max_rows := 50, newest_first := true) ORDER BY ts DESC"
            ),
            description="The 50 latest 5xx logs of one service, with a facet read from attributes",
        ),
    ]

    class Meta:
        """Function metadata."""

        name = "logs"
        description = (
            "Search Datadog logs in a time window; SQL filters on service/status/host/env/source run in Datadog"
        )
        filter_pushdown = True
        auto_apply_filters = True
        tags: ClassVar[dict[str, str]] = {}
        examples: ClassVar[list[Any]] = []

    @classmethod
    def extra_filter(cls, params: ProcessParams[Any]) -> dict[str, Any]:
        """Indexes and storage tier."""
        args: LogsArgs = params.args
        indexes = [i.strip() for i in (args.indexes or "*").split(",") if i.strip()] or ["*"]
        return {"indexes": indexes, "storage_tier": args.storage_tier}

    @classmethod
    def row(cls, item: dict[str, Any]) -> dict[str, Any]:
        """Map one log event."""
        a = item.get("attributes") or {}
        tags = a.get("tags") or []
        custom = a.get("attributes") or {}
        trace = dig(custom, "dd", "trace_id") or custom.get("trace_id")
        return {
            "id": item.get("id"),
            "ts": a.get("timestamp"),
            "status": a.get("status"),
            "service": a.get("service"),
            "host": a.get("host"),
            "env": tag_value(tags, "env"),
            "source": tag_value(tags, "source"),
            "message": a.get("message"),
            "trace_id": None if trace is None else str(trace),
            "tags": tag_map(tags),
            "attributes": json_text(custom),
        }


# ---------------------------------------------------------------------------- spans

SPAN_FIELDS = {
    "service": "service",
    "env": "env",
    "host": "host",
    "resource_name": "resource_name",
    "operation_name": "operation_name",
    "status": "status",
    "trace_id": "trace_id",
}


class Spans(SearchScan):
    """``spans(query, from_time, to_time, ...)``: POST /api/v2/spans/events/search."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("span_id", pa.string(), "Span id.", nullable=False),
            column("trace_id", pa.string(), "Trace id the span belongs to.", nullable=False),
            column("parent_id", pa.string(), "Parent span id ('0' or NULL for a root span)."),
            column("ts", TSTZ, "Span start time.", nullable=False),
            column("end_ts", TSTZ, "Span end time."),
            column("duration_seconds", pa.float64(), "Span duration in seconds."),
            column("service", pa.string(), "Service name."),
            column("env", pa.string(), "Environment."),
            column("host", pa.string(), "Host."),
            column("resource_name", pa.string(), "Resource, e.g. 'GET /api/users'."),
            column("operation_name", pa.string(), "Operation, e.g. 'http.request'."),
            column("span_type", pa.string(), "Span type: web, db, cache, http, custom ..."),
            column("status", pa.string(), "'ok' or 'error'."),
            column("is_error", pa.bool_(), "True when status is 'error'."),
            column("tags", TAGS, "Span tags as a MAP."),
            column("attributes", JSON, "Custom span attributes (the @path facets), JSON."),
        ]
    )
    FunctionArguments = SearchArgs
    PATH: ClassVar[str] = "/api/v2/spans/events/search"
    WRAP: ClassVar[str | None] = "search_request"
    SEARCH_FIELDS: ClassVar[dict[str, str]] = SPAN_FIELDS
    EXAMPLES: ClassVar[list[FunctionExample]] = [
        FunctionExample(
            sql=(
                f"SELECT resource_name, count(*) AS spans, quantile_cont(duration_seconds, 0.95) AS p95_s, "
                f"avg(is_error::INT) AS error_rate FROM {Q}.spans('*', TIMESTAMPTZ '2026-11-01 00:00:00+00', "
                "TIMESTAMPTZ '2026-11-01 01:00:00+00') WHERE service = 'web' AND env = 'prod' "
                "GROUP BY resource_name ORDER BY p95_s DESC"
            ),
            description="p95 latency and error rate per endpoint of one service",
        ),
        FunctionExample(
            sql=(
                f"SELECT s.service, s.resource_name, l.message FROM {Q}.spans('status:error', "
                "TIMESTAMPTZ '2026-11-01 00:00:00+00', TIMESTAMPTZ '2026-11-01 01:00:00+00') s "
                f"JOIN {Q}.logs('*', TIMESTAMPTZ '2026-11-01 00:00:00+00', TIMESTAMPTZ '2026-11-01 01:00:00+00') l "
                "ON l.trace_id = s.trace_id ORDER BY s.service"
            ),
            description="Logs correlated with error spans through the trace id",
        ),
    ]

    class Meta:
        """Function metadata."""

        name = "spans"
        description = "Search APM spans in a time window; SQL filters on service/env/resource/status run in Datadog"
        filter_pushdown = True
        auto_apply_filters = True
        tags: ClassVar[dict[str, str]] = {}
        examples: ClassVar[list[Any]] = []

    @classmethod
    def row(cls, item: dict[str, Any]) -> dict[str, Any]:
        """Map one span."""
        a = item.get("attributes") or {}
        custom = a.get("custom") or {}
        start, end = parse_ts(a.get("start_timestamp")), parse_ts(a.get("end_timestamp"))
        duration = custom.get("duration")
        seconds = float(duration) / 1e9 if isinstance(duration, int | float) else None
        if seconds is None and start and end:
            seconds = (end - start).total_seconds()
        status = a.get("status") or custom.get("status")
        return {
            "span_id": a.get("span_id") or item.get("id"),
            "trace_id": a.get("trace_id"),
            "parent_id": a.get("parent_id"),
            "ts": start,
            "end_ts": end,
            "duration_seconds": seconds,
            "service": a.get("service"),
            "env": a.get("env"),
            "host": a.get("host"),
            "resource_name": a.get("resource_name"),
            "operation_name": a.get("operation_name") or custom.get("operation_name"),
            "span_type": a.get("type"),
            "status": status,
            "is_error": None if status is None else status == "error",
            "tags": tag_map(a.get("tags") or []),
            "attributes": json_text({k: v for k, v in custom.items() if k != "duration"}),
        }


# ---------------------------------------------------------------------------- events

EVENT_FIELDS = {
    "source": "source",
    "status": "status",
    "priority": "priority",
    "host": "host",
    "service": "service",
}


class Events(SearchScan):
    """``events(query, from_time, to_time, ...)``: POST /api/v2/events/search."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("id", pa.string(), "Event id.", nullable=False),
            column("ts", TSTZ, "Event time.", nullable=False),
            column("title", pa.string(), "Event title."),
            column("message", pa.string(), "Event text."),
            column("source", pa.string(), "Source: the integration or 'my_apps', e.g. 'github', 'kubernetes'."),
            column(
                "status",
                pa.string(),
                "Alert status: error, warning, info, success, user_update, recommendation, snapshot.",
            ),
            column("priority", pa.string(), "Priority: normal or low."),
            column("host", pa.string(), "Host the event is about."),
            column("service", pa.string(), "Service the event is about."),
            column("aggregation_key", pa.string(), "Key that groups related events."),
            column("tags", TAGS, "Event tags as a MAP."),
            column("attributes", JSON, "The full event attributes, JSON."),
        ]
    )
    FunctionArguments = SearchArgs
    PATH: ClassVar[str] = "/api/v2/events/search"
    SEARCH_FIELDS: ClassVar[dict[str, str]] = EVENT_FIELDS
    EXAMPLES: ClassVar[list[FunctionExample]] = [
        FunctionExample(
            sql=(
                f"SELECT date_trunc('day', ts) AS day, count(*) AS deploys FROM {Q}.events('tags:deploy', "
                "TIMESTAMPTZ '2026-10-01 00:00:00+00', TIMESTAMPTZ '2026-11-01 00:00:00+00') "
                "WHERE source = 'github' GROUP BY day ORDER BY day"
            ),
            description="GitHub deploy events per day for one month",
        )
    ]

    class Meta:
        """Function metadata."""

        name = "events"
        description = (
            "Search the Datadog event stream in a time window; filters on source/status/host/service run in Datadog"
        )
        filter_pushdown = True
        auto_apply_filters = True
        tags: ClassVar[dict[str, str]] = {}
        examples: ClassVar[list[Any]] = []

    @classmethod
    def row(cls, item: dict[str, Any]) -> dict[str, Any]:
        """Map one event."""
        a = item.get("attributes") or {}
        inner = a.get("attributes") or {}
        tags = a.get("tags") or []
        return {
            "id": item.get("id"),
            "ts": a.get("timestamp") or inner.get("timestamp"),
            "title": inner.get("title"),
            "message": a.get("message"),
            "source": tag_value(tags, "source") or inner.get("source_type_name"),
            "status": inner.get("status"),
            "priority": inner.get("priority"),
            "host": inner.get("hostname") or tag_value(tags, "host"),
            "service": inner.get("service") or tag_value(tags, "service"),
            "aggregation_key": inner.get("aggregation_key"),
            "tags": tag_map(tags),
            "attributes": json_text(inner),
        }


# ---------------------------------------------------------------------------- audit_events

AUDIT_FIELDS = {
    "service": "service",
    "event_name": "@evt.name",
    "action": "@action",
    "asset_type": "@asset.type",
    "user_email": "@usr.email",
}


class AuditEvents(SearchScan):
    """``audit_events(query, from_time, to_time, ...)``: POST /api/v2/audit/events/search."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("id", pa.string(), "Audit event id.", nullable=False),
            column("ts", TSTZ, "When the action happened.", nullable=False),
            column("service", pa.string(), "Product area, e.g. 'monitor', 'dashboard', 'apikey'."),
            column("event_name", pa.string(), "Event name (@evt.name), e.g. 'Request', 'Authentication'."),
            column("action", pa.string(), "Action (@action): created, modified, deleted, accessed ..."),
            column("asset_type", pa.string(), "Type of the asset acted on (@asset.type)."),
            column("asset_id", pa.string(), "Id of the asset acted on (@asset.id)."),
            column("asset_name", pa.string(), "Name of the asset acted on (@asset.name)."),
            column("user_email", pa.string(), "Email of the acting user (@usr.email)."),
            column("message", pa.string(), "Audit message."),
            column("tags", TAGS, "Event tags as a MAP."),
            column("attributes", JSON, "The full audit attributes, JSON."),
        ]
    )
    FunctionArguments = SearchArgs
    PATH: ClassVar[str] = "/api/v2/audit/events/search"
    SEARCH_FIELDS: ClassVar[dict[str, str]] = AUDIT_FIELDS
    EXAMPLES: ClassVar[list[FunctionExample]] = [
        FunctionExample(
            sql=(
                f"SELECT user_email, asset_type, count(*) AS changes FROM {Q}.audit_events('*', "
                "TIMESTAMPTZ '2026-11-01 00:00:00+00', TIMESTAMPTZ '2026-11-08 00:00:00+00') "
                "WHERE action = 'modified' GROUP BY ALL ORDER BY changes DESC"
            ),
            description="Who changed what during one week",
        )
    ]

    class Meta:
        """Function metadata."""

        name = "audit_events"
        description = "Search the Datadog audit trail in a time window; filters on action/user/asset run in Datadog"
        filter_pushdown = True
        auto_apply_filters = True
        tags: ClassVar[dict[str, str]] = {}
        examples: ClassVar[list[Any]] = []

    @classmethod
    def row(cls, item: dict[str, Any]) -> dict[str, Any]:
        """Map one audit event."""
        a = item.get("attributes") or {}
        inner = a.get("attributes") or {}
        return {
            "id": item.get("id"),
            "ts": a.get("timestamp"),
            "service": a.get("service"),
            "event_name": dig(inner, "evt", "name"),
            "action": inner.get("action"),
            "asset_type": dig(inner, "asset", "type"),
            "asset_id": dig(inner, "asset", "id"),
            "asset_name": dig(inner, "asset", "name"),
            "user_email": dig(inner, "usr", "email"),
            "message": a.get("message"),
            "tags": tag_map(a.get("tags") or []),
            "attributes": json_text(inner),
        }


# ---------------------------------------------------------------------------- aggregates


@dataclass(slots=True, frozen=True, kw_only=True)
class AggregateArgs:
    """Arguments for ``logs_aggregate`` / ``spans_aggregate``."""

    query: Annotated[str, Arg(0, doc="Datadog search query selecting the records, e.g. 'service:web'; '*' for all.")]
    from_time: Annotated[dt.datetime, _ts_arg(1, "Start of the time window, inclusive, e.g. 2026-11-01 00:00 UTC.")]
    to_time: Annotated[dt.datetime, _ts_arg(2, "End of the time window.")]
    group_by: Annotated[
        str | None,
        Arg(
            "group_by",
            default=None,
            doc="Comma-separated facets to group by, e.g. 'service,status' or '@http.status_code'.",
        ),
    ]
    aggregation: Annotated[
        str,
        Arg(
            "aggregation",
            default="count",
            doc="count (default), cardinality, sum, min, max, avg, median or pc75..pc99.",
            choices=AGGREGATIONS,
        ),
    ]
    metric: Annotated[
        str | None,
        Arg(
            "metric",
            default=None,
            doc="Measure to aggregate, e.g. '@duration'; required for every aggregation except count.",
        ),
    ]
    group_limit: Annotated[
        int,
        Arg(
            "group_limit",
            default=10,
            doc="Top groups kept per group_by facet, ranked by the aggregate (1 to 10000).",
            ge=1,
            le=10000,
        ),
    ]
    rollup: Annotated[
        str | None,
        Arg(
            "rollup",
            default=None,
            doc="Bucket width such as '5m' or "
            "'1h' for a timeseries (one "
            "row per bucket); NULL "
            "returns one total per group.",
            pattern=r"^[0-9]+(ms|s|m|h|d|w)$",
        ),
    ]


AGG_SCHEMA = pa.schema(
    [
        column(
            "groups",
            TAGS,
            "The group-by facet values of the row as a MAP (service -> 'web'); empty without group_by.",
            nullable=False,
        ),
        column("ts", TSTZ, "Bucket start for a timeseries (rollup set); NULL for a total."),
        column("value", pa.float64(), "The aggregate value."),
        column(
            "aggregation", pa.string(), "The aggregation that produced value, e.g. 'count' or 'pc95'.", nullable=False
        ),
        column("metric", pa.string(), "The aggregated measure, e.g. '@duration'; NULL for count."),
    ]
)


def _aggregate_parts(args: AggregateArgs) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if args.aggregation != "count" and not args.metric:
        raise DatadogError(f"aggregation {args.aggregation!r} needs metric := '@some.measure'")
    compute: dict[str, Any] = {"aggregation": args.aggregation, "type": "timeseries" if args.rollup else "total"}
    if args.metric:
        compute["metric"] = args.metric
    if args.rollup:
        compute["interval"] = args.rollup
    sort: dict[str, Any] = {"aggregation": args.aggregation, "order": "desc", "type": "measure"}
    if args.metric:
        sort["metric"] = args.metric
    group_by = [
        {"facet": f.strip(), "limit": args.group_limit, "sort": sort}
        for f in (args.group_by or "").split(",")
        if f.strip()
    ]
    return compute, group_by


def _bucket_rows(by: Any, computed: Any, args: AggregateArgs) -> list[dict[str, Any]]:
    groups = [(str(k), None if v is None else str(v)) for k, v in (by or {}).items()]
    base = {"groups": groups, "aggregation": args.aggregation, "metric": args.metric}
    if isinstance(computed, list):
        return [{**base, "ts": p.get("time"), "value": p.get("value")} for p in computed if isinstance(p, dict)]
    return [{**base, "ts": None, "value": computed}]


class LogsAggregate(PagedScan):
    """``logs_aggregate(query, from_time, to_time, ...)``: POST /api/v2/logs/analytics/aggregate."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = AGG_SCHEMA
    FunctionArguments = AggregateArgs
    EXAMPLES: ClassVar[list[FunctionExample]] = [
        FunctionExample(
            sql=(
                "SELECT groups['service'] AS "
                "service, groups['status'] "
                "AS status, value AS logs "
                f"FROM {Q}.logs_aggregate("
                "'env:prod', TIMESTAMPTZ '2026-11-01 00:00:00+00', TIMESTAMPTZ '2026-11-02 00:00:00+00', "
                "group_by := 'service,status') ORDER BY logs DESC"
            ),
            description="Log volume per service and status for one day, counted in Datadog",
        ),
        FunctionExample(
            sql=(
                "SELECT ts, value AS p95_ms FROM "
                f"{Q}.logs_aggregate('service:web', "
                "TIMESTAMPTZ '2026-11-01 "
                "00:00:00+00', "
                "TIMESTAMPTZ '2026-11-01 06:00:00+00', aggregation := 'pc95', metric := '@duration', rollup := '1h') "
                "ORDER BY ts"
            ),
            description="Hourly p95 of a log measure",
        ),
    ]

    class Meta:
        """Function metadata."""

        name = "logs_aggregate"
        description = "Aggregate Datadog logs (count, percentiles ...) by facets, as totals or a timeseries"
        tags: ClassVar[dict[str, str]] = {}
        examples: ClassVar[list[Any]] = []

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One aggregate page."""
        args: AggregateArgs = params.args
        lo, hi = parse_ts(args.from_time), parse_ts(args.to_time)
        if lo is None or hi is None or lo >= hi:
            return [], None
        compute, group_by = _aggregate_parts(args)
        body: dict[str, Any] = {
            "compute": [compute],
            "filter": {"query": (args.query or "*").strip() or "*", "from": iso_ms(lo), "to": iso_ms(hi, ceil=True)},
        }
        if group_by:
            body["group_by"] = group_by
        if cursor:
            body["page"] = {"cursor": cursor}
        resp = client.post("/api/v2/logs/analytics/aggregate", body) or {}
        rows: list[dict[str, Any]] = []
        for bucket in dig(resp, "data", "buckets") or []:
            rows.extend(_bucket_rows(bucket.get("by"), (bucket.get("computes") or {}).get("c0"), args))
        after = dig(resp, "meta", "page", "after")
        return rows, (str(after) if after and rows else None)


class SpansAggregate(PagedScan):
    """``spans_aggregate(query, from_time, to_time, ...)``: POST /api/v2/spans/analytics/aggregate."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = AGG_SCHEMA
    FunctionArguments = AggregateArgs
    EXAMPLES: ClassVar[list[FunctionExample]] = [
        FunctionExample(
            sql=(
                f"SELECT groups['resource_name'] AS endpoint, value / 1e6 AS p99_ms FROM {Q}.spans_aggregate("
                "'service:web env:prod', TIMESTAMPTZ '2026-11-01 00:00:00+00', TIMESTAMPTZ '2026-11-01 01:00:00+00', "
                "group_by := 'resource_name', aggregation := 'pc99', metric := '@duration') ORDER BY p99_ms DESC"
            ),
            description="p99 latency per endpoint, computed in Datadog (span durations are in nanoseconds)",
        )
    ]

    class Meta:
        """Function metadata."""

        name = "spans_aggregate"
        description = "Aggregate APM spans (count, percentiles ...) by facets, as totals or a timeseries"
        tags: ClassVar[dict[str, str]] = {}
        examples: ClassVar[list[Any]] = []

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One spans aggregate request (the endpoint does not page)."""
        args: AggregateArgs = params.args
        lo, hi = parse_ts(args.from_time), parse_ts(args.to_time)
        if lo is None or hi is None or lo >= hi:
            return [], None
        compute, group_by = _aggregate_parts(args)
        attributes: dict[str, Any] = {
            "compute": [compute],
            "filter": {"query": (args.query or "*").strip() or "*", "from": iso_ms(lo), "to": iso_ms(hi, ceil=True)},
        }
        if group_by:
            attributes["group_by"] = group_by
        resp = (
            client.post(
                "/api/v2/spans/analytics/aggregate", {"data": {"type": "aggregate_request", "attributes": attributes}}
            )
            or {}
        )
        rows: list[dict[str, Any]] = []
        for bucket in resp.get("data") or []:
            a = bucket.get("attributes") or {}
            rows.extend(_bucket_rows(a.get("by"), (a.get("compute") or {}).get("c0"), args))
        return rows, None
