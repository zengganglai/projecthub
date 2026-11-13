"""TCP 流的有界关闭和双向转发。"""

import asyncio

from .config import RemoteControlConfig

_CHUNK_BYTES = 32768
_CLOSE_SECONDS = 1


async def close_stream(writer: asyncio.StreamWriter) -> None:
    writer.close()
    closed = asyncio.create_task(writer.wait_closed())
    try:
        async with asyncio.timeout(_CLOSE_SECONDS):
            await asyncio.shield(closed)
    except TimeoutError:
        writer.transport.abort()
        try:
            async with asyncio.timeout(_CLOSE_SECONDS):
                await asyncio.shield(closed)
        except ConnectionError:
            pass
    except OSError:
        writer.transport.abort()


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, seconds: float) -> None:
    while data := await reader.read(_CHUNK_BYTES):
        writer.write(data)
        async with asyncio.timeout(seconds):
            await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()
        async with asyncio.timeout(seconds):
            await writer.drain()


async def relay(
    viewer: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    work: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    config: RemoteControlConfig,
) -> None:
    """按块转发。单向结束后另一侧可以在有限时间内发送尾包。"""
    for writer in (viewer[1], work[1]):
        writer.transport.set_write_buffer_limits(high=_CHUNK_BYTES * 2, low=_CHUNK_BYTES)
    pumps = (
        asyncio.create_task(_copy(viewer[0], work[1], config.write_seconds)),
        asyncio.create_task(_copy(work[0], viewer[1], config.write_seconds)),
    )
    try:
        completed, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for task in completed:
            task.result()
        if pending:
            async with asyncio.timeout(config.half_close_seconds):
                await asyncio.gather(*pending)
    finally:
        for task in pumps:
            task.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
