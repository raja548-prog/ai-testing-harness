from datetime import datetime, timedelta, timezone

import httpx
import pytest

from ath_core.adapters import AdapterError
from ath_core.adapters.logs.log_api import LogApiSource
from ath_core.masking import Masker

END = datetime(2026, 10, 18, 10, 5, tzinfo=timezone.utc)


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def get(url, params=None, timeout=None):
        seen.append(params)
        return httpx.Response(200, json={"lines": ["ERROR card 4111111111111111"]}, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", get)
    return seen


def _fetch(keys, **params):
    src = LogApiSource(**{"endpoint": "https://logs", "keys": ["rrn", "stan"], **params})
    return src.fetch(END - timedelta(minutes=15), END, keys, 200, Masker())


def test_query_is_scoped_to_the_product_and_declared_keys(calls):
    logs = _fetch({"rrn": "629014773311", "user": "x"}, product="settlenxt", component="clearing")
    assert calls[0]["key"] == ["rrn:629014773311"]  # undeclared key dropped
    assert calls[0]["product"] == "settlenxt" and calls[0]["component"] == "clearing"
    assert logs.lines == ["ERROR card [PAN****1111]"]


@pytest.mark.parametrize("keys", [{}, {"user": "x"}])
def test_no_correlation_key_means_no_query(calls, keys):
    with pytest.raises(AdapterError, match="unscoped"):
        _fetch(keys, product="settlenxt")
    assert calls == []


def test_unexpected_response_shape_is_an_adapter_error(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, params=None, timeout=None: httpx.Response(
        200, json=["not", "an", "object"], request=httpx.Request("GET", url)))
    with pytest.raises(AdapterError, match="not"):
        _fetch({"rrn": "1"})
