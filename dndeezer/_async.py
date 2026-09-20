"""Cancellation-safe offloading for blocking operations."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


async def run_blocking(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Drain a worker operation before propagating even repeated cancellation.

    Cancelling to_thread does not stop its thread. Waiting prevents filesystem
    cleanup from racing an outstanding write or directory creation.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except Exception:  # noqa: BLE001 - task.result below propagates worker errors
            break
    if cancelled is not None:
        if not task.cancelled():
            task.exception()
        raise cancelled
    return task.result()
