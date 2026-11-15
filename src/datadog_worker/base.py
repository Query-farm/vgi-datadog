"""Shared machinery: every table and table function is a paged scan of one Datadog endpoint.

A concrete scan declares ``FIXED_SCHEMA`` and implements ``fetch``: given the
cursor of the previous page ('' before the first), it makes one request and
returns the page's rows (dicts keyed by column name) and the next cursor, or
None when the endpoint is exhausted.

The cursor is whatever the endpoint pages by (an opaque ``meta.page.after``
token, a page number or an offset) as text. It lives in ``PageState``, holds no
credentials, and lets a long scan resume on any worker process over the HTTP
transport. The client is rebuilt from the secret on every ``process`` call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Secret
from vgi.invocation import BindResponse
from vgi.table_function import (
    BindParams,
    OutputCollector,
    ProcessParams,
    TableFunctionGenerator,
    init_single_worker,
)
from vgi_rpc import ArrowSerializableDataclass

from .client import DATADOG_SECRET_TYPE, DatadogClient, DatadogConfig
from .convert import to_batch

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True, kw_only=True)
class NoArgs:
    """Catalog tables take no arguments."""


@dataclass(kw_only=True)
class PageState(ArrowSerializableDataclass):
    """Resumable cursor: the next page's cursor ('' before the first page), rows emitted, done flag."""

    cursor: str = ""
    emitted: int = 0
    done: bool = False


def client_from(params: ProcessParams[Any]) -> DatadogClient:
    """Build a client from the ``datadog`` secret DuckDB resolved for this call.

    Args:
        params: The process parameters carrying resolved secrets.

    Returns:
        A configured client.

    """
    secret = None
    for found in params.secrets.of_type(DATADOG_SECRET_TYPE):
        secret = dict(found)
        break
    return DatadogClient(DatadogConfig.from_secret(secret))


@init_single_worker
class PagedScan(TableFunctionGenerator[NoArgs, PageState]):
    """Base class for scans that page through one Datadog endpoint."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema([])

    class Meta:
        """Overridden by every concrete scan."""

        name = "paged_scan"
        description = "Base class for Datadog scans"

    @classmethod
    def on_bind(
        cls,
        params: BindParams[Any],
        *,
        datadog: Annotated[dict[str, pa.Scalar[Any]] | None, Secret(DATADOG_SECRET_TYPE)] = None,
    ) -> BindResponse:
        """Declare the fixed schema and ask DuckDB for the ``datadog`` secret."""
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def initial_state(cls, params: ProcessParams[Any]) -> PageState:
        """Start before the first page."""
        return PageState()

    @classmethod
    def fetch(
        cls, client: DatadogClient, params: ProcessParams[Any], cursor: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Read one page: return its rows and the next cursor (None when exhausted)."""
        raise NotImplementedError

    @classmethod
    def max_rows(cls, params: ProcessParams[Any]) -> int | None:
        """Row cap from function arguments (None: read everything)."""
        return None

    @classmethod
    def process(cls, params: ProcessParams[Any], state: PageState, out: OutputCollector) -> None:
        """Fetch one page, emit it, and advance the cursor."""
        cap = cls.max_rows(params)
        if state.done or (cap is not None and state.emitted >= cap):
            out.finish()
            return
        client = client_from(params)
        rows: list[dict[str, Any]] = []
        # An empty page with a next cursor (Datadog does this on sparse log scans) is skipped
        # within the same call, so every call either emits rows or finishes.
        while not rows and not state.done:
            rows, nxt = cls.fetch(client, params, state.cursor)
            if nxt is None or nxt == state.cursor:
                state.done = True
            else:
                state.cursor = nxt
        if cap is not None:
            rows = rows[: cap - state.emitted]
        state.emitted += len(rows)
        if rows:
            out.emit(to_batch(rows, params.output_schema))
        elif state.done:
            out.finish()
