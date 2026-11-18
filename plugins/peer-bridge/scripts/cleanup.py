"""Cleanup that a cancellation cannot cut short."""

import asyncio


async def run_to_completion(cleanup):
    """Await a cleanup step to its end even if the caller is cancelled meanwhile, then pass the cancellation on.

    A signal cancels the CLI task once per delivery, and anyio, which serves MCP requests, cancels a cancelled request
    again at every await; neither may cut short stopping the peer, removing its checkout or cancelling a job.
    """
    import anyio  # installed with mcp

    task = asyncio.ensure_future(cleanup)
    cancellation = None
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                # Keep the first: anyio recognises its own cancellation by the message.
                if cancellation is None:
                    cancellation = exc
            except Exception:
                pass  # the cleanup failed: its error is raised below, unless a cancellation came first
    if cancellation is not None:
        raise cancellation
    return task.result()
