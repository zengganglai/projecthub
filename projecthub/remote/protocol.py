"""frp 0.68.0 的帧、令牌认证及控制连接加密。"""

import asyncio
import hashlib
import hmac
import json
import secrets
import struct
import time

from projecthub.contracts import decode_json

VERSION = "0.68.0"
LOGIN = b"o"
LOGIN_RESPONSE = b"1"
PROXY = b"p"
PROXY_RESPONSE = b"2"
PING = b"h"
PONG = b"4"
WORK_REQUEST = b"r"
WORK_CONNECTION = b"w"
WORK_RESPONSE = b"s"

_HEADER = struct.Struct("!cQ")
_MAX_PAYLOAD = 10240
_IV_LENGTH = 16


class ProtocolError(Exception):
    """数据不符合远控使用的 frp 协议子集。"""


def valid_signature(fields: dict, token: str, window: int) -> bool:
    stamp = fields.get("timestamp")
    key = fields.get("privilege_key")
    if type(stamp) is not int or not isinstance(key, str) or len(key) != 32 or not key.isascii():
        return False
    if window:
        try:
            if abs(stamp - time.time()) > window:
                return False
        except OverflowError:
            return False
    digest = hashlib.md5(f"{token}{stamp}".encode("ascii"), usedforsecurity=False).hexdigest()
    return hmac.compare_digest(key, digest)


def control_cipher(token: str):
    """延迟加载可选密码库，返回以会话方向的 IV 建立 CFB 的工厂。"""
    from cryptography.hazmat.decrepit.ciphers.modes import CFB
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

    key = hashlib.pbkdf2_hmac("sha1", token.encode("ascii"), b"frp", 64, 16)

    def cipher(iv: bytes):
        return Cipher(algorithms.AES(key), CFB(iv))

    return cipher


class Wire:
    """每条连接维护两个独立且连续的 CFB 上下文。"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._cipher = None
        self._outbound = None
        self._inbound = None
        self._send_gate = asyncio.Lock()

    def start_control_encryption(self, cipher) -> None:
        self._cipher = cipher

    async def _take(self, size: int) -> bytes:
        chunk = await self._reader.readexactly(size)
        return chunk if self._inbound is None else self._inbound.update(chunk)

    async def read(self, seconds: float) -> tuple[bytes, dict]:
        async with asyncio.timeout(seconds):
            if self._cipher is not None and self._inbound is None:
                iv = await self._reader.readexactly(_IV_LENGTH)
                self._inbound = self._cipher(iv).decryptor()
            kind, size = _HEADER.unpack(await self._take(_HEADER.size))
            if not 0 < size <= _MAX_PAYLOAD:
                raise ProtocolError("帧长度无效。")
            payload = await self._take(size)

        try:
            body = decode_json(payload)
        except ValueError, RecursionError:
            raise ProtocolError("帧正文不是合法 JSON。") from None
        if not isinstance(body, dict):
            raise ProtocolError("帧正文必须为 JSON 对象。")
        return kind, body

    async def write(self, kind: bytes, body: dict, seconds: float) -> None:
        payload = json.dumps(body, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if not 0 < len(payload) <= _MAX_PAYLOAD:
            raise ProtocolError("发送帧长度无效。")
        frame = _HEADER.pack(kind, len(payload)) + payload

        async with asyncio.timeout(seconds), self._send_gate:
            if self._cipher is not None:
                if self._outbound is None:
                    iv = secrets.token_bytes(_IV_LENGTH)
                    self._outbound = self._cipher(iv).encryptor()
                    self._writer.write(iv)
                frame = self._outbound.update(frame)
            self._writer.write(frame)
            await self._writer.drain()
