# Changelog

## 0.1.2 (2026-11-16)

- Bump vgi-python to 0.52.2.

## 0.1.1

- Fix: every query whose filter is pushed down to the worker (a `WHERE` clause, or
  `ORDER BY … LIMIT`) failed with "No DuckDB-compatible engine is installed". The SDK binds
  and evaluates pushed predicates with a local engine, which was only in the dev
  dependency group; the runtime dependency is now `vgi-python[http,haybarn]`.

## 0.1.0

Initial release: Datadog (all sites) as SQL, read-only. Metric queries as long rows, log / span /
event / audit search with SQL predicates pushed into Datadog search syntax, log and span analytics,
monitors and per-group states, downtimes, SLOs and SLO history, incidents, dashboards and definitions,
synthetic tests and results, hosts and host tags, users, teams, roles, service catalog, usage metering.
Generated from vgi-starter-python v1.3.0.

Targets: vgi-python 0.51 · VGI protocol 3.3 · Haybarn 2.0.0rc11 · vgi-lint-check 2.x (unpinned)
