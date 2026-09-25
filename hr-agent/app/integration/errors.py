"""Integration-plane error type shared by the ACL, backend clients and the mock."""

from __future__ import annotations


class BackendError(Exception):
    """A system-of-record call failed. `status` follows HTTP semantics (4xx client, 5xx transient)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
