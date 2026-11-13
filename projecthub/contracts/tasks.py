"""跨模块的任务完成契约：先完成工作，再传播等待者取消。"""

import asyncio


async def finish_task(task: asyncio.Task):
    """不取消已交出的工作；工作自身的失败优先于等待者取消。"""
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = error
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result


async def run_blocking(operation, *args, **kwargs):
    """线程操作实际完成后才能交还控制权，避免资源清理与工作交错。"""
    return await finish_task(asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs)))
