#!/usr/bin/env python3
"""Write test/fixtures/datadog.json: a small, fixed Datadog organization in documented API shapes.

Every record below is shaped like the corresponding Datadog API response object
(https://docs.datadoghq.com/api/latest/), so test/mock_api/server.py can serve it
verbatim. Run ``python3 test/fixtures/generate.py`` after editing.
"""

from __future__ import annotations

import json
from pathlib import Path

T0 = 1_793_491_200  # 2026-11-01T00:00:00Z


def iso(seconds: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(seconds, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def log(i: int, offset: float, status: str, service: str, host: str, env: str, msg: str, **attrs: object) -> dict:
    tags = [f"env:{env}", f"service:{service}", "source:python" if service != "nginx" else "source:nginx"]
    if i == 5:
        tags.append("team:core")
        tags.append("team:platform")
    return {
        "id": f"AQAAAZ{i:04d}",
        "type": "log",
        "attributes": {
            "timestamp": iso(T0 + offset),
            "status": status,
            "service": service,
            "host": host,
            "message": msg,
            "tags": tags,
            "attributes": attrs,
        },
    }


LOGS = [
    log(
        1,
        60,
        "info",
        "web",
        "web-1",
        "prod",
        "GET /api/users 200",
        http={"status_code": 200, "method": "GET"},
        duration=12_000_000,
    ),
    log(
        2,
        120,
        "error",
        "web",
        "web-1",
        "prod",
        "GET /api/orders 500",
        http={"status_code": 500, "method": "GET"},
        duration=950_000_000,
        dd={"trace_id": "7001"},
    ),
    log(
        3,
        180,
        "error",
        "web",
        "web-2",
        "prod",
        'upstream "orders" failed',
        http={"status_code": 502, "method": "POST"},
        duration=30_000_000,
    ),
    log(4, 240, "warn", "api", "api-1", "prod", "slow query", duration=400_000_000),
    log(5, 300, "error", "api", "api-1", "prod", "connection reset", dd={"trace_id": "7003"}),
    log(6, 360, "error", "api", "api-2", "staging", "test failure"),
    log(7, 420, "info", "nginx", "lb-1", "prod", "200 GET /"),
    log(8, 480, "error", "o'reilly svc", "web-3", "prod", "quoted service name"),
    log(9, 3_600, "info", "web", "web-1", "prod", "hour later"),
    log(
        10,
        3_660,
        "error",
        "web",
        "web-2",
        "prod",
        "GET /api/orders 500 again",
        http={"status_code": 500, "method": "GET"},
    ),
    log(11, 86_400, "error", "web", "web-1", "prod", "next day"),
    log(12, 86_400 + 60, "info", "api", "api-1", "prod", "next day info"),
]


def span(
    i: int,
    offset: float,
    service: str,
    resource: str,
    status: str,
    duration_ms: float,
    env: str = "prod",
    trace: str | None = None,
    parent: str = "0",
) -> dict:
    return {
        "id": f"span-{i}",
        "type": "spans",
        "attributes": {
            "span_id": f"{9000 + i}",
            "trace_id": trace or f"{7000 + i}",
            "parent_id": parent,
            "start_timestamp": iso(T0 + offset),
            "end_timestamp": iso(T0 + offset + duration_ms / 1000),
            "service": service,
            "env": env,
            "host": f"{service}-1",
            "resource_name": resource,
            "type": "web",
            "status": status,
            "tags": [f"env:{env}", f"service:{service}", "version:1.4.2"],
            "custom": {
                "duration": int(duration_ms * 1_000_000),
                "operation_name": "http.request",
                "http": {"method": resource.split(" ")[0]},
            },
        },
    }


SPANS = [
    span(1, 60, "web", "GET /api/users", "ok", 12),
    span(2, 120, "web", "GET /api/orders", "error", 950, trace="7001"),
    span(3, 300, "api", "SELECT orders", "error", 410, trace="7003"),
    span(4, 310, "api", "SELECT users", "ok", 4, trace="7003", parent="9003"),
    span(5, 400, "web", "GET /api/users", "ok", 20),
    span(6, 500, "web", "POST /api/orders", "ok", 140),
    span(7, 600, "web", "GET /api/orders", "ok", 1800, env="staging"),
]


def event(
    i: int, offset: float, title: str, source: str, status: str, tags: list[str], host: str | None = None
) -> dict:
    return {
        "id": f"evt-{i}",
        "type": "event",
        "attributes": {
            "timestamp": iso(T0 + offset),
            "message": f"{title} details",
            "tags": [f"source:{source}", *tags],
            "attributes": {
                "title": title,
                "status": status,
                "priority": "normal",
                "hostname": host,
                "aggregation_key": f"agg-{i % 2}",
                "evt": {"id": f"{5000 + i}", "type": "change"},
                "timestamp": (T0 + offset) * 1000,
            },
        },
    }


EVENTS = [
    event(1, 100, "Deployed web 1.4.2", "github", "success", ["deploy", "service:web", "env:prod"]),
    event(2, 200, "Monitor triggered: High error rate", "alert", "error", ["monitor", "service:web"]),
    event(3, 900, "Deployed api 2.0.0", "github", "success", ["deploy", "service:api", "env:prod"]),
    event(4, 1_000, "Node drained", "kubernetes", "warning", ["env:prod"], host="node-7"),
    event(5, 90_000, "Deployed web 1.4.3", "github", "success", ["deploy", "service:web", "env:prod"]),
]


def audit(i: int, offset: float, service: str, action: str, asset_type: str, name: str, email: str) -> dict:
    return {
        "id": f"aud-{i}",
        "type": "audit",
        "attributes": {
            "timestamp": iso(T0 + offset),
            "service": service,
            "message": f"{email} {action} {asset_type} {name}",
            "tags": ["source:audit"],
            "attributes": {
                "evt": {"name": "Request"},
                "action": action,
                "asset": {"type": asset_type, "id": f"{asset_type}-{i}", "name": name},
                "usr": {"email": email, "id": f"u-{i}"},
            },
        },
    }


AUDIT = [
    audit(1, 50, "monitor", "modified", "monitor", "High error rate", "ada@example.com"),
    audit(2, 150, "dashboard", "created", "dashboard", "Web overview", "grace@example.com"),
    audit(3, 250, "monitor", "deleted", "monitor", "Old disk check", "ada@example.com"),
    audit(4, 350, "apikey", "accessed", "api_key", "ci key", "ci@example.com"),
]


def series(
    metric: str, scope: str, values: list[float | None], aggr: str = "avg", qi: int = 0, interval: int = 600
) -> dict:
    return {
        "metric": metric,
        "display_name": metric,
        "scope": scope,
        "tag_set": [] if scope == "*" else scope.split(","),
        "expression": f"{aggr}:{metric}{{{scope}}}",
        "aggr": aggr,
        "query_index": qi,
        "interval": interval,
        "length": len(values),
        "start": T0 * 1000,
        "end": (T0 + interval * len(values)) * 1000,
        "unit": [
            {"family": "percentage", "name": "percent", "plural": "percent", "scale_factor": 1.0, "short_name": "%"},
            None,
        ],
        "pointlist": [[float((T0 + interval * k) * 1000), v] for k, v in enumerate(values)],
    }


METRIC_QUERIES = {
    "avg:system.cpu.user{env:prod} by {host}": [
        series("system.cpu.user", "env:prod,host:web-1", [10.0, 20.0, 30.0, None, 40.0, 50.0]),
        series("system.cpu.user", "env:prod,host:web-2", [5.0, 5.0, 5.0, 5.0, 5.0, 5.0]),
    ],
    "sum:trace.http.request.hits{service:web}.as_count()": [
        series("trace.http.request.hits", "service:web", [100.0, 120.0, 90.0, 110.0, 130.0, 150.0], aggr="sum"),
    ],
    "avg:system.load.1{*}, max:system.load.1{*}": [
        series("system.load.1", "*", [1.0, 2.0, 3.0, 2.0, 1.0, 1.0]),
        series("system.load.1", "*", [2.5, 4.0, 3.5, 2.0, 1.5, 1.0], aggr="max", qi=1),
    ],
}

METRICS_LIST = [
    {"type": "metrics", "id": "system.cpu.user"},
    {"type": "metrics", "id": "system.load.1"},
    {
        "type": "manage_tags",
        "id": "trace.http.request.hits",
        "attributes": {
            "metric_type": "count",
            "tags": ["service", "env"],
            "created_at": "2026-01-02T03:04:05Z",
            "modified_at": "2026-06-01T00:00:00Z",
        },
    },
    {
        "type": "manage_tags",
        "id": "checkout.latency",
        "attributes": {
            "metric_type": "distribution",
            "tags": ["service", "region"],
            "include_percentiles": True,
            "created_at": "2026-02-01T00:00:00Z",
            "modified_at": "2026-02-01T00:00:00Z",
        },
    },
    {"type": "metrics", "id": "system.net.bytes_rcvd"},
]

METRIC_METADATA = {
    "system.net.bytes_rcvd": {
        "type": "gauge",
        "description": "The number of bytes received on a device per second.",
        "short_name": "bytes rcvd",
        "unit": "byte",
        "per_unit": "second",
        "statsd_interval": None,
        "integration": "system",
    },
    "system.cpu.user": {
        "type": "gauge",
        "description": "The percent of time the CPU spent running user space processes.",
        "short_name": "cpu user",
        "unit": "percent",
        "per_unit": None,
        "statsd_interval": None,
        "integration": "system",
    },
}

METRIC_TAGS = {
    "system.cpu.user": [
        "host:web-1",
        "host:web-2",
        "env:prod",
        "env:staging",
        "availability-zone:us-east-1a",
        "env:prod",
    ],
}


def monitor(
    mid: int,
    name: str,
    mtype: str,
    state: str,
    priority: int | None,
    tags: list[str],
    groups: dict[str, dict] | None = None,
) -> dict:
    return {
        "id": mid,
        "name": name,
        "type": mtype,
        "query": f"avg(last_5m):avg:{name.lower().replace(' ', '.')}{{*}} > 1",
        "message": "Notify @slack-ops",
        "tags": tags,
        "options": {"thresholds": {"critical": 1.0}, "notify_no_data": False},
        "multi": groups is not None and len(groups) > 1,
        "created": "2026-03-01T10:00:00.000000+00:00",
        "modified": "2026-10-01T10:00:00.000000+00:00",
        "creator": {"email": "ada@example.com", "handle": "ada@example.com", "name": "Ada"},
        "overall_state": state,
        "priority": priority,
        "state": {
            "groups": groups
            or {
                "*": {
                    "status": state,
                    "name": "*",
                    "last_triggered_ts": T0 if state == "Alert" else None,
                    "last_resolved_ts": None,
                    "last_nodata_ts": None,
                    "last_notified_ts": None,
                }
            }
        },
    }


MONITORS = [
    monitor(
        1001,
        "High error rate",
        "metric alert",
        "Alert",
        1,
        ["team:core", "service:web"],
        {
            "host:web-1": {
                "status": "Alert",
                "name": "host:web-1",
                "last_triggered_ts": T0 + 120,
                "last_resolved_ts": None,
                "last_nodata_ts": None,
                "last_notified_ts": T0 + 130,
            },
            "host:web-2": {
                "status": "OK",
                "name": "host:web-2",
                "last_triggered_ts": T0 - 86_400,
                "last_resolved_ts": T0 - 80_000,
                "last_nodata_ts": None,
                "last_notified_ts": None,
            },
        },
    ),
    monitor(1002, "Disk almost full", "metric alert", "OK", 3, ["team:platform"]),
    monitor(1003, "API latency", "query alert", "Warn", 2, ["team:core", "service:api"]),
    monitor(
        1004,
        "Log errors",
        "log alert",
        "Alert",
        None,
        ["team:platform", "service:api"],
        {
            "env:prod,service:api": {
                "status": "Alert",
                "name": "env:prod,service:api",
                "last_triggered_ts": T0 + 300,
                "last_resolved_ts": None,
                "last_nodata_ts": None,
                "last_notified_ts": T0 + 310,
            },
        },
    ),
    monitor(1005, "Heartbeat", "service check", "No Data", 5, []),
]

DOWNTIMES = [
    {
        "id": "00000000-0000-4000-8000-000000000001",
        "type": "downtime",
        "attributes": {
            "status": "active",
            "scope": "env:staging",
            "monitor_identifier": {"monitor_tags": ["team:platform"]},
            "message": "staging rebuild",
            "schedule": {"start": "2026-11-01T00:00:00+00:00", "end": None},
            "created": "2026-10-31T00:00:00+00:00",
            "modified": "2026-10-31T00:00:00+00:00",
            "canceled": None,
        },
        "relationships": {"created_by": {"data": {"id": "u-1", "type": "users"}}},
    },
    {
        "id": "00000000-0000-4000-8000-000000000002",
        "type": "downtime",
        "attributes": {
            "status": "scheduled",
            "scope": "host:web-1",
            "monitor_identifier": {"monitor_id": 1001},
            "message": "patching",
            "schedule": {
                "recurrences": [{"rrule": "FREQ=WEEKLY;BYDAY=SU", "duration": "2h", "start": "2026-11-08T02:00"}],
                "timezone": "UTC",
                "current_downtime": {"start": "2026-11-08T02:00:00+00:00", "end": "2026-11-08T04:00:00+00:00"},
            },
            "created": "2026-10-20T00:00:00+00:00",
            "modified": "2026-10-21T00:00:00+00:00",
            "canceled": None,
        },
        "relationships": {"created_by": {"data": {"id": "u-2", "type": "users"}}},
    },
    {
        "id": "00000000-0000-4000-8000-000000000003",
        "type": "downtime",
        "attributes": {
            "status": "ended",
            "scope": "*",
            "monitor_identifier": {"monitor_id": 1002},
            "message": None,
            "schedule": {"start": "2026-09-01T00:00:00+00:00", "end": "2026-09-01T01:00:00+00:00"},
            "created": "2026-08-31T00:00:00+00:00",
            "modified": "2026-09-01T01:00:00+00:00",
            "canceled": None,
        },
        "relationships": {"created_by": {"data": {"id": "u-1", "type": "users"}}},
    },
]

SLOS = [
    {
        "id": "abc123def456",
        "name": "Web availability",
        "description": "Good requests over all requests",
        "type": "metric",
        "tags": ["team:core", "service:web"],
        "thresholds": [{"timeframe": "30d", "target": 99.9, "warning": 99.95}, {"timeframe": "7d", "target": 99.5}],
        "monitor_ids": [],
        "query": {
            "numerator": "sum:requests.ok{service:web}.as_count()",
            "denominator": "sum:requests{service:web}.as_count()",
        },
        "created_at": T0 - 200 * 86_400,
        "modified_at": T0 - 10 * 86_400,
        "creator": {"email": "ada@example.com"},
    },
    {
        "id": "fed987cba654",
        "name": "API uptime",
        "description": None,
        "type": "monitor",
        "tags": ["team:platform"],
        "thresholds": [{"timeframe": "7d", "target": 99.0}],
        "monitor_ids": [1003, 1004],
        "query": None,
        "created_at": T0 - 100 * 86_400,
        "modified_at": T0 - 5 * 86_400,
        "creator": {"email": "grace@example.com"},
    },
    {
        "id": "aaa111bbb222",
        "name": "Checkout latency",
        "description": "p95 under 300ms",
        "type": "time_slice",
        "tags": [],
        "thresholds": [{"timeframe": "90d", "target": 95.0}],
        "monitor_ids": [],
        "query": None,
        "created_at": T0 - 50 * 86_400,
        "modified_at": T0 - 50 * 86_400,
        "creator": None,
    },
]

SLO_HISTORY = {
    "abc123def456": {
        "type": "metric",
        "overall": {"name": "Web availability", "sli_value": 99.95, "span_precision": 2, "precision": {"30d": 2}},
        "thresholds": {"30d": {"timeframe": "30d", "target": 99.9, "warning": 99.95}},
        "series": {"numerator": {"sum": 199_900.0}, "denominator": {"sum": 200_000.0}},
        "groups": [],
    },
    "fed987cba654": {
        "type": "monitor",
        "overall": {"name": "API uptime", "sli_value": 98.5},
        "thresholds": {"7d": {"timeframe": "7d", "target": 99.0}},
        "groups": [
            {"name": "env:prod,service:api", "sli_value": 98.0},
            {"name": "env:staging,service:api", "sli_value": 99.5},
        ],
    },
}


def incident(
    n: int,
    title: str,
    state: str,
    sev: str,
    created: float,
    resolved: float | None,
    services: list[str],
    teams: list[str],
) -> dict:
    return {
        "id": f"00000000-0000-4000-9000-0000000000{n:02d}",
        "type": "incidents",
        "attributes": {
            "public_id": n,
            "title": title,
            "customer_impacted": n % 2 == 1,
            "customer_impact_scope": "checkout failing" if n % 2 == 1 else None,
            "created": iso(created),
            "detected": iso(created - 120),
            "modified": iso((resolved or created) + 60),
            "resolved": iso(resolved) if resolved else None,
            "time_to_detect": 120,
            "time_to_internal_response": 300,
            "time_to_repair": int(resolved - created) - 60 if resolved else None,
            "time_to_resolve": int(resolved - created) if resolved else None,
            "fields": {
                "state": {"type": "dropdown", "value": state},
                "severity": {"type": "dropdown", "value": sev},
                "services": {"type": "autocomplete", "value": services},
                "teams": {"type": "autocomplete", "value": teams},
                "root_cause": {"type": "textbox", "value": "bad deploy" if resolved else None},
            },
        },
        "relationships": {"commander_user": {"data": {"id": f"u-{n}", "type": "users"}}},
    }


INCIDENTS = [
    incident(1, "Checkout errors", "resolved", "SEV-1", T0 + 100, T0 + 100 + 7_200, ["web"], ["core"]),
    incident(2, "Slow API", "resolved", "SEV-2", T0 + 5_000, T0 + 5_000 + 3_600, ["api"], ["platform"]),
    incident(3, "Login outage", "resolved", "SEV-1", T0 + 20_000, T0 + 20_000 + 14_400, ["web", "api"], ["core"]),
    incident(4, "Disk pressure", "active", "SEV-3", T0 + 40_000, None, ["db"], ["platform"]),
    incident(5, "Elevated 5xx", "stable", "SEV-2", T0 + 60_000, None, ["web"], []),
]

DASHBOARDS = [
    {
        "id": "abc-def-ghi",
        "title": "Web overview",
        "description": "Golden signals",
        "layout_type": "ordered",
        "url": "/dashboard/abc-def-ghi/web-overview",
        "author_handle": "ada@example.com",
        "created_at": "2025-06-01T00:00:00.000000+00:00",
        "modified_at": "2026-10-01T00:00:00.000000+00:00",
        "is_read_only": False,
    },
    {
        "id": "jkl-mno-pqr",
        "title": "Legacy hosts",
        "description": None,
        "layout_type": "free",
        "url": "/dashboard/jkl-mno-pqr/legacy-hosts",
        "author_handle": "grace@example.com",
        "created_at": "2024-01-01T00:00:00.000000+00:00",
        "modified_at": "2025-01-01T00:00:00.000000+00:00",
        "is_read_only": True,
    },
    {
        "id": "stu-vwx-yz1",
        "title": "API latency",
        "description": "p50/p95/p99",
        "layout_type": "ordered",
        "url": "/dashboard/stu-vwx-yz1/api-latency",
        "author_handle": "ada@example.com",
        "created_at": "2026-02-01T00:00:00.000000+00:00",
        "modified_at": "2026-02-02T00:00:00.000000+00:00",
        "is_read_only": False,
    },
]

DASHBOARD_DEFS = {
    "abc-def-ghi": {
        **DASHBOARDS[0],
        "tags": ["team:core"],
        "template_variables": [{"name": "env", "prefix": "env", "default": "prod"}],
        "widgets": [
            {
                "id": 1,
                "definition": {
                    "type": "timeseries",
                    "title": "CPU",
                    "requests": [{"q": "avg:system.cpu.user{$env} by {host}"}],
                },
            },
            {
                "id": 2,
                "definition": {"type": "query_value", "title": "Errors", "requests": [{"q": "sum:errors{$env}"}]},
            },
            {
                "id": 3,
                "definition": {"type": "timeseries", "title": "Load", "requests": [{"q": "avg:system.load.1{$env}"}]},
            },
            {"id": 4, "definition": {"type": "note", "content": "Owned by core"}},
        ],
    },
}

SYNTHETICS = [
    {
        "public_id": "abc-123-xyz",
        "name": "Homepage up",
        "type": "api",
        "subtype": "http",
        "status": "live",
        "monitor_id": 2001,
        "tags": ["team:core", "env:prod"],
        "locations": ["aws:us-east-1", "aws:eu-central-1"],
        "options": {"tick_every": 300},
        "message": "@slack-ops",
        "creator": {"email": "ada@example.com"},
        "config": {
            "request": {"method": "GET", "url": "https://shop.example/"},
            "assertions": [{"type": "statusCode", "operator": "is", "target": 200}],
        },
    },
    {
        "public_id": "def-456-uvw",
        "name": "Checkout flow",
        "type": "browser",
        "subtype": None,
        "status": "live",
        "monitor_id": 2002,
        "tags": ["team:core"],
        "locations": ["aws:us-east-1"],
        "options": {"tick_every": 900},
        "message": "",
        "creator": {"email": "grace@example.com"},
        "config": {"request": {"url": "https://shop.example/checkout"}},
    },
    {
        "public_id": "ghi-789-rst",
        "name": "DNS check",
        "type": "api",
        "subtype": "dns",
        "status": "paused",
        "monitor_id": 2003,
        "tags": [],
        "locations": ["aws:eu-central-1"],
        "options": {"tick_every": 3600},
        "message": "",
        "creator": {"email": "ada@example.com"},
        "config": {"request": {"host": "shop.example"}},
    },
]

SYNTHETICS_RESULTS = {
    "abc-123-xyz": [
        {
            "result_id": "r1",
            "status": 0,
            "check_time": (T0 + 300) * 1000.0,
            "probe_dc": "aws:us-east-1",
            "result": {"passed": True, "timings": {"total": 120.5}, "httpStatusCode": 200},
        },
        {
            "result_id": "r2",
            "status": 0,
            "check_time": (T0 + 300) * 1000.0,
            "probe_dc": "aws:eu-central-1",
            "result": {"passed": True, "timings": {"total": 210.0}, "httpStatusCode": 200},
        },
        {
            "result_id": "r3",
            "status": 1,
            "check_time": (T0 + 600) * 1000.0,
            "probe_dc": "aws:us-east-1",
            "result": {"passed": False, "timings": {"total": 5000.0}, "httpStatusCode": 503},
        },
        {
            "result_id": "r4",
            "status": 0,
            "check_time": (T0 + 600) * 1000.0,
            "probe_dc": "aws:eu-central-1",
            "result": {"passed": True, "timings": {"total": 190.0}, "httpStatusCode": 200},
        },
        {
            "result_id": "r5",
            "status": 0,
            "check_time": (T0 + 90_000) * 1000.0,
            "probe_dc": "aws:us-east-1",
            "result": {"passed": True, "timings": {"total": 100.0}, "httpStatusCode": 200},
        },
    ],
    "def-456-uvw": [
        {
            "result_id": "b1",
            "status": 0,
            "check_time": (T0 + 900) * 1000.0,
            "probe_dc": "aws:us-east-1",
            "result": {"passed": True, "duration": 8_400.0},
        },
        {
            "result_id": "b2",
            "status": 1,
            "check_time": (T0 + 1_800) * 1000.0,
            "probe_dc": "aws:us-east-1",
            "result": {"passed": False, "duration": 30_000.0},
        },
    ],
}

HOSTS = [
    {
        "name": "web-1",
        "host_name": "web-1",
        "id": 11,
        "aliases": ["i-0001"],
        "apps": ["agent", "nginx"],
        "sources": ["agent", "aws"],
        "up": True,
        "is_muted": False,
        "last_reported_time": T0 + 500,
        "tags_by_source": {
            "Datadog": ["env:prod", "role:web"],
            "Amazon Web Services": ["availability-zone:us-east-1a"],
        },
        "meta": {"platform": "linux", "agent_version": "7.60.0"},
        "metrics": {"cpu": 45.5, "iowait": 1.5, "load": 0.8},
    },
    {
        "name": "web-2",
        "host_name": "web-2",
        "id": 12,
        "aliases": [],
        "apps": ["agent"],
        "sources": ["agent"],
        "up": True,
        "is_muted": False,
        "last_reported_time": T0 + 500,
        "tags_by_source": {"Datadog": ["env:prod", "role:web"]},
        "meta": {"platform": "linux", "agent_version": "7.60.0"},
        "metrics": {"cpu": 5.0, "iowait": 0.1, "load": 0.1},
    },
    {
        "name": "api-1",
        "host_name": "api-1",
        "id": 13,
        "aliases": [],
        "apps": ["agent", "postgres"],
        "sources": ["agent"],
        "up": True,
        "is_muted": True,
        "last_reported_time": T0 + 400,
        "tags_by_source": {"Datadog": ["env:prod", "role:api"]},
        "meta": {"platform": "linux", "agent_version": "7.58.1"},
        "metrics": {"cpu": 70.0, "iowait": 4.0, "load": 1.9},
    },
    {
        "name": "api-2",
        "host_name": "api-2",
        "id": 14,
        "aliases": [],
        "apps": ["agent"],
        "sources": ["agent"],
        "up": False,
        "is_muted": False,
        "last_reported_time": T0 - 86_400,
        "tags_by_source": {"Datadog": ["env:staging", "role:api"]},
        "meta": {"platform": "windows", "agent_version": "7.58.1"},
        "metrics": {"cpu": None, "iowait": None, "load": None},
    },
    {
        "name": "lb-1",
        "host_name": "lb-1",
        "id": 15,
        "aliases": [],
        "apps": ["agent", "nginx"],
        "sources": ["agent"],
        "up": True,
        "is_muted": False,
        "last_reported_time": T0 + 500,
        "tags_by_source": {"Datadog": ["env:prod", "role:lb", "legacy"]},
        "meta": {"platform": "linux", "agent_version": "7.60.0"},
        "metrics": {"cpu": 12.0, "iowait": 0.2, "load": 0.3},
    },
]

USERS = [
    {
        "id": "u-1",
        "type": "users",
        "attributes": {
            "name": "Ada",
            "handle": "ada@example.com",
            "email": "ada@example.com",
            "status": "Active",
            "disabled": False,
            "verified": True,
            "service_account": False,
            "mfa_enabled": True,
            "title": "SRE",
            "created_at": "2024-01-01T00:00:00.000000+00:00",
            "modified_at": "2026-01-01T00:00:00.000000+00:00",
        },
        "relationships": {"roles": {"data": [{"id": "r-admin", "type": "roles"}]}},
    },
    {
        "id": "u-2",
        "type": "users",
        "attributes": {
            "name": "Grace",
            "handle": "grace@example.com",
            "email": "grace@example.com",
            "status": "Active",
            "disabled": False,
            "verified": True,
            "service_account": False,
            "mfa_enabled": False,
            "title": "Engineer",
            "created_at": "2024-02-01T00:00:00.000000+00:00",
            "modified_at": "2026-02-01T00:00:00.000000+00:00",
        },
        "relationships": {"roles": {"data": [{"id": "r-std", "type": "roles"}]}},
    },
    {
        "id": "u-3",
        "type": "users",
        "attributes": {
            "name": "CI bot",
            "handle": "ci@example.com",
            "email": "ci@example.com",
            "status": "Active",
            "disabled": False,
            "verified": True,
            "service_account": True,
            "mfa_enabled": False,
            "title": None,
            "created_at": "2025-01-01T00:00:00.000000+00:00",
            "modified_at": "2025-01-01T00:00:00.000000+00:00",
        },
        "relationships": {"roles": {"data": [{"id": "r-ro", "type": "roles"}]}},
    },
    {
        "id": "u-4",
        "type": "users",
        "attributes": {
            "name": "Linus",
            "handle": "linus@example.com",
            "email": "linus@example.com",
            "status": "Pending",
            "disabled": False,
            "verified": False,
            "service_account": False,
            "mfa_enabled": False,
            "title": None,
            "created_at": "2026-10-30T00:00:00.000000+00:00",
            "modified_at": "2026-10-30T00:00:00.000000+00:00",
        },
        "relationships": {"roles": {"data": [{"id": "r-std", "type": "roles"}, {"id": "r-ro", "type": "roles"}]}},
    },
    {
        "id": "u-5",
        "type": "users",
        "attributes": {
            "name": "Old",
            "handle": "old@example.com",
            "email": "old@example.com",
            "status": "Disabled",
            "disabled": True,
            "verified": True,
            "service_account": False,
            "mfa_enabled": False,
            "title": None,
            "created_at": "2023-01-01T00:00:00.000000+00:00",
            "modified_at": "2024-01-01T00:00:00.000000+00:00",
        },
        "relationships": {"roles": {"data": []}},
    },
]

TEAMS = [
    {
        "id": "t-1",
        "type": "team",
        "attributes": {
            "name": "Core",
            "handle": "core",
            "summary": "Web and checkout",
            "description": "## Core",
            "user_count": 6,
            "link_count": 2,
            "created_at": "2024-01-01T00:00:00.000000+00:00",
            "modified_at": "2026-01-01T00:00:00.000000+00:00",
        },
    },
    {
        "id": "t-2",
        "type": "team",
        "attributes": {
            "name": "Platform",
            "handle": "platform",
            "summary": "Infra",
            "description": None,
            "user_count": 4,
            "link_count": 0,
            "created_at": "2024-01-01T00:00:00.000000+00:00",
            "modified_at": "2026-01-01T00:00:00.000000+00:00",
        },
    },
    {
        "id": "t-3",
        "type": "team",
        "attributes": {
            "name": "Data",
            "handle": "data",
            "summary": None,
            "description": None,
            "user_count": 0,
            "link_count": 0,
            "created_at": "2026-05-01T00:00:00.000000+00:00",
            "modified_at": "2026-05-01T00:00:00.000000+00:00",
        },
    },
]

ROLES = [
    {
        "id": "r-admin",
        "type": "roles",
        "attributes": {
            "name": "Datadog Admin Role",
            "user_count": 1,
            "created_at": "2023-01-01T00:00:00.000000+00:00",
            "modified_at": "2023-01-01T00:00:00.000000+00:00",
        },
    },
    {
        "id": "r-std",
        "type": "roles",
        "attributes": {
            "name": "Datadog Standard Role",
            "user_count": 2,
            "created_at": "2023-01-01T00:00:00.000000+00:00",
            "modified_at": "2023-01-01T00:00:00.000000+00:00",
        },
    },
    {
        "id": "r-ro",
        "type": "roles",
        "attributes": {
            "name": "Datadog Read Only Role",
            "user_count": 2,
            "created_at": "2023-01-01T00:00:00.000000+00:00",
            "modified_at": "2023-01-01T00:00:00.000000+00:00",
        },
    },
]


def service(name: str, team: str | None, tier: str, lifecycle: str, langs: list[str]) -> dict:
    schema = {
        "schema-version": "v2.2",
        "dd-service": name,
        "team": team,
        "tier": tier,
        "lifecycle": lifecycle,
        "application": "shop",
        "type": "web",
        "description": f"The {name} service",
        "languages": langs,
        "tags": [f"service:{name}", "business-unit:retail"],
        "contacts": [{"type": "slack", "contact": "https://example.slack.com/archives/C1"}],
        "links": [{"name": "Runbook", "type": "runbook", "url": "https://wiki.example/runbook"}],
    }
    if team is None:
        del schema["team"]
    return {
        "type": "service-definition",
        "id": f"sd-{name}",
        "attributes": {
            "schema": schema,
            "meta": {
                "last-modified-time": "2026-09-01T00:00:00Z",
                "ingestion-source": "github",
                "github-html-url": f"https://github.com/example/{name}",
            },
        },
    }


SERVICES = [
    service("web", "core", "1", "production", ["python"]),
    service("api", "platform", "1", "production", ["go"]),
    service("checkout", "core", "2", "production", ["java"]),
    service("legacy-batch", None, "3", "deprecated", ["python"]),
]

USAGE = [
    {
        "date": "2026-09-01T00:00:00+00:00",
        "infra_host_top99p": 40,
        "apm_host_top99p": 12,
        "logs_indexed_logs_usage_sum": 1_500_000,
        "ingested_events_bytes_sum": 9.5e10,
        "billing_plan": "pro",
        "orgs": [],
    },
    {
        "date": "2026-10-01T00:00:00+00:00",
        "infra_host_top99p": 44,
        "apm_host_top99p": 13,
        "logs_indexed_logs_usage_sum": 1_750_000,
        "ingested_events_bytes_sum": 1.1e11,
        "billing_plan": "pro",
        "orgs": [],
    },
]

FIXTURE = {
    "logs": LOGS,
    "spans": SPANS,
    "events": EVENTS,
    "audit": AUDIT,
    "metric_queries": METRIC_QUERIES,
    "metrics": METRICS_LIST,
    "metric_metadata": METRIC_METADATA,
    "metric_tags": METRIC_TAGS,
    "monitors": MONITORS,
    "downtimes": DOWNTIMES,
    "slos": SLOS,
    "slo_history": SLO_HISTORY,
    "incidents": INCIDENTS,
    "dashboards": DASHBOARDS,
    "dashboard_definitions": DASHBOARD_DEFS,
    "synthetics": SYNTHETICS,
    "synthetics_results": SYNTHETICS_RESULTS,
    "hosts": HOSTS,
    "host_tags": {
        "role:web": ["web-1", "web-2"],
        "role:api": ["api-1", "api-2"],
        "env:prod": ["web-1", "web-2", "api-1", "lb-1"],
        "env:staging": ["api-2"],
        "legacy": ["lb-1"],
    },
    "users": USERS,
    "teams": TEAMS,
    "roles": ROLES,
    "services": SERVICES,
    "usage": USAGE,
}

if __name__ == "__main__":
    out = Path(__file__).with_name("datadog.json")
    out.write_text(json.dumps(FIXTURE, indent=1, sort_keys=True) + "\n")
    print(f"wrote {out}")
