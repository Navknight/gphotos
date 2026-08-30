#!/usr/bin/env python3
"""Where a capture date comes from, in order, and what may override what.

The order is the whole point, and it is the one thing the Go version got wrong:
it unconditionally wrote Google's sidecar time over whatever the file already
carried. A camera's own EXIF is better evidence of when a photo was taken than
Google's copy of it, which may have been re-derived at upload or hand-edited in
the Photos app. So:

    existing EXIF  >  sidecar photoTakenTime  >  filename  >  nothing

with one exception carried over from the Go tool, because it is right: when a
filename date is EARLIER than the sidecar date and is otherwise plausible, the
filename wins. Google records the *upload* time for files that arrived without
EXIF -- WhatsApp images, screenshots, downloads -- so for exactly those files
the name (IMG-20201231-WA0001.jpg) is the better evidence and the sidecar is
months or years late.

`learn` is the interactive half. Filenames in a personal archive follow local
conventions no built-in table can anticipate, so it groups the still-undated
files by shape, proposes a regex + strptime format for each cluster, shows what
the pattern would produce for real files, and requires the operator to type
APPLY before a single pattern is saved. The gate is not decoration: a wrong
pattern silently stamps thousands of files with a plausible-looking wrong date,
and a wrong date is much harder to notice later than no date at all.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Built-in patterns, ported from the Go tool's date.go. Each is (regex, format
# for datetime.strptime), tried in order; the first that both matches and parses
# to a plausible date wins.
#
# The year alternation (20|19|18) and the bounded month/day classes are load
# bearing: an unanchored \d{8} matches the "20201231" inside a serial number
# and yields a confident, wrong date.
PATTERNS: list[tuple[str, str]] = [
    # Screenshot_20190919-053857.jpg
    (r"(?:20|19|18)\d{2}(?:0[1-9]|1[0-2])[0-3]\d-\d{6}", "%Y%m%d-%H%M%S"),
    # IMG_20190509_154733.jpg
    (r"(?:20|19|18)\d{2}(?:0[1-9]|1[0-2])[0-3]\d_\d{6}", "%Y%m%d_%H%M%S"),
    # Screenshot_2019-04-16-11-19-37.jpg
    (r"(?:20|19|18)\d{2}-(?:0[1-9]|1[0-2])-[0-3]\d-\d{2}-\d{2}-\d{2}",
     "%Y-%m-%d-%H-%M-%S"),
    # signal-2020-10-26-163832.jpg
    (r"(?:20|19|18)\d{2}-(?:0[1-9]|1[0-2])-[0-3]\d-\d{6}", "%Y-%m-%d-%H%M%S"),
    # 2016_01_30_11_49_15.mp4
    (r"(?:20|19|18)\d{2}_(?:0[1-9]|1[0-2])_[0-3]\d_\d{2}_\d{2}_\d{2}",
     "%Y_%m_%d_%H_%M_%S"),
    # WhatsApp: IMG-20201231-WA0001.jpg / VID-20201231-WA0001.mp4. Date only,
    # no time -- WhatsApp does not put one in the name, and inventing 00:00:00
    # is honest here in a way that inventing an hour would not be.
    (r"(?:IMG|VID)-((?:20|19|18)\d{2}(?:0[1-9]|1[0-2])[0-3]\d)-WA\d+", "%Y%m%d"),
    # Pixel with millis: PXL_20210102_123456789.jpg -- take the first 6 digits
    # of the time field. Listed before the plain PXL rule, which would otherwise
    # match its prefix and read the millis as part of the seconds.
    (r"PXL_((?:20|19|18)\d{2}(?:0[1-9]|1[0-2])[0-3]\d_\d{6})\d{3}",
     "%Y%m%d_%H%M%S"),
    # Pixel: PXL_20210102_123456.jpg
    (r"PXL_((?:20|19|18)\d{2}(?:0[1-9]|1[0-2])[0-3]\d_\d{6})", "%Y%m%d_%H%M%S"),
    # Android: IMG_20210102_123456.jpg / VID_20210102_123456.mp4
    (r"(?:IMG|VID)_((?:20|19|18)\d{2}(?:0[1-9]|1[0-2])[0-3]\d_\d{6})",
     "%Y%m%d_%H%M%S"),
    # 201801261147521000.jpg -- a long digit run; use the first 14.
    (r"((?:20|19|18)\d{2}(?:0[1-9]|1[0-2])[0-3]\d\d{6})\d+", "%Y%m%d%H%M%S"),
]

MIN_YEAR = 1990


def is_reasonable(dt: datetime) -> bool:
    """A date outside living memory of this archive is a false positive."""
    return MIN_YEAR <= dt.year <= datetime.now().year + 1


def _try(pattern: str, fmt: str, name: str) -> datetime | None:
    m = re.search(pattern, name, re.IGNORECASE)
    if not m:
        return None
    # Group 1 when the pattern isolates the parseable part, else the whole match.
    text = m.group(1) if m.lastindex else m.group(0)
    try:
        dt = datetime.strptime(text, fmt)
    except ValueError:
        return None
    return dt if is_reasonable(dt) else None


def from_filename(path, custom=(), exclude=()) -> datetime | None:
    """A capture date read out of the file's name, or None.

    Custom (learned) patterns are tried first: they were added because the
    built-ins got these files wrong or missed them entirely, so they must win.
    `exclude` is a set of basenames a human has marked as "no pattern applies",
    which stops `learn` re-proposing the same rejected cluster every run.
    """
    name = os.path.basename(str(path))
    if name in exclude:
        return None
    for pattern, fmt in list(custom) + PATTERNS:
        dt = _try(pattern, fmt, name)
        if dt is not None:
            return dt
    return None


def resolve(sidecar_dt: datetime | None, path, has_exif: bool,
            custom=(), exclude=()) -> tuple[datetime | None, str]:
    """Pick the date and say where it came from.

    Returns (datetime or None, source) where source is one of
    "exif" / "sidecar" / "filename" / "none".

    "exif" means the file already has one and NOTHING should be written. That
    return is the gap-only rule, and it is why this function exists rather than
    each caller reimplementing the precedence.
    """
    if has_exif:
        return None, "exif"
    file_dt = from_filename(path, custom, exclude)
    if sidecar_dt and file_dt:
        # A filename date earlier than Google's is the signature of an upload
        # timestamp masquerading as a capture time. Prefer the name.
        if file_dt < sidecar_dt:
            return file_dt, "filename"
        return sidecar_dt, "sidecar"
    if sidecar_dt:
        return sidecar_dt, "sidecar"
    if file_dt:
        return file_dt, "filename"
    return None, "none"


# ---------------------------------------------------------------------------
# Learned patterns
# ---------------------------------------------------------------------------

def patterns_file(cfg) -> Path:
    return cfg.data / "date_patterns.json"


def load_custom(cfg):
    """(patterns, excluded basenames). A missing file is not an error."""
    p = patterns_file(cfg)
    if not p.exists():
        return [], set()
    try:
        doc = json.loads(p.read_text())
    except (OSError, ValueError) as exc:
        sys.exit(f"cannot read {p}: {exc}")
    pats = [(d["regex"], d["format"]) for d in doc.get("patterns", [])
            if d.get("regex") and d.get("format")]
    return pats, set(doc.get("exclude", []))


def save_custom(cfg, patterns, exclude):
    p = patterns_file(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "patterns": [{"regex": r, "format": f} for r, f in patterns],
        "exclude": sorted(exclude),
    }, indent=2) + "\n")
    return p


def _shape(name: str) -> str:
    """Collapse a filename to its shape so like names cluster together.

    IMG_20190509_154733.jpg and IMG_20200101_090000.jpg both become
    IMG_########_######.jpg, which is the unit a pattern is proposed for.
    """
    return re.sub(r"\d", "#", name)


# Shapes are matched against these to propose a regex + format. The proposal is
# always shown to the operator with real examples before anything is saved --
# these are guesses, not rules.
PROPOSALS: list[tuple[str, str, str]] = [
    (r"(?:20|19|18)\d{2}(?:0[1-9]|1[0-2])[0-3]\d[_-]\d{6}", "%Y%m%d_%H%M%S",
     "8 digits then 6, separated"),
    (r"(?:20|19|18)\d{2}-(?:0[1-9]|1[0-2])-[0-3]\d", "%Y-%m-%d",
     "ISO date, no time"),
    (r"(?:20|19|18)\d{2}(?:0[1-9]|1[0-2])[0-3]\d", "%Y%m%d",
     "8-digit date, no time"),
]


def learn(cfg, paths, apply=False, limit=40):
    """Propose filename-date patterns for undated files. Saves nothing without APPLY.

    `paths` is whatever the caller decided still has no date -- typically the
    audit's audit_no_date.tsv. The review gate is mandatory and deliberate: an
    accepted-by-default pattern would stamp a wrong-but-plausible date on
    thousands of files, and a wrong date is far harder to spot afterwards than a
    missing one.
    """
    custom, exclude = load_custom(cfg)
    undated = [p for p in paths if from_filename(p, custom, exclude) is None]
    print(f"{len(paths):,} file(s) given, {len(undated):,} still have no "
          f"filename date under the current patterns")
    if not undated:
        return 0

    by_shape = defaultdict(list)
    for p in undated:
        by_shape[_shape(os.path.basename(str(p)))].append(p)

    proposed = []
    for shape, members in sorted(by_shape.items(), key=lambda kv: -len(kv[1])):
        if len(proposed) >= limit:
            break
        for regex, fmt, why in PROPOSALS:
            hits = [(m, _try(regex, fmt, os.path.basename(str(m))))
                    for m in members[:200]]
            good = [(m, dt) for m, dt in hits if dt is not None]
            if len(good) < max(2, len(hits) // 2):
                continue
            proposed.append((shape, len(members), regex, fmt, why, good[:3]))
            break

    if not proposed:
        print("\nNo pattern could be proposed for any remaining shape.")
        return 0

    print(f"\n{len(proposed)} pattern(s) proposed. Check EVERY example: a wrong")
    print("pattern writes a plausible wrong date, which is worse than no date.\n")
    for shape, n, regex, fmt, why, examples in proposed:
        print(f"  shape {shape}   ({n:,} file(s))  -- {why}")
        print(f"    regex  {regex}")
        print(f"    format {fmt}")
        for m, dt in examples:
            print(f"      {os.path.basename(str(m)):<44} -> {dt:%Y-%m-%d %H:%M:%S}")
        print()

    if not apply:
        print("Nothing was saved. Re-run with --apply and type APPLY to accept.")
        return 0

    print("Type APPLY (exactly) to save these patterns, anything else to abort.")
    if input("> ").strip() != "APPLY":
        print("aborted -- nothing saved")
        return 1

    for _shape_, _n, regex, fmt, _why, _ex in proposed:
        if (regex, fmt) not in custom:
            custom.append((regex, fmt))
    out = save_custom(cfg, custom, exclude)
    print(f"saved {len(custom)} pattern(s) -> {out}")
    print("Re-run `gphotos audit` to see how many files they resolve, then feed")
    print("the result through `gphotos plan` / `gphotos embed` as usual -- the")
    print("gap-only rule still applies, so nothing with a date is touched.")
    return 0


def demo():
    """Self-check for the precedence rules and the built-in pattern table."""
    assert from_filename("PXL_20210102_123456789.jpg") == datetime(2021, 1, 2, 12, 34, 56)
    assert from_filename("IMG_20190509_154733.jpg") == datetime(2019, 5, 9, 15, 47, 33)
    assert from_filename("IMG-20201231-WA0001.jpg") == datetime(2020, 12, 31)
    assert from_filename("Screenshot_20190919-053857.jpg") == datetime(2019, 9, 19, 5, 38, 57)
    assert from_filename("DSC_0001.jpg") is None
    # A serial number must not be read as a date.
    assert from_filename("scan-99887766.jpg") is None

    google = datetime(2021, 6, 1, 12, 0, 0)
    # Existing EXIF always wins and yields nothing to write.
    assert resolve(google, "IMG_20190509_154733.jpg", has_exif=True) == (None, "exif")
    # Filename earlier than Google's upload time wins.
    dt, src = resolve(google, "IMG_20190509_154733.jpg", has_exif=False)
    assert src == "filename" and dt.year == 2019, (dt, src)
    # Filename later than the sidecar does not.
    dt, src = resolve(google, "IMG_20220509_154733.jpg", has_exif=False)
    assert (dt, src) == (google, "sidecar"), (dt, src)
    assert resolve(None, "DSC_0001.jpg", has_exif=False) == (None, "none")
    print("dates: ok")


if __name__ == "__main__":
    demo()
