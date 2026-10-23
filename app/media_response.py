"""Stop media disk reads when a browser abandons playback or seeks elsewhere."""
import anyio
from starlette.responses import FileResponse


class _PlaybackDisconnected(Exception):
    pass


class MediaFileResponse(FileResponse):
    """Keep FileResponse's Range support and release abandoned file streams.

    Uvicorn may silently ignore sends after disconnect. FileResponse otherwise
    keeps reading the remaining file, so canceled seeks can accumulate disk work.
    Raise at the next send boundary instead of canceling an open-file context;
    its ordinary exception cleanup can then finish closing the file.
    """

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await super().__call__(scope, receive, send)
        disconnected = anyio.Event()

        async def watch_disconnect():
            while True:
                if (await receive())['type'] == 'http.disconnect':
                    disconnected.set()
                    return

        async def send_connected(message):
            if disconnected.is_set():
                raise _PlaybackDisconnected()
            await send(message)
            if disconnected.is_set():
                raise _PlaybackDisconnected()

        async with anyio.create_task_group() as group:
            group.start_soon(watch_disconnect)
            try:
                await super().__call__(scope, receive, send_connected)
            except _PlaybackDisconnected:
                pass
            finally:
                group.cancel_scope.cancel()
