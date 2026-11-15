"""Human- and agent-facing documentation for every object in the ``datadog`` catalog."""

from __future__ import annotations

import json
from typing import Any

from .meta import API_DOCS, SOURCE_URL, keywords_array

CREDENTIALS_MD = (
    "Credentials come from a DuckDB secret of type `datadog`, created **after** `ATTACH`:\n\n"
    "```sql\n"
    "-- US1 (datadoghq.com) is the default site\n"
    "CREATE SECRET dd (TYPE datadog, API_KEY '...', APP_KEY '...');\n"
    "-- any other site: datadoghq.eu, us3.datadoghq.com, us5.datadoghq.com, ap1.datadoghq.com,\n"
    "-- ap2.datadoghq.com, ddog-gov.com\n"
    "CREATE SECRET dd (TYPE datadog, API_KEY '...', APP_KEY '...', SITE 'datadoghq.eu');\n"
    "```\n\n"
    "Use an application key scoped to read permissions (metrics_read, logs_read_data, apm_read, "
    "monitors_read, incident_read, dashboards_read, synthetics_read, user_access_read, audit_logs_read, "
    "usage_read). The worker never writes to Datadog."
)

CATALOG_DESCRIPTION = (
    "Datadog as SQL: query metrics, search logs, spans, events and the audit trail, and list monitors, "
    "SLOs, incidents, dashboards, synthetic tests, hosts and users, on any Datadog site. Read-only."
)

CATALOG_DOC_LLM = (
    "Read one Datadog organization from DuckDB (sites US1, US3, US5, EU, AP1, AP2 and US1-FED). A DuckDB "
    "secret of type `datadog` created after ATTACH supplies API_KEY, APP_KEY and, outside US1, SITE. Schema "
    "`main` holds telemetry: metrics_query(query, from_time, to_time) returns long rows (metric, tags "
    "`MAP(VARCHAR, VARCHAR)`, ts `TIMESTAMPTZ`, value `DOUBLE`) that UNION with other metric sources; "
    "logs, spans, events and audit_events search a time window with Datadog's search syntax, and SQL "
    "equality, IN and <> predicates on their hoisted columns (service, status, host, env ...) are appended "
    "to the search query while ts predicates narrow the window; logs_aggregate and spans_aggregate compute "
    "counts and percentiles in Datadog. Schema `resources` lists monitors with per-group states, downtimes, "
    "SLOs with slo_history over a window, incidents (incident_id, title, status, severity, created_at, "
    "resolved_at, time_to_resolve_seconds, url), dashboards with full definitions, synthetic tests with "
    "results, hosts and host tags, users, teams, roles, the service catalog and the monthly usage summary. "
    "Tags are maps; open shapes (attributes, options, widgets) are `JSON`. Rate limits (HTTP 429 with "
    "X-RateLimit-Reset) are waited out and retried."
)

CATALOG_DOC_MD = (
    "# Datadog\n\n"
    "Datadog in DuckDB SQL through the [Datadog API]("
    f"{API_DOCS}): metric queries as long rows, log, span, event and audit search with your `WHERE` "
    "clause translated into Datadog search syntax, log and span analytics, and monitors, SLOs, incidents, "
    "dashboards, synthetics, hosts, users and the service catalog as tables.\n\n"
    f"{CREDENTIALS_MD}"
)

MAIN_DOC_LLM = (
    "Datadog telemetry for the secret's organization. metrics_query runs a metric query (the query editor "
    "syntax, e.g. 'avg:system.cpu.user{env:prod} by {host}') over an explicit window and returns one row per "
    "point; read group values from tags['host']. The search functions take a Datadog search query plus "
    "from_time and to_time and page with Datadog's cursor; filtering on their typed columns in SQL is pushed "
    "into the search query, so `WHERE service = 'web' AND status = 'error'` searches only those logs. Use "
    "max_rows with newest_first := true for the latest N. JSON attribute predicates are applied by DuckDB "
    "after reading; put facet filters such as @http.status_code:>=500 in the query text instead. "
    "logs_aggregate / spans_aggregate group by facets in Datadog and return (groups `MAP`, ts, value) rows. "
    "Spans join logs on trace_id. The metrics table lists metric names; metric_metadata and metric_tags "
    "describe one metric."
)

MAIN_DOC_MD = (
    "## main\n\n"
    "Telemetry: metric queries, log / span / event / audit search and analytics.\n\n"
    "`WHERE` predicates on service, status, host, env (and the other hoisted columns) become Datadog search "
    "terms; predicates on `ts` narrow the request window. Put facet filters in the query text."
)

RESOURCES_DOC_LLM = (
    "Datadog configuration and records for the secret's organization: monitors (with overall state, "
    "priority and tags) and monitor_groups (one row per monitor group and its state; WHERE monitor_id = n "
    "fetches only that monitor), downtimes, SLOs with targets and slo_history(slo_id, from_time, to_time) for "
    "the SLI and remaining error budget, incidents with severity and time-to-resolve, dashboards and "
    "dashboard(id) for widget JSON, synthetics_tests and synthetics_results(public_id, from_time, to_time), "
    "hosts (tags from every source merged into one `MAP`) and host_tags, users with role ids, teams, roles, "
    "the service catalog (owning team, tier, lifecycle) and usage_summary(start_month) for monthly usage "
    "metering. Join monitors to teams through tags['team'] = teams.handle, and spans or logs to services "
    "on the service name."
)

RESOURCES_DOC_MD = (
    "## resources\n\n"
    "Monitors, downtimes, SLOs, incidents, dashboards, synthetics, hosts, users, teams, roles, the service "
    "catalog and usage metering.\n\n"
    "Every table pages through its list endpoint; tags are maps you can index (`tags['team']`)."
)


def _t(title: str, category: str, keywords: str, comment: str, doc_llm: str, doc_md: str) -> dict[str, str]:
    return {
        "title": title,
        "category": category,
        "keywords": keywords,
        "comment": comment,
        "doc_llm": doc_llm,
        "doc_md": doc_md,
    }


TABLE_DOCS: dict[str, dict[str, str]] = {
    "metrics": _t(
        "Metric Names",
        "metrics",
        "metric list, metric names, metrics without limits, tag configuration, distribution",
        "Every metric name in the organization, with its Metrics without Limits tag configuration if any.",
        "One row per metric name reported to the organization (GET /api/v2/metrics). has_tag_configuration is "
        "true for metrics with a Metrics without Limits configuration, which also fills metric_type, "
        "configured_tags and include_percentiles; other metrics have NULLs there. WHERE metric_type = 'x' is "
        "sent as filter[metric_type]. Use metric_metadata for a metric's unit and type, metric_tags for its "
        "tag values, and metrics_query to read points.",
        "Every metric name, plus its tag configuration when one exists.",
    ),
    "monitors": _t(
        "Alert Monitors",
        "monitors",
        "monitors, alerts, alerting, monitor state, priority, notifications",
        "Datadog monitors with their query, overall state, priority, tags and options.",
        "One row per monitor (GET /api/v1/monitor, paged). overall_state is OK, Alert, Warn, No Data, Ignored, "
        "Skipped or Unknown; priority is 1 (highest) to 5 or NULL. tags is a `MAP`, so tags['team'] gives the "
        "owning team. WHERE name = 'x' is sent as Datadog's name filter (substring) and re-checked; WHERE id = n "
        "fetches that one monitor. Per-group states live in monitor_groups; links open url.",
        "Monitors and their current overall state. Per-group states are in `monitor_groups`.",
    ),
    "monitor_groups": _t(
        "Monitor Group States",
        "monitors",
        "monitor groups, multi alert, alerting hosts, group state, triggered",
        "One row per monitor and group (e.g. per host) with that group's state and last trigger times.",
        "The per-group states of every monitor (GET /api/v1/monitor?group_states=all). A multi-alert monitor "
        "has one row per group such as 'host:web-1,env:prod' (group_tags splits it into a `MAP`); a simple "
        "monitor has one row with group_name '*'. WHERE monitor_id = n fetches just that monitor. Use it to "
        "find which hosts or services are alerting: WHERE status = 'Alert'.",
        "Which groups of each monitor are alerting, and since when.",
    ),
    "downtimes": _t(
        "Monitor Downtimes",
        "monitors",
        "downtimes, mute, maintenance window, scheduled downtime",
        "Monitor downtimes: scope, target monitor or monitor tags, schedule and status.",
        "One row per downtime (GET /api/v2/downtime). status is active, scheduled, ended or canceled; "
        "WHERE status = 'active' asks Datadog for current downtimes only. A downtime targets either one "
        "monitor (monitor_id) or monitors by tag (monitor_tags) within a scope such as env:staging. start_at / "
        "end_at give the current or first window; recurring schedules are in schedule JSON.",
        "Monitor downtimes and what they mute.",
    ),
    "slos": _t(
        "Service Level Objectives",
        "slos",
        "slo, service level objective, error budget, target, sli",
        "Service level objectives with their type, targets and tags.",
        "One row per SLO (GET /api/v1/slo). target / timeframe / warning come from the first threshold, "
        "thresholds lists all of them. Metric SLOs carry numerator / denominator queries, monitor SLOs carry "
        "monitor_ids. WHERE id = 'x' (or IN) is sent as ids=. For the measured SLI and remaining error budget "
        "over a window call slo_history(id, from_time, to_time).",
        "SLOs and their targets. Measured values come from `slo_history`.",
    ),
    "incidents": _t(
        "Incident Records",
        "incidents",
        "incidents, incident management, sev, severity, mttr, time to resolve, postmortem",
        "Datadog incidents with status, severity, impact, timestamps and time-to-resolve metrics.",
        "One row per incident (GET /api/v2/incidents). Columns follow the shared incident vocabulary: "
        "incident_id, title, status (active, stable, resolved), severity (SEV-1 .. SEV-5, UNKNOWN), "
        "created_at, resolved_at, time_to_resolve_seconds and url, so the table UNIONs with other incident "
        "sources. incident_number is the public #n. services and teams are lists from the incident fields; "
        "every field is also in fields JSON. Compute MTTR as avg(time_to_resolve_seconds).",
        "Incidents with severity, impact and time-to-resolve.",
    ),
    "dashboards": _t(
        "Dashboard List",
        "dashboards",
        "dashboards, timeboard, screenboard, dashboard list",
        "Every dashboard: title, layout, author and timestamps.",
        "One row per dashboard (GET /api/v1/dashboard, paged). layout_type is 'ordered' (timeboard) or 'free' "
        "(screenboard). The listing has no widgets; call dashboard(id) for the full definition.",
        "Every dashboard. The widget JSON comes from `dashboard(id)`.",
    ),
    "synthetics_tests": _t(
        "Synthetic Tests",
        "synthetics",
        "synthetics, synthetic monitoring, api test, browser test, uptime check",
        "Synthetic API and browser tests: type, status, locations, frequency and tags.",
        "One row per synthetic test (GET /api/v1/synthetics/tests). type is api, browser or mobile; subtype "
        "names the API test kind (http, ssl, dns ...). status is live or paused. monitor_id joins to monitors. "
        "Run history comes from synthetics_results(public_id, from_time, to_time).",
        "Synthetic tests. Their runs come from `synthetics_results`.",
    ),
    "hosts": _t(
        "Infrastructure Hosts",
        "infrastructure",
        "hosts, infrastructure list, agents, servers, cpu, host map",
        "Hosts reporting to Datadog with their status, tags, agent version and recent CPU and load.",
        "One row per host (GET /api/v1/hosts, paged). tags merges the tags of every source (agent, cloud "
        "provider, user) into one `MAP`: tags['env']. up is true while the host reports. WHERE host_name = 'x' "
        "is sent as Datadog's host filter and re-checked. cpu_pct / iowait_pct / load are the infrastructure "
        "list's recent values; use metrics_query for history.",
        "Hosts, their tags and recent CPU and load.",
    ),
    "host_tags": _t(
        "Host Tag Assignments",
        "infrastructure",
        "host tags, tags, role, environment, inventory",
        "Every tag of every host, one row per host and tag, split into key and value.",
        "One row per (host, tag) from GET /api/v1/tags/hosts. key and value split the tag at its first colon "
        "(a bare tag has value ''). Use it to count hosts per role or env, or to find hosts missing a tag.",
        "Host tags as rows: one per host and tag.",
    ),
    "users": _t(
        "Organization Users",
        "organization",
        "users, accounts, members, mfa, service accounts, roles",
        "Users of the organization with status, MFA, service-account flag and role ids.",
        "One row per user (GET /api/v2/users). status is Active, Pending or Disabled; WHERE status = 'x' is "
        "sent as filter[status]. role_ids lists the user's roles: unnest it and join roles.id for role names.",
        "Users with status, MFA and roles.",
    ),
    "teams": _t(
        "Team Directory",
        "organization",
        "teams, ownership, team handle, members",
        "Teams with handle, description and member count.",
        "One row per team (GET /api/v2/team). handle is what team:<handle> tags use, so monitors, services "
        "and SLOs join to teams through tags['team'] = handle or services.team = handle.",
        "Teams and their member counts.",
    ),
    "roles": _t(
        "Access Roles",
        "organization",
        "roles, rbac, permissions, access control",
        "Access roles defined in the organization, with how many users hold each one.",
        "One row per role (GET /api/v2/roles), with user_count. users.role_ids reference roles.id.",
        "Roles and how many users hold each.",
    ),
    "services": _t(
        "Service Catalog",
        "services",
        "service catalog, software catalog, service definitions, ownership, tier, lifecycle",
        "Service catalog definitions: owning team, tier, lifecycle, languages, contacts and links.",
        "One row per service definition (GET /api/v2/services/definitions, schema v2.2). service is the "
        "dd-service name used in service:<name> tags on spans, logs and metrics; team, tier and lifecycle "
        "describe ownership. contacts and links are JSON arrays; definition holds the whole document.",
        "Service catalog entries with ownership, tier and lifecycle.",
    ),
}

FUNCTION_DOCS: dict[str, dict[str, Any]] = {
    "metrics_query": {
        "title": "Metrics Query",
        "category": "metrics",
        "keywords": "metrics, timeseries, query, avg, sum, by host, points, long format, /api/v1/query",
        "doc_llm": (
            "Run a Datadog metric query (the query editor syntax: 'avg:system.cpu.user{env:prod} by {host}', "
            "functions and arithmetic allowed, several queries comma-separated) over [from_time, to_time] "
            "(both `TIMESTAMPTZ`, positional) through GET /api/v1/query. Returns long rows: metric, tags "
            "`MAP(VARCHAR, VARCHAR)` with the series' group-by tags (tags['host']), ts `TIMESTAMPTZ`, value "
            "`DOUBLE` (NULL in a gap), plus scope, expression, query_index, aggr, unit and interval_seconds. "
            "Datadog picks the rollup from the window length (append .rollup(avg, 60) to the query to fix it). "
            "The shape matches other metric workers' long format: alias tags as labels and the rows UNION ALL "
            "BY NAME with them."
        ),
        "doc_md": (
            "## metrics_query(query, from_time, to_time)\n\n"
            "Metric points as long rows `(metric, tags, ts, value)`. Group values are in the `tags` map."
        ),
    },
    "metric_metadata": {
        "title": "Metric Metadata",
        "category": "metrics",
        "keywords": "metric metadata, unit, metric type, description, integration",
        "doc_llm": (
            "Return one row describing a metric (GET /api/v1/metrics/{metric}): type (gauge, count, rate, "
            "distribution), description, short_name, unit and per_unit (bytes per second), statsd_interval and "
            "the reporting integration. Positional arg: the metric name."
        ),
        "doc_md": "## metric_metadata(metric)\n\nType, unit and description of one metric.",
    },
    "metric_tags": {
        "title": "Metric Tags",
        "category": "metrics",
        "keywords": "metric tags, tag values, cardinality, all tags, dimensions",
        "doc_llm": (
            "Return every tag reported with one metric (GET /api/v2/metrics/{metric}/all-tags), one row per "
            "tag with key and value split at the first colon. Count rows per key for tag cardinality, or list "
            "the values to build a by {...} clause. Positional arg: the metric name."
        ),
        "doc_md": "## metric_tags(metric)\n\nEvery tag of one metric, split into key and value.",
    },
    "logs": {
        "title": "Log Search",
        "category": "logs",
        "keywords": "logs, log search, log explorer, errors, status, service, /api/v2/logs/events/search",
        "doc_llm": (
            "Search logs (POST /api/v2/logs/events/search) matching a Datadog search query (positional, e.g. "
            "'service:web @http.status_code:>=500'; '*' for all) between from_time and to_time (`TIMESTAMPTZ`, "
            "positional). Columns: id, ts, status, service, host, env and source (hoisted tags), message, "
            "trace_id, tags `MAP`, attributes `JSON` (custom facets: attributes->>'$.http.method'). SQL "
            "predicates on service, status, host, env and source (=, <>, IN, OR of equalities, IS NOT NULL) are "
            "appended to the query, and ts predicates narrow the window. Named: max_rows caps the rows read, "
            "newest_first := true pages from the newest log, indexes (comma-separated) and storage_tier "
            "('indexes', 'online-archives', 'flex'). Pages of up to 1,000 logs follow Datadog's cursor. For "
            "counts over large volumes prefer logs_aggregate."
        ),
        "doc_md": (
            "## logs(query, from_time, to_time, max_rows := NULL, newest_first := false, indexes := NULL, "
            "storage_tier := 'indexes')\n\n"
            "Log search. `WHERE service = ... AND status = ...` runs inside Datadog."
        ),
    },
    "logs_aggregate": {
        "title": "Log Analytics",
        "category": "logs",
        "keywords": "log analytics, aggregate, count by, percentile, timeseries, group by facet",
        "doc_llm": (
            "Aggregate logs in Datadog (POST /api/v2/logs/analytics/aggregate) instead of reading them. "
            "Positional: query, from_time, to_time. Named: group_by (comma-separated facets such as "
            "'service,status' or '@http.status_code'), aggregation ('count' default, cardinality, sum, min, "
            "max, avg, median, pc75, pc90, pc95, pc98, pc99), metric (the measure, e.g. '@duration', required "
            "except for count), group_limit (top N groups per facet, default 10) and rollup (e.g. '5m' for one "
            "row per bucket). Returns long rows: groups `MAP` (groups['service']), ts (bucket start, NULL for "
            "totals), value, aggregation, metric."
        ),
        "doc_md": (
            "## logs_aggregate(query, from_time, to_time, group_by := NULL, aggregation := 'count', "
            "metric := NULL, group_limit := 10, rollup := NULL)\n\nCounts and percentiles computed by Datadog."
        ),
    },
    "spans": {
        "title": "Span Search",
        "category": "apm",
        "keywords": "apm, spans, traces, latency, errors, resource, trace search, /api/v2/spans/events/search",
        "doc_llm": (
            "Search APM spans (POST /api/v2/spans/events/search) with a Datadog search query between from_time "
            "and to_time (positional). Columns: span_id, trace_id, parent_id, ts (start), end_ts, "
            "duration_seconds, service, env, host, resource_name, operation_name, span_type, status ('ok' or "
            "'error'), is_error, tags `MAP`, attributes `JSON`. SQL predicates on service, env, host, "
            "resource_name, operation_name, status and trace_id are appended to the query; ts predicates narrow "
            "the window. Named: max_rows, newest_first. Join logs on trace_id. For latency percentiles over "
            "many spans prefer spans_aggregate."
        ),
        "doc_md": (
            "## spans(query, from_time, to_time, max_rows := NULL, newest_first := false)\n\n"
            "APM span search; filters on service, env, resource and status run in Datadog."
        ),
    },
    "spans_aggregate": {
        "title": "Span Analytics",
        "category": "apm",
        "keywords": "apm analytics, latency percentiles, p99, request count, aggregate spans",
        "doc_llm": (
            "Aggregate spans in Datadog (POST /api/v2/spans/analytics/aggregate). Same arguments and long "
            "output (groups `MAP`, ts, value, aggregation, metric) as logs_aggregate. Span @duration is in "
            "nanoseconds, so divide pc99 by 1e6 for milliseconds."
        ),
        "doc_md": (
            "## spans_aggregate(query, from_time, to_time, group_by := NULL, aggregation := 'count', "
            "metric := NULL, group_limit := 10, rollup := NULL)\n\nSpan counts and latency percentiles."
        ),
    },
    "events": {
        "title": "Event Search",
        "category": "events",
        "keywords": "events, event stream, deploys, alerts, changes, /api/v2/events/search",
        "doc_llm": (
            "Search the event stream (POST /api/v2/events/search) with a Datadog search query between "
            "from_time and to_time (positional). Columns: id, ts, title, message, source, status, priority, "
            "host, service, aggregation_key, tags `MAP`, attributes `JSON`. SQL predicates on source, status, "
            "priority, host and service are appended to the query; ts predicates narrow the window. Named: "
            "max_rows, newest_first. Use it to line deploys up against metrics or incidents."
        ),
        "doc_md": (
            "## events(query, from_time, to_time, max_rows := NULL, newest_first := false)\n\n"
            "Event stream search; filters on source, status, host and service run in Datadog."
        ),
    },
    "audit_events": {
        "title": "Audit Trail Search",
        "category": "audit",
        "keywords": "audit trail, audit log, who changed, compliance, security, /api/v2/audit/events/search",
        "doc_llm": (
            "Search the audit trail (POST /api/v2/audit/events/search) between from_time and to_time "
            "(positional) with a Datadog search query. Columns: id, ts, service (product area), event_name, "
            "action, asset_type, asset_id, asset_name, user_email, message, tags `MAP`, attributes `JSON`. SQL "
            "predicates on service, event_name (@evt.name), action (@action), asset_type (@asset.type) and "
            "user_email (@usr.email) are appended to the query. Needs audit_logs_read."
        ),
        "doc_md": (
            "## audit_events(query, from_time, to_time, max_rows := NULL, newest_first := false)\n\n"
            "Who did what: audit trail search with user, action and asset filters pushed down."
        ),
    },
    "slo_history": {
        "title": "SLO History",
        "category": "slos",
        "keywords": "slo history, sli, error budget, burn, compliance, uptime",
        "doc_llm": (
            "Measure one SLO over [from_time, to_time] (GET /api/v1/slo/{id}/history). Positional: slo_id "
            "(slos.id), from_time, to_time. One overall row (group_name NULL) plus one row per monitor group for "
            "grouped monitor SLOs: sli_value (percent), target (primary threshold), error_budget_remaining_pct "
            "= (sli - target) / (100 - target) * 100 (negative once the budget is exhausted), and for metric "
            "SLOs good_events / total_events."
        ),
        "doc_md": "## slo_history(slo_id, from_time, to_time)\n\nSLI and error budget left over a window.",
    },
    "dashboard": {
        "title": "Dashboard Definition",
        "category": "dashboards",
        "keywords": "dashboard definition, widgets, template variables, dashboard json",
        "doc_llm": (
            "Return one dashboard's full definition (GET /api/v1/dashboard/{id}) as one row: title, layout_type, "
            "tags, widget_count, widgets `JSON` (iterate with json_each), template_variables `JSON` and the "
            "whole definition `JSON`. Positional arg: dashboard_id from the dashboards table."
        ),
        "doc_md": "## dashboard(dashboard_id)\n\nOne dashboard's widgets and template variables as JSON.",
    },
    "synthetics_results": {
        "title": "Synthetic Test Results",
        "category": "synthetics",
        "keywords": "synthetics results, uptime, availability, check results, success rate",
        "doc_llm": (
            "Return the recent runs of one synthetic test in [from_time, to_time]. Positional: public_id "
            "(synthetics_tests.public_id), from_time, to_time. The worker looks the test up to choose the API "
            "or browser results endpoint. One row per run and location: check_time, location, passed, status "
            "(0 passed, 1 failed, 2 no data), duration_ms and http_status_code. Datadog returns the latest "
            "results only (about 150 per call), so narrow the window for long histories."
        ),
        "doc_md": (
            "## synthetics_results(public_id, from_time, to_time)\n\n"
            "Recent runs of one test: pass/fail, duration and location."
        ),
    },
    "usage_summary": {
        "title": "Usage Summary",
        "category": "usage",
        "keywords": "usage metering, billing, hosts, indexed logs, consumption, monthly usage",
        "doc_llm": (
            "Monthly usage metering (GET /api/v1/usage/summary) as long rows: month `DATE`, usage_type (the "
            "API's field name, e.g. infra_host_top99p, apm_host_top99p, logs_indexed_logs_usage_sum, "
            "ingested_events_bytes_sum) and value. Positional: start_month (`DATE`, any day of the month). "
            "Named: end_month (`DATE`). Needs an application key with usage_read on the parent organization; "
            "without it the call fails with a message naming that permission."
        ),
        "doc_md": (
            "## usage_summary(start_month, end_month := NULL)\n\n"
            "Monthly usage per usage field. Needs `usage_read` on the parent org."
        ),
    },
}

AGENT_TEST_TASKS = [
    {
        "name": "prod_errors_by_service",
        "prompt": (
            "Using the datadog worker, how many error-status logs did each service emit in env prod between "
            "2026-11-01 00:00 and 2026-11-02 00:00 UTC?"
        ),
    },
    {
        "name": "alerting_monitors",
        "prompt": "Which Datadog monitors are currently in the Alert state? Give their ids and names.",
    },
    {
        "name": "cpu_by_host",
        "prompt": (
            "What was the average of avg:system.cpu.user{env:prod} by {host} per host between 2026-11-01 00:00 "
            "and 01:00 UTC?"
        ),
    },
    {
        "name": "mttr_by_severity",
        "prompt": "What is the mean time to resolve, in hours, of resolved Datadog incidents per severity?",
    },
    {
        "name": "slow_endpoints",
        "prompt": (
            "Which resource of the web service had the slowest single span between 2026-11-01 00:00 and 01:00 "
            "UTC, and how long did it take in seconds?"
        ),
    },
    {
        "name": "log_volume_by_status",
        "prompt": "Using Datadog log analytics (computed "
        "in Datadog, not by reading logs), "
        "count prod logs per status between "
        "2026-11-01 00:00 and 2026-11-02 "
        "00:00 UTC.",
    },
    {
        "name": "span_p99_by_resource",
        "prompt": "Using Datadog span analytics, what is "
        "the p99 span duration in milliseconds "
        "per resource in env prod between "
        "2026-11-01 00:00 and 2026-11-02 "
        "00:00 UTC?",
    },
    {
        "name": "deploys_per_service",
        "prompt": "How many GitHub deploy events "
        "(tag deploy) did each "
        "service get between 2026-11-01 "
        "and 2026-11-03 UTC?",
    },
    {
        "name": "audit_deletions",
        "prompt": "Who deleted what (asset type "
        "and name) in the Datadog "
        "audit trail during the week "
        "starting 2026-11-01 UTC?",
    },
    {
        "name": "configured_metrics",
        "prompt": "Which metric names have a Metrics without Limits tag configuration, and what type are they?",
    },
    {"name": "metric_unit", "prompt": "What unit and per-unit does the metric system.net.bytes_rcvd report in?"},
    {
        "name": "cpu_host_cardinality",
        "prompt": "How many distinct host tag values does the metric system.cpu.user have?",
    },
    {
        "name": "active_downtimes",
        "prompt": "Which Datadog downtimes are active right now, and what scope do they mute?",
    },
    {
        "name": "alerting_hosts",
        "prompt": "Which hosts are alerting on at least one monitor group, and on which monitors?",
    },
    {
        "name": "slo_error_budget",
        "prompt": "How much error budget (percent) "
        "was left on the 'Web availability' "
        "SLO over October 2026 "
        "(2026-10-01 to 2026-11-01 UTC)?",
    },
    {"name": "dashboard_widgets", "prompt": "How many widgets does the 'Web overview' dashboard have?"},
    {
        "name": "synthetic_success_rate",
        "prompt": "What was the success rate per location of the 'Homepage up' synthetic test on 2026-11-01 UTC?",
    },
    {"name": "synthetic_tests_live", "prompt": "How many live synthetic tests are there per test type?"},
    {"name": "hosts_per_role", "prompt": "How many hosts carry each role tag?"},
    {"name": "busiest_host", "prompt": "Which reporting host has the highest CPU utilisation right now, and how much?"},
    {
        "name": "roles_without_mfa",
        "prompt": "Which roles do active human users without MFA hold? List user email and role name.",
    },
    {"name": "largest_team", "prompt": "Which Datadog team has the most members?"},
    {"name": "unowned_services", "prompt": "Which services in the service catalog have no owning team?"},
    {
        "name": "infra_hosts_october",
        "prompt": "What was the 99th-percentile infrastructure host count in October 2026 according to usage metering?",
    },
    {
        "name": "quoted_search_term",
        "prompt": 'Build the escaped Datadog search term that matches the service named checkout "v2" exactly.',
    },
    {
        "name": "tag_list_team",
        "prompt": "Which team does the Datadog tag list ['env:prod', 'team:core', 'legacy'] name?",
    },
    {"name": "slo_targets_by_team", "prompt": "List every SLO with its owning team tag, target and timeframe."},
    {
        "name": "dashboards_per_author",
        "prompt": "How many dashboards has each author created, and how many of them are read-only?",
    },
]

CATALOG_TAGS = {
    "vgi.title": "Datadog Observability",
    "vgi.subtitle": "Metrics, logs, spans, monitors, SLOs and incidents from Datadog as DuckDB tables.",
    "vgi.doc_llm": CATALOG_DOC_LLM,
    "vgi.doc_md": CATALOG_DOC_MD,
    "vgi.source_url": SOURCE_URL,
    "vgi.icon_url": "https://query.farm/media-kit/products/vgi/vgi-logo.png",
    "vgi.license": "0BSD",
    "vgi.author": "Query.Farm",
    "vgi.copyright": "Copyright 2026 Query Farm LLC - https://query.farm",
    "vgi.keywords": keywords_array(
        "datadog, observability, monitoring, metrics, logs, apm, traces, slo, incidents, monitors, duckdb"
    ),
    "vgi.support_contact": f"{SOURCE_URL}/issues",
    "vgi.support_policy_url": f"{SOURCE_URL}#support",
    "vgi.agent_test_tasks": json.dumps(AGENT_TEST_TASKS),
}
