# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import contextlib
import os
from collections.abc import AsyncIterator

from a2a.server.tasks import InMemoryTaskStore
from dotenv import load_dotenv
from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner

load_dotenv()

from app.app_utils import services  # noqa: E402
from app.app_utils.a2a import attach_a2a_routes  # noqa: E402

allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)
otel_to_cloud = True

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.agent import app as adk_app
    from app.agent import root_agent

    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        auto_create_session=True,
    )
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name
    await attach_a2a_routes(
        app,
        agent=root_agent,
        runner=runner,
        task_store=InMemoryTaskStore(),
        rpc_path=f"/a2a/{adk_app.name}",
    )
    await _warm_up()
    yield
    from app.integration.http_pool import aclose_all

    await aclose_all()


async def _warm_up() -> None:
    """Pay one-time initialisation at startup, not on the first user's request (latency).

    Best effort: a failure here is logged and the lazy path still works on first use.
    """
    import asyncio
    import logging

    def _sync() -> None:
        from app.policy.retriever import get_retriever
        from app.ui_bff import _corpus_index

        retriever = get_retriever()  # BM25 index build, or Vertex RAG client module
        rag_module = getattr(retriever, "_rag_module", None)
        if rag_module is not None:
            rag_module()  # vertexai.init + import (network-free)
        _corpus_index()

    try:
        await asyncio.to_thread(_sync)
    except Exception:  # pragma: no cover - never block startup
        logging.getLogger(__name__).exception("warm-up failed; continuing with lazy init")


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    otel_to_cloud=otel_to_cloud,
    lifespan=lifespan,
)
app.title = "hr-agent"
app.description = "API for interacting with the Agent hr-agent"

# --- Chat UI (SDD §3.10): thin BFF + static React bundle ---------------------------
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app import config as hr_config  # noqa: E402
from app.ui_bff import router as ui_router  # noqa: E402

app.include_router(ui_router)
if (hr_config.UI_DIST_DIR / "index.html").exists():
    # ADK registers GET "/" -> /dev-ui/; hand the root to the HR UI instead (dev UI stays at /dev-ui/).
    # Serve only "/" and "/assets" — a catch-all mount would shadow the A2A routes added in lifespan.
    app.router.routes[:] = [
        r for r in app.router.routes
        if not (getattr(r, "path", None) == "/" and "GET" in (getattr(r, "methods", None) or set()))
    ]
    app.mount("/assets", StaticFiles(directory=hr_config.UI_DIST_DIR / "assets"), name="hr-ui-assets")

    @app.get("/", include_in_schema=False)
    async def hr_ui_index() -> FileResponse:
        return FileResponse(hr_config.UI_DIST_DIR / "index.html", headers={"Cache-Control": "no-cache"})


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
