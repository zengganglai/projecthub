"""ASGI 默认入口及显式配置的应用工厂。"""

from threading import Lock

from projecthub.application import create_app as create_app

__all__ = ["app", "create_app"]


class _DefaultApp:
    def __init__(self):
        self._application = None
        self._gate = Lock()

    async def __call__(self, scope, receive, send) -> None:
        with self._gate:
            if self._application is None:
                self._application = create_app()
            application = self._application
        await application(scope, receive, send)


app = _DefaultApp()
