"""Stand-in for HA core in the WireGuard rig: what the tunnel forward reaches.

Answers on :8123 under the network alias `homeassistant`, like HA core does for
an add-on. `/api/` reports the peer address it saw, so a test can show HA sees
the add-on, not the tunnel client. `/api/websocket` echoes text frames.
"""

from aiohttp import WSMsgType, web


async def api(request: web.Request) -> web.Response:
    return web.json_response({"message": "API running.", "remote": request.remote})


async def websocket(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            await ws.send_str(f"echo:{msg.data}")
    return ws


app = web.Application()
app.router.add_get("/api/", api)
app.router.add_get("/api/websocket", websocket)
web.run_app(app, port=8123)
