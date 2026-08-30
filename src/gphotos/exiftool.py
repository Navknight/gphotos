"""The one place that talks to exiftool, plus the two "is this nothing?" tests.

Three lessons are baked in here, each of which cost a re-run of the whole
archive when it was learned the hard way:

  * "no date" has THREE spellings -- "", "-" (what -f substitutes for a missing
    tag) and "0000:00:00 00:00:00" (a tag that exists but holds a zero date).
    Testing only the first two made hundreds of dateless videos look dated, so
    the embed pass skipped them. See absent().
  * `exiftool -r` filters by extension while recursing and silently never opens
    .MP motion-photo files. Paths are therefore always passed explicitly
    through -@, never discovered by exiftool's own recursion.
  * starting one exiftool per file is roughly a hundred times slower than
    handing it a batch. Everything goes through read_tags(), which chunks.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# .MP is a Pixel motion photo and is an MP4 container despite the extension --
# `file` reports "ISO Media, MP4 Base Media v1". Classifying it as an image made
# the writer ask for EXIF:DateTimeOriginal, which exiftool cannot put in an MP4:
# it wrote the XMP tag, skipped the EXIF one, and verification then found the tag
# missing on 353 files. .MV, .MP~2 and .MP~3 are the same container under
# Takeout's split-motion-photo naming.
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".3gp", ".3g2", ".avi", ".mkv", ".webm",
             ".mts", ".mp", ".mv", ".mp~2", ".mp~3", ".wmv"}
IMAGE_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".webp", ".tif", ".tiff",
             ".dng", ".nef", ".gif", ".bmp", ".avif"}
MEDIA_EXT = IMAGE_EXT | VIDEO_EXT

BATCH = 200


def absent(v: str | None) -> bool:
    """True when a tag value carries no usable information.

    exiftool reports three different kinds of nothing and they are easy to
    conflate: "" when the tag is missing, "-" when -f substitutes a placeholder,
    and "0000:00:00 00:00:00" when the tag exists but holds a zero date. That
    last one is a real value as far as a naive `v not in ("", "-")` test is
    concerned, which is how hundreds of dateless videos were judged to already
    have a date and skipped by the embed pass.
    """
    v = (v or "").strip()
    return v in ("", "-") or v.startswith("0000")


ZERO_GPS = re.compile(r"^0 deg 0' 0(\.0+)?\"")


def absent_gps(v: str | None) -> bool:
    """GPS emptiness, including the 0/0 case.

    exiftool renders a missing-but-present GPS tag as 0 deg 0' 0.00" -- a real
    coordinate in the Gulf of Guinea, and one no photo here was taken at. Treated
    as absent, the same way a 0000 date is. Counting it as a location is the
    same mistake as counting a zero date as a date.
    """
    v = (v or "").strip()
    return v in ("", "-") or bool(ZERO_GPS.match(v))


def is_video(path) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXT


def is_media(path) -> bool:
    return Path(path).suffix.lower() in MEDIA_EXT


def work_extension(path) -> str:
    """The extension exiftool should see, which is not always the one on disk.

    A few files carry a .webp extension but are actually JPEG bytes (seen on
    2024 phone exports). exiftool picks its writer module from the extension, so
    it refuses to write EXIF into "work.webp" even though the content is a
    perfectly normal JPEG. Sniffing the magic bytes gives the real extension for
    the scratch copy; the file in the library keeps its original (wrong) name.
    """
    ext = Path(path).suffix.lower()
    if ext != ".webp":
        return ext
    try:
        with open(path, "rb") as fh:
            if fh.read(3) == b"\xff\xd8\xff":
                return ".jpg"
    except OSError:
        pass
    return ext


def read_tags(paths, fmt: str, extra=(), batch: int = BATCH):
    """Read tags for named files, yielding one output line per file.

    `fmt` is an exiftool -p print format, e.g.
    "$FilePath\\t$DateTimeOriginal". Paths are fed through -@ - rather than
    given as arguments, which both avoids ARG_MAX and stops exiftool applying
    its own extension filter to them.
    """
    paths = [str(p) for p in paths]
    for i in range(0, len(paths), batch):
        chunk = paths[i:i + batch]
        res = subprocess.run(
            ["exiftool", "-fast2", "-q", "-q", "-m", "-f", "-p", fmt,
             *extra, "-@", "-"],
            input="\n".join(chunk), capture_output=True, text=True,
        )
        for line in res.stdout.splitlines():
            if line.strip():
                yield line.rstrip("\n")


def date_gps_state(paths, root: Path):
    """{relpath: (has_date, has_gps)} for files under `root`.

    Files exiftool could not read at all are simply absent from the result --
    never reported as "has nothing". Treating an unreadable file as having no
    metadata would quietly turn fill-gaps-only into overwrite.
    """
    root = Path(root)
    prefix = str(root) + "/"
    state = {}
    fmt = "$FilePath|$DateTimeOriginal|$CreateDate|$GPSLatitude"
    for line in read_tags(paths, fmt):
        p = line.split("|")
        if len(p) < 4 or not p[0].startswith(prefix):
            continue
        state[p[0][len(prefix):]] = (not (absent(p[1]) and absent(p[2])),
                                     not absent_gps(p[3]))
    return state


def available() -> bool:
    try:
        subprocess.run(["exiftool", "-ver"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False
