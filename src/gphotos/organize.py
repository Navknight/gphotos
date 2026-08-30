#!/usr/bin/env python3
"""Turn a freshly extracted Google Takeout into a browsable library. The default command.

This is the one thing you can do with nothing but exiftool installed and no
configuration: point it at an unzipped Takeout and get back

    <output>/Library/            every distinct photo, once
    <output>/Albums/<name>/      a copy for each album it belonged to

The interesting parts are all things a naive copy gets wrong:

  * PAIRING media to its sidecar. Takeout's JSON naming is a minefield --
    IMG_1.jpg.json, IMG_1.jpg.supplemental-metadata.json, IMG_1(1).json
    describing IMG_1(1).jpg, a 46-character truncation of a long name, a
    motion photo's .MP whose sidecar is filed under the .jpg. Four tiers of
    decreasing confidence, ported from the Go tool's scanner.go, which is the
    single best piece of code in that version.
  * DEDUP. A photo in three albums arrives three times. The copies are merged
    and the survivor inherits the UNION of their albums, so nothing loses its
    album membership on the way through.
  * DATES, gap-only. This is where the Go version was wrong: it wrote Google's
    sidecar time over whatever the file already carried. A camera's own EXIF is
    better evidence than Google's copy of it, so a file that already has a date
    keeps it, and only genuinely dateless files get one written. See dates.py.
  * VERIFIED WRITES. Every metadata write goes through embed.embed_file(), the
    same copy-to-scratch -> edit -> verify -> temp-write -> rename -> re-hash
    path the incremental pipeline uses. The Go version wrote in place and never
    read back, which is exactly the check that caught a bridge silently dropping
    writes.

Nothing is moved or deleted: the Takeout is only read.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import dedup, embed, ledger
from .dates import from_filename, load_custom, resolve
from .exiftool import MEDIA_EXT, absent, absent_gps, read_tags
from .sidecars import album_of, app_source_of, coord, origin_label, ts

LIBRARY = "Library"
ALBUMS = "Albums"

LEDGER_HEADER = ["src", "dst", "album", "date_source", "status", "note"]

RE_TRAILING_INDEX = re.compile(r"\(\d+\)$")
EDIT_SUFFIXES = ("-edited", "-collage", "-color_pop", "-photo_frame", "-overlayed")


# ---------------------------------------------------------------------------
# Four-tier media <-> sidecar matching, ported from the Go tool's scanner.go.
# ---------------------------------------------------------------------------

def strip_ext(name: str) -> str:
    root, ext = os.path.splitext(name)
    return root if ext else name


def strip_trailing_index(name: str) -> str:
    return RE_TRAILING_INDEX.sub("", name)


def normalize_json_key(filename: str) -> str:
    """The media basename a sidecar filename claims to describe.

    Strips the .json, any (n) duplicate marker, and Google's two metadata
    infixes. IMG_1.jpg.supplemental-metadata.json and IMG_1.jpg(1).json both
    reduce to IMG_1.jpg.
    """
    if not filename.endswith(".json"):
        return ""
    name = filename[:-len(".json")].rstrip(".")
    name = strip_trailing_index(name)
    lower = name.lower()
    for infix in (".supp", ".meta"):
        idx = lower.find(infix)
        if idx >= 0:
            return name[:idx]
    return name


def media_keys(base: str):
    """Every spelling of a media basename a sidecar might be filed under."""
    base = base.rstrip(".")
    keys = [base, strip_ext(base), strip_trailing_index(base),
            strip_trailing_index(strip_ext(base))]
    seen, out = set(), []
    for k in keys:
        k = k.strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def normalize_for_match(base: str) -> str:
    """Last-resort key: lower-cased, de-duplicated, with edit suffixes removed."""
    b = strip_trailing_index(base.strip().lower())
    for suffix in EDIT_SUFFIXES:
        if b.endswith(suffix):
            b = b[:-len(suffix)]
            break
    return b.strip()


def matches_metadata_name(filename: str, base: str) -> bool:
    """Does this sidecar filename look like it was named for `base`?

    Allows base(.supplemental-metadata|.metadata)?(.json) with an optional (n).
    Used only to break ties when several sidecars claim the same title.
    """
    if not base:
        return False
    pattern = ("^" + re.escape(base) +
               r"(\(\d+\))?(\.supplemental-metadata|\.metadata)?\.json$")
    return re.match(pattern, filename) is not None


def _pick(candidates, base):
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    for c in candidates:
        if matches_metadata_name(os.path.basename(c), base):
            return c
    return candidates[0]


class SidecarIndex:
    """The four lookup tables the matcher consults, built in one walk."""

    def __init__(self):
        self.by_title = defaultdict(list)
        self.by_key = defaultdict(list)
        self.by_dir = defaultdict(list)   # dir -> [(title, path)]
        self.by_norm = defaultdict(list)

    def add(self, path):
        base = os.path.basename(path)
        if base == "metadata.json":
            # An album's own metadata.json describes the album, not a photo.
            return
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            title = doc.get("title") or "" if isinstance(doc, dict) else ""
        except (OSError, ValueError):
            title = ""
        if title:
            self.by_title[title.lower()].append(path)
            self.by_dir[os.path.dirname(path)].append((title, path))
            norm = normalize_for_match(strip_ext(title))
            if norm:
                self.by_norm[norm].append(path)
        key = normalize_json_key(base)
        if key:
            self.by_key[key].append(path)

    def _live_photo_sibling(self, media_path):
        """A .mp4/.mov shot as a live photo is filed under its still's title."""
        ext = Path(media_path).suffix.lower()
        if ext not in (".mp4", ".mov"):
            return None
        stem = strip_ext(os.path.basename(media_path)).lower()
        for e in (".heic", ".jpg", ".jpeg", ".png"):
            hit = _pick(self.by_title.get(stem + e), stem + e)
            if hit:
                return hit
        return None

    def _prefix_in_dir(self, media_path):
        """A sidecar in the same directory whose title starts with this name.

        Google truncates long titles, so the media file's name is a prefix of
        the sidecar's. Shortest matching title wins -- it is the least
        speculative extension of the prefix.
        """
        entries = self.by_dir.get(os.path.dirname(media_path)) or []
        base = os.path.basename(media_path)
        ext = Path(base).suffix.lower()
        if not ext:
            return None
        stem = strip_ext(base).lower()
        best, best_len = None, 0
        for title, path in entries:
            t = title.lower()
            if not t.endswith(ext):
                continue
            title_base = t[:-len(ext)]
            if not title_base.startswith(stem):
                continue
            if best is None or len(title_base) < best_len:
                best, best_len = path, len(title_base)
        return best

    def resolve(self, media_path):
        """The sidecar for one media file, or None. Four tiers, most confident first."""
        base = os.path.basename(media_path)
        base_lower = base.lower()
        stem_lower = strip_ext(base).lower()
        ext = Path(base).suffix.lower()

        # Tier 1: the sidecar's own `title` field names this file exactly.
        for key in (base_lower, stem_lower):
            hit = _pick(self.by_title.get(key), base)
            if hit:
                return hit
        if ext == ".mp":
            # A Pixel motion photo's sidecar is filed under the still it came
            # from, so IMG.MP is described by the JSON titled IMG.MP.jpg.
            for e in (".jpg", ".jpeg"):
                hit = _pick(self.by_title.get(base_lower + e), base)
                if hit:
                    return hit
        hit = self._live_photo_sibling(media_path)
        if hit:
            return hit

        # Tier 2: the sidecar's FILENAME names this file, after normalisation.
        for key in media_keys(base):
            hit = _pick(self.by_key.get(key), base)
            if hit:
                return hit

        # Tier 3: same directory, title is an extension of this name.
        hit = self._prefix_in_dir(media_path)
        if hit:
            return hit

        # Tier 4: normalised, ignoring (n) markers and -edited style suffixes.
        norm = normalize_for_match(strip_ext(base))
        if norm:
            hit = _pick(self.by_norm.get(norm), base)
            if hit:
                return hit
        return None


def detect_album(root, path) -> str:
    """The album a Takeout path implies, or "" for the undifferentiated library.

    `Photos from YYYY` is Takeout's name for "no album", not an album.
    """
    rel = os.path.relpath(path, root)
    parts = rel.split(os.sep)
    for i, part in enumerate(parts):
        if part.lower() == "google photos":
            if i + 1 >= len(parts):
                return ""
            seg = parts[i + 1]
            if seg.lower() == "albums" and i + 2 < len(parts):
                seg = parts[i + 2]
            return "" if seg.startswith("Photos from") else seg
    if len(parts) > 1:
        if parts[0] == "Google Photos" and len(parts) > 2:
            return "" if parts[1].startswith("Photos from") else parts[1]
        if not parts[0].startswith("Photos from"):
            return parts[0]
    return ""


# ---------------------------------------------------------------------------


def scan(root, verbose=False):
    """Walk a Takeout once, returning dedup.Item list with sidecars resolved."""
    index = SidecarIndex()
    media = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            if name.lower().endswith(".json"):
                index.add(path)
            elif Path(name).suffix.lower() in MEDIA_EXT:
                media.append(path)

    items = []
    for path in media:
        sidecar = index.resolve(path)
        album = detect_album(root, path)
        try:
            item = dedup.Item(path, albums={album} if album else set(),
                              sidecar=sidecar)
        except OSError:
            continue
        items.append(item)
        if verbose:
            print(f"  {os.path.relpath(path, root)} -> "
                  f"{os.path.basename(sidecar) if sidecar else '(no sidecar)'}")
    return items, len(index.by_key) + len(index.by_title)


def sidecar_meta(path):
    """The fields plan.py's row format needs, read straight from one sidecar."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    lat, lon, alt = coord(doc.get("geoData"))
    elat, elon, ealt = coord(doc.get("geoDataExif"))
    people = doc.get("people")
    people_s = "|".join(p.get("name", "") for p in people
                        if isinstance(p, dict) and p.get("name")) \
        if isinstance(people, list) else ""
    return {
        "photo_taken_ts": ts(doc.get("photoTakenTime")),
        "lat": elat or lat, "lon": elon or lon, "alt": ealt or alt,
        "description": " ".join((doc.get("description") or "").split()),
        "favorited": "1" if doc.get("favorited") else "",
        "people": people_s,
        "url": doc.get("url") or "",
        "app_source": app_source_of(doc),
        "origin": origin_label(doc),
    }


def existing_state(items):
    """{path: (has_date, has_gps)} for the source files, read in batches.

    A file exiftool cannot read is absent from the result and is then treated
    as "unknown", never as "has nothing" -- guessing "nothing" is what turns
    fill-gaps-only into overwrite.
    """
    state = {}
    fmt = "$FilePath|$DateTimeOriginal|$CreateDate|$GPSLatitude"
    for line in read_tags([i.path for i in items], fmt):
        p = line.split("|")
        if len(p) < 4:
            continue
        state[p[0]] = (not (absent(p[1]) and absent(p[2])), not absent_gps(p[3]))
    return state


def sanitize_folder(name: str) -> str:
    name = name.strip().replace(os.sep, "_").replace("\x00", "")
    return name or "Untitled"


def unique_path(directory: Path, filename: str, file_hash: str | None,
                taken: set) -> Path:
    """A free path in `directory`, disambiguated by content hash.

    The hash suffix is deliberate: two different photos genuinely named
    IMG_1234.jpg become IMG_1234-a1b2c3d4.jpg and IMG_1234-e5f6a7b8.jpg, which
    are stable across re-runs. A running counter would renumber them differently
    every time the input order changed.
    """
    path = directory / filename
    if path not in taken and not path.exists():
        taken.add(path)
        return path
    stem, ext = os.path.splitext(filename)
    if file_hash:
        path = directory / f"{stem}-{file_hash[:8]}{ext}"
        if path not in taken and not path.exists():
            taken.add(path)
            return path
    for i in range(1, 10000):
        path = directory / f"{stem}-{i}{ext}"
        if path not in taken and not path.exists():
            taken.add(path)
            return path
    raise RuntimeError(f"too many name collisions for {filename}")


def _copy_verified(src, dst, verify_readback=True):
    """Copy, fsync, and (by default) re-read off the destination to prove it landed."""
    shutil.copyfile(src, dst)
    with open(dst, "rb+") as fh:
        os.fsync(fh.fileno())
    if not verify_readback:
        return True
    try:
        with open(dst, "rb") as fh:
            os.posix_fadvise(fh.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    except (OSError, AttributeError):
        pass
    return dedup.sha256(dst) == dedup.sha256(src)


def main(cfg, takeout, output=None, apply=False, verbose=False, limit=0):
    root = Path(os.path.expanduser(takeout)).resolve()
    if not root.is_dir():
        sys.exit(f"not a directory: {root}")
    out_root = Path(os.path.expanduser(output)).resolve() if output else cfg.output
    dry = not apply

    print(f"scanning {root} ...")
    items, n_sidecars = scan(root, verbose=verbose)
    print(f"  {len(items):,} media file(s), {n_sidecars:,} sidecar key(s)")
    paired = sum(1 for i in items if i.sidecar)
    print(f"  {paired:,} paired with a sidecar, {len(items)-paired:,} without")
    if not items:
        return 0

    print("reading existing dates/GPS (gap-only rule needs to know)...")
    state = existing_state(items)
    custom, exclude = load_custom(cfg)
    for it in items:
        it.meta = sidecar_meta(it.sidecar)
        has_date, _has_gps = state.get(it.path, (False, False))
        sidecar_dt = None
        if it.meta.get("photo_taken_ts"):
            try:
                sidecar_dt = datetime.fromtimestamp(
                    int(it.meta["photo_taken_ts"]), tz=timezone.utc).astimezone()
                sidecar_dt = sidecar_dt.replace(tzinfo=None)
            except (ValueError, OSError, OverflowError):
                sidecar_dt = None
        _dt, source = resolve(sidecar_dt, it.path, has_date, custom, exclude)
        it.date_source = source

    print("deduplicating...")
    before = len(items)
    items = dedup.merge_identical(items)
    items, degraded = dedup.prefer_originals(items)
    print(f"  {before:,} -> {len(items):,} distinct "
          f"({degraded:,} Storage-saver re-encode(s) dropped in favour of originals)")
    if limit:
        items = items[:limit]

    n_copies = sum(max(1, len(i.albums)) for i in items)
    print(f"\n{'would write' if dry else 'writing'} {n_copies:,} file(s) under {out_root}")
    if dry:
        for it in items[:20]:
            dests = sorted(it.albums) or ["(Library)"]
            print(f"  {os.path.relpath(it.path, root)}  ->  {', '.join(dests)}"
                  f"   date:{it.date_source}")
        if len(items) > 20:
            print(f"  ... and {len(items)-20:,} more")
        print("\ndry run -- pass --apply to write")
        return 0

    lib_dir = out_root / LIBRARY
    alb_root = out_root / ALBUMS
    lib_dir.mkdir(parents=True, exist_ok=True)
    alb_root.mkdir(parents=True, exist_ok=True)

    taken = set()
    stats = defaultdict(int)
    scratch = cfg.data / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="organize-", dir=scratch) as tmpdir, \
            ledger.Writer(cfg.ledgers / "organize_ledger.tsv", LEDGER_HEADER) as log:
        for n, it in enumerate(items, 1):
            targets = [(alb_root / sanitize_folder(a), a) for a in sorted(it.albums)]
            if not targets:
                targets = [(lib_dir, "")]
            for directory, album in targets:
                directory.mkdir(parents=True, exist_ok=True)
                if it.hash is None:
                    try:
                        it.hash = dedup.sha256(it.path)
                    except OSError:
                        it.hash = None
                dst = unique_path(directory, os.path.basename(it.path),
                                  it.hash, taken)
                try:
                    ok = _copy_verified(it.path, dst, cfg.safety.verify_readback)
                except OSError as exc:
                    stats["COPY_FAIL"] += 1
                    log.write([it.path, str(dst), album, it.date_source,
                               "COPY_FAIL", str(exc)[:200]])
                    continue
                if not ok:
                    # The destination gave back different bytes than we sent it.
                    stats["READBACK_MISMATCH"] += 1
                    log.write([it.path, str(dst), album, it.date_source,
                               "READBACK_MISMATCH", "copy did not re-read equal"])
                    continue

                # Only ever fill gaps: date_source == "exif" means the file has
                # its own and nothing is written over it.
                row = dict(it.meta)
                row["set_date"] = ""
                row["set_mtime"] = ""
                if it.date_source in ("sidecar", "filename"):
                    dt = None
                    if it.date_source == "sidecar" and row.get("photo_taken_ts"):
                        try:
                            dt = datetime.fromtimestamp(
                                int(row["photo_taken_ts"]), tz=timezone.utc
                            ).astimezone().replace(tzinfo=None)
                        except (ValueError, OSError, OverflowError):
                            dt = None
                    else:
                        dt = from_filename(it.path, custom, exclude)
                    if dt is not None:
                        row["set_date"] = dt.strftime("%Y:%m:%d %H:%M:%S")
                        row["set_mtime"] = str(int(dt.timestamp()))

                status, _old, _new, fields, note = embed.embed_file(
                    dst, row, tmpdir, cfg.safety.verify_readback)
                stats[status] += 1
                log.write([it.path, str(dst), album, it.date_source, status,
                           note or fields])
            if n % 200 == 0 or n == len(items):
                print(f"  {n:,}/{len(items):,}  " +
                      "  ".join(f"{k}={v}" for k, v in sorted(stats.items())),
                      flush=True)

    print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    print(f"library -> {lib_dir}")
    print(f"albums  -> {alb_root}")
    print(f"ledger  -> {cfg.ledgers / 'organize_ledger.tsv'}")
    bad = stats["READBACK_MISMATCH"] + stats["COPY_FAIL"] + stats["WRITE_FAIL"]
    return 1 if bad else 0
