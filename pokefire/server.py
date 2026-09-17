"""Uvicorn transport for the dashboard and its managed monitor."""

import argparse
import asyncio
from email.message import Message
from io import BytesIO
import os
from pathlib import Path

import uvicorn

from pokefire.env_config import load_env
from pokefire.viewer import Controller, request_handler_for


class Application:
    def __init__(self, controller, data_dir, *, host="127.0.0.1", start_monitor=True):
        self.controller = controller
        self.start_monitor = start_monitor
        self.handler = request_handler_for(
            data_dir, controller, allowed_hosts={"localhost", "127.0.0.1", host})

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    try:
                        if self.start_monitor:
                            await asyncio.to_thread(self.controller.start, "ebay")
                    except Exception as exc:
                        await asyncio.to_thread(self.controller.close)
                        await send({"type": "lifespan.startup.failed", "message": str(exc)})
                        return
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    await asyncio.to_thread(self.controller.close)
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return
        if scope["type"] != "http":
            return
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            body.extend(event.get("body", b""))
            if len(body) > 256000:
                await send({"type": "http.response.start", "status": 413, "headers": []})
                await send({"type": "http.response.body", "body": b"Request too large"})
                return
            if not event.get("more_body", False):
                break
        status, headers, response = await asyncio.to_thread(self.dispatch, scope, body)
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": response})

    def dispatch(self, scope, body):
        # Reuse the same routes and response protections in the dashboard.
        handler = self.handler()
        handler.headers = Message()
        for key, value in scope["headers"]:
            handler.headers[key.decode("latin-1")] = value.decode("latin-1")
        if "Content-Length" not in handler.headers:
            handler.headers["Content-Length"] = str(len(body))
        handler.path = scope["path"]
        if scope.get("query_string"):
            handler.path += "?" + scope["query_string"].decode("ascii")
        handler.rfile = BytesIO(body)
        if scope["method"] in ("GET", "POST"):
            getattr(handler, "do_" + scope["method"])()
        else:
            handler.reply(405, {"error": "Method not allowed"})
        return handler.response


def main():
    root = Path.cwd()
    parser = argparse.ArgumentParser(description="Run Pokefire with Uvicorn; Ctrl+C stops the server and monitor.")
    parser.add_argument("--host", help="Bind address (default: HOST or 127.0.0.1); use your LAN IP for network access")
    parser.add_argument("--port", type=int, help="Port (default: PORT or 8765)")
    parser.add_argument("--config", type=Path, default=root / "watchlist.json")
    parser.add_argument("--data-dir", type=Path, default=root / "data")
    parser.add_argument("--no-monitor", action="store_true", help="Start the dashboard with polling paused")
    args = parser.parse_args()
    try:
        load_env(root / ".env")
        port = args.port if args.port is not None else int(os.environ.get("PORT", "8765"))
        if not 1 <= port <= 65535:
            raise ValueError("PORT must be an integer between 1 and 65535")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    host = args.host or os.environ.get("HOST", "127.0.0.1")
    if host in ("0.0.0.0", "::"):
        parser.error("Use the machine's specific LAN IP with --host (for example 192.168.0.239)")
    app = Application(Controller(args.config), args.data_dir, host=host, start_monitor=not args.no_monitor)
    uvicorn.run(app, host=host, port=port, lifespan="on", workers=1, ws="none")
