#!/usr/bin/env python3
"""
Set file modification times from embedded EXIF/metadata dates.

Walks a folder tree, reads DateTimeOriginal (or fallback date tags) via
exiftool, and sets each file's mtime to match.  This makes OneDrive and
other cloud services that rely on filesystem dates sort photos correctly.

Requirements:
  - exiftool on PATH
  - pip install (nothing extra needed, uses stdlib only)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SUPPORTED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
    ".bmp", ".tif", ".tiff", ".avif",
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".wmv",
    ".mp", ".mv", ".mp~2", ".mp~3",
    ".dng", ".nef",
}

# exiftool date tags in priority order
DATE_TAGS = [
    "DateTimeOriginal",
    "CreateDate",
    "MediaCreateDate",
    "TrackCreateDate",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Set file mtime from EXIF dates.")
    p.add_argument("--root", required=True, help="Root folder to process.")
    p.add_argument("--dry-run", action="store_true", help="Print changes without applying.")
    p.add_argument("--batch-size", type=int, default=500, help="Files per exiftool batch call.")
    p.add_argument("--tolerance", type=int, default=2, help="Skip if mtime already within N seconds of EXIF date.")
    return p.parse_args()


def collect_files(root: Path) -> list[Path]:
    files = []
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(path)
    return files


def read_dates_batch(paths: list[Path]) -> dict[str, float]:
    """Run exiftool on a batch and return {path: epoch} for files with dates."""
    tag_args = []
    for tag in DATE_TAGS:
        tag_args += [f"-{tag}"]

    cmd = [
        "exiftool", "-j", "-q", "-q",
        *tag_args,
        "-d", "%Y-%m-%dT%H:%M:%S%z",
        *[str(p) for p in paths],
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:
        print(f"  exiftool error: {e}")
        return {}

    if not result.stdout.strip():
        return {}

    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}

    out: dict[str, float] = {}
    for row in rows:
        src = row.get("SourceFile", "")
        if not src:
            continue
        for tag in DATE_TAGS:
            val = row.get(tag, "")
            if not val or "0000" in str(val):
                continue
            epoch = parse_exif_date(str(val))
            if epoch is not None:
                out[os.path.normpath(src)] = epoch
                break
    return out


def parse_exif_date(val: str) -> float | None:
    from datetime import datetime, timezone, timedelta
    import re

    val = val.strip()
    if not val:
        return None

    # Try ISO-like: 2021-01-02T15:04:05+0530
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})([+-]\d{2}):?(\d{2})?", val)
    if m:
        y, mo, d, h, mi, s = (int(x) for x in m.groups()[:6])
        tz_h = int(m.group(7))
        tz_m = int(m.group(8)) if m.group(8) else 0
        tz_off = timedelta(hours=tz_h, minutes=tz_m if tz_h >= 0 else -tz_m)
        try:
            dt = datetime(y, mo, d, h, mi, s, tzinfo=timezone(tz_off))
            return dt.timestamp()
        except Exception:
            pass

    # Without timezone
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", val)
    if m:
        y, mo, d, h, mi, s = (int(x) for x in m.groups())
        try:
            dt = datetime(y, mo, d, h, mi, s)
            return dt.timestamp()
        except Exception:
            pass

    # EXIF format: 2021:01:02 15:04:05
    m = re.match(r"(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})", val)
    if m:
        y, mo, d, h, mi, s = (int(x) for x in m.groups())
        try:
            dt = datetime(y, mo, d, h, mi, s)
            return dt.timestamp()
        except Exception:
            pass

    return None


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}")
        return 1

    # Check exiftool
    try:
        subprocess.run(["exiftool", "-ver"], capture_output=True, timeout=10)
    except FileNotFoundError:
        print("exiftool not found on PATH. Install it first.")
        return 1

    print(f"Scanning {root} ...")
    files = collect_files(root)
    print(f"Found {len(files)} media files.")

    updated = 0
    skipped_already = 0
    skipped_no_date = 0
    failed = 0

    for i in range(0, len(files), args.batch_size):
        batch = files[i : i + args.batch_size]
        dates = read_dates_batch(batch)

        for path in batch:
            key = os.path.normpath(str(path))
            epoch = dates.get(key)
            if epoch is None:
                skipped_no_date += 1
                continue

            current_mtime = path.stat().st_mtime
            if abs(current_mtime - epoch) <= args.tolerance:
                skipped_already += 1
                continue

            if args.dry_run:
                old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(current_mtime))
                new = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))
                print(f"  WOULD SET: {path.name}  {old} -> {new}")
            else:
                try:
                    os.utime(str(path), (epoch, epoch))
                    updated += 1
                except Exception as e:
                    print(f"  FAILED: {path.name}: {e}")
                    failed += 1
                    continue

        done = min(i + args.batch_size, len(files))
        pct = done / len(files) * 100 if files else 100
        print(f"Progress: {done}/{len(files)} ({pct:.1f}%) | updated={updated} skipped_ok={skipped_already} no_date={skipped_no_date}")

    print(f"\nDone. Updated: {updated}, Already correct: {skipped_already}, No date: {skipped_no_date}, Failed: {failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
