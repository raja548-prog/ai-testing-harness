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
        query = {
            "from": start.isoformat(), "to": end.isoformat(), "maxLines": str(limit_lines),
            "key": [f"{k}:{v}" for k, v in keys.items() if not allowed or k in allowed],
        }
        try:
            # Auth: short-lived service token or mTLS, injected by the platform transport.
            resp = httpx.get(endpoint, params=query, timeout=30)
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise AdapterError(f"log-api fetch failed: {e}")
        # Mask again here even if the service masks at source: defence in depth.
        lines = [masker.mask(str(line)) for line in body.get("lines", [])][:limit_lines]
        return MaskedLogs(source="log-api", lines=lines, truncated=bool(body.get("truncated")))
