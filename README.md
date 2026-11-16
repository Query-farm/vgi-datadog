<p align="center">
  <img src="https://query.farm/media-kit/products/vgi/vgi-logo.png" alt="Vector Gateway Interface (VGI)" width="320">
</p>

# Query Datadog Metrics, Logs, Traces, Monitors and Incidents in DuckDB

> **vgi-datadog** · a [Query.Farm](https://query.farm) VGI worker

[![CI](https://github.com/Query-farm/vgi-datadog/actions/workflows/ci.yml/badge.svg)](https://github.com/Query-farm/vgi-datadog/actions/workflows/ci.yml)
[![vgi worker: query.farm](https://query.farm/media-kit/shields/vgi-worker.svg)](https://query.farm)

[Datadog](https://www.datadoghq.com) as SQL, read-only, on every Datadog site. Run metric queries and
get long rows `(metric, tags, ts, value)`; search logs, APM spans, events and the audit trail with your
`WHERE` clause translated into Datadog search syntax; let Datadog compute log and span analytics; and
list monitors, downtimes, SLOs, incidents, dashboards, synthetic tests, hosts, users, teams, roles, the
service catalog and usage metering as tables.

```sql
-- Which production services logged the most errors yesterday?
SELECT service, count(*) AS errors
FROM datadog.main.logs('*', TIMESTAMPTZ '2026-11-14 00:00:00+00', TIMESTAMPTZ '2026-11-15 00:00:00+00')
WHERE status = 'error' AND env = 'prod'
GROUP BY service ORDER BY errors DESC;
```

`status = 'error' AND env = 'prod'` is sent to Datadog as `status:"error" env:"prod"`, so only matching
logs cross the wire.

## Quick start

**Prerequisites:** [uv](https://docs.astral.sh/uv/) and DuckDB 2.0 or [Haybarn](https://query.farm/products/haybarn) 2.0.

The worker is installed straight from this repository (it is not published to PyPI). `LOCATION` is
the command DuckDB runs to start it:

```sql
-- DuckDB 2.0
SET allow_extension_repositories = 'allowed';
CREATE EXTENSION REPOSITORY IF NOT EXISTS query_farm WITH PREFIX 'https://duckdb-extensions.query.farm';
INSTALL vgi FROM query_farm;
LOAD vgi FROM query_farm;
-- Haybarn 2.0: INSTALL vgi FROM community; LOAD vgi;

ATTACH 'datadog' (TYPE vgi,
  LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-datadog@v0.1.1 vgi-datadog-worker');
```

From a checkout, `make build` and use `LOCATION '/abs/path/to/vgi-datadog/.venv/bin/vgi-datadog-worker'`.

## Credentials

Create an API key and an application key (Organization settings → API keys / Application keys), then
a DuckDB secret **after** `ATTACH`:

```sql
-- US1 (datadoghq.com) is the default site
CREATE SECRET dd (TYPE datadog, API_KEY '...', APP_KEY '...');

-- Any other site
CREATE SECRET dd (TYPE datadog, API_KEY '...', APP_KEY '...', SITE 'datadoghq.eu');
```

| Field | Required | Meaning |
|---|---|---|
| `API_KEY` | yes | Sent as `DD-API-KEY`. Redacted in `duckdb_secrets()`. |
| `APP_KEY` | yes | Sent as `DD-APPLICATION-KEY`. Scope it to read permissions. Redacted. |
| `SITE` | no | `datadoghq.com` (US1, default), `us3.datadoghq.com`, `us5.datadoghq.com`, `datadoghq.eu`, `ap1.datadoghq.com`, `ap2.datadoghq.com` or `ddog-gov.com`. Aliases: `us1`, `us3`, `us5`, `eu`, `ap1`, `ap2`, `gov`. Picks the API origin (`https://api.<site>`) and the app origin used in `url` columns. |
| `BASE_URL` | no | Override the API origin: a proxy, or a local fixture server in tests. |
| `PAGE_SIZE` | no | Rows per page on search endpoints, 1–1000 (default 1000). List endpoints cap it at their own maximum. |
| `TIMEOUT` | no | Per-request timeout in seconds (default 60). |

Suggested application-key scopes: `metrics_read`, `timeseries_query`, `logs_read_data`, `logs_read_index_data`,
`apm_read`, `events_read`, `audit_logs_read`, `monitors_read`, `monitors_downtime`, `slos_read`,
`incident_read`, `dashboards_read`, `synthetics_read`, `hosts_read`, `user_access_read`, `teams_read`,
`apm_service_catalog_read`, `usage_read`. The worker never calls a write endpoint.

Keys are read from the secret on every call. They never enter table-function state, logs or query
text.

## What's inside

### `datadog.main`: telemetry

| Object | Kind | Endpoint |
|---|---|---|
| `metrics_query(query, from_time, to_time)` | table function | `GET /api/v1/query` |
| `metrics` | table | `GET /api/v2/metrics` |
| `metric_metadata(metric)` | table function | `GET /api/v1/metrics/{metric}` |
| `metric_tags(metric)` | table function | `GET /api/v2/metrics/{metric}/all-tags` |
| `logs(query, from_time, to_time, max_rows, newest_first, indexes, storage_tier)` | table function | `POST /api/v2/logs/events/search` |
| `logs_aggregate(query, from_time, to_time, group_by, aggregation, metric, group_limit, rollup)` | table function | `POST /api/v2/logs/analytics/aggregate` |
| `spans(query, from_time, to_time, max_rows, newest_first)` | table function | `POST /api/v2/spans/events/search` |
| `spans_aggregate(...)` | table function | `POST /api/v2/spans/analytics/aggregate` |
| `events(query, from_time, to_time, max_rows, newest_first)` | table function | `POST /api/v2/events/search` |
| `audit_events(query, from_time, to_time, max_rows, newest_first)` | table function | `POST /api/v2/audit/events/search` |
| `search_term(attribute, value)` | scalar (offline) | builds `attribute:"value"`, quoted and escaped |
| `tag_map(tags)` | scalar (offline) | `['env:prod', 'team:core']` → `MAP {'env': 'prod', 'team': 'core'}` |

`from_time` / `to_time` are positional `TIMESTAMPTZ` arguments.

### `datadog.resources`: configuration and records

| Object | Kind | Endpoint |
|---|---|---|
| `monitors` | table | `GET /api/v1/monitor` |
| `monitor_groups` | table (one row per monitor group and its state) | `GET /api/v1/monitor?group_states=all` |
| `downtimes` | table | `GET /api/v2/downtime` |
| `slos` | table | `GET /api/v1/slo` |
| `slo_history(slo_id, from_time, to_time)` | table function | `GET /api/v1/slo/{id}/history` |
| `incidents` | table | `GET /api/v2/incidents` |
| `dashboards` | table | `GET /api/v1/dashboard` |
| `dashboard(dashboard_id)` | table function (widgets as JSON) | `GET /api/v1/dashboard/{id}` |
| `synthetics_tests` | table | `GET /api/v1/synthetics/tests` |
| `synthetics_results(public_id, from_time, to_time)` | table function | `GET /api/v1/synthetics/tests/[browser/]{id}/results` |
| `hosts` | table | `GET /api/v1/hosts` |
| `host_tags` | table | `GET /api/v1/tags/hosts` |
| `users`, `teams`, `roles` | tables | `GET /api/v2/users`, `/api/v2/team`, `/api/v2/roles` |
| `services` | table (service catalog) | `GET /api/v2/services/definitions` |
| `usage_summary(start_month, end_month := NULL)` | table function | `GET /api/v1/usage/summary` |

### Shapes that UNION with other workers

* **Metrics** come back in long format: `metric VARCHAR`, `tags MAP(VARCHAR, VARCHAR)`, `ts TIMESTAMPTZ`,
  `value DOUBLE` (plus `scope`, `expression`, `query_index`, `aggr`, `unit`, `interval_seconds`). It
  matches the Prometheus worker's `(metric, labels, ts, value)`:

  ```sql
  SELECT metric, tags AS labels, ts, value FROM datadog.main.metrics_query('avg:system.cpu.user{*} by {host}', TIMESTAMPTZ '2026-11-14 00:00:00+00', TIMESTAMPTZ '2026-11-14 01:00:00+00')
  UNION ALL BY NAME
  SELECT metric, labels, ts, value FROM prometheus.main.query_range('node_load1', TIMESTAMPTZ '2026-11-14 00:00:00+00', TIMESTAMPTZ '2026-11-14 01:00:00+00', 60);
  ```

* **Incidents** use the shared incident vocabulary: `incident_id`, `title`, `status`, `severity`,
  `created_at`, `resolved_at`, `time_to_resolve_seconds`, `url` (plus Datadog's `incident_number`,
  detect/repair times, customer impact, services, teams and every field as JSON).
* **Tags** are always `MAP(VARCHAR, VARCHAR)`, split at the first colon. A bare tag maps to `''`; a key
  that repeats keeps all its values joined with `,`.

## Pushdown

| Where | What is sent to Datadog |
|---|---|
| `logs` | `service`, `status`, `host`, `env`, `source`: `=`, `<>`, `IN`, `OR` of equalities on one column, `IS NOT NULL` → search terms; `ts` comparisons → `filter.from` / `filter.to` |
| `spans` | `service`, `env`, `host`, `resource_name`, `operation_name`, `status`, `trace_id` → search terms; `ts` → window |
| `events` | `source`, `status`, `priority`, `host`, `service` → search terms; `ts` → window |
| `audit_events` | `service`, `event_name` (`@evt.name`), `action` (`@action`), `asset_type` (`@asset.type`), `user_email` (`@usr.email`) → search terms; `ts` → window |
| `max_rows`, `newest_first` | `page.limit`, `sort: -timestamp` |
| `metrics` | `metric_type = 'x'` → `filter[metric_type]` |
| `monitors` | `id = n` → `GET /api/v1/monitor/{n}`; `name = 'x'` → `name=` (substring, re-checked) |
| `monitor_groups` | `monitor_id = n` → `GET /api/v1/monitor/{n}?group_states=all` |
| `downtimes` | `status = 'active'` → `current_only=true` |
| `slos` | `id = 'x'` / `id IN (...)` → `ids=` |
| `hosts` | `host_name = 'x'` → `filter=` (re-checked) |
| `users` | `status = 'x'` → `filter[status]` |

Every pushed term is `attribute:"value"` with `\` and `"` escaped, so values can never change the
query's structure. The user's own query is parenthesised before terms are appended, so its `OR` cannot
swallow them. Every scan sets `auto_apply_filters`: DuckDB re-applies every predicate to the returned
rows, so predicates that are not translated (or only widened, such as `ts <= x`, sent as `to = x + 1 ms`
because Datadog's `to` is exclusive) still hold. A contradictory `ts` window sends no request at all.

Not pushed: predicates on JSON `attributes` and on `tags` maps (put facets such as
`@http.status_code:>=500` in the query text), and time predicates on `metrics_query` and the aggregate
functions (Datadog chooses the rollup from the window, so narrowing it would change the values).

## Limits, paging and rate limits

* Search endpoints page with Datadog's opaque `meta.page.after` cursor, up to 1,000 rows per page; list
  endpoints page with their own page number or offset. The cursor lives in the scan state, so a long
  scan resumes on any worker process over the HTTP transport.
* Datadog rate-limits each endpoint separately and reports it in `X-RateLimit-Limit` / `-Period` /
  `-Remaining` / `-Reset` / `-Name`. A 429 is retried after `X-RateLimit-Reset` seconds (or
  `Retry-After`, then exponential backoff, capped at 60 s, up to 6 attempts); a successful answer with
  `X-RateLimit-Remaining: 0` makes the next request to that endpoint wait for the reset instead of
  spending a 429. 5xx answers and connection errors are retried with backoff.
* Log search covers the retention of the chosen indexes; use `storage_tier := 'flex'` or
  `'online-archives'` for other tiers. For large volumes prefer `logs_aggregate`.
* `synthetics_results` returns Datadog's latest results for the window (about 150 per call).
* `usage_summary` needs `usage_read` on the parent organization. Without it the call fails with a
  message naming that permission (it is feature-detected from the 403, not assumed).
* The incident timeline has no documented public read endpoint, so it is not exposed; every incident
  field is available in `incidents.fields` JSON.

## Examples

```sql
-- CPU per production host over one hour, in long format.
SELECT tags['host'] AS host, avg(value) AS avg_cpu
FROM datadog.main.metrics_query('avg:system.cpu.user{env:prod} by {host}',
       TIMESTAMPTZ '2026-11-14 00:00:00+00', TIMESTAMPTZ '2026-11-14 01:00:00+00')
GROUP BY host ORDER BY avg_cpu DESC;

-- p95 latency and error rate per endpoint of one service.
SELECT resource_name, quantile_cont(duration_seconds, 0.95) AS p95_s, avg(is_error::INT) AS error_rate
FROM datadog.main.spans('*', TIMESTAMPTZ '2026-11-14 00:00:00+00', TIMESTAMPTZ '2026-11-14 01:00:00+00')
WHERE service = 'web' AND env = 'prod'
GROUP BY resource_name ORDER BY p95_s DESC;

-- Log volume per service and status, counted inside Datadog.
SELECT groups['service'] AS service, groups['status'] AS status, value AS logs
FROM datadog.main.logs_aggregate('env:prod', TIMESTAMPTZ '2026-11-14 00:00:00+00',
       TIMESTAMPTZ '2026-11-15 00:00:00+00', group_by := 'service,status')
ORDER BY logs DESC;

-- Hosts alerting on any monitor.
SELECT group_tags['host'] AS host, list(monitor_name ORDER BY monitor_name) AS monitors
FROM datadog.resources.monitor_groups WHERE status = 'Alert' GROUP BY host;

-- MTTR per severity.
SELECT severity, count(*) AS incidents, avg(time_to_resolve_seconds) / 3600 AS mttr_hours
FROM datadog.resources.incidents WHERE status = 'resolved' GROUP BY severity ORDER BY severity;

-- Error budget left on one SLO over October.
SELECT sli_value, target, error_budget_remaining_pct
FROM datadog.resources.slo_history('abc123def456', TIMESTAMPTZ '2026-10-01 00:00:00+00',
       TIMESTAMPTZ '2026-11-01 00:00:00+00')
WHERE group_name IS NULL;

-- Monitors owned by each team.
SELECT t.handle, count(m.id) AS monitors
FROM datadog.resources.teams t LEFT JOIN datadog.resources.monitors m ON m.tags['team'] = t.handle
GROUP BY t.handle ORDER BY monitors DESC;
```

## Testing

CI needs no network and no Datadog account. `test/mock_api/server.py` is a strict replay server over
`test/fixtures/datadog.json` (regenerate with `python3 test/fixtures/generate.py`), built from the
documented response shapes. It requires the `DD-API-KEY` / `DD-APPLICATION-KEY` headers, rejects unknown
query parameters and unknown POST body fields, validates time windows and page limits, **evaluates** the
search query (terms, quoting, `OR`, negation, `@facets`) so a wrong pushdown returns wrong rows, pages
with opaque cursors, answers the first request to every route with a 429 and Datadog's
`X-RateLimit-*` headers, and keeps a request log (`GET /__requests`) that `test/sql/pushdown.test`
asserts against.

```sh
make check                      # ruff, mypy, pydoclint, pytest, SQL over stdio/launch/http, vgi-lint
make test-sql TRANSPORT=stdio   # one transport
```

## Metadata and the quality score

`make lint-explain` runs [vgi-lint](https://github.com/Query-farm/vgi-lint-check) at `fail_on = "info"`.
Every table and function has a title, category, keywords, LLM- and human-facing docs, typed column
comments and examples; the two offline helpers carry executable examples. The worker is
credential-gated, so CI runs the linter with `execute: "false"`.

## Logging and debugging

Diagnostics go to stderr. `vgi-datadog-worker --debug` (or `make debug`) turns on debug logging,
including every retry and rate-limit wait. Keys are never logged.

## Deploying

`vgi-datadog-worker --http --port 8000` serves the worker over HTTP (see the `Dockerfile`); attach it with
`LOCATION 'http://host:8000'`. Each request carries the DuckDB secret, so one deployment serves many
Datadog organizations.

## Support

Open an issue at <https://github.com/Query-farm/vgi-datadog/issues>.

## Authorship & License

Copyright 2026 Query Farm LLC - https://query.farm

Released under the [BSD Zero Clause License](LICENSE) (0BSD). Datadog is a trademark of Datadog, Inc.;
this worker is an independent project that uses Datadog's public API.
