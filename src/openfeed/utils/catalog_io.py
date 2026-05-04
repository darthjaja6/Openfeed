"""Per-topic source-catalog storage.

Why per-topic: discover, learn, filter all touch SourceEntry rows. With a
single `state/source_catalog.json` they need to read-modify-write the whole
catalog; concurrent writers (per-topic discover processes + supply_cycle
filter + refill_cycle learn) race on it. Per-topic files isolate writes
naturally — different topics never write the same file. Same-topic writes
get an `fcntl.flock` for cross-process safety.

Storage layout:

    state/
    └── source_catalog/
        ├── beauty.json
        ├── 做菜美食.json
        ├── ...

Each per-topic file matches the existing `SourceCatalog` pydantic shape
(`{generated_at, sources: dict[str, SourceEntry]}`) but contains only that
topic's sources. `load_catalog` returns the merged in-memory view (same
pydantic type) so callers don't need to know about the on-disk split.

Topic names go straight into the filename. Chinese / unicode topics work
on macOS APFS + Linux ext4/btrfs; Windows is out of scope for this app.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from openfeed.models.source import SourceCatalog, SourceEntry


CATALOG_DIR_NAME = "source_catalog"
_LOCK_SUFFIX = ".lock"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def catalog_dir(state_dir: Path) -> Path:
    return state_dir / CATALOG_DIR_NAME


def topic_path(state_dir: Path, topic: str) -> Path:
    """Per-topic catalog file path. Topic name used verbatim (UTF-8 OK)."""
    return catalog_dir(state_dir) / f"{topic}.json"


@contextmanager
def _topic_file_lock(state_dir: Path, topic: str):
    """Cross-process exclusive lock on one topic's catalog write path.

    Uses a sibling .lock file rather than locking the catalog file itself so
    `atomic_write_json`-style rename-over doesn't blow up our lock fd.
    """
    catalog_dir(state_dir).mkdir(parents=True, exist_ok=True)
    lock_path = catalog_dir(state_dir) / f"{topic}{_LOCK_SUFFIX}"
    # O_CREAT keeps the lock file around between callers; that's fine, lock
    # state is held by the OPEN file descriptor not the file's existence.
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON atomically via temp-file + rename (write-then-rename never
    leaves a half-written file visible). Local copy of utils.state_io's
    helper to avoid an import cycle."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def load_catalog(state_dir: Path) -> SourceCatalog:
    """Read every per-topic file under `state_dir/source_catalog/` and merge
    into a single in-memory `SourceCatalog`. Missing dir → empty catalog.

    Sources are keyed by `platform:source_id` globally (existing convention),
    so topic files don't conflict on keys for well-formed catalogs.
    """
    cat_dir = catalog_dir(state_dir)
    if not cat_dir.exists():
        return SourceCatalog(generated_at=_utc_now_iso(), sources={})
    merged: dict[str, SourceEntry] = {}
    latest_ts = ""
    for fp in sorted(cat_dir.glob("*.json")):
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        try:
            cat = SourceCatalog.model_validate(raw)
        except Exception:
            continue
        if cat.generated_at and cat.generated_at > latest_ts:
            latest_ts = cat.generated_at
        # Rekey by entry.catalog_key (which now includes topic) — this also
        # transparently migrates legacy 2-tuple `<platform>:<source_id>` keys
        # to the new 3-tuple `<platform>:<source_id>:<topic>` format on first
        # read. Save will then write back the canonical key shape.
        for entry in cat.sources.values():
            merged[entry.catalog_key] = entry
    return SourceCatalog(
        generated_at=latest_ts or _utc_now_iso(),
        sources=merged,
    )


def save_catalog_topic(
    state_dir: Path, topic: str, sources: dict[str, SourceEntry],
) -> None:
    """Atomically write a single topic's catalog file, holding a per-topic
    lock so concurrent writers (other process) wait their turn.

    `sources` is the dict scoped to this topic — caller is responsible for
    filtering the merged in-memory catalog down to one topic.
    """
    payload = SourceCatalog(
        generated_at=_utc_now_iso(),
        sources=sources,
    ).model_dump()
    path = topic_path(state_dir, topic)
    with _topic_file_lock(state_dir, topic):
        _atomic_write_json(path, payload)


def archive_topic(state_dir: Path, topic: str, target: Path) -> bool:
    """Move one topic catalog file out of active state under the topic lock.

    Returns False when the topic has no active catalog file.
    """
    path = topic_path(state_dir, topic)
    with _topic_file_lock(state_dir, topic):
        if not path.exists():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        path.replace(target)
        return True


def save_catalog(state_dir: Path, catalog: SourceCatalog) -> None:
    """Write the merged in-memory catalog back as per-topic files.

    Iterates every source, groups by topic, writes one file per topic.
    Topics that have no sources (dropped sources, or empty topic) leave
    their existing file untouched — call `delete_topic` explicitly if you
    want to remove a topic file.
    """
    by_topic: dict[str, dict[str, SourceEntry]] = {}
    for entry in catalog.sources.values():
        # Always key by entry.catalog_key (canonical) so saves are
        # consistent regardless of how the in-memory dict was built.
        by_topic.setdefault(entry.topic, {})[entry.catalog_key] = entry
    for topic, sources in by_topic.items():
        save_catalog_topic(state_dir, topic, sources)


def list_topics(state_dir: Path) -> list[str]:
    """Topic names with a catalog file on disk, alphabetised."""
    cat_dir = catalog_dir(state_dir)
    if not cat_dir.exists():
        return []
    return sorted(
        fp.stem for fp in cat_dir.glob("*.json")
    )
