"""Unit tests for search translation, value conversion and the client. SQL tests live in test/sql."""

from __future__ import annotations

import datetime as dt
import time

import pyarrow as pa
import pytest
from vgi.table_filter_pushdown import (
    AndFilter,
    ComparisonOp,
    ConstantFilter,
    InFilter,
    IsNotNullFilter,
    OrFilter,
    PushdownFilters,
)

from datadog_worker import client as client_mod
from datadog_worker.client import DatadogClient, DatadogConfig, DatadogError, resolve_site, retry_delay
from datadog_worker.convert import TAGS, TSTZ, convert, parse_ts, tag_map, tag_value
from datadog_worker.search import combine, iso_ms, quote, search_terms, time_window

FIELDS = {"service": "service", "status": "status", "user_email": "@usr.email"}
UTC = dt.UTC


def _const(col: str, op: ComparisonOp, value: object, type_: pa.DataType | None = None) -> ConstantFilter:
    return ConstantFilter(column_name=col, column_index=0, op=op, value=pa.scalar(value, type=type_))


def _filters(*fs: object) -> PushdownFilters:
    return PushdownFilters(filters=tuple(fs))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------- search syntax


def test_quote_escapes_quotes_and_backslashes() -> None:
    assert quote("web") == '"web"'
    assert quote('a"b') == '"a\\"b"'
    assert quote("a\\b") == '"a\\\\b"'
    assert quote('x" OR service:"y') == '"x\\" OR service:\\"y"'


def test_terms_for_eq_ne_in_or_and_not_null() -> None:
    eq = _const("service", ComparisonOp.EQ, "web")
    ne = _const("status", ComparisonOp.NE, "info")
    in_ = InFilter(column_name="user_email", column_index=0, values=pa.array(["a@x", "b@x"]))
    same_or = OrFilter(
        column_name="service",
        column_index=0,
        children=(_const("service", ComparisonOp.EQ, "a"), _const("service", ComparisonOp.EQ, "b")),
    )
    nn = IsNotNullFilter(column_name="status", column_index=0)
    terms = search_terms(_filters(eq, ne, in_, same_or, nn), FIELDS)
    assert terms == [
        'service:"web"',
        '-status:"info"',
        '@usr.email:("a@x" OR "b@x")',
        'service:("a" OR "b")',
        "status:*",
    ]


def test_mixed_column_or_and_unknown_columns_are_not_pushed() -> None:
    mixed = OrFilter(
        column_name="service",
        column_index=0,
        children=(_const("service", ComparisonOp.EQ, "a"), _const("status", ComparisonOp.EQ, "error")),
    )
    other = _const("message", ComparisonOp.EQ, "x")
    rng = _const("service", ComparisonOp.GT, "a")
    assert search_terms(_filters(mixed, other, rng), FIELDS) == []


def test_nested_and_is_flattened() -> None:
    nested = AndFilter(
        column_name="service",
        column_index=0,
        children=(_const("service", ComparisonOp.EQ, "web"), _const("status", ComparisonOp.EQ, "error")),
    )
    assert search_terms(_filters(nested), FIELDS) == ['service:"web"', 'status:"error"']


def test_combine_parenthesises_user_query() -> None:
    assert combine("*", []) == "*"
    assert combine("", ['a:"b"']) == 'a:"b"'
    assert combine("service:web OR service:api", ['status:"error"']) == '(service:web OR service:api) status:"error"'
    assert combine("service:web", []) == "service:web"


def test_time_window_tightest_bounds() -> None:
    a = dt.datetime(2026, 11, 1, 1, tzinfo=UTC)
    b = dt.datetime(2026, 11, 1, 2, tzinfo=UTC)
    w = time_window(
        _filters(
            _const("ts", ComparisonOp.GE, a, TSTZ),
            _const("ts", ComparisonOp.LT, b, TSTZ),
            _const("ts", ComparisonOp.GT, dt.datetime(2026, 11, 1, 0, tzinfo=UTC), TSTZ),
        ),
        "ts",
    )
    assert (w.lo, w.hi, w.empty) == (a, b, False)
    empty = time_window(_filters(_const("ts", ComparisonOp.GE, b, TSTZ), _const("ts", ComparisonOp.LE, a, TSTZ)), "ts")
    assert empty.empty


def test_iso_ms_rounds_down_and_up() -> None:
    t = dt.datetime(2026, 11, 1, 0, 0, 0, 999_500, tzinfo=UTC)
    assert iso_ms(t) == "2026-11-01T00:00:00.999Z"
    assert iso_ms(t, ceil=True) == "2026-11-01T00:00:01.000Z"
    est = dt.datetime(2026, 11, 1, 5, tzinfo=dt.timezone(dt.timedelta(hours=5)))
    assert iso_ms(est) == "2026-11-01T00:00:00.000Z"


# ---------------------------------------------------------------------------- conversion


def test_parse_ts_epoch_units_and_iso() -> None:
    want = dt.datetime(2026, 11, 1, tzinfo=UTC)
    for raw in (1_793_491_200, 1_793_491_200_000, 1_793_491_200_000_000, 1_793_491_200_000_000_000, "1793491200"):
        assert parse_ts(raw) == want
    assert parse_ts("2026-11-01T00:00:00Z") == want
    assert parse_ts("2026-11-01T00:00:00.000000+00:00") == want
    assert parse_ts("nope") is None
    assert parse_ts(None) is None
    assert parse_ts(True) is None


def test_tag_map_splits_bare_and_repeated_keys() -> None:
    assert tag_map(["env:prod", "team:a", "legacy", "team:b", "url:http://x", "team:a"]) == [
        ("env", "prod"),
        ("team", "a,b"),
        ("legacy", ""),
        ("url", "http://x"),
    ]
    assert tag_map(None) is None
    assert tag_value(["source:python", "env:prod"], "env") == "prod"
    assert tag_value([], "env") is None


def test_convert_types() -> None:
    assert convert("3", pa.int64()) == 3
    assert convert("x", pa.int64()) is None
    assert convert("1.5", pa.float64()) == 1.5
    assert convert({"a": 1}, pa.json_()) == '{"a":1}'
    assert convert({"a": 1}, TAGS) == [("a", "1")]
    assert convert(["1", None], pa.list_(pa.int64())) == [1, None]
    assert convert(True, pa.string()) == "true"
    assert convert("true", pa.bool_()) is True


# ---------------------------------------------------------------------------- client


def test_sites_and_aliases() -> None:
    assert resolve_site(None) == "datadoghq.com"
    assert resolve_site("EU") == "datadoghq.eu"
    assert resolve_site("https://api.us5.datadoghq.com/") == "us5.datadoghq.com"
    assert resolve_site("gov") == "ddog-gov.com"
    with pytest.raises(DatadogError, match="unknown SITE"):
        resolve_site("datadoghq.org")


def test_config_from_secret() -> None:
    cfg = DatadogConfig.from_secret({"api_key": pa.scalar("k"), "app_key": "a", "site": "us3"})
    assert cfg.api_url == "https://api.us3.datadoghq.com"
    assert cfg.app_url == "https://us3.datadoghq.com"
    assert "k" not in repr(cfg) and "'a'" not in repr(cfg)
    cfg = DatadogConfig.from_secret({"api_key": "k", "app_key": "a", "base_url": "http://127.0.0.1:9/", "page_size": 5})
    assert (cfg.api_url, cfg.page_size) == ("http://127.0.0.1:9", 5)
    with pytest.raises(DatadogError, match="CREATE SECRET"):
        DatadogConfig.from_secret(None)
    with pytest.raises(DatadogError, match="APP_KEY"):
        DatadogConfig.from_secret({"api_key": "k"})
    with pytest.raises(DatadogError, match="PAGE_SIZE"):
        DatadogConfig.from_secret({"api_key": "k", "app_key": "a", "page_size": 5000})
    with pytest.raises(DatadogError, match="BASE_URL"):
        DatadogConfig.from_secret({"api_key": "k", "app_key": "a", "base_url": "ftp://x"})


def test_retry_delay_prefers_ratelimit_reset() -> None:
    assert retry_delay({"X-RateLimit-Reset": "7", "Retry-After": "2"}, 1) == 7.0
    assert retry_delay({"Retry-After": "2"}, 1) == 2.0
    assert retry_delay({"X-RateLimit-Reset": "9999"}, 1) == client_mod.MAX_BACKOFF_SECONDS
    assert retry_delay({}, 2) == 2.0
    assert retry_delay(None, 1) == 1.0


def test_error_detail_formats() -> None:
    assert client_mod._error_detail('{"errors": ["Forbidden"]}') == "Forbidden"
    assert client_mod._error_detail('{"errors": [{"detail": "bad query"}]}') == "bad query"
    assert client_mod._error_detail('{"status": "error", "error": "parse"}') == "parse"
    assert client_mod._error_detail("<html>") == "<html>"


def test_exhausted_window_gates_next_request(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    key = ("http://x", "GET", "/api/v1/monitor")
    DatadogClient._record_gate(key, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "3"})
    c = DatadogClient(DatadogConfig.from_secret({"api_key": "k", "app_key": "a", "base_url": "http://x"}))
    c._wait_for_gate(key)
    assert slept and 2.0 < slept[0] <= 3.0
    slept.clear()
    c._wait_for_gate(key)
    assert slept == []
    DatadogClient._record_gate(key, {"X-RateLimit-Remaining": "5", "X-RateLimit-Reset": "3"})
    assert key not in client_mod._GATES


def test_client_against_mock_sends_keys_and_reports_errors(mock_api: str) -> None:
    good = DatadogClient(
        DatadogConfig.from_secret({"api_key": "fixture-api-key", "app_key": "fixture-app-key", "base_url": mock_api})
    )
    roles = good.get("/api/v2/roles", [("page[size]", 10), ("page[number]", 0)])
    assert [r["id"] for r in roles["data"]] == ["r-admin", "r-std", "r-ro"]
    with pytest.raises(DatadogError, match=r"HTTP 400.*Unknown parameter: bogus"):
        good.get("/api/v2/roles", {"bogus": 1})
    bad = DatadogClient(
        DatadogConfig.from_secret({"api_key": "nope", "app_key": "fixture-app-key", "base_url": mock_api})
    )
    with pytest.raises(DatadogError, match=r"HTTP 403.*API_KEY, APP_KEY"):
        bad.get("/api/v2/roles")
    start = time.monotonic()
    body = good.post(
        "/api/v2/logs/events/search",
        {
            "filter": {
                "query": 'service:"web"',
                "from": "2026-11-01T00:00:00Z",
                "to": "2026-11-02T00:00:00Z",
                "indexes": ["*"],
            },
            "page": {"limit": 1000},
        },
    )
    assert time.monotonic() - start < 5
    assert {d["attributes"]["service"] for d in body["data"]} == {"web"}
