"""远控生命周期、设备控制连接和唯一 RFB 通道。"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock

from projecthub.contracts import finish_task

from . import protocol
from .config import RemoteControlConfig, validate_remote_config
from .errors import RemoteError
from .state import DevicePages, Lease, Leases, OnlineDevice, Status
from .streams import close_stream, relay

_LOG = logging.getLogger(__name__)
_DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_REQUEST_ID = re.compile(r"[a-f0-9]{32}")
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_NETWORK_ERRORS = (protocol.ProtocolError, OSError, EOFError)

type _Streams = tuple[asyncio.StreamReader, asyncio.StreamWriter]


@dataclass(slots=True)
class _Device:
    device_id: str
    run_id: str
    wire: protocol.Wire
    writer: asyncio.StreamWriter
    online: OnlineDevice | None = None

    @property
    def proxy_name(self) -> str:
        return f"{self.device_id}.vnc"

    def seen(self) -> None:
        self.online = OnlineDevice(self.device_id, datetime.now(UTC))


@dataclass(slots=True)
class _Viewer:
    lease: Lease
    device: _Device
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    work: asyncio.Future[_Streams]
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    handler: asyncio.Task | None = None
    cleanup: asyncio.Task | None = None
    work_writer: asyncio.StreamWriter | None = None
    requested: bool = False
    paired: bool = False


def _require_owner(owner: str) -> None:
    if not isinstance(owner, str) or not owner.strip():
        raise RemoteError("CLIENT_ADDRESS_REQUIRED", "必须提供客户端地址。")


def _require_id(value: str, pattern: re.Pattern) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise RemoteError("INVALID_REQUEST", "输入格式无效。")


def _valid_token(token: str, lower_bound: int) -> bool:
    return 0 < len(token) <= 256 and all(lower_bound <= ord(character) <= 126 for character in token)


class RemoteService:
    """远控运行实例。活动网络和业务状态由启动时的 asyncio 循环独占。"""

    def __init__(self, config: RemoteControlConfig):
        self._config = config
        self._lifecycle_gate = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lifecycle: asyncio.Lock | None = None
        self._closing: asyncio.Task | None = None
        self._started = False
        self._ready = False
        self._reason = "not_started"
        self._frp_port: int | None = None
        self._rfb_port: int | None = None
        self._cipher = None
        self._listeners: list[asyncio.Server] = []
        self._writers: set[asyncio.StreamWriter] = set()
        self._pending: set[asyncio.StreamWriter] = set()
        self._device_tasks: set[asyncio.Task] = set()
        self._sweep_task: asyncio.Task | None = None
        self._devices: dict[str, _Device] = {}
        self._runs: dict[str, _Device] = {}
        self._viewer: _Viewer | None = None
        self._leases = Leases(self._config)
        self._pages = DevicePages(self._config)

    def _require_loop(self) -> None:
        try:
            caller = asyncio.get_running_loop()
        except RuntimeError:
            caller = None
        if self._loop is not caller:
            raise RemoteError("REMOTE_LIFECYCLE_ERROR", "活动远控只能在所属事件循环调用。")

    async def initialize(self) -> None:
        """启动或重试监听。同循环重复调用共享已启动资源。"""
        try:
            validate_remote_config(self._config)
        except ValueError as error:
            raise RemoteError("REMOTE_CONFIGURATION_ERROR", "远控配置无效。") from error
        while True:
            with self._lifecycle_gate:
                if self._lifecycle is None:
                    self._loop = asyncio.get_running_loop()
                    self._lifecycle = asyncio.Lock()
                    self._reason = "not_started"
                self._require_loop()
                lifecycle = self._lifecycle
                closing = self._closing
            if closing is not None:
                await asyncio.shield(closing)
                continue
            async with lifecycle:
                if self._lifecycle is not lifecycle or self._closing is not None:
                    continue
                if self._reason == "close_failed":
                    raise RemoteError("REMOTE_CLOSE_ERROR", "网络资源尚未完全释放，请重试关闭。")
                if not self._started:
                    await self._listen()
                return

    async def _listen(self) -> None:
        if not self._config.enabled:
            self._started, self._reason = True, "disabled"
            return

        config = self._config
        try:
            if not _valid_token(config.api_token, 33) or not _valid_token(config.frp_token, 32):
                raise ValueError("远控令牌格式无效。")
            self._cipher = protocol.control_cipher(config.frp_token)
            frp = await asyncio.start_server(
                self._accept_frp, config.bind_host, config.frp_port, start_serving=False
            )
            self._listeners.append(frp)
            rfb = await asyncio.start_server(
                self._accept_viewer,
                config.bind_host,
                config.rfb_port,
                start_serving=False,
            )
            self._listeners.append(rfb)
            await frp.start_serving()
            await rfb.start_serving()
            self._frp_port = frp.sockets[0].getsockname()[1]
            self._rfb_port = rfb.sockets[0].getsockname()[1]
            self._sweep_task = self._observe(asyncio.create_task(self._sweep()))
            self._started = self._ready = True
            self._reason = "ready"
        except asyncio.CancelledError:
            self._reason = "startup_cancelled"
            await finish_task(asyncio.create_task(self._release_resources()))
            raise
        except (ImportError, OSError, ValueError) as error:
            self._reason = "startup_failed"
            _LOG.error("远控启动失败 type=%s", type(error).__name__)
            await finish_task(asyncio.create_task(self._release_resources()))

    async def close(self) -> None:
        """停止接入并等待收尾，即使关闭等待者被取消。"""
        with self._lifecycle_gate:
            if self._lifecycle is None:
                return
            self._require_loop()
            if self._closing is None or self._closing.done():
                self._closing = asyncio.create_task(self._stop(self._lifecycle))
            closing = self._closing
        await finish_task(closing)

    async def _stop(self, lifecycle: asyncio.Lock) -> None:
        async with lifecycle:
            self._reason = "stopped"
            await self._release_resources()
            with self._lifecycle_gate:
                self._loop = self._lifecycle = self._closing = None

    async def _release_resources(self) -> None:
        self._ready = False
        try:
            for listener in self._listeners:
                listener.close()
            cleanup = self._end_viewer(self._viewer) if self._viewer is not None else None
            tasks = list(self._device_tasks)
            if self._sweep_task is not None:
                tasks.append(self._sweep_task)
            for task in tasks:
                task.cancel()
            if cleanup is not None:
                tasks.append(cleanup)
            await asyncio.gather(*tasks, return_exceptions=True)

            # 回收在首次调度前就取消或上一次关闭失败的连接。
            results = await asyncio.gather(
                *(self._drop_writer(writer) for writer in tuple(self._writers)),
                return_exceptions=True,
            )
            failures = [result for result in results if isinstance(result, BaseException)]
            if failures:
                raise BaseExceptionGroup("远控流关闭失败。", failures)
            async with asyncio.timeout(2):
                await asyncio.gather(*(listener.wait_closed() for listener in self._listeners))
        except Exception as error:
            self._reason = "close_failed"
            raise RemoteError("REMOTE_CLOSE_ERROR", "网络资源尚未完全释放，请重试关闭。") from error

        self._listeners.clear()
        self._pending.clear()
        self._device_tasks.clear()
        self._sweep_task = None
        self._viewer = None
        self._devices.clear()
        self._runs.clear()
        self._leases = Leases(self._config)
        self._pages = DevicePages(self._config)
        self._frp_port = self._rfb_port = None
        self._cipher = None
        self._started = False

    def health(self) -> dict:
        """返回无凭据、无可变内部对象的状态快照。"""
        with self._lifecycle_gate:
            if self._loop is not None:
                self._require_loop()
            return {
                "enabled": self._config.enabled
                if self._loop is not None or self._reason == "stopped"
                else False,
                "ready": self._ready,
                "reason": self._reason,
                "frpPort": self._frp_port,
                "rfbPort": self._rfb_port,
                "devices": sum(device.online is not None for device in self._devices.values()),
                "connections": len(self._writers),
                "activeSession": self._leases.active is not None,
                "requestRecords": len(self._leases),
            }

    def _require_started(self) -> None:
        with self._lifecycle_gate:
            if self._loop is None:
                raise RemoteError("REMOTE_UNAVAILABLE", "远控尚未启动。")
            self._require_loop()
            if self._closing is not None:
                raise RemoteError("REMOTE_UNAVAILABLE", "远控正在关闭。")

    def _require_ready(self) -> None:
        self._require_started()
        if not self._ready:
            raise RemoteError("REMOTE_UNAVAILABLE")

    def authenticate_api(self, token: str | None) -> None:
        """验证适配层提取的原始令牌。"""
        self._require_started()
        expected = self._config.api_token
        if not expected or not token:
            raise RemoteError("AUTH_REQUIRED")
        if not isinstance(token, str) or not token.isascii() or not expected.isascii():
            raise RemoteError("AUTH_FAILED")
        if not hmac.compare_digest(token, expected):
            raise RemoteError("AUTH_FAILED")

    def list_devices(self, limit: int, cursor: str | None, owner: str) -> dict:
        """按所有者和游标读取固定在线快照，limit 为 1～200。"""
        _require_owner(owner)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise RemoteError("INVALID_LIMIT")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 1024):
            raise RemoteError("INVALID_CURSOR")
        self._require_ready()
        devices = [device.online for device in self._devices.values() if device.online is not None]
        return self._pages.page(devices, limit, cursor, owner)

    def _lease_payload(self, lease: Lease) -> dict:
        return {
            "sessionId": lease.session_id,
            "deviceId": lease.device_id,
            "host": self._config.public_host,
            "port": self._rfb_port,
            "expiresAt": lease.expires_at.isoformat(),
            "renewAfterSeconds": self._config.renew_after_seconds,
        }

    async def _ready_after_expiration(self) -> None:
        self._require_ready()
        lifecycle = self._lifecycle
        cleanup = self._expire()
        if cleanup is not None:
            await asyncio.shield(cleanup)
            if lifecycle is not self._lifecycle:
                raise RemoteError("REMOTE_UNAVAILABLE", "远控运行已结束。")
            self._require_ready()

    async def acquire(self, device_id: str, request_id: str, owner: str) -> dict:
        """申请全局唯一租约，或续活同一幂等请求。"""
        _require_owner(owner)
        _require_id(device_id, _DEVICE_ID)
        _require_id(request_id, _REQUEST_ID)
        await self._ready_after_expiration()
        device = self._devices.get(device_id)
        if device is None or device.online is None:
            raise RemoteError("DEVICE_OFFLINE")
        lease = self._leases.acquire(device_id, request_id, owner, self._viewer is not None)
        return self._lease_payload(lease)

    async def lookup(self, request_id: str, owner: str) -> dict:
        """查询所属请求。已授予的请求在返回时同步续活。"""
        _require_owner(owner)
        _require_id(request_id, _REQUEST_ID)
        await self._ready_after_expiration()
        record = self._leases.lookup(request_id, owner)
        result = {"status": record.status.value}
        if record.status is Status.GRANTED:
            result["lease"] = self._lease_payload(record.lease)
        return result

    async def cancel(self, request_id: str, owner: str) -> None:
        """为指定请求保留取消结果，并清理已存在的查看通道。"""
        _require_owner(owner)
        _require_id(request_id, _REQUEST_ID)
        self._require_ready()
        self._expire()
        cleanup = self._close_viewer_for(self._leases.cancel(request_id, owner))
        if cleanup is not None:
            await finish_task(cleanup)

    async def renew(self, session_id: str, owner: str) -> dict:
        """续期仍有效且归该所有者持有的租约。"""
        _require_owner(owner)
        _require_id(session_id, _SESSION_ID)
        await self._ready_after_expiration()
        return self._lease_payload(self._leases.renew(session_id, owner))

    async def release(self, session_id: str, owner: str) -> None:
        """释放所属租约，等待对应通道关闭。未知 ID 幂等返回。"""
        _require_owner(owner)
        _require_id(session_id, _SESSION_ID)
        self._require_ready()
        self._expire()
        cleanup = self._close_viewer_for(self._leases.release(session_id, owner))
        if cleanup is not None:
            await finish_task(cleanup)

    def _expire(self) -> asyncio.Task | None:
        expired = self._leases.expire()
        if expired is not None:
            self._close_viewer_for(expired)
        return self._viewer.cleanup if self._viewer is not None else None

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(self._config.sweep_seconds)
            self._pages.prune()
            cleanup = self._expire()
            if cleanup is not None:
                await asyncio.shield(cleanup)

    @staticmethod
    def _observe(task: asyncio.Task) -> asyncio.Task:
        def report(done):
            if not done.cancelled() and (error := done.exception()) is not None:
                _LOG.error("远控后台任务失败 type=%s", type(error).__name__)

        task.add_done_callback(report)
        return task

    async def _drop_writer(self, writer: asyncio.StreamWriter) -> None:
        await close_stream(writer)
        self._writers.discard(writer)

    def _accept_frp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not self._ready or len(self._pending) >= self._config.pending_capacity:
            writer.close()
            return
        self._pending.add(writer)
        self._writers.add(writer)
        task = self._observe(asyncio.create_task(self._frp_client(reader, writer)))
        self._device_tasks.add(task)
        task.add_done_callback(self._device_tasks.discard)

    async def _frp_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        wire = protocol.Wire(reader, writer)
        try:
            kind, fields = await wire.read(self._config.handshake_seconds)
            if kind == protocol.LOGIN:
                await self._login(wire, writer, fields)
            elif kind == protocol.WORK_CONNECTION:
                await self._attach_work(wire, reader, writer, fields)
        except _NETWORK_ERRORS as error:
            _LOG.debug("frp 连接结束 type=%s", type(error).__name__)
        finally:
            self._pending.discard(writer)
            await self._drop_writer(writer)

    def _signed(self, fields: dict) -> bool:
        return protocol.valid_signature(fields, self._config.frp_token, self._config.timestamp_window_seconds)

    async def _login(self, wire: protocol.Wire, writer: asyncio.StreamWriter, fields: dict) -> None:
        name = fields.get("user")
        metadata = fields.get("metas")
        pool_count = fields.get("pool_count")
        config = self._config
        if (
            not self._signed(fields)
            or not isinstance(name, str)
            or _DEVICE_ID.fullmatch(name) is None
            or not isinstance(metadata, dict)
            or metadata.get("deviceId") != name
            or fields.get("version") != protocol.VERSION
            or type(pool_count) is not int
            or pool_count != 0
            or name in self._devices
            or len(self._devices) >= config.capacity
        ):
            await wire.write(
                protocol.LOGIN_RESPONSE,
                {"error": "login rejected"},
                config.write_seconds,
            )
            return

        device = _Device(name, secrets.token_hex(24), wire, writer)
        self._devices[name] = device
        self._runs[device.run_id] = device
        try:
            await wire.write(
                protocol.LOGIN_RESPONSE,
                {"version": protocol.VERSION, "run_id": device.run_id},
                config.write_seconds,
            )
            wire.start_control_encryption(self._cipher)
            if await self._register_proxy(device):
                while True:
                    tag, ping = await wire.read(config.heartbeat_seconds)
                    if tag != protocol.PING or not self._signed(ping):
                        return
                    device.seen()
                    await wire.write(protocol.PONG, {}, config.write_seconds)
        finally:
            if self._detach_device(device):
                lease = self._leases.active
                if lease is not None and lease.device_id == name:
                    self._leases.terminate(lease, Status.EXPIRED)
                    self._close_viewer_for(lease)

    async def _register_proxy(self, device: _Device) -> bool:
        config = self._config
        tag, fields = await device.wire.read(config.handshake_seconds)
        port = fields.get("remote_port")
        response = {"proxy_name": device.proxy_name}
        accepted = (
            tag == protocol.PROXY
            and fields.get("proxy_name") == device.proxy_name
            and fields.get("proxy_type") == "tcp"
            and type(port) is int
            and port == self._rfb_port
            and fields.get("use_encryption", False) is False
            and fields.get("use_compression", False) is False
        )
        if accepted:
            device.seen()
            self._pending.discard(device.writer)
            response["remote_addr"] = f":{port}"
        else:
            response["error"] = "proxy rejected"
        await device.wire.write(protocol.PROXY_RESPONSE, response, config.write_seconds)
        return accepted

    def _detach_device(self, device: _Device) -> bool:
        if self._devices.get(device.device_id) is not device:
            return False
        del self._devices[device.device_id]
        del self._runs[device.run_id]
        device.writer.close()
        return True

    async def _attach_work(
        self,
        wire: protocol.Wire,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        fields: dict,
    ) -> None:
        self._expire()
        viewer = self._viewer
        run_id = fields.get("run_id")
        device = self._runs.get(run_id) if isinstance(run_id, str) else None
        if (
            not self._signed(fields)
            or viewer is None
            or device is not viewer.device
            or self._leases.active is not viewer.lease
            or not viewer.requested
            or viewer.work_writer is not None
        ):
            await wire.write(
                protocol.WORK_RESPONSE,
                {"error": "work rejected"},
                self._config.write_seconds,
            )
            return

        # 先占用配对位置，再写回应；第二个工作连接不能抢占。
        viewer.work_writer = writer
        self._pending.discard(writer)
        await wire.write(
            protocol.WORK_RESPONSE,
            {"proxy_name": device.proxy_name},
            self._config.write_seconds,
        )
        self._expire()
        if self._viewer is viewer and viewer.cleanup is None and not viewer.work.done():
            viewer.paired = True
            viewer.work.set_result((reader, writer))
            await viewer.closed.wait()

    def _accept_viewer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._expire()
        lease = self._leases.active
        peer = writer.get_extra_info("peername")
        if not self._ready or lease is None or self._viewer is not None or not peer or peer[0] != lease.owner:
            writer.close()
            return
        device = self._devices.get(lease.device_id)
        if device is None or device.online is None:
            writer.close()
            return
        self._writers.add(writer)
        viewer = _Viewer(lease, device, reader, writer, asyncio.get_running_loop().create_future())
        self._viewer = viewer
        viewer.handler = self._observe(asyncio.create_task(self._serve_viewer(viewer)))

    async def _get_work(self, viewer: _Viewer) -> tuple[_Streams, bytes]:
        first = asyncio.create_task(viewer.reader.read(1))
        try:
            async with asyncio.timeout(self._config.work_seconds):
                await asyncio.wait((first, viewer.work), return_when=asyncio.FIRST_COMPLETED)
                prefix = b""
                if first.done():
                    prefix = first.result()
                    if not prefix:
                        raise EOFError("查看者在配对前断开。")
                else:
                    first.cancel()
                return await asyncio.shield(viewer.work), prefix
        finally:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)

    async def _serve_viewer(self, viewer: _Viewer) -> None:
        try:
            viewer.requested = True
            await viewer.device.wire.write(protocol.WORK_REQUEST, {}, self._config.write_seconds)
            work, prefix = await self._get_work(viewer)
            if prefix:
                work[1].write(prefix)
                async with asyncio.timeout(self._config.write_seconds):
                    await work[1].drain()
            await relay((viewer.reader, viewer.writer), work, self._config)
        except _NETWORK_ERRORS as error:
            _LOG.debug("RFB 通道结束 type=%s", type(error).__name__)
        finally:
            self._end_viewer(viewer)

    def _close_viewer_for(self, lease: Lease | None) -> asyncio.Task | None:
        viewer = self._viewer
        if viewer is not None and viewer.lease is lease:
            return self._end_viewer(viewer)
        return None

    def _end_viewer(self, viewer: _Viewer) -> asyncio.Task:
        if viewer.cleanup is None:
            self._leases.terminate(viewer.lease, Status.EXPIRED)
            if viewer.requested and not viewer.paired:
                # frp 工作帧只含 run_id。废弃没有完成配对的运行标识，拒绝迟到工作连接。
                self._detach_device(viewer.device)
            viewer.cleanup = self._observe(asyncio.create_task(self._dispose_viewer(viewer)))
        return viewer.cleanup

    async def _dispose_viewer(self, viewer: _Viewer) -> None:
        if viewer.handler is not None:
            viewer.handler.cancel()
            await asyncio.gather(viewer.handler, return_exceptions=True)
        viewer.work.cancel()
        writers = [viewer.writer]
        if viewer.work_writer is not None:
            writers.append(viewer.work_writer)
        try:
            results = await asyncio.gather(
                *(self._drop_writer(writer) for writer in writers),
                return_exceptions=True,
            )
            failures = [result for result in results if isinstance(result, BaseException)]
            if failures:
                raise RemoteError("REMOTE_CLOSE_ERROR", "查看连接清理失败。") from BaseExceptionGroup(
                    "查看连接清理失败。", failures
                )
        finally:
            viewer.closed.set()
        if self._viewer is viewer:
            self._viewer = None
