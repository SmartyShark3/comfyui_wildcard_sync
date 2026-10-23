"""aiohttp route handlers. Built by a factory so they can be tested without ComfyUI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Callable, Optional

from aiohttp import web

from . import core

log = logging.getLogger("comfyui_wildcard_sync")

TOKEN_ENV = "VP_WILDCARD_TOKEN"
TOKEN_HEADER = "X-VP-Token"
VERSION = "0.1.0"

# Room for JSON escaping overhead on top of the raw content limit.
MAX_BODY_BYTES = core.MAX_TOTAL_BYTES * 2 + 1024 * 1024


class TargetUnavailable(RuntimeError):
    pass


def build_routes(
    get_target_dir: Callable[[], Path],
    refresh: Callable[[], None],
    get_token: Callable[[], Optional[str]] = lambda: os.environ.get(TOKEN_ENV),
    routes: Optional[web.RouteTableDef] = None,
) -> web.RouteTableDef:
    if routes is None:
        routes = web.RouteTableDef()
    lock = asyncio.Lock()

    def check_token(request: web.Request) -> Optional[web.Response]:
        if core.token_matches(get_token(), request.headers.get(TOKEN_HEADER)):
            return None
        return web.json_response({"error": "invalid or missing token"}, status=401)

    def resolve_target() -> Path | web.Response:
        try:
            return Path(get_target_dir())
        except TargetUnavailable as e:
            return web.json_response({"error": str(e)}, status=503)

    @routes.get("/wildcard_sync/hash")
    async def get_hash(request: web.Request) -> web.Response:
        if (denied := check_token(request)) is not None:
            return denied
        target = resolve_target()
        if isinstance(target, web.Response):
            return target
        digest, count = await asyncio.to_thread(core.folder_hash, target)
        return web.json_response({"hash": digest, "files": count, "version": VERSION})

    @routes.put("/wildcard_sync")
    async def put_files(request: web.Request) -> web.Response:
        if (denied := check_token(request)) is not None:
            return denied
        target = resolve_target()
        if isinstance(target, web.Response):
            return target

        if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
            return web.json_response({"error": "payload too large"}, status=413)
        body = await request.content.read(MAX_BODY_BYTES + 1)
        if len(body) > MAX_BODY_BYTES:
            return web.json_response({"error": "payload too large"}, status=413)

        try:
            payload = json.loads(body.decode("utf-8"))
            files = core.validate_payload(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return web.json_response({"error": "body must be UTF-8 JSON"}, status=400)
        except core.ValidationError as e:
            return web.json_response({"error": str(e)}, status=400)

        async with lock:
            try:
                await asyncio.to_thread(core.replace_folder, target, files)
            except OSError as e:
                log.exception("[wildcard_sync] failed to write %s", target)
                return web.json_response({"error": f"write failed: {e.strerror or e}"}, status=500)

            refreshed = True
            try:
                await asyncio.to_thread(refresh)
            except Exception:
                refreshed = False
                log.exception("[wildcard_sync] files written but wildcard refresh failed")

        log.info("[wildcard_sync] synced %d wildcard files to %s", len(files), target)
        return web.json_response(
            {"hash": core.compute_hash(files), "files": len(files), "refreshed": refreshed}
        )

    return routes
