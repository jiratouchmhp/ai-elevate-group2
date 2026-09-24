"""Environment Configuration & .env Loader for MCP Server and GCP Settings.

Automatically loads key-value pairs from the repository `.env` file into `os.environ`
(without overwriting variables already explicitly set in the process environment)
and provides typed accessors for the Anti-Corruption Layer (ACL) MCP proxy.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_PATH = REPO_ROOT / ".env"

DEFAULT_MCP_BASE_URL = "https://mock-saas.aishprabhat.demo.altostrat.com"


def load_dotenv_file(env_path: Optional[Path] = None, *, override: bool = False) -> Dict[str, str]:
    """Parses a `.env` file and populates `os.environ`."""
    target = env_path or DEFAULT_ENV_PATH
    loaded: Dict[str, str] = {}
    if not target.exists() or not target.is_file():
        return loaded

    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and ((val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")):
            val = val[1:-1]
        if not key:
            continue
        loaded[key] = val
        if override or key not in os.environ:
            os.environ[key] = val

    return loaded


# Load `.env` on module import so all tests and CLI entrypoints have access to env vars
load_dotenv_file()


def get_mcp_base_url() -> str:
    """Returns the base URL for the Unified Mock Enterprise Services host."""
    return os.environ.get("MCP_SERVER_BASE_URL", DEFAULT_MCP_BASE_URL).rstrip("/")


def get_workweek_mcp_url() -> str:
    """Returns the stateless Streamable HTTP MCP endpoint URL for WorkWeek (HCM)."""
    explicit = os.environ.get("WORKWEEK_MCP_URL", "").strip()
    if explicit:
        return explicit if explicit.endswith("/") else f"{explicit}/"
    return f"{get_mcp_base_url()}/work-week/mcp/"


def get_service_immediately_mcp_url() -> str:
    """Returns the stateless Streamable HTTP MCP endpoint URL for ServiceImmediately (ITSM)."""
    explicit = os.environ.get("SERVICE_IMMEDIATELY_MCP_URL", "").strip()
    if explicit:
        return explicit if explicit.endswith("/") else f"{explicit}/"
    return f"{get_mcp_base_url()}/service-immediately/mcp/"


def get_mcp_token() -> Optional[str]:
    """Returns the configured MCP Personal Access Token (`MCP_TOKEN`), if set."""
    token = os.environ.get("MCP_TOKEN", "").strip()
    if not token or token == "your_mcp_personal_access_token_here":
        return None
    return token


def get_mcp_authenticated_employee_id() -> str:
    """Returns the employee ID bound to the configured `MCP_TOKEN` (default: EMP-836)."""
    return os.environ.get("MCP_AUTHENTICATED_EMPLOYEE_ID", "EMP-836").strip() or "EMP-836"


def is_live_mcp_enabled(default: bool = False) -> bool:
    """Returns True if `USE_LIVE_MCP` is truthy and a valid `MCP_TOKEN` is available."""
    raw = os.environ.get("USE_LIVE_MCP")
    if raw is None:
        return default and (get_mcp_token() is not None)
    enabled = raw.strip().lower() in ("1", "true", "yes", "on")
    return enabled and (get_mcp_token() is not None)
