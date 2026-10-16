"""Fallback log source over SSH. Server-side only; enabled per product after security review.

Constraints this adapter relies on (configured on the UAT side, not here):
- Connection only via the jump host in the profile.
- The `ath-logread` user has a forced command: one read-only script that takes
  (path-glob, from, to, key) and returns only matching lines. No interactive shell.
- Only allow-listed log paths; SCP only for allow-listed files.
- Sessions are recorded.
"""
from __future__ import annotations

import os
from datetime import datetime

from ath_core.adapters.base import AdapterError, LogSource, MaskedLogs, registry


@registry.register("logs", "ssh")
class SshLogSource(LogSource):
    def fetch(self, start: datetime, end: datetime, keys: dict[str, str], limit_lines: int, masker) -> MaskedLogs:
        if os.environ.get("ATH_RUNTIME") != "server":
            raise AdapterError("ssh log source is server-only; desktop runs must use log-api", retryable=False)
        # TODO: use paramiko/asyncssh via jumpHost, key from vault (keyRef), call the forced
        # command with the arguments below, then mask every line before returning.
        _args = {"paths": self.params.get("paths", []), "from": start.isoformat(),
                 "to": end.isoformat(), "keys": keys, "maxLines": limit_lines}
        raise NotImplementedError("wire the SSH transport after security sign-off")
