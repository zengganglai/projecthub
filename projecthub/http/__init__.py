"""Install the project's HTTP API on a FastAPI app with injected services."""

from fastapi import FastAPI, Request

from .admission import Admission, readiness
from .catalog import accounts, catalog, configuration
from .changes import authorizations, changes, managed
from .control import control
from .docs import register_docs
from .protocol import register_errors, respond

__all__ = ["install_routes"]


def install_routes(app: FastAPI) -> None:
    app.state.active_requests = 0
    register_errors(app)
    app.add_middleware(Admission, state=app.state)
    for router in (catalog, accounts, changes, managed, authorizations, configuration, control):
        app.include_router(router, prefix="/api/v1")

    @app.get("/health", summary="检查服务是否正常运行")
    async def health():
        return {"status": "ok"}

    @app.get("/health/live", summary="检查服务存活状态")
    async def live(request: Request):
        return respond(request, {"live": True, "active_requests": app.state.active_requests})

    @app.get("/health/ready", summary="检查服务就绪状态")
    async def ready(request: Request):
        available, reason = readiness(app.state)
        return respond(request, {"ready": available, "reason": reason}, 200 if available else 503)

    register_docs(app)
