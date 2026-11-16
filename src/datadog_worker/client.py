"""HTTP client for the Datadog API (v1 and v2), read-only.

Everything that touches the network lives here.

* **Configuration** comes from a DuckDB secret of type ``datadog`` (see
  ``DATADOG_SECRET``): an API key, an application key and the ``site``
  (``datadoghq.com`` by default; ``datadoghq.eu``, ``us3.datadoghq.com``,
  ``us5.datadoghq.com``, ``ap1.datadoghq.com``, ``ap2.datadoghq.com`` or
  ``ddog-gov.com``). ``base_url`` overrides the API origin for a proxy or a
  local fixture server. Nothing is read from the environment.
* **Authentication** sends ``DD-API-KEY`` and ``DD-APPLICATION-KEY`` headers.
* **Rate limits.** Datadog limits each endpoint separately and reports it in
  ``X-RateLimit-Limit`` / ``-Period`` / ``-Remaining`` / ``-Reset`` / ``-Name``.
  A 429 is retried after ``X-RateLimit-Reset`` seconds (or ``Retry-After``);
  a successful answer that reports ``Remaining: 0`` makes the next request to
  the same endpoint wait for the reset instead of spending a 429. 5xx answers
  and connection failures are retried with exponential backoff.

Keys are never written to table-function state or logs: every ``process``
call rebuilds the client from the secret the SDK hands it.
"""

from __future__ import annotations

import email.utils
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import pyarrow as pa
from vgi.catalog import SecretTypeSpec

log = logging.getLogger(__name__)

DATADOG_SECRET_TYPE = "datadog"
DEFAULT_SITE = "datadoghq.com"
USER_AGENT = "vgi-datadog-worker/0.1"
MAX_ATTEMPTS = 6
MAX_BACKOFF_SECONDS = 60.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
DOC_URL = "https://github.com/Query-farm/vgi-datadog#credentials"

# site -> (API origin, web app origin). Short aliases are accepted too.
SITES: dict[str, tuple[str, str]] = {
    "datadoghq.com": ("https://api.datadoghq.com", "https://app.datadoghq.com"),
    "us3.datadoghq.com": ("https://api.us3.datadoghq.com", "https://us3.datadoghq.com"),
    "us5.datadoghq.com": ("https://api.us5.datadoghq.com", "https://us5.datadoghq.com"),
    "datadoghq.eu": ("https://api.datadoghq.eu", "https://app.datadoghq.eu"),
    "ap1.datadoghq.com": ("https://api.ap1.datadoghq.com", "https://ap1.datadoghq.com"),
    "ap2.datadoghq.com": ("https://api.ap2.datadoghq.com", "https://ap2.datadoghq.com"),
    "ddog-gov.com": ("https://api.ddog-gov.com", "https://app.ddog-gov.com"),
}
SITE_ALIASES = {
    "us1": "datadoghq.com",
    "us": "datadoghq.com",
    "app.datadoghq.com": "datadoghq.com",
    "us3": "us3.datadoghq.com",
    "us5": "us5.datadoghq.com",
    "eu": "datadoghq.eu",
    "eu1": "datadoghq.eu",
    "app.datadoghq.eu": "datadoghq.eu",
    "ap1": "ap1.datadoghq.com",
    "ap2": "ap2.datadoghq.com",
    "gov": "ddog-gov.com",
    "us1-fed": "ddog-gov.com",
    "app.ddog-gov.com": "ddog-gov.com",
}


def _field(
    name: str, doc: str, type_: pa.DataType | None = None, *, redact: bool = False, required: bool = False
) -> Any:
    meta = {"vgi_doc": doc}
    if redact:
        meta["redact"] = "true"
    if required:
        meta["vgi_required"] = "true"
    return pa.field(name, type_ or pa.string(), metadata=meta)


DATADOG_SECRET = SecretTypeSpec(
    name=DATADOG_SECRET_TYPE,
    description=(
        "Connection to one Datadog organization: an API key, an application key with read scopes, and the "
        "site the organization lives on (datadoghq.com by default)."
    ),
    schema=pa.schema(
        [
            _field("api_key", "Datadog API key (Organization settings, API keys).", redact=True, required=True),
            _field(
                "app_key",
                "Datadog application key (Organization settings, Application keys). Scope it to read-only "
                "permissions such as metrics_read, logs_read_data, apm_read, monitors_read, incident_read.",
                redact=True,
                required=True,
            ),
            _field(
                "site",
                "Datadog site: 'datadoghq.com' (US1, default), 'us3.datadoghq.com', 'us5.datadoghq.com', "
                "'datadoghq.eu', 'ap1.datadoghq.com', 'ap2.datadoghq.com' or 'ddog-gov.com' "
                "(aliases us1, us3, us5, eu, ap1, ap2, gov).",
            ),
            _field(
                "base_url",
                "Override the API origin, e.g. a proxy or a local fixture server 'http://127.0.0.1:8126'. "
                "Default: https://api.<site>.",
            ),
            _field("timeout", "Per-request timeout in seconds (default 60).", pa.float64()),
            _field(
                "page_size",
                "Rows requested per page from search endpoints (logs, spans, events, audit), 1 to 1000 "
                "(default 1000). List endpoints cap it at their own maximum.",
                pa.int64(),
            ),
        ],
        metadata={"vgi_doc_url": DOC_URL},
    ),
)


class Headers(Protocol):
    """Anything with a case-insensitive ``get`` for HTTP headers (``Message``, ``dict``)."""

    def get(self, name: str, default: None = None, /) -> str | None:
        """Return a header value, or None."""
        ...


class DatadogError(RuntimeError):
    """The Datadog API returned an error, or the secret is unusable."""


def resolve_site(site: str | None) -> str:
    """Normalise a site name or alias to its canonical form.

    Args:
        site: Site such as 'datadoghq.eu', 'us3' or None for the default.

    Returns:
        The canonical site, a key of ``SITES``.

    Raises:
        DatadogError: An unknown site.

    """
    raw = (site or DEFAULT_SITE).strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
    raw = raw.removeprefix("api.") if raw.startswith("api.") else raw
    canonical = SITE_ALIASES.get(raw, raw)
    if canonical not in SITES:
        known = ", ".join(SITES)
        raise DatadogError(f"datadog secret: unknown SITE {site!r}; use one of {known}")
    return canonical


@dataclass(frozen=True, slots=True)
class DatadogConfig:
    """Connection settings parsed from a ``datadog`` secret."""

    api_url: str
    app_url: str
    site: str
    api_key: str = field(repr=False)
    app_key: str = field(repr=False)
    timeout: float = 60.0
    page_size: int = 1000

    @classmethod
    def from_secret(cls, secret: Mapping[str, Any] | None) -> DatadogConfig:
        """Build and validate a config from a resolved secret's key/value pairs.

        Args:
            secret: Secret fields; values may be ``pa.Scalar`` or plain Python.

        Returns:
            The validated configuration.

        Raises:
            DatadogError: No secret, or an invalid field value.

        """
        if not secret:
            raise DatadogError(
                "no Datadog credentials: run CREATE SECRET (TYPE datadog, API_KEY '...', APP_KEY '...') after "
                f"ATTACH (add SITE 'datadoghq.eu' or another site outside US1). See {DOC_URL}"
            )

        def get(name: str) -> Any:
            value = secret.get(name)
            if isinstance(value, pa.Scalar):
                value = value.as_py()
            return None if value == "" else value

        api_key, app_key = get("api_key"), get("app_key")
        if not api_key:
            raise DatadogError("datadog secret: API_KEY must be set")
        if not app_key:
            raise DatadogError("datadog secret: APP_KEY must be set (an application key; read endpoints need it)")
        site = resolve_site(get("site"))
        api_url, app_url = SITES[site]
        base = get("base_url")
        if base:
            base = str(base).rstrip("/")
            if not base.startswith(("http://", "https://")):
                raise DatadogError("datadog secret: BASE_URL must be an http:// or https:// URL")
            api_url = base
        page_size = int(get("page_size") or 1000)
        if not 1 <= page_size <= 1000:
            raise DatadogError("datadog secret: PAGE_SIZE must be between 1 and 1000")
        timeout = float(get("timeout") or 60.0)
        if timeout <= 0:
            raise DatadogError("datadog secret: TIMEOUT must be positive")
        return cls(
            api_url=api_url,
            app_url=app_url,
            site=site,
            api_key=str(api_key),
            app_key=str(app_key),
            timeout=timeout,
            page_size=page_size,
        )


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


def retry_delay(headers: Headers | Mapping[str, str] | None, attempt: int) -> float:
    """Seconds to wait before retry ``attempt`` of a 429 / 5xx answer.

    ``X-RateLimit-Reset`` (seconds until the window resets) wins, then
    ``Retry-After`` (seconds or an HTTP date), then exponential backoff.

    Args:
        headers: Response headers (case-insensitive mapping), if any.
        attempt: 1-based attempt number.

    Returns:
        Delay in seconds, capped at ``MAX_BACKOFF_SECONDS``.

    """
    get = headers.get if headers is not None else (lambda _k: None)
    reset = _number(get("X-RateLimit-Reset"))
    if reset is not None and reset >= 0:
        return min(reset, MAX_BACKOFF_SECONDS)
    after = get("Retry-After")
    if after:
        seconds = _number(after)
        if seconds is not None:
            return min(max(seconds, 0.0), MAX_BACKOFF_SECONDS)
        try:
            when = email.utils.parsedate_to_datetime(after)
            return max(0.0, min(when.timestamp() - time.time(), MAX_BACKOFF_SECONDS))
        except TypeError, ValueError:
            pass
    return min(0.5 * float(2**attempt), MAX_BACKOFF_SECONDS)


def _error_detail(body: str) -> str:
    try:
        detail = json.loads(body)
    except ValueError:
        return body.strip()[:500]
    if isinstance(detail, dict):
        errors = detail.get("errors")
        if isinstance(errors, list) and errors:
            parts = []
            for e in errors:
                if isinstance(e, dict):
                    parts.append(str(e.get("detail") or e.get("title") or e))
                else:
                    parts.append(str(e))
            return "; ".join(parts)
        return str(detail.get("error") or detail.get("message") or body)[:500]
    return body[:500]


# Endpoints that answered with X-RateLimit-Remaining: 0, keyed by (origin, method, path);
# value = monotonic time at which the window resets. Shared by every client in the process.
_GATES: dict[tuple[str, str, str], float] = {}
_GATES_LOCK = threading.Lock()


class DatadogClient:
    """Authenticated JSON requests against one Datadog API origin."""

    def __init__(self, config: DatadogConfig) -> None:
        """Bind to a configuration."""
        self.config = config

    def url(self, path: str, query: Sequence[tuple[str, Any]] | Mapping[str, Any] | None = None) -> str:
        """Absolute URL on the configured origin.

        Args:
            path: Path starting with ``/``.
            query: Query parameters; ``None`` values are dropped, booleans become true/false.

        Returns:
            The URL.

        """
        items = list(query.items()) if isinstance(query, Mapping) else list(query or [])
        clean = [(k, ("true" if v else "false") if isinstance(v, bool) else v) for k, v in items if v is not None]
        qs = urllib.parse.urlencode(clean, doseq=True)
        return f"{self.config.api_url}{path}" + (f"?{qs}" if qs else "")

    def get(self, path: str, query: Sequence[tuple[str, Any]] | Mapping[str, Any] | None = None) -> Any:
        """GET a path and decode its JSON body.

        Args:
            path: API path, e.g. ``/api/v1/monitor``.
            query: Query parameters.

        Returns:
            The decoded JSON response.

        """
        return self._send("GET", path, self.url(path, query))

    def post(self, path: str, body: Any) -> Any:
        """POST a JSON body and decode the JSON response.

        Args:
            path: API path, e.g. ``/api/v2/logs/events/search``.
            body: JSON-serialisable request body.

        Returns:
            The decoded JSON response.

        """
        return self._send("POST", path, self.url(path), body)

    def _wait_for_gate(self, key: tuple[str, str, str]) -> None:
        with _GATES_LOCK:
            until = _GATES.get(key)
        if until is None:
            return
        delay = until - time.monotonic()
        if delay > 0:
            log.info("datadog: %s %s rate limit exhausted; waiting %.1fs for the reset", key[1], key[2], delay)
            time.sleep(min(delay, MAX_BACKOFF_SECONDS))
        with _GATES_LOCK:
            _GATES.pop(key, None)

    @staticmethod
    def _record_gate(key: tuple[str, str, str], headers: Headers | Mapping[str, str]) -> None:
        remaining = _number(headers.get("X-RateLimit-Remaining"))
        reset = _number(headers.get("X-RateLimit-Reset"))
        if remaining is not None and remaining <= 0 and reset is not None and reset > 0:
            with _GATES_LOCK:
                _GATES[key] = time.monotonic() + min(reset, MAX_BACKOFF_SECONDS)

    def _send(self, method: str, path: str, url: str, body: Any = None) -> Any:
        """Send one request with rate-limit handling and retries; return the decoded JSON body."""
        data = None if body is None else json.dumps(body).encode()
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "DD-API-KEY": self.config.api_key,
            "DD-APPLICATION-KEY": self.config.app_key,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        key = (self.config.api_url, method, path)
        shown = url.split("?", 1)[0]
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait_for_gate(key)
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout) as resp:
                    self._record_gate(key, resp.headers)
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as err:
                if err.code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                    delay = retry_delay(err.headers, attempt)
                    log.info("datadog: HTTP %s from %s %s; retrying in %.1fs", err.code, method, shown, delay)
                    time.sleep(delay)
                    continue
                detail = _error_detail(err.read(4000).decode(errors="replace"))
                hint = ""
                if err.code in (401, 403):
                    hint = (
                        " (check the datadog secret: API_KEY, APP_KEY and its scopes, and that SITE matches the "
                        f"organization's site, currently {self.config.site})"
                    )
                elif err.code == 400 and "No valid indexes specified" in detail:
                    # Seen on a live trial org (2026-11-15): logs and events search both answer this
                    # until the organization has at least one index (i.e. before any data was sent).
                    hint = (
                        " (this organization has no log or event index yet: send some logs/events first, or create "
                        "an index under Logs > Configuration > Indexes)"
                    )
                raise DatadogError(f"{method} {shown} failed with HTTP {err.code}{hint}: {detail}") from err
            except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
                if attempt == MAX_ATTEMPTS:
                    reason = getattr(err, "reason", err)
                    raise DatadogError(f"{method} {shown} failed: {reason}") from err
                time.sleep(min(0.5 * 2**attempt, MAX_BACKOFF_SECONDS))
        raise DatadogError(f"{method} {shown}: gave up after {MAX_ATTEMPTS} attempts")
