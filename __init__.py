"""
@title: Wildcard Sync
@nickname: Wildcard Sync
@description: HTTP endpoints to replace Impact Pack wildcard files from a remote app (e.g. video_producer).
"""

import logging

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

try:
    from .wildcard_sync.comfy import register

    register()
except Exception:  # never break ComfyUI startup because of this extension
    logging.exception("[wildcard_sync] failed to register routes")

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
