"""Shared test setup: deterministic clock and an isolated runtime dir (ledger/audit).

Set before any `app.*` import so `app.config` picks them up.
"""

import os
import tempfile

os.environ.setdefault("HR_FIXED_TODAY", "2026-10-05")
os.environ.setdefault("HR_RUNTIME_DIR", tempfile.mkdtemp(prefix="hr-agent-test-"))
os.environ.setdefault("BACKEND_MODE", "inprocess")
os.environ.setdefault("GUARDRAIL_MODE", "enforce")
os.environ.pop("MODEL_ARMOR_TEMPLATE_ID", None)
