"""应用启动、停止和同步工作收尾；所有业务实例属于一次 lifespan。"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from projecthub.contracts import HubError, finish_task, run_blocking

from .config import AppConfig, resolve_settings


async def _close_services(app):
    state = app.state
    state.ready, state.reason = False, "stopping"
    await state.drained.wait()
    errors = []
    for name, resource, asynchronous in tuple(reversed(state.resources)):
        if name == "lock" and len(state.resources) > 1:
            continue
        if name == "database" and any(item[0] == "files" for item in state.resources):
            continue
        try:
            if name == "lock":
                await run_blocking(resource.release)
            elif asynchronous:
                await resource.close()
            else:
                await run_blocking(resource.close)
        except BaseException as error:
            errors.append(error)
        else:
            state.resources.remove((name, resource, asynchronous))
    state.reason = "close_failed" if errors else "stopped"
    if errors:
        raise HubError(
            "APPLICATION_CLOSE_FAILED", "资源关闭失败，可重试 app.state.close()。"
        ) from BaseExceptionGroup("服务关闭", errors)


def create_app(settings: AppConfig | None = None) -> FastAPI:
    """构造路由和冻结配置，不打开数据库、创建运行目录或监听端口。"""
    supplied = AppConfig() if settings is None else settings
    settings = resolve_settings(supplied)

    @asynccontextmanager
    async def lifespan(app):
        from projecthub.database import Database
        from projecthub.files import FileService
        from projecthub.permissions import Permissions
        from projecthub.projects import ProjectService
        from projecthub.remote import RemoteService
        from projecthub.storage import RuntimeLock
        from projecthub.users import Users, UserStore

        state = app.state
        if state.entered or state.resources:
            raise HubError("APPLICATION_LIFECYCLE_ERROR", "此应用已有活动或未完成清理的生命周期。")
        state.entered = True
        state.drained = asyncio.Event()
        state.drained.set()
        state.ready, state.reason = False, "starting"
        try:
            guard = RuntimeLock(settings)
            state.resources.append(("lock", guard, False))
            await run_blocking(guard.acquire)
            state.db = Database(settings)
            state.user_store = UserStore(state.db, settings.users.max_users)
            state.permissions = Permissions(state.db, state.user_store)
            state.users = Users(state.db, state.permissions)
            state.files = FileService(settings, state.db, state.permissions)
            state.projects = ProjectService(settings, state.db, state.permissions, state.files)
            state.resources.append(("database", state.db, False))
            await run_blocking(state.db.open, initialize=state.files.scan_initial)
            state.resources.append(("users", state.user_store, False))
            await run_blocking(state.user_store.initialize)
            await run_blocking(state.permissions.validate)
            state.resources.append(("files", state.files, False))
            await run_blocking(state.projects.initialize)
            await run_blocking(state.files.initialize)
            state.remote = RemoteService(settings.remote_control)
            state.resources.append(("remote", state.remote, True))
            await state.remote.initialize()
            state.ready, state.reason = True, "ready"
            yield
        finally:
            try:
                await state.close()
            finally:
                state.entered = False

    app = FastAPI(
        title="ProjectHub 文件与设备服务", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.settings = settings
    app.state.ready = False
    app.state.reason = "not_started"
    app.state.active_requests = 0
    app.state.resources = []
    app.state.entered = False
    app.state.drained = None
    app.state.closing = None

    async def close():
        closing = app.state.closing
        if closing is None or closing.done():
            closing = asyncio.create_task(_close_services(app))
            app.state.closing = closing
        await finish_task(closing)

    app.state.close = close
    from projecthub.http import install_routes

    install_routes(app)
    return app


def run(settings=None, *, on_started=None, stop_event=None):
    """同步运行一个 Uvicorn worker；可由受管入口的事件请求优雅停止。"""
    from uvicorn import Config, Server

    app = create_app(settings)
    settings = app.state.settings

    class ServerWithStop(Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            if self.started and on_started:
                on_started()

        async def on_tick(self, counter):
            if stop_event is not None and stop_event.is_set():
                self.should_exit = True
            return await super().on_tick(counter)

    server = ServerWithStop(
        Config(
            app,
            host=settings.http.bind_host,
            port=settings.http.port,
            workers=1,
            lifespan="on",
        )
    )
    server.run()
