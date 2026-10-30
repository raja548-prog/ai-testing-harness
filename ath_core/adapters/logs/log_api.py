"""Preferred log source: read-only query API (observability stack or a UAT log-fetch service).

Expected endpoint shape (the UAT log-fetch service implements exactly this):
  GET {endpoint}?product=&component=&from=&to=&key=rrn:629014773311&maxLines=500
  -> {"lines": [...], "truncated": false}
"""
from __future__ import annotations

from datetime import datetime

import httpx

from ath_core.adapters.base import AdapterError, LogSource, MaskedLogs, registry


@registry.register("logs", "log-api")
class LogApiSource(LogSource):
    def fetch(self, start: datetime, end: datetime, keys: dict[str, str], limit_lines: int, masker) -> MaskedLogs:
        endpoint = self.params.get("endpoint")
        if not endpoint:
            raise AdapterError("log-api: 'endpoint' missing in profile", retryable=False)
        allowed = set(self.params.get("keys", []))
        key = [f"{k}:{v}" for k, v in keys.items() if not allowed or k in allowed]
        if not key:
            # Without a key the service returns any lines from the window, of any product: they
            # would reach the triage agents as evidence for a failure they have nothing to do with.
            raise AdapterError("log-api: the case has no declared correlation key; not running an unscoped query",
                               retryable=False)
        query = {"from": start.isoformat(), "to": end.isoformat(), "maxLines": str(limit_lines), "key": key}
        for name in ("product", "component"):  # `product` is set by the orchestrator, `component` in the profile
            if self.params.get(name):
                query[name] = str(self.params[name])
        try:
            # Auth: short-lived service token or mTLS, injected by the platform transport.
            resp = httpx.get(endpoint, params=query, timeout=30)
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise AdapterError(f"log-api fetch failed: {e}")
        if not isinstance(body, dict) or not isinstance(body.get("lines", []), list):
            raise AdapterError('log-api: response is not {"lines": [...]}', retryable=False)
        # Mask again here even if the service masks at source: defence in depth.
        lines = [masker.mask(str(line)) for line in body.get("lines", [])][:limit_lines]
        return MaskedLogs(source="log-api", lines=lines, truncated=bool(body.get("truncated")))
