"""Glue between the route handlers and a running ComfyUI + Impact Pack."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .routes import TargetUnavailable

log = logging.getLogger("comfyui_wildcard_sync")

DIR_ENV = "WILDCARD_SYNC_DIR"


def get_target_dir() -> Path:
    """The folder to manage: $WILDCARD_SYNC_DIR, else Impact Pack's custom_wildcards."""
    override = os.environ.get(DIR_ENV)
    if override:
        return Path(override)
    try:
        import impact.config  # put on sys.path by ComfyUI-Impact-Pack
    except ImportError as e:
        raise TargetUnavailable(
            f"ComfyUI-Impact-Pack not loaded and {DIR_ENV} not set"
        ) from e
    return Path(impact.config.get_config()["custom_wildcards"]).resolve()


def refresh_wildcards() -> None:
    """Reload Impact Pack's wildcard cache (same as GET /impact/wildcards/refresh)."""
    try:
        import impact.wildcards
    except ImportError:
        log.warning("[wildcard_sync] Impact Pack not loaded; skipping wildcard refresh")
        return
    impact.wildcards.wildcard_load()


def register() -> None:
    from server import PromptServer

    from .routes import build_routes

    build_routes(get_target_dir, refresh_wildcards, routes=PromptServer.instance.routes)
    log.info("[wildcard_sync] routes registered: GET /wildcard_sync/hash, PUT /wildcard_sync")
