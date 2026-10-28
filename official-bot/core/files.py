"""Atomic files and bounded cross-process locks for local runtime data."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()
_held = threading.local()
if os.name != "nt":
    try:
        os.umask(0)
    except OSError:
        pass


def ensure_shared_perms(path: Path | str, *, is_dir: bool = False) -> None:
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o777 if is_dir else 0o666)
    except OSError:
        pass


def confined_path(root: Path, relative: str | Path) -> Path:
    base = Path(root).resolve()
    candidate = Path(relative)
    if candidate.is_absolute():
        raise ValueError("Expected a relative managed path")
    result = (base / candidate).resolve()
    if not result.is_relative_to(base):
        raise ValueError("Path escapes its managed directory")
    return result


@contextmanager
def file_lock(path: Path | str, timeout: float = 30):
    """Serialize threads and processes; reentrant for the owning thread."""
    path = Path(path).resolve()
    key = str(path)
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    if not lock.acquire(timeout=timeout):
        raise TimeoutError("Timed out waiting for local state lock")
    held = getattr(_held, "paths", set())
    _held.paths = held
    handle = None
    acquired = False
    try:
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        ensure_shared_perms(path.parent, is_dir=True)
        handle = path.open("a+b")
        ensure_shared_perms(path)
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
        else:
            import fcntl
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for runtime state lock")
                time.sleep(0.025)
        held.add(key)
        yield
    finally:
        if acquired:
            held.discard(key)
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
        if handle is not None:
            handle.close()
        lock.release()


def atomic_write_bytes(path: Path | str, content: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        ensure_shared_perms(temporary)
        os.replace(temporary, path)
        ensure_shared_perms(path)
        if os.name != "nt" and hasattr(os, "O_DIRECTORY"):
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write_text(path: Path | str, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path | str, obj: object) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def append_jsonl(path: Path | str, record: dict) -> None:
    path = Path(path)
    with file_lock(path.with_name(path.name + ".lock")):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
        ensure_shared_perms(path)


def tail_jsonl(path: Path | str, count: int = 200, max_bytes: int = 1024 * 1024) -> list[dict]:
    """Bound both disk reads and parsed history; tolerate torn trailing records."""
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            offset = max(0, stream.tell() - max_bytes)
            stream.seek(offset)
            content = stream.read(max_bytes)
    except FileNotFoundError:
        return []
    lines = content.splitlines()
    if offset and lines:
        lines = lines[1:]  # May begin in the middle of a UTF-8 sequence or record.
    records = []
    for line in lines[-count:]:
        try:
            record = json.loads(line)
            if isinstance(record, dict):
                records.append(record)
        except (ValueError, UnicodeDecodeError):
            continue
    return records
