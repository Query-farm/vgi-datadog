"""Discovery metadata: catalog, schemas, categories and the shared tag helpers.

DuckDB, coding agents and the ``vgi-lint`` metadata checker all read these
tags. Every table and function carries ``object_tags(...)``: a title, a
category from its schema's category list, keywords, and LLM- and human-facing
docs. The per-object docs live next to each scan; the catalog- and
schema-level docs are here.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa

CATALOG_NAME = "datadog"
SOURCE_URL = "https://github.com/Query-farm/vgi-datadog"
API_DOCS = "https://docs.datadoghq.com/api/latest/"

MAIN_CATEGORIES = [
    {
        "name": "metrics",
        "title": "Metrics",
        "description": "Metric queries as long rows, metric names, metadata and tags.",
    },
    {"name": "logs", "title": "Logs", "description": "Log search and log analytics aggregates."},
    {"name": "apm", "title": "APM", "description": "Trace spans, span aggregates and the service catalog."},
    {"name": "events", "title": "Events", "description": "The event stream: deploys, alerts, integrations."},
    {"name": "audit", "title": "Audit trail", "description": "Who did what in the Datadog organization."},
    {"name": "helpers", "title": "Helpers", "description": "Offline helpers for search syntax and tag lists."},
]
RESOURCES_CATEGORIES = [
    {"name": "monitors", "title": "Monitors", "description": "Monitors, their per-group states and downtimes."},
    {"name": "slos", "title": "SLOs", "description": "Service level objectives and their history over a window."},
    {"name": "incidents", "title": "Incidents", "description": "Incident management records."},
    {"name": "dashboards", "title": "Dashboards", "description": "Dashboard listings and full definitions."},
    {"name": "synthetics", "title": "Synthetics", "description": "Synthetic tests and their recent results."},
    {"name": "infrastructure", "title": "Infrastructure", "description": "Hosts and host tags."},
    {"name": "organization", "title": "Organization", "description": "Users, teams and roles."},
    {"name": "usage", "title": "Usage metering", "description": "Monthly usage summary (needs usage_read)."},
    {"name": "services", "title": "Service catalog", "description": "Service definitions: ownership, tier, lifecycle."},
]
_ALL_CATEGORIES = {c["name"] for c in MAIN_CATEGORIES + RESOURCES_CATEGORIES}


def keywords_array(keywords: str) -> str:
    """Turn ``"a, b, c"`` into the JSON array string ``vgi.keywords`` requires."""
    return json.dumps([k.strip() for k in keywords.split(",") if k.strip()])


def object_tags(*, title: str, category: str, keywords: str, doc_llm: str, doc_md: str) -> dict[str, str]:
    """Build the standard per-object discovery tags."""
    if category not in _ALL_CATEGORIES:
        raise ValueError(f"unknown category {category!r}")
    return {
        "vgi.title": title,
        "vgi.category": category,
        "vgi.keywords": keywords_array(keywords),
        "vgi.doc_llm": doc_llm,
        "vgi.doc_md": doc_md,
    }


def column(name: str, type_: pa.DataType, comment: str, *, nullable: bool = True) -> pa.Field[Any]:
    """Build an Arrow field whose ``comment`` shows up in DuckDB's ``DESCRIBE`` and agent tools."""
    return pa.field(name, type_, nullable=nullable, metadata={b"comment": comment.encode()})


def sql_type(t: pa.DataType) -> str:
    """DuckDB type name for an Arrow type, as published in ``vgi.result_columns_schema``."""
    if getattr(t, "extension_name", None) == "arrow.json":
        return "JSON"
    if pa.types.is_timestamp(t):
        return "TIMESTAMPTZ" if t.tz else "TIMESTAMP"
    if pa.types.is_date(t):
        return "DATE"
    if pa.types.is_boolean(t):
        return "BOOLEAN"
    if pa.types.is_int64(t):
        return "BIGINT"
    if pa.types.is_int32(t):
        return "INTEGER"
    if pa.types.is_floating(t):
        return "DOUBLE"
    if pa.types.is_map(t):
        return f"MAP({sql_type(t.key_type)}, {sql_type(t.item_type)})"
    if pa.types.is_list(t):
        return f"{sql_type(t.value_type)}[]"
    if pa.types.is_struct(t):
        inner = ", ".join(f"{t.field(i).name} {sql_type(t.field(i).type)}" for i in range(t.num_fields))
        return f"STRUCT({inner})"
    return "VARCHAR"


def result_columns(schema: pa.Schema) -> str:
    """Build the ``vgi.result_columns_schema`` JSON for a table function's fixed output schema."""
    return json.dumps(
        [
            {"name": f.name, "type": sql_type(f.type), "description": (f.metadata or {})[b"comment"].decode()}
            for f in schema
        ]
    )


def function_tags(
    *,
    title: str,
    category: str,
    keywords: str,
    doc_llm: str,
    doc_md: str,
    schema: pa.Schema,
    examples: list[Any],
    domain: str = "observability",
) -> dict[str, str]:
    """Discovery tags for a table function: object tags, result columns and example queries."""
    return {
        **object_tags(title=title, category=category, keywords=keywords, doc_llm=doc_llm, doc_md=doc_md),
        "domain": domain,
        "vgi.result_columns_schema": result_columns(schema),
        "vgi.example_queries": json.dumps([{"description": e.description, "sql": e.sql} for e in examples]),
    }


def finish_function(fn: type[Any], *, title: str, category: str, keywords: str, doc_llm: str, doc_md: str) -> None:
    """Attach discovery tags and examples (from ``fn.EXAMPLES``) to a table function class."""
    examples = list(fn.EXAMPLES)
    fn.Meta.examples = examples
    fn.Meta.tags = function_tags(
        title=title,
        category=category,
        keywords=keywords,
        doc_llm=doc_llm,
        doc_md=doc_md,
        schema=fn.FIXED_SCHEMA,
        examples=examples,
    )
