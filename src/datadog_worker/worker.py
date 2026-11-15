"""Assemble the ``datadog`` catalog and run the worker.

Run it:

    vgi-datadog-worker                      # stdio: what DuckDB launches by default
    vgi-datadog-worker --http --port 8000   # HTTP: for Docker / hosted deploys

Attach it from DuckDB, then give it credentials:

    ATTACH 'datadog' (TYPE vgi, LOCATION '/abs/path/to/.venv/bin/vgi-datadog-worker');
    CREATE SECRET (TYPE datadog, API_KEY '...', APP_KEY '...', SITE 'datadoghq.eu');
"""

from __future__ import annotations

import json
from typing import Any

from vgi import Worker
from vgi.catalog import Catalog, Schema, Table
from vgi.catalog.constraints import constraint_record

from .client import DATADOG_SECRET
from .docs import (
    CATALOG_DESCRIPTION,
    CATALOG_TAGS,
    FUNCTION_DOCS,
    MAIN_DOC_LLM,
    MAIN_DOC_MD,
    RESOURCES_DOC_LLM,
    RESOURCES_DOC_MD,
    TABLE_DOCS,
)
from .helpers import SearchTerm, TagMap
from .meta import (
    CATALOG_NAME,
    MAIN_CATEGORIES,
    RESOURCES_CATEGORIES,
    SOURCE_URL,
    finish_function,
    keywords_array,
    object_tags,
)
from .resources import (
    Dashboard,
    Dashboards,
    Downtimes,
    Hosts,
    HostTags,
    Incidents,
    MonitorGroups,
    Monitors,
    Roles,
    Services,
    SloHistory,
    Slos,
    SyntheticsResults,
    SyntheticsTests,
    Teams,
    UsageSummary,
    Users,
)
from .telemetry import (
    AuditEvents,
    Events,
    Logs,
    LogsAggregate,
    MetricMetadata,
    Metrics,
    MetricsQuery,
    MetricTags,
    Spans,
    SpansAggregate,
)


def _table(name: str, scan: type[Any], schema: str, key: list[str]) -> Table:
    """Build a catalog table over ``scan`` with NOT NULL and primary key records."""
    doc = TABLE_DOCS[name]
    path = [schema]
    constraints = [
        constraint_record(
            path,
            name,
            f"{name}_{f.name}_not_null",
            "NOT_NULL",
            [[f.name]],
            enforcement="ENFORCED",
            validation="ALL_ROWS",
        )
        for f in scan.FIXED_SCHEMA
        if not f.nullable
    ]
    constraints.append(
        constraint_record(
            path, name, f"{name}_pk", "PRIMARY_KEY", [[c] for c in key], enforcement="ENFORCED", validation="ALL_ROWS"
        )
    )
    return Table(
        name=name,
        function=scan,
        comment=doc["comment"],
        constraints=tuple(constraints),
        column_comments={f.name: (f.metadata or {})[b"comment"].decode() for f in scan.FIXED_SCHEMA},
        tags={
            **object_tags(
                title=doc["title"],
                category=doc["category"],
                keywords=doc["keywords"],
                doc_llm=doc["doc_llm"],
                doc_md=doc["doc_md"],
            ),
            "domain": "observability",
            "vgi.example_queries": json.dumps([{"description": d, "sql": s} for d, s in scan.EXAMPLES]),
        },
    )


MAIN_TABLES = [_table("metrics", Metrics, "main", ["metric"])]
MAIN_FUNCTIONS: list[type[Any]] = [
    MetricsQuery,
    MetricMetadata,
    MetricTags,
    Logs,
    LogsAggregate,
    Spans,
    SpansAggregate,
    Events,
    AuditEvents,
]
RESOURCE_TABLES = [
    _table("monitors", Monitors, "resources", ["id"]),
    _table("monitor_groups", MonitorGroups, "resources", ["monitor_id", "group_name"]),
    _table("downtimes", Downtimes, "resources", ["id"]),
    _table("slos", Slos, "resources", ["id"]),
    _table("incidents", Incidents, "resources", ["incident_id"]),
    _table("dashboards", Dashboards, "resources", ["id"]),
    _table("synthetics_tests", SyntheticsTests, "resources", ["public_id"]),
    _table("hosts", Hosts, "resources", ["host_name"]),
    _table("host_tags", HostTags, "resources", ["host_name", "tag"]),
    _table("users", Users, "resources", ["id"]),
    _table("teams", Teams, "resources", ["id"]),
    _table("roles", Roles, "resources", ["id"]),
    _table("services", Services, "resources", ["service"]),
]
RESOURCE_FUNCTIONS: list[type[Any]] = [SloHistory, Dashboard, SyntheticsResults, UsageSummary]

for _fn in MAIN_FUNCTIONS + RESOURCE_FUNCTIONS:
    finish_function(_fn, **FUNCTION_DOCS[_fn.Meta.name])
    _fn.EXAMPLES = _fn.Meta.examples

HELPERS: list[type[Any]] = [SearchTerm, TagMap]
for _fn in HELPERS:
    _fn.EXAMPLES = _fn.Meta.examples
    _fn.Meta.tags = {
        **_fn.Meta.tags,
        "vgi.example_queries": json.dumps([{"description": e.description, "sql": e.sql} for e in _fn.Meta.examples]),
    }
MAIN_FUNCTIONS += HELPERS


def _schema_examples(tables: list[Table], functions: list[type[Any]]) -> str:
    out = [json.loads(t.tags["vgi.example_queries"])[0] for t in tables]
    out += [{"description": fn.EXAMPLES[0].description, "sql": fn.EXAMPLES[0].sql} for fn in functions]
    return json.dumps(out)


class DatadogWorker(Worker):
    """The worker process: the ``datadog`` catalog with ``main`` and ``resources`` schemas."""

    catalog = Catalog(
        name=CATALOG_NAME,
        comment=CATALOG_DESCRIPTION,
        source_url=SOURCE_URL,
        tags=CATALOG_TAGS,
        schemas=[
            Schema(
                path=["main"],
                comment="Datadog telemetry: metric queries as long rows, logs, APM spans, events and the audit trail.",
                tables=MAIN_TABLES,
                functions=MAIN_FUNCTIONS,
                tags={
                    "vgi.title": "Datadog Telemetry",
                    "domain": "observability",
                    "vgi.keywords": keywords_array(
                        "metrics, logs, spans, traces, apm, events, audit trail, log search"
                    ),
                    "vgi.doc_llm": MAIN_DOC_LLM,
                    "vgi.doc_md": MAIN_DOC_MD,
                    "vgi.categories": json.dumps(MAIN_CATEGORIES),
                    "vgi.example_queries": _schema_examples(MAIN_TABLES, MAIN_FUNCTIONS),
                },
            ),
            Schema(
                path=["resources"],
                comment="Datadog configuration and "
                "records: monitors, SLOs, "
                "incidents, dashboards, "
                "synthetics, hosts, users.",
                tables=RESOURCE_TABLES,
                functions=RESOURCE_FUNCTIONS,
                tags={
                    "vgi.title": "Datadog Resources",
                    "domain": "observability",
                    "vgi.keywords": keywords_array(
                        "monitors, slos, incidents, dashboards, synthetics, hosts, users, teams, service catalog"
                    ),
                    "vgi.doc_llm": RESOURCES_DOC_LLM,
                    "vgi.doc_md": RESOURCES_DOC_MD,
                    "vgi.categories": json.dumps(RESOURCES_CATEGORIES),
                    "vgi.example_queries": _schema_examples(RESOURCE_TABLES, RESOURCE_FUNCTIONS),
                },
            ),
        ],
    )

    secret_types = [DATADOG_SECRET]


def main() -> None:
    """Console-script entry point: parse ``--http`` / ``--unix`` / logging flags and serve."""
    DatadogWorker.main()


if __name__ == "__main__":
    main()
