"""The ``resources`` schema: Datadog configuration and records as tables.

Monitors (and their per-group states), downtimes, SLOs (and their history over
a window), incidents, dashboards (and full definitions), synthetic tests (and
their results), hosts and host tags, users, teams, roles, the service catalog,
and the monthly usage summary. Each table pages through one list endpoint;
the cursor is the endpoint's own page number or offset, as text.
"""

from __future__ import annotations

import datetime as dt
import urllib.parse
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.metadata import FunctionExample
from vgi.table_filter_pushdown import ComparisonOp, ConstantFilter, InFilter, PushdownFilters
from vgi.table_function import ProcessParams

from .base import PagedScan
from .client import DatadogClient, DatadogError
from .convert import INT_LIST, JSON, STR_LIST, TAGS, TSTZ, dig, json_text, parse_ts, tag_map
from .meta import column
from .search import epoch_ms, epoch_s

R = "datadog.resources"


def _quote(segment: str) -> str:
    if not segment.strip():
        raise DatadogError("identifier must not be empty")
    return urllib.parse.quote(segment, safe="")


def eq_values(filters: PushdownFilters | None, column_name: str) -> list[Any] | None:
    """Values of a top-level ``col = v`` or ``col IN (...)`` predicate, or None."""
    for f in filters.filters if filters else ():
        if isinstance(f, ConstantFilter) and f.column_name == column_name and f.op == ComparisonOp.EQ:
            return [f.value.as_py()]
        if isinstance(f, InFilter) and f.column_name == column_name:
            return [v for v in f.values.to_pylist() if v is not None]
    return None


def _offset_next(cursor: str, got: int, limit: int) -> str | None:
    return str(int(cursor or 0) + got) if got >= limit else None


def _number_next(cursor: str, got: int, size: int) -> str | None:
    return str(int(cursor or 0) + 1) if got >= size else None


# ---------------------------------------------------------------------------- monitors

MONITOR_PAGE = 1000


class Monitors(PagedScan):
    """Rows for ``datadog.resources.monitors`` (GET /api/v1/monitor)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("id", pa.int64(), "Numeric identifier of the monitor (an id, not a quantity).", nullable=False),
            column("name", pa.string(), "Monitor name."),
            column("type", pa.string(), "Monitor type, e.g. 'metric alert', 'log alert', 'service check'."),
            column("query", pa.string(), "The monitor query."),
            column("message", pa.string(), "Notification message (with @-mentions)."),
            column(
                "overall_state",
                pa.string(),
                "Current overall state: OK, Alert, Warn, No Data, Ignored, Skipped, Unknown.",
            ),
            column("priority", pa.int32(), "Priority 1 (highest) to 5; NULL when unset."),
            column("tags", TAGS, "Monitor tags as a MAP (team -> 'core')."),
            column("multi", pa.bool_(), "True for a multi-alert monitor (one state per group)."),
            column("creator_email", pa.string(), "Email of the creator."),
            column("created_at", TSTZ, "When the monitor was created."),
            column("modified_at", TSTZ, "When the monitor was last changed."),
            column("options", JSON, "Monitor options (thresholds, notify_no_data, renotify ...), JSON."),
            column("url", pa.string(), "Link to the monitor in the Datadog app."),
        ]
    )
    EXAMPLES: ClassVar[list[tuple[str, str]]] = [
        (
            "Monitors currently alerting, by priority",
            "SELECT id, name, priority "
            f"FROM {R}.monitors WHERE "
            "overall_state = 'Alert' ORDER "
            "BY priority NULLS LAST, name",
        ),
        (
            "Monitor count per owning team",
            f"SELECT tags['team'] AS team, count(*) AS monitors FROM {R}.monitors GROUP BY team ORDER BY monitors DESC",
        ),
    ]

    class Meta:
        """Table metadata."""

        name = "monitors_scan"
        description = "Scan behind the monitors table"
        filter_pushdown = True
        auto_apply_filters = True

    @classmethod
    def monitor_row(cls, client: DatadogClient, m: dict[str, Any]) -> dict[str, Any]:
        """Map one monitor object."""
        return {
            **m,
            "tags": tag_map(m.get("tags") or []),
            "creator_email": dig(m, "creator", "email"),
            "created_at": m.get("created"),
            "modified_at": m.get("modified"),
            "options": json_text(m.get("options")),
            "url": f"{client.config.app_url}/monitors/{m.get('id')}",
        }

    @classmethod
    def page(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str, *, groups: bool
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page of monitors (or the single monitor a ``monitor_id`` predicate names)."""
        extra: list[tuple[str, Any]] = [("group_states", "all")] if groups else []
        ids = eq_values(params.current_pushdown_filters, "monitor_id" if groups else "id")
        if ids is not None and len(ids) == 1 and isinstance(ids[0], int):
            return [client.get(f"/api/v1/monitor/{int(ids[0])}", extra) or {}], None
        page = int(cursor or 0)
        size = min(client.config.page_size, MONITOR_PAGE)
        query: list[tuple[str, Any]] = [("page", page), ("page_size", size), *extra]
        names = eq_values(params.current_pushdown_filters, "monitor_name" if groups else "name")
        if names is not None and len(names) == 1 and isinstance(names[0], str):
            query.append(("name", names[0]))  # substring match in Datadog; DuckDB re-checks equality
        items = [m for m in client.get("/api/v1/monitor", query) or [] if isinstance(m, dict)]
        return items, _number_next(cursor, len(items), size)

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page of monitors."""
        items, nxt = cls.page(client, params, cursor, groups=False)
        return [cls.monitor_row(client, m) for m in items], nxt


class MonitorGroups(PagedScan):
    """Rows for ``datadog.resources.monitor_groups``: per-group state of every monitor."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("monitor_id", pa.int64(), "Monitor id (monitors.id).", nullable=False),
            column("monitor_name", pa.string(), "Display name of the monitor (monitors.name)."),
            column(
                "group_name",
                pa.string(),
                "Group the state belongs to, e.g. 'host:web-1,env:prod' ('*' for a simple monitor).",
                nullable=False,
            ),
            column("group_tags", TAGS, "The group split into a MAP (host -> 'web-1')."),
            column("status", pa.string(), "State of this group: OK, Alert, Warn, No Data ..."),
            column("last_triggered_at", TSTZ, "When the group last triggered."),
            column("last_resolved_at", TSTZ, "When the group last resolved."),
            column("last_nodata_at", TSTZ, "When the group last reported no data."),
            column("last_notified_at", TSTZ, "When the group last sent a notification."),
        ]
    )
    EXAMPLES: ClassVar[list[tuple[str, str]]] = [
        (
            "Hosts alerting on any monitor",
            "SELECT group_tags['host'] AS host, "
            "list(monitor_name ORDER "
            "BY monitor_name) AS "
            f"monitors FROM {R}.monitor_groups WHERE "
            "status = 'Alert' GROUP BY host ORDER BY host",
        ),
        (
            "Groups of one monitor (fetched by id)",
            "SELECT group_name, status, "
            "last_triggered_at FROM "
            f"{R}.monitor_groups WHERE monitor_id "
            "= 1001 ORDER BY group_name",
        ),
    ]

    class Meta:
        """Table metadata."""

        name = "monitor_groups_scan"
        description = "Scan behind the monitor_groups table"
        filter_pushdown = True
        auto_apply_filters = True

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page of monitors with group_states=all, one row per group."""
        items, nxt = Monitors.page(client, params, cursor, groups=True)
        rows = []
        for m in items:
            for group, st in sorted((dig(m, "state", "groups") or {}).items()):
                st = st if isinstance(st, dict) else {}
                rows.append(
                    {
                        "monitor_id": m.get("id"),
                        "monitor_name": m.get("name"),
                        "group_name": group,
                        "group_tags": tag_map([] if group == "*" else str(group).split(",")),
                        "status": st.get("status"),
                        "last_triggered_at": st.get("last_triggered_ts"),
                        "last_resolved_at": st.get("last_resolved_ts"),
                        "last_nodata_at": st.get("last_nodata_ts"),
                        "last_notified_at": st.get("last_notified_ts"),
                    }
                )
        return rows, nxt


# ---------------------------------------------------------------------------- downtimes


class Downtimes(PagedScan):
    """Rows for ``datadog.resources.downtimes`` (GET /api/v2/downtime)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("id", pa.string(), "Downtime id (UUID).", nullable=False),
            column("status", pa.string(), "active, scheduled, ended or canceled."),
            column("scope", pa.string(), "Scope muted, e.g. 'env:staging'."),
            column(
                "monitor_id", pa.int64(), "Identifier of the single monitor muted (monitors.id; an id, not a quantity)."
            ),
            column("monitor_tags", STR_LIST, "Monitor tags selecting the muted monitors, if it targets tags."),
            column("message", pa.string(), "Downtime message."),
            column("start_at", TSTZ, "Start of the (current or first) downtime window."),
            column("end_at", TSTZ, "End of the window; NULL for an indefinite downtime."),
            column("is_recurring", pa.bool_(), "True for a recurring schedule."),
            column("schedule", JSON, "The full schedule (one-time or recurrences), JSON."),
            column("created_by", pa.string(), "User id of the creator."),
            column("created_at", TSTZ, "When the downtime was created."),
            column("modified_at", TSTZ, "When the downtime was last changed."),
            column("canceled_at", TSTZ, "When the downtime was canceled, if it was."),
        ]
    )
    EXAMPLES: ClassVar[list[tuple[str, str]]] = [
        (
            "Active downtimes and what they mute",
            f"SELECT scope, monitor_id, end_at FROM {R}.downtimes WHERE status = 'active' ORDER BY end_at NULLS LAST",
        ),
    ]

    class Meta:
        """Table metadata."""

        name = "downtimes_scan"
        description = "Scan behind the downtimes table"
        filter_pushdown = True
        auto_apply_filters = True

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One offset page; ``status = 'active'`` sends current_only=true."""
        limit = min(client.config.page_size, 100)
        query: list[tuple[str, Any]] = [("page[offset]", int(cursor or 0)), ("page[limit]", limit)]
        if eq_values(params.current_pushdown_filters, "status") == ["active"]:
            query.append(("current_only", True))
        body = client.get("/api/v2/downtime", query) or {}
        rows = []
        for item in body.get("data") or []:
            a = item.get("attributes") or {}
            sched = a.get("schedule") or {}
            current = sched.get("current_downtime") or {}
            rows.append(
                {
                    "id": item.get("id"),
                    "status": a.get("status"),
                    "scope": a.get("scope"),
                    "monitor_id": dig(a, "monitor_identifier", "monitor_id"),
                    "monitor_tags": dig(a, "monitor_identifier", "monitor_tags"),
                    "message": a.get("message"),
                    "start_at": current.get("start") or sched.get("start"),
                    "end_at": current.get("end") or sched.get("end"),
                    "is_recurring": "recurrences" in sched,
                    "schedule": json_text(sched),
                    "created_by": dig(item, "relationships", "created_by", "data", "id"),
                    "created_at": a.get("created"),
                    "modified_at": a.get("modified"),
                    "canceled_at": a.get("canceled"),
                }
            )
        return rows, _offset_next(cursor, len(body.get("data") or []), limit)


# ---------------------------------------------------------------------------- SLOs

THRESHOLD = pa.struct(
    [
        pa.field("timeframe", pa.string()),
        pa.field("target", pa.float64()),
        pa.field("warning", pa.float64()),
    ]
)


class Slos(PagedScan):
    """Rows for ``datadog.resources.slos`` (GET /api/v1/slo)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("id", pa.string(), "SLO id.", nullable=False),
            column("name", pa.string(), "SLO name."),
            column("description", pa.string(), "SLO description."),
            column("type", pa.string(), "metric, monitor or time_slice."),
            column("tags", TAGS, "SLO tags as a MAP."),
            column("target", pa.float64(), "Target of the primary threshold, in percent (e.g. 99.9)."),
            column("timeframe", pa.string(), "Timeframe of the primary threshold: 7d, 30d, 90d, custom."),
            column("warning", pa.float64(), "Warning level of the primary threshold, in percent."),
            column("thresholds", pa.list_(THRESHOLD), "Every threshold (timeframe, target, warning)."),
            column(
                "monitor_ids", INT_LIST, "Identifiers of the monitors behind a monitor-based SLO (monitors.id values)."
            ),
            column("numerator", pa.string(), "Good-events query of a metric SLO."),
            column("denominator", pa.string(), "Total-events query of a metric SLO."),
            column("creator_email", pa.string(), "Email of the creator."),
            column("created_at", TSTZ, "When the SLO was created."),
            column("modified_at", TSTZ, "When the SLO was last changed."),
            column("url", pa.string(), "Link to the SLO in the Datadog app."),
        ]
    )
    EXAMPLES: ClassVar[list[tuple[str, str]]] = [
        (
            "SLOs per team with their targets",
            f"SELECT tags['team'] AS team, name, target, timeframe FROM {R}.slos ORDER BY team, name",
        ),
    ]

    class Meta:
        """Table metadata."""

        name = "slos_scan"
        description = "Scan behind the slos table"
        filter_pushdown = True
        auto_apply_filters = True

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One offset page; ``id = / IN`` sends ids=."""
        limit = min(client.config.page_size, 1000)
        query: list[tuple[str, Any]] = [("limit", limit), ("offset", int(cursor or 0))]
        ids = eq_values(params.current_pushdown_filters, "id")
        if ids and all(isinstance(i, str) for i in ids):
            query.append(("ids", ",".join(ids)))
        body = client.get("/api/v1/slo", query) or {}
        data = body.get("data") or []
        rows = []
        for s in data:
            thresholds = s.get("thresholds") or []
            first = thresholds[0] if thresholds else {}
            rows.append(
                {
                    **s,
                    "tags": tag_map(s.get("tags") or []),
                    "target": first.get("target"),
                    "timeframe": first.get("timeframe"),
                    "warning": first.get("warning"),
                    "thresholds": thresholds,
                    "numerator": dig(s, "query", "numerator"),
                    "denominator": dig(s, "query", "denominator"),
                    "creator_email": dig(s, "creator", "email"),
                    "url": f"{client.config.app_url}/slo?slo_id={s.get('id')}",
                }
            )
        return rows, _offset_next(cursor, len(data), limit)


@dataclass(slots=True, frozen=True, kw_only=True)
class SloHistoryArgs:
    """Arguments for ``slo_history(slo_id, from_time, to_time)``."""

    slo_id: Annotated[str, Arg(0, doc="SLO id (slos.id).")]
    from_time: Annotated[
        dt.datetime, Arg(1, doc="Start of the time window, inclusive, e.g. 2026-11-01 00:00 UTC.", arrow_type=TSTZ)
    ]
    to_time: Annotated[dt.datetime, Arg(2, doc="End of the time window.", arrow_type=TSTZ)]


class SloHistory(PagedScan):
    """``slo_history(slo_id, from_time, to_time)``: SLI and error budget over a window."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("slo_id", pa.string(), "SLO id.", nullable=False),
            column("group_name", pa.string(), "Monitor group for a grouped SLO; NULL for the overall row."),
            column("from_ts", TSTZ, "Start of the evaluated window."),
            column("to_ts", TSTZ, "End of the evaluated window."),
            column("type", pa.string(), "SLO type: metric, monitor or time_slice."),
            column("sli_value", pa.float64(), "Measured SLI over the window, in percent."),
            column("target", pa.float64(), "Target of the primary threshold, in percent."),
            column(
                "error_budget_remaining_pct",
                pa.float64(),
                "Share of the error budget left: (sli - target) / (100 - target) * 100; negative when exhausted.",
            ),
            column("good_events", pa.float64(), "Metric SLO: numerator sum over the window (count)."),
            column("total_events", pa.float64(), "Metric SLO: denominator sum over the window (count)."),
        ]
    )
    FunctionArguments = SloHistoryArgs
    EXAMPLES: ClassVar[list[FunctionExample]] = [
        FunctionExample(
            sql=(
                f"SELECT sli_value, target, error_budget_remaining_pct FROM {R}.slo_history('abc123def456', "
                "TIMESTAMPTZ '2026-10-01 00:00:00+00', TIMESTAMPTZ '2026-11-01 00:00:00+00') WHERE group_name IS NULL"
            ),
            description="October SLI and error budget left for one SLO",
        )
    ]

    class Meta:
        """Function metadata."""

        name = "slo_history"
        description = "SLI value and remaining error budget of one SLO over a time window, overall and per group"
        tags: ClassVar[dict[str, str]] = {}
        examples: ClassVar[list[Any]] = []

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /api/v1/slo/{id}/history."""
        args: SloHistoryArgs = params.args
        lo, hi = parse_ts(args.from_time), parse_ts(args.to_time)
        if lo is None or hi is None or lo >= hi:
            return [], None
        body = (
            client.get(
                f"/api/v1/slo/{_quote(args.slo_id)}/history",
                [("from_ts", epoch_s(lo)), ("to_ts", epoch_s(hi, ceil=True))],
            )
            or {}
        )
        data = body.get("data") or {}
        thresholds = data.get("thresholds") or {}
        target = None
        for t in thresholds.values() if isinstance(thresholds, dict) else []:
            target = t.get("target") if isinstance(t, dict) else None
            break

        def budget(sli: Any) -> float | None:
            if not isinstance(sli, int | float) or not isinstance(target, int | float) or target >= 100:
                return None
            return (float(sli) - float(target)) / (100.0 - float(target)) * 100.0

        base = {
            "slo_id": args.slo_id,
            "from_ts": data.get("from_ts"),
            "to_ts": data.get("to_ts"),
            "type": data.get("type") or data.get("type_id"),
            "target": target,
        }
        overall = data.get("overall") or {}
        sli = overall.get("sli_value")
        rows = [
            {
                **base,
                "group_name": None,
                "sli_value": sli,
                "error_budget_remaining_pct": budget(sli),
                "good_events": dig(data, "series", "numerator", "sum"),
                "total_events": dig(data, "series", "denominator", "sum"),
            }
        ]
        for g in data.get("groups") or []:
            gs = g.get("sli_value")
            rows.append(
                {**base, "group_name": g.get("name"), "sli_value": gs, "error_budget_remaining_pct": budget(gs)}
            )
        return rows, None


# ---------------------------------------------------------------------------- incidents


class Incidents(PagedScan):
    """Rows for ``datadog.resources.incidents`` (GET /api/v2/incidents)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("incident_id", pa.string(), "Incident id (UUID).", nullable=False),
            column("incident_number", pa.int64(), "Public incident number shown in the app (#42)."),
            column("title", pa.string(), "Incident title."),
            column("status", pa.string(), "State: active, stable or resolved."),
            column("severity", pa.string(), "Severity: SEV-1 (highest) .. SEV-5, or UNKNOWN."),
            column("customer_impacted", pa.bool_(), "True when customers were impacted."),
            column("customer_impact_scope", pa.string(), "Description of the customer impact."),
            column("created_at", TSTZ, "When the incident was declared."),
            column("detected_at", TSTZ, "When the issue was detected."),
            column("resolved_at", TSTZ, "When the incident was resolved; NULL while open."),
            column("modified_at", TSTZ, "When the incident record last changed."),
            column("time_to_detect_seconds", pa.float64(), "Seconds from impact start to detection."),
            column("time_to_internal_response_seconds", pa.float64(), "Seconds from detection to the first response."),
            column("time_to_repair_seconds", pa.float64(), "Seconds from detection to repair."),
            column("time_to_resolve_seconds", pa.float64(), "Seconds from creation to resolution."),
            column("services", STR_LIST, "Services the incident affects."),
            column("teams", STR_LIST, "Teams the incident is assigned to."),
            column("commander_user_id", pa.string(), "User id of the incident commander."),
            column("fields", JSON, "Every incident field (state, severity, root cause, custom fields), JSON."),
            column("url", pa.string(), "Link to the incident in the Datadog app."),
        ]
    )
    EXAMPLES: ClassVar[list[tuple[str, str]]] = [
        (
            "Mean time to resolve per severity",
            "SELECT severity, count(*) AS incidents, "
            "avg(time_to_resolve_seconds) "
            "/ 3600 AS mttr_hours "
            f"FROM {R}.incidents WHERE status = 'resolved' "
            "GROUP BY severity ORDER BY severity",
        ),
        (
            "Open incidents and the services they hit",
            "SELECT incident_number, title, "
            "severity, services FROM "
            f"{R}.incidents WHERE status <> "
            "'resolved' ORDER BY created_at",
        ),
    ]

    class Meta:
        """Table metadata."""

        name = "incidents_scan"
        description = "Scan behind the incidents table"

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One offset page (page[size] at most 100)."""
        size = min(client.config.page_size, 100)
        body = client.get("/api/v2/incidents", [("page[size]", size), ("page[offset]", int(cursor or 0))]) or {}
        data = body.get("data") or []
        rows = []
        for item in data:
            a = item.get("attributes") or {}
            fields = a.get("fields") or {}

            def field_value(name: str, attrs: dict[str, Any] = a, flds: dict[str, Any] = fields) -> Any:
                value = attrs.get(name)
                return value if value is not None else dig(flds, name, "value")

            number = a.get("public_id")
            rows.append(
                {
                    "incident_id": item.get("id"),
                    "incident_number": number,
                    "title": a.get("title"),
                    "status": field_value("state"),
                    "severity": field_value("severity"),
                    "customer_impacted": a.get("customer_impacted"),
                    "customer_impact_scope": a.get("customer_impact_scope"),
                    "created_at": a.get("created"),
                    "detected_at": a.get("detected"),
                    "resolved_at": a.get("resolved"),
                    "modified_at": a.get("modified"),
                    "time_to_detect_seconds": a.get("time_to_detect"),
                    "time_to_internal_response_seconds": a.get("time_to_internal_response"),
                    "time_to_repair_seconds": a.get("time_to_repair"),
                    "time_to_resolve_seconds": a.get("time_to_resolve"),
                    "services": dig(fields, "services", "value"),
                    "teams": dig(fields, "teams", "value"),
                    "commander_user_id": dig(item, "relationships", "commander_user", "data", "id"),
                    "fields": json_text(fields),
                    "url": f"{client.config.app_url}/incidents/{number}" if number is not None else None,
                }
            )
        nxt = dig(body, "meta", "pagination", "next_offset")
        return rows, (str(nxt) if nxt is not None and len(data) >= size else None)


# ---------------------------------------------------------------------------- dashboards


class Dashboards(PagedScan):
    """Rows for ``datadog.resources.dashboards`` (GET /api/v1/dashboard)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("id", pa.string(), "Dashboard id, e.g. 'abc-def-ghi'.", nullable=False),
            column("title", pa.string(), "Dashboard title."),
            column("description", pa.string(), "Dashboard description."),
            column("layout_type", pa.string(), "'ordered' (timeboard) or 'free' (screenboard)."),
            column("author_handle", pa.string(), "Handle of the author."),
            column("is_read_only", pa.bool_(), "True when only the author and admins may edit."),
            column("created_at", TSTZ, "When the dashboard was created."),
            column("modified_at", TSTZ, "When the dashboard was last changed."),
            column("url", pa.string(), "Link to the dashboard in the Datadog app."),
        ]
    )
    EXAMPLES: ClassVar[list[tuple[str, str]]] = [
        (
            "Dashboards per author",
            "SELECT author_handle, "
            "count(*) AS dashboards FROM "
            f"{R}.dashboards GROUP BY "
            "author_handle "
            "ORDER BY dashboards DESC",
        ),
        (
            "Stale dashboards",
            "SELECT title, modified_at FROM "
            f"{R}.dashboards WHERE modified_at "
            "< TIMESTAMPTZ '2026-01-01 "
            "00:00:00+00' ORDER BY modified_at",
        ),
    ]

    class Meta:
        """Table metadata."""

        name = "dashboards_scan"
        description = "Scan behind the dashboards table"

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One start/count page."""
        count = min(client.config.page_size, 1000)
        body = client.get("/api/v1/dashboard", [("count", count), ("start", int(cursor or 0))]) or {}
        data = body.get("dashboards") or []
        rows = [
            {**d, "url": f"{client.config.app_url}{d.get('url') or '/dashboard/' + str(d.get('id'))}"} for d in data
        ]
        return rows, _offset_next(cursor, len(data), count)


@dataclass(slots=True, frozen=True, kw_only=True)
class DashboardArgs:
    """Arguments for ``dashboard(dashboard_id)``."""

    dashboard_id: Annotated[str, Arg(0, doc="Dashboard id (dashboards.id), e.g. 'abc-def-ghi'.")]


class Dashboard(PagedScan):
    """``dashboard(dashboard_id)``: one dashboard's full definition."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("id", pa.string(), "Dashboard id.", nullable=False),
            column("title", pa.string(), "Dashboard title."),
            column("description", pa.string(), "Dashboard description."),
            column("layout_type", pa.string(), "'ordered' or 'free'."),
            column("tags", STR_LIST, "Dashboard tags (team:...)."),
            column("widget_count", pa.int64(), "Number of top-level widgets (count)."),
            column("widgets", JSON, "The widget definitions, JSON array."),
            column("template_variables", JSON, "Template variables, JSON array."),
            column("definition", JSON, "The whole dashboard definition as returned by the API, JSON."),
            column("author_handle", pa.string(), "Handle of the author."),
            column("created_at", TSTZ, "When the dashboard was created."),
            column("modified_at", TSTZ, "When the dashboard was last changed."),
            column("url", pa.string(), "Link to the dashboard in the Datadog app."),
        ]
    )
    FunctionArguments = DashboardArgs
    EXAMPLES: ClassVar[list[FunctionExample]] = [
        FunctionExample(
            sql=(
                "SELECT w.value->>'$.definition.type' "
                "AS widget_type, "
                "count(*) AS widgets FROM "
                f"{R}.dashboard('abc-def-ghi') d, "
                "json_each(d.widgets) w GROUP BY widget_type ORDER BY widgets DESC"
            ),
            description="Widget types used on one dashboard",
        )
    ]

    class Meta:
        """Function metadata."""

        name = "dashboard"
        description = "One Datadog dashboard's full definition: widgets, template variables and layout as JSON"
        tags: ClassVar[dict[str, str]] = {}
        examples: ClassVar[list[Any]] = []

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /api/v1/dashboard/{id}."""
        d = client.get(f"/api/v1/dashboard/{_quote(params.args.dashboard_id)}") or {}
        widgets = d.get("widgets") or []
        return [
            {
                **d,
                "widget_count": len(widgets),
                "widgets": json_text(widgets),
                "template_variables": json_text(d.get("template_variables") or []),
                "definition": json_text(d),
                "url": f"{client.config.app_url}{d.get('url') or '/dashboard/' + str(d.get('id'))}",
            }
        ], None


# ---------------------------------------------------------------------------- synthetics


class SyntheticsTests(PagedScan):
    """Rows for ``datadog.resources.synthetics_tests`` (GET /api/v1/synthetics/tests)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("public_id", pa.string(), "Test public id, e.g. 'abc-123-xyz'.", nullable=False),
            column("name", pa.string(), "Test name."),
            column("type", pa.string(), "api, browser or mobile."),
            column("subtype", pa.string(), "API test subtype: http, ssl, dns, tcp, icmp, multi, grpc, websocket, udp."),
            column("status", pa.string(), "live or paused."),
            column("monitor_id", pa.int64(), "Monitor that alerts on the test (monitors.id)."),
            column("tags", TAGS, "Test tags as a MAP."),
            column("locations", STR_LIST, "Locations the test runs from, e.g. 'aws:eu-central-1'."),
            column("tick_every_seconds", pa.int64(), "Run frequency in seconds."),
            column("message", pa.string(), "Notification message."),
            column("creator_email", pa.string(), "Email of the creator."),
            column("config", JSON, "Request and assertion configuration, JSON."),
            column("url", pa.string(), "Link to the test in the Datadog app."),
        ]
    )
    EXAMPLES: ClassVar[list[tuple[str, str]]] = [
        (
            "Live tests per type and subtype",
            "SELECT type, subtype, count(*) AS "
            f"tests FROM {R}.synthetics_tests "
            "WHERE status = 'live' GROUP "
            "BY ALL ORDER BY tests DESC",
        ),
    ]

    class Meta:
        """Table metadata."""

        name = "synthetics_tests_scan"
        description = "Scan behind the synthetics_tests table"

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page_number/page_size page."""
        size = min(client.config.page_size, 100)
        body = client.get("/api/v1/synthetics/tests", [("page_size", size), ("page_number", int(cursor or 0))]) or {}
        data = body.get("tests") or []
        rows = [
            {
                **t,
                "tags": tag_map(t.get("tags") or []),
                "tick_every_seconds": dig(t, "options", "tick_every"),
                "creator_email": dig(t, "creator", "email"),
                "config": json_text(t.get("config")),
                "url": f"{client.config.app_url}/synthetics/details/{t.get('public_id')}",
            }
            for t in data
        ]
        return rows, _number_next(cursor, len(data), size)


@dataclass(slots=True, frozen=True, kw_only=True)
class SyntheticsResultsArgs:
    """Arguments for ``synthetics_results(public_id, from_time, to_time)``."""

    public_id: Annotated[str, Arg(0, doc="Test public id (synthetics_tests.public_id).")]
    from_time: Annotated[
        dt.datetime, Arg(1, doc="Start of the time window, inclusive, e.g. 2026-11-01 00:00 UTC.", arrow_type=TSTZ)
    ]
    to_time: Annotated[dt.datetime, Arg(2, doc="End of the time window.", arrow_type=TSTZ)]


class SyntheticsResults(PagedScan):
    """``synthetics_results(public_id, from_time, to_time)``: recent runs of one test."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("public_id", pa.string(), "Test public id.", nullable=False),
            column("result_id", pa.string(), "Result id.", nullable=False),
            column("check_time", TSTZ, "When the run happened.", nullable=False),
            column("location", pa.string(), "Location the run came from, e.g. 'aws:eu-central-1'."),
            column("passed", pa.bool_(), "True when every assertion passed."),
            column("status", pa.int32(), "Result status code: 0 passed, 1 failed, 2 no data."),
            column("duration_ms", pa.float64(), "Total run time in milliseconds."),
            column("http_status_code", pa.int32(), "HTTP status of an HTTP API test."),
        ]
    )
    FunctionArguments = SyntheticsResultsArgs
    EXAMPLES: ClassVar[list[FunctionExample]] = [
        FunctionExample(
            sql=(
                f"SELECT location, avg(passed::INT) AS success_rate, median(duration_ms) AS median_ms FROM "
                f"{R}.synthetics_results('abc-123-xyz', TIMESTAMPTZ '2026-11-01 00:00:00+00', "
                "TIMESTAMPTZ '2026-11-02 00:00:00+00') GROUP BY location ORDER BY success_rate"
            ),
            description="Success rate and median duration per location for one test",
        )
    ]

    class Meta:
        """Function metadata."""

        name = "synthetics_results"
        description = "Recent results of one synthetic test in a time window, one row per run and location"
        tags: ClassVar[dict[str, str]] = {}
        examples: ClassVar[list[Any]] = []

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Look up the test's type, then GET its (API or browser) results."""
        args: SyntheticsResultsArgs = params.args
        lo, hi = parse_ts(args.from_time), parse_ts(args.to_time)
        if lo is None or hi is None or lo >= hi:
            return [], None
        pid = _quote(args.public_id)
        test = client.get(f"/api/v1/synthetics/tests/{pid}") or {}
        kind = "browser/" if test.get("type") == "browser" else ""
        body = (
            client.get(
                f"/api/v1/synthetics/tests/{kind}{pid}/results",
                [("from_ts", epoch_ms(lo)), ("to_ts", epoch_ms(hi, ceil=True))],
            )
            or {}
        )
        rows = []
        for r in body.get("results") or []:
            res = r.get("result") or {}
            rows.append(
                {
                    "public_id": args.public_id,
                    "result_id": r.get("result_id"),
                    "check_time": r.get("check_time"),
                    "location": r.get("probe_dc"),
                    "passed": res.get("passed"),
                    "status": r.get("status"),
                    "duration_ms": dig(res, "timings", "total")
                    if dig(res, "timings", "total") is not None
                    else res.get("duration"),
                    "http_status_code": res.get("httpStatusCode"),
                }
            )
        return rows, None


# ---------------------------------------------------------------------------- hosts


class Hosts(PagedScan):
    """Rows for ``datadog.resources.hosts`` (GET /api/v1/hosts)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column(
                "host_name",
                pa.string(),
                "Name the host reports under (the agent hostname), e.g. 'web-1'.",
                nullable=False,
            ),
            column("id", pa.int64(), "Numeric Datadog identifier of the host (an id, not a quantity)."),
            column("aliases", STR_LIST, "Other names of the host."),
            column("apps", STR_LIST, "Integrations reporting from the host."),
            column("sources", STR_LIST, "Sources of the host: agent, aws, gcp, azure ..."),
            column("up", pa.bool_(), "True when the host is reporting."),
            column("is_muted", pa.bool_(), "True when the host is muted."),
            column("last_reported_at", TSTZ, "Last time the host reported."),
            column("tags", TAGS, "Host tags from every source as a MAP."),
            column("platform", pa.string(), "OS platform, e.g. 'linux'."),
            column("agent_version", pa.string(), "Datadog Agent version."),
            column("cpu_pct", pa.float64(), "Recent CPU utilisation in percent."),
            column("iowait_pct", pa.float64(), "Recent I/O wait in percent."),
            column("load", pa.float64(), "Recent normalised load (load15 / cores)."),
        ]
    )
    EXAMPLES: ClassVar[list[tuple[str, str]]] = [
        (
            "Hosts per environment and agent version",
            "SELECT tags['env'] AS env, "
            "agent_version, count(*) AS hosts "
            f"FROM {R}.hosts GROUP BY ALL "
            "ORDER BY env, agent_version",
        ),
        (
            "Busiest hosts",
            f"SELECT host_name, cpu_pct, load FROM {R}.hosts WHERE up ORDER BY cpu_pct DESC NULLS LAST LIMIT 10",
        ),
    ]

    class Meta:
        """Table metadata."""

        name = "hosts_scan"
        description = "Scan behind the hosts table"
        filter_pushdown = True
        auto_apply_filters = True

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One start/count page; ``host_name = 'x'`` is sent as filter=x."""
        count = min(client.config.page_size, 1000)
        query: list[tuple[str, Any]] = [("start", int(cursor or 0)), ("count", count)]
        names = eq_values(params.current_pushdown_filters, "host_name")
        if names is not None and len(names) == 1 and isinstance(names[0], str):
            query.append(("filter", names[0]))
        body = client.get("/api/v1/hosts", query) or {}
        data = body.get("host_list") or []
        rows = []
        for h in data:
            tags = [t for ts in (h.get("tags_by_source") or {}).values() for t in (ts or [])]
            rows.append(
                {
                    **h,
                    "host_name": h.get("host_name") or h.get("name"),
                    "last_reported_at": h.get("last_reported_time"),
                    "tags": tag_map(tags),
                    "platform": dig(h, "meta", "platform"),
                    "agent_version": dig(h, "meta", "agent_version"),
                    "cpu_pct": dig(h, "metrics", "cpu"),
                    "iowait_pct": dig(h, "metrics", "iowait"),
                    "load": dig(h, "metrics", "load"),
                }
            )
        return rows, _offset_next(cursor, len(data), count)


class HostTags(PagedScan):
    """Rows for ``datadog.resources.host_tags`` (GET /api/v1/tags/hosts)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("host_name", pa.string(), "Host name (hosts.host_name).", nullable=False),
            column("tag", pa.string(), "The tag, 'key:value'.", nullable=False),
            column("key", pa.string(), "Tag key.", nullable=False),
            column("value", pa.string(), "Tag value ('' for a bare tag).", nullable=False),
        ]
    )
    EXAMPLES: ClassVar[list[tuple[str, str]]] = [
        (
            "Hosts per role",
            "SELECT value AS role, count(*) "
            f"AS hosts FROM {R}.host_tags "
            "WHERE key = 'role' GROUP BY "
            "role ORDER BY hosts DESC",
        ),
    ]

    class Meta:
        """Table metadata."""

        name = "host_tags_scan"
        description = "Scan behind the host_tags table"

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /api/v1/tags/hosts (one response, no paging)."""
        body = client.get("/api/v1/tags/hosts") or {}
        rows = []
        for tag, hosts in sorted((body.get("tags") or {}).items()):
            key, _, value = str(tag).partition(":")
            for host in sorted(set(hosts or [])):
                rows.append({"host_name": host, "tag": tag, "key": key, "value": value})
        return rows, None


# ---------------------------------------------------------------------------- users, teams, roles


class NumberPagedV2(PagedScan):
    """A v2 JSON:API list paged by page[number] / page[size]."""

    PATH: ClassVar[str] = ""

    class Meta:
        """Overridden."""

        name = "number_paged_v2"
        description = "Base class"

    @classmethod
    def row(cls, item: dict[str, Any]) -> dict[str, Any]:
        """Map one JSON:API resource."""
        return {"id": item.get("id"), **(item.get("attributes") or {})}

    @classmethod
    def extra(cls, params: ProcessParams[Any]) -> list[tuple[str, Any]]:
        """Pushed query parameters."""
        return []

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page."""
        size = min(client.config.page_size, 100)
        query = [("page[size]", size), ("page[number]", int(cursor or 0)), *cls.extra(params)]
        body = client.get(cls.PATH, query) or {}
        data = [d for d in body.get("data") or [] if isinstance(d, dict)]
        return [cls.row(d) for d in data], _number_next(cursor, len(data), size)


class Users(NumberPagedV2):
    """Rows for ``datadog.resources.users`` (GET /api/v2/users)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("id", pa.string(), "User id (UUID).", nullable=False),
            column("name", pa.string(), "Display name."),
            column("handle", pa.string(), "Login handle."),
            column("email", pa.string(), "Email address."),
            column("status", pa.string(), "Active, Pending or Disabled."),
            column("disabled", pa.bool_(), "True for a disabled user."),
            column("verified", pa.bool_(), "True once the email is verified."),
            column("service_account", pa.bool_(), "True for a service account."),
            column("mfa_enabled", pa.bool_(), "True when multi-factor authentication is on."),
            column("title", pa.string(), "Job title."),
            column("role_ids", STR_LIST, "Ids of the user's roles (roles.id)."),
            column("created_at", TSTZ, "When the user was created."),
            column("modified_at", TSTZ, "When the user was last changed."),
        ]
    )
    PATH: ClassVar[str] = "/api/v2/users"
    EXAMPLES: ClassVar[list[tuple[str, str]]] = [
        (
            "Active users without MFA",
            f"SELECT email, name FROM {R}.users "
            "WHERE status = 'Active' "
            "AND NOT service_account AND "
            "NOT mfa_enabled ORDER BY email",
        ),
        (
            "Users per role",
            "SELECT r.name AS role, count(*) AS users "
            f"FROM {R}.users u, unnest(u.role_ids) AS "
            f"t(role_id) JOIN {R}.roles r ON r.id = "
            "t.role_id GROUP BY role "
            "ORDER BY users DESC",
        ),
    ]

    class Meta:
        """Table metadata."""

        name = "users_scan"
        description = "Scan behind the users table"
        filter_pushdown = True
        auto_apply_filters = True

    @classmethod
    def extra(cls, params: ProcessParams[Any]) -> list[tuple[str, Any]]:
        """``status = 'Active'`` becomes filter[status]=Active."""
        values = eq_values(params.current_pushdown_filters, "status")
        if values and all(isinstance(v, str) for v in values):
            return [("filter[status]", ",".join(values))]
        return []

    @classmethod
    def row(cls, item: dict[str, Any]) -> dict[str, Any]:
        """Map one user."""
        roles = dig(item, "relationships", "roles", "data") or []
        return {**super().row(item), "role_ids": [r.get("id") for r in roles if isinstance(r, dict)]}


class Teams(NumberPagedV2):
    """Rows for ``datadog.resources.teams`` (GET /api/v2/team)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("id", pa.string(), "Team id (UUID).", nullable=False),
            column("name", pa.string(), "Team name."),
            column("handle", pa.string(), "Team handle, used in team:<handle> tags."),
            column("summary", pa.string(), "One-line summary."),
            column("description", pa.string(), "Team description (Markdown)."),
            column("user_count", pa.int64(), "Members (count)."),
            column("link_count", pa.int64(), "Links on the team page (count)."),
            column("created_at", TSTZ, "When the team was created."),
            column("modified_at", TSTZ, "When the team was last changed."),
        ]
    )
    PATH: ClassVar[str] = "/api/v2/team"
    EXAMPLES: ClassVar[list[tuple[str, str]]] = [
        ("Largest teams", f"SELECT handle, user_count FROM {R}.teams ORDER BY user_count DESC, handle"),
        (
            "Monitors owned by each team",
            "SELECT t.handle, count(m.id) "
            "AS monitors "
            f"FROM {R}.teams t LEFT JOIN {R}.monitors "
            "m ON m.tags['team'] = t.handle GROUP "
            "BY t.handle ORDER BY monitors DESC",
        ),
    ]

    class Meta:
        """Table metadata."""

        name = "teams_scan"
        description = "Scan behind the teams table"


class Roles(NumberPagedV2):
    """Rows for ``datadog.resources.roles`` (GET /api/v2/roles)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("id", pa.string(), "Role id (UUID).", nullable=False),
            column("name", pa.string(), "Role name, e.g. 'Datadog Admin Role'."),
            column("user_count", pa.int64(), "Users holding the role (count)."),
            column("created_at", TSTZ, "When the role was created."),
            column("modified_at", TSTZ, "When the role was last changed."),
        ]
    )
    PATH: ClassVar[str] = "/api/v2/roles"
    EXAMPLES: ClassVar[list[tuple[str, str]]] = [
        ("Roles by membership", f"SELECT name, user_count FROM {R}.roles ORDER BY user_count DESC, name"),
    ]

    class Meta:
        """Table metadata."""

        name = "roles_scan"
        description = "Scan behind the roles table"


# ---------------------------------------------------------------------------- service catalog


class Services(NumberPagedV2):
    """Rows for ``datadog.resources.services`` (GET /api/v2/services/definitions)."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column(
                "service", pa.string(), "Service name (dd-service), as used in service:<name> tags.", nullable=False
            ),
            column("schema_version", pa.string(), "Service definition schema version, e.g. 'v2.2'."),
            column("team", pa.string(), "Owning team handle."),
            column("tier", pa.string(), "Importance tier, e.g. '1'."),
            column("lifecycle", pa.string(), "Lifecycle: production, experimental, deprecated ..."),
            column("application", pa.string(), "Application the service belongs to."),
            column("type", pa.string(), "Service type: web, db, cache, function, custom ..."),
            column("description", pa.string(), "Service description."),
            column("languages", STR_LIST, "Programming languages."),
            column("tags", TAGS, "Tags declared in the definition, as a MAP."),
            column("contacts", JSON, "Contacts (email, Slack, Teams), JSON."),
            column("links", JSON, "Links (runbooks, docs, repos, dashboards), JSON."),
            column("definition", JSON, "The full service definition, JSON."),
            column("last_modified_at", TSTZ, "When the definition was last changed."),
            column("ingestion_source", pa.string(), "Where the definition came from: api, github, ui ..."),
        ]
    )
    PATH: ClassVar[str] = "/api/v2/services/definitions"
    EXAMPLES: ClassVar[list[tuple[str, str]]] = [
        (
            "Tier-1 services per team",
            "SELECT team, list(service ORDER "
            "BY service) AS services FROM "
            f"{R}.services WHERE tier = '1' "
            "GROUP BY team ORDER BY team",
        ),
        (
            "Services without an owner",
            f"SELECT service, lifecycle FROM {R}.services WHERE team IS NULL ORDER BY service",
        ),
    ]

    class Meta:
        """Table metadata."""

        name = "services_scan"
        description = "Scan behind the services table"

    @classmethod
    def extra(cls, params: ProcessParams[Any]) -> list[tuple[str, Any]]:
        """Ask for the newest schema version."""
        return [("schema_version", "v2.2")]

    @classmethod
    def row(cls, item: dict[str, Any]) -> dict[str, Any]:
        """Map one service definition."""
        a = item.get("attributes") or {}
        schema = a.get("schema") or {}
        meta = a.get("meta") or {}
        return {
            "service": schema.get("dd-service"),
            "schema_version": schema.get("schema-version"),
            "team": schema.get("team"),
            "tier": None if schema.get("tier") is None else str(schema.get("tier")),
            "lifecycle": schema.get("lifecycle"),
            "application": schema.get("application"),
            "type": schema.get("type"),
            "description": schema.get("description"),
            "languages": schema.get("languages"),
            "tags": tag_map(schema.get("tags") or []),
            "contacts": json_text(schema.get("contacts") or []),
            "links": json_text(schema.get("links") or []),
            "definition": json_text(schema),
            "last_modified_at": meta.get("last-modified-time"),
            "ingestion_source": meta.get("ingestion-source"),
        }


# ---------------------------------------------------------------------------- usage


@dataclass(slots=True, frozen=True, kw_only=True)
class UsageArgs:
    """Arguments for ``usage_summary(start_month, end_month := NULL)``."""

    start_month: Annotated[
        dt.date, Arg(0, doc="First month to report; any day within it works, e.g. 2026-09-01.", arrow_type=pa.date32())
    ]
    end_month: Annotated[
        dt.date | None,
        Arg(
            "end_month",
            default=None,
            doc="Month the report ends with, sent as Datadog's end_month; NULL reports up to the current month.",
            arrow_type=pa.date32(),
        ),
    ]


class UsageSummary(PagedScan):
    """``usage_summary(start_month, end_month := NULL)``: monthly usage as long rows."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(
        [
            column("month", pa.date32(), "First day of the month.", nullable=False),
            column(
                "usage_type",
                pa.string(),
                "Usage field, e.g. 'infra_host_top99p', 'logs_indexed_logs_usage_sum'.",
                nullable=False,
            ),
            column("value", pa.float64(), "Usage value for the month (hosts, bytes, events ... per the field's unit)."),
        ]
    )
    FunctionArguments = UsageArgs
    EXAMPLES: ClassVar[list[FunctionExample]] = [
        FunctionExample(
            sql=(
                "SELECT month, value AS infra_hosts "
                f"FROM {R}.usage_summary(DATE "
                "'2026-06-01', end_month "
                ":= DATE '2026-10-01') "
                "WHERE usage_type = 'infra_host_top99p' ORDER BY month"
            ),
            description="Monthly infrastructure host count (99th percentile) over five months",
        )
    ]

    class Meta:
        """Function metadata."""

        name = "usage_summary"
        description = "Monthly Datadog usage metering per usage field (needs usage_read on the parent org)"
        tags: ClassVar[dict[str, str]] = {}
        examples: ClassVar[list[Any]] = []

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET /api/v1/usage/summary; a 403 explains the permission it needs."""
        args: UsageArgs = params.args
        query: list[tuple[str, Any]] = [("start_month", f"{args.start_month:%Y-%m}"), ("include_org_details", False)]
        if args.end_month is not None:
            query.append(("end_month", f"{args.end_month:%Y-%m}"))
        try:
            body = client.get("/api/v1/usage/summary", query) or {}
        except DatadogError as err:
            if "HTTP 403" in str(err):
                raise DatadogError(
                    "usage_summary: this key cannot read usage metering. It needs an application key with the "
                    "usage_read permission on the parent organization (multi-org accounts), and a plan that "
                    f"includes usage metering. Datadog said: {err}"
                ) from err
            raise
        rows = []
        for month in body.get("usage") or []:
            day = parse_ts(month.get("date"))
            if day is None:
                continue
            for key, value in sorted(month.items()):
                if key in ("date", "orgs") or isinstance(value, bool) or not isinstance(value, int | float):
                    continue
                rows.append({"month": day.date(), "usage_type": key, "value": value})
        return rows, None
