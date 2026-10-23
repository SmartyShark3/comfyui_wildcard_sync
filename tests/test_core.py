import json
from pathlib import Path

import pytest

from wildcard_sync import core
from wildcard_sync.core import ValidationError, WildcardFile


def payload(*files, **extra):
    return {"files": [{"path": p, "content": c} for p, c in files], **extra}


# ---------- path validation ----------

@pytest.mark.parametrize("path", ["a.txt", "hairstyles/short.txt", "My List-1_x.txt", "a/b/c.v2.txt"])
def test_valid_paths(path):
    assert core.validate_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "", "../a.txt", "a/../b.txt", "./a.txt", "/a.txt", "C:/a.txt", "C:a.txt",
        "a\\b.txt", "a//b.txt", "a.py", "a.yaml", "a", "a.txt/", ".hidden.txt",
        "con.txt", "dir/NUL.txt", "a .txt/b.txt", "trailing./x.txt", "x" * 300 + ".txt",
        "/".join(["d"] * 9) + ".txt", "a*b.txt", "a<b.txt",
    ],
)
def test_invalid_paths(path):
    with pytest.raises(ValidationError):
        core.validate_path(path)


def test_non_string_path():
    with pytest.raises(ValidationError):
        core.validate_path(None)


# ---------- payload validation ----------

def test_payload_ok():
    files = core.validate_payload(payload(("a.txt", "red\nblue\n"), ("d/b.txt", "x")))
    assert files == [WildcardFile("a.txt", b"red\nblue\n"), WildcardFile("d/b.txt", b"x")]


@pytest.mark.parametrize(
    "body",
    [
        None, [], {}, {"files": "nope"}, {"files": [1]}, {"files": [{"path": "a.txt"}]},
        {"files": [{"path": "a.txt", "content": 5}]},
    ],
)
def test_payload_shape_rejected(body):
    with pytest.raises(ValidationError):
        core.validate_payload(body)


def test_duplicate_paths_case_insensitive():
    with pytest.raises(ValidationError, match="duplicate"):
        core.validate_payload(payload(("A.txt", "1"), ("a.txt", "2")))


def test_file_folder_clash():
    with pytest.raises(ValidationError, match="both file and folder"):
        core.validate_payload(payload(("a.txt", "1"), ("a.txt/b.txt", "2")))


def test_too_many_files(monkeypatch):
    monkeypatch.setattr(core, "MAX_FILES", 2)
    with pytest.raises(ValidationError, match="too many"):
        core.validate_payload(payload(("a.txt", ""), ("b.txt", ""), ("c.txt", "")))


def test_file_too_large(monkeypatch):
    monkeypatch.setattr(core, "MAX_FILE_BYTES", 3)
    with pytest.raises(ValidationError, match="too large"):
        core.validate_payload(payload(("a.txt", "abcd")))


def test_total_too_large(monkeypatch):
    monkeypatch.setattr(core, "MAX_TOTAL_BYTES", 5)
    with pytest.raises(ValidationError, match="too large"):
        core.validate_payload(payload(("a.txt", "abc"), ("b.txt", "abc")))


def test_hash_check():
    good = core.compute_hash([WildcardFile("a.txt", b"x")])
    core.validate_payload(payload(("a.txt", "x"), hash=good.upper()))
    with pytest.raises(ValidationError, match="hash"):
        core.validate_payload(payload(("a.txt", "y"), hash=good))


# ---------- hashing ----------

def test_hash_is_order_independent():
    a, b = WildcardFile("a.txt", b"1"), WildcardFile("b/c.txt", b"2")
    assert core.compute_hash([a, b]) == core.compute_hash([b, a])


def test_hash_known_values():
    """Pinned values: clients (video_producer) must produce the same digests."""
    import hashlib

    assert core.compute_hash([]) == hashlib.sha256(b"").hexdigest()
    expected = hashlib.sha256(b"a.txt\0red\nblue\n\0d/b.txt\0x\0").hexdigest()
    files = [WildcardFile("d/b.txt", b"x"), WildcardFile("a.txt", b"red\nblue\n")]
    assert core.compute_hash(files) == expected


def test_hash_distinguishes_path_and_content_boundaries():
    assert core.compute_hash([WildcardFile("ab.txt", b"c")]) != core.compute_hash(
        [WildcardFile("a.txt", b"bc")]
    )


# ---------- folder replace ----------

def write(root: Path, rel: str, data: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode())


def test_replace_creates_missing_folder(tmp_path):
    root = tmp_path / "custom_wildcards"
    core.replace_folder(root, [WildcardFile("a.txt", b"1"), WildcardFile("d/e/b.txt", b"2")])
    assert (root / "a.txt").read_bytes() == b"1"
    assert (root / "d" / "e" / "b.txt").read_bytes() == b"2"


def test_replace_removes_stale_and_keeps_unmanaged(tmp_path):
    root = tmp_path / "custom_wildcards"
    write(root, "old.txt", "stale")
    write(root, "sub/old2.txt", "stale")
    write(root, "put_wildcards_here", "")
    write(root, "sub/notes.yaml", "k: v")

    files = [WildcardFile("new.txt", b"fresh")]
    core.replace_folder(root, files)

    assert not (root / "old.txt").exists()
    assert not (root / "sub" / "old2.txt").exists()
    assert (root / "put_wildcards_here").exists()
    assert (root / "sub" / "notes.yaml").read_text() == "k: v"
    assert (root / "new.txt").read_bytes() == b"fresh"
    assert core.folder_hash(root) == (core.compute_hash(files), 1)
    assert [p.name for p in tmp_path.iterdir()] == ["custom_wildcards"]  # no temp leftovers


def test_replace_with_empty_set(tmp_path):
    root = tmp_path / "w"
    write(root, "a.txt", "x")
    core.replace_folder(root, [])
    assert root.is_dir() and core.folder_hash(root)[1] == 0


def test_replace_writes_bytes_exactly(tmp_path):
    root = tmp_path / "w"
    core.replace_folder(root, [WildcardFile("a.txt", "é\r\nb".encode())])
    assert (root / "a.txt").read_bytes() == "é\r\nb".encode()


def test_replace_rolls_back_when_swap_fails(tmp_path, monkeypatch):
    root = tmp_path / "w"
    write(root, "keep.txt", "original")
    real = core._rename_with_retry
    calls = []

    def flaky(src, dst, **kw):
        calls.append((src, dst))
        if len(calls) == 2:  # moving the new tree into place
            raise PermissionError("locked")
        return real(src, dst, **kw)

    monkeypatch.setattr(core, "_rename_with_retry", flaky)
    with pytest.raises(PermissionError):
        core.replace_folder(root, [WildcardFile("new.txt", b"x")])

    assert (root / "keep.txt").read_text() == "original"
    assert not (root / "new.txt").exists()
    assert [p.name for p in tmp_path.iterdir()] == ["w"]


def test_folder_hash_missing_dir(tmp_path):
    assert core.folder_hash(tmp_path / "nope") == (core.compute_hash([]), 0)


# ---------- token ----------

@pytest.mark.parametrize(
    "expected,provided,ok",
    [(None, None, True), ("", "x", True), ("s3cret", "s3cret", True), ("s3cret", None, False), ("s3cret", "nope", False)],
)
def test_token_matches(expected, provided, ok):
    assert core.token_matches(expected, provided) is ok
