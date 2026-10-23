"""Pure (ComfyUI-independent) logic for comfyui_wildcard_sync.

Everything here can be unit tested without ComfyUI or Impact Pack installed.

Hash spec (must be reproduced exactly by any client, e.g. video_producer):
    sha256 over every managed file, sorted by relative POSIX path, of
        utf8(path) + b"\\0" + content_bytes + b"\\0"
    hex-encoded. An empty set hashes to sha256(b"").
Only ``.txt`` files are managed; anything else in the folder is ignored by the
hash and preserved across a sync.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

MANAGED_SUFFIX = ".txt"
MAX_FILES = 2000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_PATH_LENGTH = 200
MAX_DEPTH = 8

# A path segment: starts with an alphanumeric/underscore/hyphen, then letters,
# digits, underscore, hyphen, space or dot. Must not end with a dot or space
# (Windows silently strips those).
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_\-][A-Za-z0-9_\-. ]*$")
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class ValidationError(ValueError):
    """Raised when a sync payload is rejected. Message is safe to return to clients."""


@dataclass(frozen=True)
class WildcardFile:
    path: str  # relative POSIX path, e.g. "hairstyles/short.txt"
    content: bytes


def validate_path(path: object) -> str:
    if not isinstance(path, str) or not path:
        raise ValidationError("path must be a non-empty string")
    if len(path) > MAX_PATH_LENGTH:
        raise ValidationError(f"path too long: {path[:50]}...")
    if "\\" in path or path.startswith("/") or ":" in path:
        raise ValidationError(f"path must be relative and use '/': {path}")

    segments = path.split("/")
    if len(segments) > MAX_DEPTH:
        raise ValidationError(f"path too deep: {path}")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise ValidationError(f"invalid path segment in: {path}")
        if (
            not _SEGMENT_RE.match(segment)
            or segment.endswith((".", " "))
            or " ." in segment
        ):
            raise ValidationError(f"invalid characters in path: {path}")
        if segment.split(".")[0].lower() in _WINDOWS_RESERVED:
            raise ValidationError(f"reserved name in path: {path}")

    if not segments[-1].endswith(MANAGED_SUFFIX):
        raise ValidationError(f"only {MANAGED_SUFFIX} files are allowed: {path}")
    return path


def validate_payload(payload: object) -> list[WildcardFile]:
    """Validate a PUT body and return the files it describes."""
    if not isinstance(payload, dict):
        raise ValidationError("body must be a JSON object")
    raw_files = payload.get("files")
    if not isinstance(raw_files, list):
        raise ValidationError("'files' must be a list")
    if len(raw_files) > MAX_FILES:
        raise ValidationError(f"too many files (max {MAX_FILES})")

    files: list[WildcardFile] = []
    seen: set[str] = set()
    total = 0
    for entry in raw_files:
        if not isinstance(entry, dict):
            raise ValidationError("each file must be an object with 'path' and 'content'")
        path = validate_path(entry.get("path"))
        content = entry.get("content")
        if not isinstance(content, str):
            raise ValidationError(f"content must be a string: {path}")

        key = path.lower()  # Windows / macOS filesystems are case-insensitive
        if key in seen:
            raise ValidationError(f"duplicate path: {path}")
        seen.add(key)

        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise ValidationError(f"file too large: {path}")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ValidationError("payload too large")
        files.append(WildcardFile(path, data))

    _reject_file_folder_clashes(seen)

    expected = payload.get("hash")
    if expected is not None:
        if not isinstance(expected, str):
            raise ValidationError("'hash' must be a string")
        actual = compute_hash(files)
        if expected.lower() != actual:
            raise ValidationError("hash does not match files (corrupted upload?)")
    return files


def _reject_file_folder_clashes(lower_paths: set[str]) -> None:
    """A path cannot be both a file and a folder (e.g. 'a.txt' and 'a.txt/b.txt')."""
    for path in lower_paths:
        parts = path.split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in lower_paths:
                raise ValidationError(f"path used as both file and folder: {path}")


def compute_hash(files: Iterable[WildcardFile]) -> str:
    digest = hashlib.sha256()
    for f in sorted(files, key=lambda f: f.path):
        digest.update(f.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(f.content)
        digest.update(b"\0")
    return digest.hexdigest()


def read_managed_files(root: Path) -> list[WildcardFile]:
    """All managed (.txt) files under root, as relative POSIX paths."""
    if not root.is_dir():
        return []
    files = []
    for p in root.rglob(f"*{MANAGED_SUFFIX}"):
        if p.is_file():
            files.append(WildcardFile(p.relative_to(root).as_posix(), p.read_bytes()))
    return files


def folder_hash(root: Path) -> tuple[str, int]:
    files = read_managed_files(root)
    return compute_hash(files), len(files)


def replace_folder(root: Path, files: list[WildcardFile]) -> None:
    """Replace every managed file under ``root`` with ``files``, in one swap.

    The new tree is built in a sibling temp directory (non-.txt files from the
    old tree are copied across so they survive), then swapped in with two
    renames. If the swap fails the old folder is restored.
    """
    root = Path(root)
    parent = root.parent
    parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:8]
    tmp = parent / f".{root.name}.wcsync-new-{token}"
    old = parent / f".{root.name}.wcsync-old-{token}"

    try:
        tmp.mkdir()
        if root.is_dir():
            _copy_unmanaged(root, tmp)
        for f in files:
            target = tmp / Path(*f.path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f.content)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    had_root = root.exists()
    try:
        if had_root:
            _rename_with_retry(root, old)
        try:
            _rename_with_retry(tmp, root)
        except BaseException:
            if had_root:
                _rename_with_retry(old, root)
            raise
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    shutil.rmtree(old, ignore_errors=True)


def _copy_unmanaged(src: Path, dst: Path) -> None:
    for p in src.rglob("*"):
        if p.is_file() and not p.name.endswith(MANAGED_SUFFIX):
            target = dst / p.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)


def _rename_with_retry(src: Path, dst: Path, attempts: int = 10, delay: float = 0.1) -> None:
    # On Windows a rename fails while another process (e.g. Impact Pack reading
    # wildcards, an antivirus scan) holds a handle inside the folder.
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


def token_matches(expected: str | None, provided: str | None) -> bool:
    """True if no token is configured, or the provided token matches."""
    import hmac

    if not expected:
        return True
    if not provided:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))
