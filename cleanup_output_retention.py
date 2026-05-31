"""
Archive and remove old router output files.

Files older than OUTPUT_RETENTION_DAYS are written into timestamped tar.gz
archives under OUTPUT_ARCHIVE_DIR, then deleted from OUTPUT_DIR.
"""

import argparse
import logging
import os
import re
import shutil
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path


OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/app/output"))
ARCHIVE_DIR = Path(os.getenv("OUTPUT_ARCHIVE_DIR", "/app/data/output_archive"))
RETENTION_DAYS = int(os.getenv("OUTPUT_RETENTION_DAYS", "30"))
INTERVAL_SEC = int(os.getenv("OUTPUT_RETENTION_INTERVAL_SEC", "86400"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


def parse_size(value: str | None, default: int) -> int:
    if not value:
        return default
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?i?b?|bytes?)?\s*$", value, re.I)
    if not match:
        raise ValueError(f"Invalid size value: {value}")
    number = float(match.group(1))
    unit = (match.group(2) or "").lower()
    multiplier = 1
    if unit.startswith("k"):
        multiplier = 1024
    elif unit.startswith("m"):
        multiplier = 1024 ** 2
    elif unit.startswith("g"):
        multiplier = 1024 ** 3
    elif unit.startswith("t"):
        multiplier = 1024 ** 4
    return int(number * multiplier)


ARCHIVE_RETENTION_DAYS = int(os.getenv("OUTPUT_ARCHIVE_RETENTION_DAYS", "90"))
ARCHIVE_MAX_BYTES = parse_size(os.getenv("OUTPUT_ARCHIVE_MAX_BYTES"), 20 * 1024 ** 3)
OUTPUT_MAX_BYTES = parse_size(os.getenv("OUTPUT_MAX_BYTES"), 20 * 1024 ** 3)
MIN_FREE_BYTES = parse_size(os.getenv("OUTPUT_MIN_FREE_BYTES"), 10 * 1024 ** 3)
ARCHIVE_EXPIRED_FILES = os.getenv("OUTPUT_ARCHIVE_EXPIRED_FILES", "true").lower() not in {"0", "false", "no"}

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("output_retention")


def disk_usage_path(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def free_bytes_for(path: Path) -> int:
    return shutil.disk_usage(disk_usage_path(path)).free


def iter_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    files = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            path.stat()
        except FileNotFoundError:
            continue
        files.append(path)
    return files


def iter_expired_files(cutoff_ts: float) -> list[Path]:
    expired = []
    for path in iter_files(OUTPUT_DIR):
        try:
            if path.stat().st_mtime < cutoff_ts:
                expired.append(path)
        except FileNotFoundError:
            continue
    return sorted(expired)


def total_size(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except FileNotFoundError:
            continue
    return total


def delete_files(paths: list[Path]) -> tuple[int, int]:
    deleted = 0
    deleted_bytes = 0
    for path in paths:
        try:
            size = path.stat().st_size
            path.unlink()
        except FileNotFoundError:
            continue
        deleted += 1
        deleted_bytes += size
    return deleted, deleted_bytes


def archive_files(paths: list[Path], total_bytes_value: int) -> tuple[Path | None, int, int]:
    if not ARCHIVE_EXPIRED_FILES:
        logger.warning("Archive disabled; deleting expired output files without archive")
        return None, 0, 0

    free_before = free_bytes_for(ARCHIVE_DIR)
    if MIN_FREE_BYTES > 0 and free_before - total_bytes_value < MIN_FREE_BYTES:
        logger.warning(
            "Skipping archive to protect disk space: free_bytes=%s source_bytes=%s min_free_bytes=%s",
            free_before,
            total_bytes_value,
            MIN_FREE_BYTES,
        )
        return None, 0, 0

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_name = datetime.now(timezone.utc).strftime("output-%Y%m%dT%H%M%SZ.tar.gz")
    archive_path = ARCHIVE_DIR / archive_name
    archived = 0
    with tarfile.open(archive_path, "w:gz") as archive:
        for path in paths:
            try:
                path.stat()
            except FileNotFoundError:
                continue
            archive.add(path, arcname=path.relative_to(OUTPUT_DIR))
            archived += 1

    archive_size = archive_path.stat().st_size if archive_path.exists() else 0
    return archive_path, archived, archive_size


def archive_files_sorted() -> list[Path]:
    if not ARCHIVE_DIR.exists():
        return []
    files = [path for path in ARCHIVE_DIR.glob("output-*.tar.gz") if path.is_file()]
    return sorted(files, key=lambda path: path.stat().st_mtime)


def prune_archive_files() -> tuple[int, int]:
    deleted = 0
    deleted_bytes = 0
    now_ts = time.time()

    if ARCHIVE_RETENTION_DAYS > 0:
        cutoff_ts = now_ts - (ARCHIVE_RETENTION_DAYS * 86400)
        old_archives = []
        for path in archive_files_sorted():
            try:
                if path.stat().st_mtime < cutoff_ts:
                    old_archives.append(path)
            except FileNotFoundError:
                continue
        count, size = delete_files(old_archives)
        deleted += count
        deleted_bytes += size

    if ARCHIVE_MAX_BYTES > 0:
        archives = archive_files_sorted()
        archive_bytes = total_size(archives)
        to_delete = []
        for path in archives:
            if archive_bytes <= ARCHIVE_MAX_BYTES:
                break
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            to_delete.append(path)
            archive_bytes -= size
        count, size = delete_files(to_delete)
        deleted += count
        deleted_bytes += size

    if deleted:
        logger.info("Pruned output archive files, deleted=%s bytes=%s", deleted, deleted_bytes)
    return deleted, deleted_bytes


def prune_output_size() -> tuple[int, int]:
    if OUTPUT_MAX_BYTES <= 0:
        return 0, 0

    files = sorted(iter_files(OUTPUT_DIR), key=lambda path: path.stat().st_mtime)
    output_bytes = total_size(files)
    to_delete = []
    for path in files:
        if output_bytes <= OUTPUT_MAX_BYTES:
            break
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            continue
        to_delete.append(path)
        output_bytes -= size

    deleted, deleted_bytes = delete_files(to_delete)
    if deleted:
        logger.warning(
            "Output size cap pruned files, deleted=%s bytes=%s max_bytes=%s",
            deleted,
            deleted_bytes,
            OUTPUT_MAX_BYTES,
        )
    return deleted, deleted_bytes


def protect_min_free_space() -> tuple[int, int, int, int]:
    if MIN_FREE_BYTES <= 0:
        return 0, 0, 0, 0

    archive_deleted = 0
    archive_deleted_bytes = 0
    output_deleted = 0
    output_deleted_bytes = 0

    archives = archive_files_sorted()
    to_delete = []
    free_bytes = free_bytes_for(ARCHIVE_DIR)
    for path in archives:
        if free_bytes >= MIN_FREE_BYTES:
            break
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            continue
        to_delete.append(path)
        free_bytes += size
    archive_deleted, archive_deleted_bytes = delete_files(to_delete)

    output_files = sorted(iter_files(OUTPUT_DIR), key=lambda path: path.stat().st_mtime)
    to_delete = []
    free_bytes = free_bytes_for(OUTPUT_DIR)
    for path in output_files:
        if free_bytes >= MIN_FREE_BYTES:
            break
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            continue
        to_delete.append(path)
        free_bytes += size
    output_deleted, output_deleted_bytes = delete_files(to_delete)

    if archive_deleted or output_deleted:
        logger.warning(
            "Minimum free-space guard pruned files, archive_deleted=%s archive_bytes=%s output_deleted=%s output_bytes=%s min_free_bytes=%s",
            archive_deleted,
            archive_deleted_bytes,
            output_deleted,
            output_deleted_bytes,
            MIN_FREE_BYTES,
        )
    return archive_deleted, archive_deleted_bytes, output_deleted, output_deleted_bytes


def prune_empty_dirs() -> int:
    removed = 0
    if not OUTPUT_DIR.exists():
        return removed
    dirs = sorted(
        (path for path in OUTPUT_DIR.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in dirs:
        try:
            path.rmdir()
        except OSError:
            continue
        removed += 1
    return removed


def run_once() -> int:
    cutoff_ts = time.time() - (RETENTION_DAYS * 86400)
    expired_files = iter_expired_files(cutoff_ts)
    archive_pruned, archive_pruned_bytes = prune_archive_files()

    if not expired_files:
        size_pruned, size_pruned_bytes = prune_output_size()
        guard_archive_pruned, guard_archive_bytes, guard_output_pruned, guard_output_bytes = protect_min_free_space()
        removed_dirs = prune_empty_dirs()
        logger.info(
            "Output retention finished, no files older than %s day(s), archive_pruned=%s archive_bytes=%s size_pruned=%s size_bytes=%s guard_archive_pruned=%s guard_archive_bytes=%s guard_output_pruned=%s guard_output_bytes=%s empty_dirs_removed=%s",
            RETENTION_DAYS,
            archive_pruned,
            archive_pruned_bytes,
            size_pruned,
            size_pruned_bytes,
            guard_archive_pruned,
            guard_archive_bytes,
            guard_output_pruned,
            guard_output_bytes,
            removed_dirs,
        )
        return 0

    source_bytes = total_size(expired_files)

    logger.info(
        "Processing %s output file(s) older than %s day(s)",
        len(expired_files),
        RETENTION_DAYS,
    )
    archive_path, archived, archive_size = archive_files(expired_files, source_bytes)
    deleted, deleted_bytes = delete_files(expired_files)

    archive_pruned_2, archive_pruned_bytes_2 = prune_archive_files()
    archive_pruned += archive_pruned_2
    archive_pruned_bytes += archive_pruned_bytes_2
    size_pruned, size_pruned_bytes = prune_output_size()
    guard_archive_pruned, guard_archive_bytes, guard_output_pruned, guard_output_bytes = protect_min_free_space()
    removed_dirs = prune_empty_dirs()
    logger.info(
        "Output retention finished, archive=%s archived=%s deleted=%s deleted_bytes=%s source_bytes=%s archive_bytes=%s archive_pruned=%s archive_pruned_bytes=%s size_pruned=%s size_pruned_bytes=%s guard_archive_pruned=%s guard_archive_bytes=%s guard_output_pruned=%s guard_output_bytes=%s empty_dirs_removed=%s",
        archive_path,
        archived,
        deleted,
        deleted_bytes,
        source_bytes,
        archive_size,
        archive_pruned,
        archive_pruned_bytes,
        size_pruned,
        size_pruned_bytes,
        guard_archive_pruned,
        guard_archive_bytes,
        guard_output_pruned,
        guard_output_bytes,
        removed_dirs,
    )
    return deleted + size_pruned + guard_output_pruned


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Run retention once per interval")
    args = parser.parse_args()

    logger.info("Output retention started")
    logger.info("OUTPUT_DIR=%s", OUTPUT_DIR)
    logger.info("OUTPUT_ARCHIVE_DIR=%s", ARCHIVE_DIR)
    logger.info("OUTPUT_RETENTION_DAYS=%s", RETENTION_DAYS)
    logger.info("OUTPUT_ARCHIVE_RETENTION_DAYS=%s", ARCHIVE_RETENTION_DAYS)
    logger.info("OUTPUT_ARCHIVE_MAX_BYTES=%s", ARCHIVE_MAX_BYTES)
    logger.info("OUTPUT_MAX_BYTES=%s", OUTPUT_MAX_BYTES)
    logger.info("OUTPUT_MIN_FREE_BYTES=%s", MIN_FREE_BYTES)
    logger.info("OUTPUT_ARCHIVE_EXPIRED_FILES=%s", ARCHIVE_EXPIRED_FILES)

    if not args.loop:
        run_once()
        return

    while True:
        try:
            run_once()
        except Exception:
            logger.exception("Output retention loop failed")
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
