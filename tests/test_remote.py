import asyncio
import hashlib
import json
import os
import socket
import struct
import time
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime

import pytest
from cryptography.hazmat.decrepit.ciphers.modes import CFB
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

from projecthub.remote import RemoteError, RemoteService
from projecthub.remote.config import RemoteControlConfig

HOST = "127.0.0.1"
API_TOKEN = "local-viewer"
DEVICE_TOKEN = "local-device"
TIMEOUT = 4


async def read(reader, count):
    return await asyncio.wait_for(reader.readexactly(count), TIMEOUT)


class Peer:
    """Independent frp fixture using the documented bytes on the wire."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.key = None
        self.send_cipher = None
        self.recv_cipher = None

    def enable_control_encryption(self):
        self.key = hashlib.pbkdf2_hmac("sha1", DEVICE_TOKEN.encode(), b"frp", 64, 16)

    def cipher(self, iv):
        return Cipher(algorithms.AES(self.key), CFB(iv))

    async def send(self, tag, fields):
        body = json.dumps(fields).encode()
        wire = tag.encode() + len(body).to_bytes(8, "big") + body
        if self.key is not None:
            if self.send_cipher is None:
                iv = os.urandom(16)
                self.writer.write(iv)
                self.send_cipher = self.cipher(iv).encryptor()
            wire = self.send_cipher.update(wire)
        self.writer.write(wire)
        await self.writer.drain()

    async def receive(self):
        if self.key is not None and self.recv_cipher is None:
            iv = await read(self.reader, 16)
            self.recv_cipher = self.cipher(iv).decryptor()
        header = await read(self.reader, 9)
        if self.recv_cipher is not None:
            header = self.recv_cipher.update(header)
        body = await read(self.reader, int.from_bytes(header[1:], "big"))
        if self.recv_cipher is not None:
            body = self.recv_cipher.update(body)
        return chr(header[0]), json.loads(body)


def credentials(token=DEVICE_TOKEN, stamp=None):
    stamp = int(time.time()) if stamp is None else stamp
    digest = hashlib.md5(f"{token}{stamp}".encode()).hexdigest()
    return {"timestamp": stamp, "privilege_key": digest}


@asynccontextmanager
async def connected(port):
    reader, writer = await asyncio.wait_for(asyncio.open_connection(HOST, port), TIMEOUT)
    try:
        yield Peer(reader, writer)
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), TIMEOUT)
        except ConnectionError:
            pass


async def login(peer, device, **fields):
    await peer.send(
        "o",
        {
            "version": "0.68.0",
            "user": device,
            "metas": {"deviceId": device},
            "pool_count": 0,
            **credentials(),
            **fields,
        },
    )
    tag, body = await peer.receive()
    assert tag == "1"
    return body


async def register(peer, device, rfb_port):
    response = await login(peer, device)
    assert response["version"] == "0.68.0"
    peer.enable_control_encryption()
    await peer.send(
        "p",
        {"proxy_name": f"{device}.vnc", "proxy_type": "tcp", "remote_port": rfb_port},
    )
    assert await peer.receive() == (
        "2",
        {"proxy_name": f"{device}.vnc", "remote_addr": f":{rfb_port}"},
    )
    return response["run_id"]


async def start_work(peer, run_id):
    await peer.send("w", {"run_id": run_id, **credentials()})
    return await peer.receive()


@asynccontextmanager
async def running(**overrides):
    config = RemoteControlConfig(
        **{
            "enabled": True,
            "bind_host": HOST,
            "frp_port": 0,
            "rfb_port": 0,
            "api_token": API_TOKEN,
            "frp_token": DEVICE_TOKEN,
            "sweep_seconds": 0.01,
            **overrides,
        }
    )
    service = RemoteService(config)
    try:
        await service.initialize()
        health = service.health()
        assert health["reason"] == "ready"
        yield service, health["frpPort"], health["rfbPort"]
    finally:
        await service.close()


async def wait_until(condition):
    async with asyncio.timeout(TIMEOUT):
        while not condition():
            await asyncio.sleep(0.01)


async def eof(peer):
    try:
        assert await asyncio.wait_for(peer.reader.read(1), TIMEOUT) == b""
    except ConnectionResetError:
        pass


def assert_code(expected, caught):
    assert caught.value.code == expected


def test_protocol_rejects_invalid_credentials_and_bounds_frames():
    async def scenario():
        async with running(timestamp_window_seconds=20) as (service, frp, _):
            for fields in (
                credentials("wrong"),
                credentials(stamp=1),
                {"pool_count": False},
            ):
                async with connected(frp) as peer:
                    assert await login(peer, "rejected", **fields) == {"error": "login rejected"}
                    await eof(peer)
            for packet in (
                struct.pack("!cQ", b"o", 10241),
                b"o" + struct.pack("!Q", 13) + b'{"x":1,"x":2}',
            ):
                async with connected(frp) as peer:
                    peer.writer.write(packet)
                    await peer.writer.drain()
                    await eof(peer)
            assert service.health()["devices"] == 0

    asyncio.run(scenario())


def test_continuous_control_cipher_and_bidirectional_rfb_with_half_close():
    async def scenario():
        async with running() as (service, frp, rfb), connected(frp) as control:
            run_id = await register(control, "alpha", rfb)
            for _ in range(2):
                await control.send("h", credentials())
                assert await control.receive() == ("4", {})
            lease = await service.acquire("alpha", "1" * 32, HOST)
            assert lease["port"] == rfb
            assert lease["host"] == HOST
            async with connected(rfb) as viewer, connected(frp) as work:
                assert await control.receive() == ("r", {})
                viewer.writer.write(b"RFB 003.008\n")
                await viewer.writer.drain()
                assert await start_work(work, run_id) == (
                    "s",
                    {"proxy_name": "alpha.vnc"},
                )
                assert await read(work.reader, 12) == b"RFB 003.008\n"
                pixels = bytes(range(256)) * 512
                work.writer.write(pixels)
                await work.writer.drain()
                assert await read(viewer.reader, len(pixels)) == pixels
                viewer.writer.write_eof()
                await eof(work)
                work.writer.write(b"tail")
                await work.writer.drain()
                work.writer.write_eof()
                assert await read(viewer.reader, 4) == b"tail"
                await eof(viewer)
            await wait_until(lambda: not service.health()["activeSession"])
            assert await service.lookup("1" * 32, HOST) == {"status": "expired"}

    asyncio.run(scenario())


def test_leases_enforce_owners_refresh_expiry_and_preserve_terminal_records():
    async def scenario():
        async with running(lease_seconds=1.1, renew_after_seconds=1, record_capacity=3) as (
            service,
            frp,
            rfb,
        ):
            with pytest.raises(RemoteError) as missing:
                service.authenticate_api(None)
            assert_code("AUTH_REQUIRED", missing)
            with pytest.raises(RemoteError) as bad:
                service.authenticate_api("wrong")
            assert_code("AUTH_FAILED", bad)
            service.authenticate_api(API_TOKEN)
            async with connected(frp) as peer:
                await register(peer, "alpha", rfb)
                first, second, third = ("1" * 32, "2" * 32, "3" * 32)
                lease = await service.acquire("alpha", first, HOST)
                session = lease["sessionId"]
                recovered = await service.acquire("alpha", first, HOST)
                assert recovered["sessionId"] == session
                assert datetime.fromisoformat(recovered["expiresAt"]) >= datetime.fromisoformat(
                    lease["expiresAt"]
                )
                with pytest.raises(RemoteError) as busy:
                    await service.acquire("alpha", second, HOST)
                assert_code("CONTROL_BUSY", busy)
                with pytest.raises(RemoteError) as owner:
                    await service.renew(session, "127.0.0.2")
                assert_code("SESSION_OWNER", owner)
                assert (await service.lookup(first, HOST))["lease"]["sessionId"] == session
                assert (await service.renew(session, HOST))["sessionId"] == session
                await wait_until(lambda: not service.health()["activeSession"])
                assert await service.lookup(first, HOST) == {"status": "expired"}
                with pytest.raises(RemoteError) as expired:
                    await service.renew(session, HOST)
                assert_code("SESSION_TERMINATED", expired)
                await service.cancel(second, HOST)
                assert await service.lookup(second, HOST) == {"status": "cancelled"}
                with pytest.raises(RemoteError) as cancelled:
                    await service.acquire("alpha", second, HOST)
                assert_code("REQUEST_TERMINATED_OR_CHANGED", cancelled)
                next_lease = await service.acquire("alpha", third, HOST)
                await service.release(next_lease["sessionId"], HOST)
                await service.release(next_lease["sessionId"], HOST)
                assert await service.lookup(third, HOST) == {"status": "cancelled"}
                with pytest.raises(RemoteError) as full:
                    await service.cancel("4" * 32, HOST)
                assert_code("REQUEST_CAPACITY", full)

    asyncio.run(scenario())


def test_snapshot_pagination_is_stable_owned_bounded_and_expiring():
    async def scenario():
        async with (
            running(capacity=2, snapshot_capacity=1, snapshot_seconds=0.5) as (
                service,
                frp,
                rfb,
            ),
            connected(frp) as alpha,
        ):
            await register(alpha, "alpha", rfb)
            async with connected(frp) as beta:
                await register(beta, "beta", rfb)
                async with connected(frp) as gamma:
                    assert await login(gamma, "gamma") == {"error": "login rejected"}
                page = service.list_devices(1, None, HOST)
                assert page["total"] == page["capacity"] == 2
                assert [item["deviceId"] for item in page["items"]] == ["alpha"]
                assert page["items"][0]["model"] == "T507 · Linux"
                cursor = page["nextCursor"]
                assert cursor
                with pytest.raises(RemoteError) as capacity:
                    service.list_devices(1, None, HOST)
                assert_code("SNAPSHOT_CAPACITY", capacity)
            await wait_until(lambda: service.health()["devices"] == 1)
            with pytest.raises(RemoteError) as owner:
                service.list_devices(1, cursor, "127.0.0.2")
            assert_code("SNAPSHOT_OWNER", owner)
            second = service.list_devices(1, cursor, HOST)
            assert second["nextCursor"] is None
            assert [item["deviceId"] for item in second["items"]] == ["beta"]
            await asyncio.sleep(0.5)
            with pytest.raises(RemoteError) as expired:
                service.list_devices(1, cursor, HOST)
            assert_code("SNAPSHOT_EXPIRED", expired)

    asyncio.run(scenario())


def test_unfulfilled_work_invalidates_run_before_next_viewer():
    async def scenario():
        async with running() as (service, frp, rfb), AsyncExitStack() as stack:
            old = await stack.enter_async_context(connected(frp))
            old_run = await register(old, "alpha", rfb)
            await service.acquire("alpha", "1" * 32, HOST)
            viewer = await stack.enter_async_context(connected(rfb))
            assert await old.receive() == ("r", {})
            await service.cancel("1" * 32, HOST)
            await eof(viewer)
            await eof(old)
            assert service.health()["devices"] == 0
            current = await stack.enter_async_context(connected(frp))
            current_run = await register(current, "alpha", rfb)
            assert current_run != old_run
            await service.acquire("alpha", "2" * 32, HOST)
            current_viewer = await stack.enter_async_context(connected(rfb))
            assert await current.receive() == ("r", {})
            async with connected(frp) as late:
                assert await start_work(late, old_run) == (
                    "s",
                    {"error": "work rejected"},
                )
            async with connected(frp) as work:
                assert await start_work(work, current_run) == (
                    "s",
                    {"proxy_name": "alpha.vnc"},
                )
                work.writer.write(b"active")
                await work.writer.drain()
                assert await read(current_viewer.reader, 6) == b"active"

    asyncio.run(scenario())


def test_viewer_owner_and_single_viewer_admission():
    async def scenario():
        async with running() as (service, frp, rfb), connected(frp) as control:
            await register(control, "alpha", rfb)
            await service.acquire("alpha", "1" * 32, "127.0.0.2")
            async with connected(rfb) as refused:
                await eof(refused)
            assert service.health()["activeSession"]
            await service.cancel("1" * 32, "127.0.0.2")
            await service.acquire("alpha", "2" * 32, HOST)
            async with connected(rfb) as viewer:
                assert await control.receive() == ("r", {})
                async with connected(rfb) as duplicate:
                    await eof(duplicate)
                assert not viewer.reader.at_eof()

    asyncio.run(scenario())


def test_partial_startup_failure_rolls_back_and_retry_can_bind():
    async def scenario():
        with socket.socket() as busy:
            busy.bind((HOST, 0))
            busy.listen()
            blocked = busy.getsockname()[1]
            with socket.socket() as reservation:
                reservation.bind((HOST, 0))
                frp_port = reservation.getsockname()[1]
            service = RemoteService(
                RemoteControlConfig(
                    enabled=True,
                    bind_host=HOST,
                    frp_port=frp_port,
                    rfb_port=blocked,
                    api_token=API_TOKEN,
                    frp_token=DEVICE_TOKEN,
                )
            )
            await service.initialize()
            assert service.health()["reason"] == "startup_failed"
            assert service.health()["frpPort"] is None
            with socket.socket() as reuse:
                reuse.bind((HOST, frp_port))
        try:
            await service.initialize()
            assert service.health()["ready"]
        finally:
            await service.close()

    asyncio.run(scenario())


def test_close_completes_after_caller_cancellation_and_restart_has_empty_state():
    async def scenario():
        async with running() as (service, frp, rfb), connected(frp) as control:
            await register(control, "alpha", rfb)
            await service.acquire("alpha", "1" * 32, HOST)
            async with connected(rfb) as viewer:
                assert await control.receive() == ("r", {})
                closing = asyncio.create_task(service.close())
                await asyncio.sleep(0)
                closing.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(closing, TIMEOUT)
                assert service.health()["reason"] == "stopped"
                assert service.health()["connections"] == 0
                assert service.health()["requestRecords"] == 0
                await eof(viewer)
                await eof(control)
            await service.initialize()
            assert service.health()["ready"]
            assert service.list_devices(100, None, HOST)["items"] == []

    asyncio.run(scenario())


def test_pending_handshake_capacity_is_reclaimed_on_timeout():
    async def scenario():
        async with running(pending_capacity=1, handshake_seconds=0.4) as (
            service,
            frp,
            rfb,
        ):
            async with connected(frp) as idle:
                await wait_until(lambda: service.health()["connections"] == 1)
                async with connected(frp) as refused:
                    await eof(refused)
                await eof(idle)
            await wait_until(lambda: service.health()["connections"] == 0)
            async with connected(frp) as device:
                await register(device, "alpha", rfb)
                assert service.health()["devices"] == 1

    asyncio.run(scenario())


def test_heartbeat_timeout_closes_paired_channel_and_expires_lease():
    async def scenario():
        async with (
            running(heartbeat_seconds=0.5) as (service, frp, rfb),
            connected(frp) as control,
        ):
            run_id = await register(control, "alpha", rfb)
            request_id = "1" * 32
            await service.acquire("alpha", request_id, HOST)
            async with connected(rfb) as viewer, connected(frp) as work:
                assert await control.receive() == ("r", {})
                await work.send("w", {"run_id": run_id, **credentials("bad")})
                assert await work.receive() == ("s", {"error": "work rejected"})
                async with connected(frp) as paired:
                    assert await start_work(paired, run_id) == (
                        "s",
                        {"proxy_name": "alpha.vnc"},
                    )
                    await eof(control)
                    await eof(viewer)
                    await eof(paired)
                assert await service.lookup(request_id, HOST) == {"status": "expired"}
                assert service.health()["devices"] == 0

    asyncio.run(scenario())


def test_stream_cleanup_reports_abort_without_close_notification(monkeypatch):
    from projecthub.remote import streams

    class StalledClose:
        def __init__(self):
            self.transport = self
            self.aborted = False
            self.notification = asyncio.Event()

        def close(self):
            pass

        def abort(self):
            self.aborted = True

        async def wait_closed(self):
            await self.notification.wait()

    async def scenario():
        writer = StalledClose()
        monkeypatch.setattr(streams, "_CLOSE_SECONDS", 0.01)
        try:
            with pytest.raises(TimeoutError):
                await streams.close_stream(writer)
            assert writer.aborted
        finally:
            writer.notification.set()

    asyncio.run(scenario())
