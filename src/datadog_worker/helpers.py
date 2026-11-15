"""Offline helper scalars: build safe Datadog search terms and turn tag lists into maps.

Neither calls Datadog. ``search_term`` uses the same quoting as the pushdown
(``search.quote``), so a value can never change a query's structure;
``tag_map`` uses the same splitting as every ``tags`` column.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import pyarrow as pa
from vgi.arguments import Param, Returns
from vgi.metadata import FunctionExample
from vgi.scalar_function import ScalarFunction

from .convert import TAGS, tag_map
from .meta import object_tags
from .search import quote

Q = "datadog.main"


class SearchTerm(ScalarFunction):
    """``search_term(attribute, value)``: one quoted Datadog search term."""

    class Meta:
        """Function metadata."""

        name = "search_term"
        description = 'Build a Datadog search term attribute:"value" with the value quoted and escaped'
        examples = [
            FunctionExample(
                sql=f"SELECT {Q}.search_term('service', 'checkout \"v2\"') AS term",
                description="A service filter whose value contains quotes",
            ),
            FunctionExample(
                sql=(
                    f"SELECT string_agg({Q}.search_term('host', h), ' OR ' ORDER BY h) AS hosts_query "
                    "FROM (VALUES ('web-1'), ('web-2')) t(h)"
                ),
                description="An OR query over a list of hosts, ready to paste into logs()",
            ),
        ]
        tags = {
            **object_tags(
                title="Search Term Builder",
                category="helpers",
                keywords="search syntax, quote, escape, log query, build query, search term",
                doc_llm=(
                    'Return the Datadog search term attribute:"value" for each row, with backslashes and double '
                    "quotes in the value escaped, so arbitrary text (user input, values read from another table) "
                    "can be put into the query argument of logs, spans, events or audit_events without changing "
                    "the query's structure. attribute is used as written: a reserved attribute (service, host, "
                    "status, env, source), a tag key, or a facet such as @http.url. NULL in, NULL out. Runs "
                    "offline; no Datadog call."
                ),
                doc_md=(
                    "## search_term(attribute, value)\n\n"
                    "A quoted, escaped search term: `search_term('service', 'web')` returns `service:\"web\"`."
                ),
            ),
            "domain": "observability",
            "vgi.executable_examples": json.dumps(
                [
                    {
                        "description": "Quote and escape a value",
                        "sql": f"SELECT {Q}.search_term('service', 'checkout \"v2\"') AS term",
                        "expected_result": [{"term": 'service:"checkout \\"v2\\""'}],
                    },
                    {
                        "description": "OR several values into one query",
                        "sql": (
                            f"SELECT string_agg({Q}.search_term('host', h), ' OR ' ORDER BY h) AS q "
                            "FROM (VALUES ('web-1'), ('web-2')) t(h)"
                        ),
                        "expected_result": [{"q": 'host:"web-1" OR host:"web-2"'}],
                    },
                ]
            ),
        }

    @classmethod
    def compute(
        cls,
        attribute: Annotated[pa.StringArray, Param(doc="Attribute, tag key or @facet to match, e.g. 'service'.")],
        value: Annotated[pa.StringArray, Param(doc="The exact value to match; quoted and escaped.")],
    ) -> Annotated[pa.StringArray, Returns()]:
        """Return ``attribute:"value"`` per row."""
        out = [
            None if a is None or v is None else f"{a}:{quote(v)}"
            for a, v in zip(attribute.to_pylist(), value.to_pylist(), strict=True)
        ]
        return pa.array(out, type=pa.string())


class TagMap(ScalarFunction):
    """``tag_map(tags)``: a Datadog tag list as a MAP."""

    class Meta:
        """Function metadata."""

        name = "tag_map"
        description = "Turn a list of Datadog key:value tags into a MAP(VARCHAR, VARCHAR) keyed by tag key"
        examples = [
            FunctionExample(
                sql=f"SELECT {Q}.tag_map(['env:prod', 'team:core', 'legacy'])['team'] AS team",
                description="Read one key from a tag list",
            ),
        ]
        tags = {
            **object_tags(
                title="Tag List to Map",
                category="helpers",
                keywords="tags, tag list, key value, map, split tags",
                doc_llm=(
                    "Convert a list of Datadog tags (key:value strings) into a `MAP(VARCHAR, VARCHAR)` keyed by "
                    "tag key, split at the first colon, exactly as the worker builds every tags column: a bare tag "
                    "maps to '' and a key that repeats keeps all its values joined with ','. Use it on tag lists "
                    "the worker returns as lists (dashboard tags, downtimes.monitor_tags) or on your own data. "
                    "NULL in, NULL out. Runs offline."
                ),
                doc_md=("## tag_map(tags)\n\n`tag_map(['env:prod', 'team:core'])['team']` returns `core`."),
            ),
            "domain": "observability",
            "vgi.executable_examples": json.dumps(
                [
                    {
                        "description": "Split a tag list into a map and read keys",
                        "sql": (
                            f"SELECT m['env'] AS env, m['team'] AS team, m['legacy'] AS legacy FROM "
                            f"(SELECT {Q}.tag_map(['env:prod', 'team:core', 'team:web', 'legacy']) AS m)"
                        ),
                        "expected_result": [{"env": "prod", "team": "core,web", "legacy": ""}],
                    }
                ]
            ),
        }

    @classmethod
    def compute(
        cls,
        tags: Annotated[
            pa.Array[Any],
            Param(
                pa.list_(pa.string()), "Datadog tags, each a key:value string (a bare tag without a colon is allowed)."
            ),
        ],
    ) -> Annotated[pa.Array[Any], Returns(TAGS)]:
        """Return one MAP per row."""
        return pa.array([tag_map(t) for t in tags.to_pylist()], type=TAGS)
