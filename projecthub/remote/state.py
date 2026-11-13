"""同一事件循环内的租约及设备分页状态。所有变更同步完成。"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from time import monotonic

from .config import RemoteControlConfig
from .errors import RemoteError


class Status(StrEnum):
    GRANTED = "granted"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(slots=True)
class Lease:
    device_id: str
    owner: str
    session_id: str
    deadline: float
    expires_at: datetime


@dataclass(slots=True)
class Request:
    owner: str
    status: Status
    lease: Lease | None = None

    def require_owner(self, owner: str) -> None:
        if owner != self.owner:
            raise RemoteError("SESSION_OWNER")


class Leases:
    def __init__(self, config: RemoteControlConfig):
        self._config = config
        self._by_request: dict[str, Request] = {}
        self._by_session: dict[str, Request] = {}
        self.active: Lease | None = None

    def __len__(self) -> int:
        return len(self._by_request)

    def _add(self, request_id: str, record: Request) -> None:
        if len(self) >= self._config.record_capacity:
            raise RemoteError("REQUEST_CAPACITY")
        self._by_request[request_id] = record

    def _expiration(self) -> tuple[float, datetime]:
        ttl = self._config.lease_seconds
        return monotonic() + ttl, datetime.now(UTC) + timedelta(seconds=ttl)

    def _refresh(self, lease: Lease) -> Lease:
        lease.deadline, lease.expires_at = self._expiration()
        return lease

    def _require_active(self, record: Request) -> Lease:
        if record.status is not Status.GRANTED or record.lease is None:
            raise RemoteError("SESSION_TERMINATED")
        return record.lease

    def acquire(self, device_id: str, request_id: str, owner: str, viewer_closing: bool) -> Lease:
        previous = self._by_request.get(request_id)
        if previous is not None:
            previous.require_owner(owner)
            if previous.status is not Status.GRANTED or previous.lease.device_id != device_id:
                raise RemoteError("REQUEST_TERMINATED_OR_CHANGED")
            return self._refresh(previous.lease)
        if self.active is not None or viewer_closing:
            raise RemoteError("CONTROL_BUSY")
        lease = Lease(device_id, owner, secrets.token_hex(24), *self._expiration())
        record = Request(owner, Status.GRANTED, lease)
        self._add(request_id, record)
        self._by_session[lease.session_id] = record
        self.active = lease
        return lease

    def lookup(self, request_id: str, owner: str) -> Request:
        record = self._by_request.get(request_id)
        if record is None:
            raise RemoteError("REQUEST_NOT_FOUND")
        record.require_owner(owner)
        if record.status is Status.GRANTED:
            self._refresh(record.lease)
        return record

    def cancel(self, request_id: str, owner: str) -> Lease | None:
        record = self._by_request.get(request_id)
        if record is None:
            self._add(request_id, Request(owner, Status.CANCELLED))
            return None
        record.require_owner(owner)
        return self.terminate(record.lease, Status.CANCELLED)

    def renew(self, session_id: str, owner: str) -> Lease:
        record = self._by_session.get(session_id)
        if record is None:
            raise RemoteError("SESSION_NOT_FOUND")
        record.require_owner(owner)
        return self._refresh(self._require_active(record))

    def release(self, session_id: str, owner: str) -> Lease | None:
        record = self._by_session.get(session_id)
        if record is None:
            return None
        record.require_owner(owner)
        return self.terminate(record.lease, Status.CANCELLED)

    def expire(self) -> Lease | None:
        lease = self.active
        if lease is not None and monotonic() >= lease.deadline:
            return self.terminate(lease, Status.EXPIRED)
        return None

    def terminate(self, lease: Lease | None, status: Status) -> Lease | None:
        if lease is not None and self.active is lease:
            self._by_session[lease.session_id].status = status
            self.active = None
        return lease


@dataclass(frozen=True, slots=True)
class OnlineDevice:
    device_id: str
    seen_at: datetime

    def payload(self) -> dict:
        return {
            "deviceId": self.device_id,
            "model": "T507 · Linux",
            "lastSeenAt": self.seen_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class _Snapshot:
    deadline: float
    owner: str
    devices: tuple[OnlineDevice, ...]


class DevicePages:
    def __init__(self, config: RemoteControlConfig):
        self._config = config
        self._snapshots: dict[str, _Snapshot] = {}

    def prune(self) -> None:
        now = monotonic()
        for key, snapshot in tuple(self._snapshots.items()):
            if now >= snapshot.deadline:
                del self._snapshots[key]

    def page(self, devices: list[OnlineDevice], limit: int, cursor: str | None, owner: str) -> dict:
        self.prune()
        if cursor:
            try:
                key, position = cursor.split(":")
                offset = int(position)
                snapshot = self._snapshots[key]
            except ValueError, KeyError:
                raise RemoteError("SNAPSHOT_EXPIRED") from None
            if snapshot.owner != owner:
                raise RemoteError("SNAPSHOT_OWNER")
            items = snapshot.devices
            if not 0 < offset < len(items):
                raise RemoteError("INVALID_CURSOR")
        else:
            key, offset = "", 0
            items = tuple(sorted(devices, key=lambda item: item.device_id))
            if len(items) > limit:
                if len(self._snapshots) >= self._config.snapshot_capacity:
                    raise RemoteError("SNAPSHOT_CAPACITY")
                key = secrets.token_hex(16)
                self._snapshots[key] = _Snapshot(monotonic() + self._config.snapshot_seconds, owner, items)
        end = offset + limit
        return {
            "items": [item.payload() for item in items[offset:end]],
            "total": len(items),
            "capacity": self._config.capacity,
            "nextCursor": f"{key}:{end}" if end < len(items) else None,
        }
