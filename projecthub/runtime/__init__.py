"""Windows受管进程的启动、状态和停止接口。"""

from .processes import run_managed, service_status, start_service, stop_service

__all__ = ["run_managed", "service_status", "start_service", "stop_service"]
