#!/usr/bin/env python3
"""Collapse the many copies Takeout ships into one file, without losing albums.

Takeout puts a full copy of every photo inside every album folder it belongs to,
so a photo in three albums arrives three times. Grouping is by size first and
sha256 second, which matters at this scale: hashing 33,000 files takes minutes,
and a file whose byte size is unique cannot possibly have a duplicate, so it is
never opened at all.

Two rules for picking the survivor, both learned the hard way:

  * ALBUM SETS UNION. The kept copy inherits the albums of every copy in the
    group. The Go version did this and it is the reason the album structure
    survives dedup at all -- keep only the first copy's album and a photo in
    three albums lands in one.
  * ORIGINALS BEAT STORAGE-SAVER RE-ENCODES. Takeout ships some photos twice in
    two encodings: the album folder often carries a Storage-saver re-encode
    while `Photos from YYYY` carries the full original. These are NOT
    byte-identical, so they never meet in a sha256 group -- they are caught by
    the second pass here, on filename, where the larger file wins. Measured on
    the July export: 2,106 such pairs, and a previous consolidation kept the
    degraded copy in 1,217 of them.

The Go version's tiebreak was `if a.DateAccuracy < b.DateAccuracy { return
a.DateAccuracy < b.DateAccuracy }` -- a tautology that made the comparison
collapse to path length alone. Fixed here: better date evidence wins, then the
shorter path (which is the un-suffixed, un-renamed copy).
"""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict

# How much bigger a same-name file must be before it is believed to be the
# original rather than a re-encode. Below this the difference is metadata blocks
# -- an extra XMP packet or a thumbnail is not a higher-quality image.
MIN_GAIN = 0.05
MIN_BYTES = 64 * 1024

# Lower is better evidence. Matches dates.resolve()'s source names.
DATE_RANK = {"exif": 0, "sidecar": 1, "filename": 2, "none": 3}


def sha256(path, chunk=4 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class Item:
    """One media file found in a Takeout, before dedup."""

    __slots__ = ("path", "size", "albums", "date_source", "hash", "sidecar",
                 "meta")

    def __init__(self, path, size=None, albums=(), date_source="none",
                 sidecar=None, meta=None):
        self.path = str(path)
        self.size = os.path.getsize(self.path) if size is None else size
        self.albums = set(albums)
        self.date_source = date_source
        self.sidecar = sidecar
        self.meta = meta or {}
        self.hash = None

    def __repr__(self):
        return f"Item({os.path.basename(self.path)!r}, albums={sorted(self.albums)})"


def group_identical(items, hash_fn=sha256):
    """{key: [items]} grouped by exact content.

    Size first so unique-sized files are never hashed. A file that cannot be
    hashed gets a group of its own keyed by its path -- never merged with
    anything, because "we could not read it" is not evidence of sameness.
    """
    by_size = defaultdict(list)
    for it in items:
        by_size[it.size].append(it)

    groups = {}
    for size, group in by_size.items():
        if len(group) == 1:
            groups[f"{size}bytes:{group[0].path}"] = group
            continue
        by_hash = defaultdict(list)
        for it in group:
            if it.hash is None:
                try:
                    it.hash = hash_fn(it.path)
                except OSError:
                    by_hash[f"nohash:{size}:{it.path}"].append(it)
                    continue
            by_hash[it.hash].append(it)
        groups.update(by_hash)
    return groups


def _better(a: Item, b: Item) -> bool:
    """True if `a` is the copy to keep."""
    ra, rb = DATE_RANK.get(a.date_source, 9), DATE_RANK.get(b.date_source, 9)
    if ra != rb:
        return ra < rb
    # The shorter path is the un-suffixed original: "IMG_1.jpg" beats
    # "Album/IMG_1(1).jpg".
    if len(a.path) != len(b.path):
        return len(a.path) < len(b.path)
    return a.path <= b.path


def merge_identical(items, hash_fn=sha256):
    """One item per distinct content, carrying the UNION of every copy's albums."""
    out = []
    for group in group_identical(items, hash_fn).values():
        best = group[0]
        for it in group[1:]:
            if _better(it, best):
                best = it
        for it in group:
            best.albums |= it.albums
            # A copy that found a sidecar hands it to the survivor; the album
            # copy often has one when the Photos-from-YYYY copy does not.
            if best.sidecar is None and it.sidecar is not None:
                best.sidecar = it.sidecar
                best.meta = it.meta
                best.date_source = it.date_source
        out.append(best)
    return out


def prefer_originals(items):
    """Second pass: same name, different bytes -- keep the larger.

    This is the Storage-saver case. A re-encode has the same filename as the
    original but different content, so sha256 grouping cannot see it. Only a
    materially larger file displaces a smaller one; a few kilobytes is a
    metadata block, not more pixels.
    """
    by_name = defaultdict(list)
    for it in items:
        by_name[os.path.basename(it.path).lower()].append(it)

    out, dropped = [], 0
    for group in by_name.values():
        if len(group) == 1:
            out.append(group[0])
            continue
        keep = max(group, key=lambda i: i.size)
        losers = [i for i in group if i is not keep]
        # Only collapse the ones the size rule is confident about; anything too
        # close in size may be a genuinely different picture (two files in the
        # July export turned out to be different crops) and is kept separately.
        for it in losers:
            gain = (keep.size - it.size) / it.size if it.size else 0
            if keep.size - it.size >= MIN_BYTES and gain >= MIN_GAIN:
                keep.albums |= it.albums
                dropped += 1
            else:
                out.append(it)
        out.append(keep)
    return out, dropped


def demo():
    """Self-check: album union, tiebreak, and the size rule."""
    a = Item.__new__(Item)
    a.path, a.size, a.albums, a.date_source, a.hash = "Photos from 2020/x.jpg", 100, {""}, "sidecar", "h1"
    a.sidecar, a.meta = None, {}
    b = Item.__new__(Item)
    b.path, b.size, b.albums, b.date_source, b.hash = "Albums/Trip/x.jpg", 100, {"Trip"}, "sidecar", "h1"
    b.sidecar, b.meta = None, {}
    c = Item.__new__(Item)
    c.path, c.size, c.albums, c.date_source, c.hash = "Albums/Family/x.jpg", 100, {"Family"}, "sidecar", "h1"
    c.sidecar, c.meta = None, {}

    merged = merge_identical([a, b, c], hash_fn=lambda p: "h1")
    assert len(merged) == 1, merged
    assert merged[0].albums == {"", "Trip", "Family"}, merged[0].albums
    # Shortest path wins the tiebreak at equal date evidence.
    assert merged[0].path == "Albums/Trip/x.jpg", merged[0].path

    # Better date evidence beats a shorter path.
    d = Item.__new__(Item)
    d.path, d.size, d.albums, d.date_source, d.hash = "zzzzzzzzzzzz/x.jpg", 100, set(), "exif", "h1"
    d.sidecar, d.meta = None, {}
    e = Item.__new__(Item)
    e.path, e.size, e.albums, e.date_source, e.hash = "a.jpg", 100, set(), "none", "h1"
    e.sidecar, e.meta = None, {}
    assert merge_identical([e, d], hash_fn=lambda p: "h1")[0].date_source == "exif"

    # Storage-saver: the big one wins and inherits the small one's album.
    big = Item.__new__(Item)
    big.path, big.size, big.albums, big.date_source, big.hash = "Photos from 2020/y.jpg", 5_000_000, set(), "sidecar", "h2"
    big.sidecar, big.meta = None, {}
    small = Item.__new__(Item)
    small.path, small.size, small.albums, small.date_source, small.hash = "Albums/Trip/y.jpg", 400_000, {"Trip"}, "sidecar", "h3"
    small.sidecar, small.meta = None, {}
    kept, dropped = prefer_originals([big, small])
    assert dropped == 1 and len(kept) == 1, (kept, dropped)
    assert kept[0].size == 5_000_000 and kept[0].albums == {"Trip"}, kept[0]

    # Nearly-equal sizes are NOT collapsed -- they may be different crops.
    near = Item.__new__(Item)
    near.path, near.size, near.albums, near.date_source, near.hash = "Albums/T/z.jpg", 1_000_000, set(), "sidecar", "h4"
    near.sidecar, near.meta = None, {}
    near2 = Item.__new__(Item)
    near2.path, near2.size, near2.albums, near2.date_source, near2.hash = "Photos from 2020/z.jpg", 1_010_000, set(), "sidecar", "h5"
    near2.sidecar, near2.meta = None, {}
    kept, dropped = prefer_originals([near, near2])
    assert dropped == 0 and len(kept) == 2, (kept, dropped)
    print("dedup: ok")


if __name__ == "__main__":
    demo()
