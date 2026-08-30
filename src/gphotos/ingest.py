#!/usr/bin/env python3
"""Read a Google Takeout archive WITHOUT extracting the media.

Why this exists: the last Takeout extracted to 236.5 GB, because Takeout puts a
full copy of every photo inside every album folder it belongs to. There is only
239 GB free on the NVMe, so "download it and unzip it" does not fit -- and it
does not need to. We already hold a verified copy of the media on the archive
drive. What was lost, and what this run is actually for, is the *sidecar
metadata*: geoData, description, favorited, people. That is a few hundred MB of
JSON.

So each archive is read in a single streaming pass:

  * every .json member is extracted (small, and it is the whole point)
  * every other member is piped through sha256 and thrown away -- never written

That gives a complete hash inventory of the new Takeout at zero disk cost, which
is what lets us (a) prove the re-download matches the Takeout that was deleted,
by diffing against the frozen hash manifest, and (b) find files that exist in
Google Photos but are missing from the archive. Only those get extracted, later,
deliberately.

Resumable at archive granularity: an archive is recorded as done only after it
has been read end to end, so an interrupted run re-reads at most one archive.
Nothing here modifies the archive drive.

`salvage` is the other half of this module: the same idea applied to a zip that
never finished downloading.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import tarfile
import time
import zipfile
import zlib

CHUNK = 4 << 20

LOCAL_SIG = b"PK\x03\x04"
CENTRAL_SIG = b"PK\x01\x02"


def _load_done(done_file):
    if not os.path.exists(done_file):
        return set()
    with open(done_file) as fh:
        return {ln.strip() for ln in fh if ln.strip()}


def _mark_done(done_file, name):
    with open(done_file, "a") as fh:
        fh.write(name + "\n")


def safe_member_path(root, member):
    """Map an archive member to a path under `root`, refusing to escape it.

    Takeout is not hostile, but this reads untrusted archive members and a member
    named ../../.ssh/authorized_keys would otherwise be written wherever it liked.
    """
    parts = []
    for part in member.replace("\\", "/").split("/"):
        if part in ("", ".", ".."):
            continue
        parts.append(part)
    if not parts:
        return None
    dest = os.path.join(root, *parts)
    real_root = os.path.realpath(root)
    if os.path.commonpath(
        [os.path.realpath(os.path.dirname(dest)) or real_root, real_root]
    ) != real_root:
        return None
    return dest


def _handle_stream(sidecar_dir, member, reader, out, counts):
    """Either save a sidecar or hash-and-discard a media file."""
    if member.lower().endswith(".json"):
        dest = safe_member_path(sidecar_dir, member)
        if dest is None:
            counts["skipped"] += 1
            return
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        size = 0
        with open(dest, "wb") as fh:
            while True:
                buf = reader.read(CHUNK)
                if not buf:
                    break
                size += len(buf)
                fh.write(buf)
        counts["sidecars"] += 1
        counts["sidecar_bytes"] += size
        return

    h = hashlib.sha256()
    size = 0
    while True:
        buf = reader.read(CHUNK)
        if not buf:
            break
        size += len(buf)
        h.update(buf)
    out.write(f"{h.hexdigest()}\t{size}\t{member}\n")
    counts["media"] += 1
    counts["media_bytes"] += size


def _ingest_zip(sidecar_dir, path, out, counts):
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            with zf.open(info) as reader:
                _handle_stream(sidecar_dir, info.filename, reader, out, counts)


def _ingest_tar(sidecar_dir, path, out, counts):
    # "r|*" is the streaming mode: sequential, no seeking, constant memory.
    with tarfile.open(path, "r|*") as tf:
        for info in tf:
            if not info.isfile():
                continue
            reader = tf.extractfile(info)
            if reader is None:
                continue
            _handle_stream(sidecar_dir, info.name, reader, out, counts)


def ingest(cfg, paths):
    sidecar_dir = str(cfg.sidecar_dir)
    hashes = cfg.manifests / "takeout_new_hashes.tsv"
    done_file = cfg.ledgers / "ingest_done.txt"
    cfg.mkdirs()
    os.makedirs(sidecar_dir, exist_ok=True)

    done = _load_done(done_file)
    todo = [p for p in paths if os.path.basename(p) not in done]
    if not todo:
        print(f"nothing to do -- every archive given is already in {done_file}")
        return 0

    print(f"{len(todo)} archive(s) to read, {len(paths) - len(todo)} already done")
    grand = {"sidecars": 0, "media": 0, "sidecar_bytes": 0, "media_bytes": 0,
             "skipped": 0}

    for path in todo:
        name = os.path.basename(path)
        counts = {"sidecars": 0, "media": 0, "sidecar_bytes": 0,
                  "media_bytes": 0, "skipped": 0}
        t0 = time.time()
        print(f"\n=== {name} ===", flush=True)

        # Append, and flush per archive, so an interrupted run loses nothing that
        # ingest_done.txt claims is complete.
        with open(hashes, "a") as out:
            if zipfile.is_zipfile(path):
                _ingest_zip(sidecar_dir, path, out, counts)
            elif tarfile.is_tarfile(path):
                _ingest_tar(sidecar_dir, path, out, counts)
            else:
                print(f"  !! not a zip or tar, skipping: {path}")
                continue
            out.flush()
            os.fsync(out.fileno())

        dt = time.time() - t0
        rate = (counts["media_bytes"] / dt / 1048576) if dt else 0
        print(
            f"  sidecars {counts['sidecars']:,} ({counts['sidecar_bytes']/1048576:.1f} MB)"
            f"  media {counts['media']:,} ({counts['media_bytes']/1073741824:.2f} GB)"
            f"  {dt/60:.1f} min, {rate:.0f} MB/s",
            flush=True,
        )
        _mark_done(done_file, name)
        for k in grand:
            grand[k] += counts[k]

    print(
        f"\nTOTAL  sidecars {grand['sidecars']:,} ({grand['sidecar_bytes']/1048576:.1f} MB)"
        f"  media {grand['media']:,} ({grand['media_bytes']/1073741824:.2f} GB)"
    )
    if grand["skipped"]:
        print(f"  {grand['skipped']} member(s) skipped as unsafe paths")
    print(f"\nsidecars -> {sidecar_dir}")
    print(f"hashes   -> {hashes}")
    print("\nThe archives are still on disk and nothing has been deleted.")
    return 0


# --------------------------------------------------------------------------
# Salvage: sidecars out of a zip that never finished downloading.
#
# Chrome's .crdownload files held ~140 GB of Takeout that stalled when the disk
# filled. Python's zipfile cannot open them: it reads the central directory,
# which lives at the *end* of the archive and therefore does not exist yet.
#
# But a zip is also a linear sequence of entries, each introduced by a local file
# header carrying its own name and size. Walking those headers from byte zero
# recovers everything that finished downloading, which for archives sitting at
# ~90% is nearly all of it. The sidecars are what matter here -- they are tiny,
# they are scattered evenly through the archive, and they are the only part of
# the Takeout that is not already on the archive drive.
#
# Media entries are skipped without decompressing. Any entry whose data runs past
# the end of the truncated file is dropped, and JSON that fails to parse is
# discarded rather than written, so a torn final entry cannot poison the output.
#
# This is a salvage path, not a replacement for the real ingest. When the
# download is eventually completed, run `gphotos ingest` over the finished
# archives -- it produces the authoritative sidecar set and the media hashes
# this cannot.
# --------------------------------------------------------------------------


def _find_next_header(fh, start):
    """Scan forward for the next local header when the size field was 0.

    Streaming zips set sizes to zero in the local header and put the real values
    in a trailing data descriptor. Rather than parse descriptors, just look for
    where the next entry begins.
    """
    fh.seek(start)
    window = b""
    pos = start
    while True:
        chunk = fh.read(1 << 20)
        if not chunk:
            return None
        buf = window + chunk
        idx = buf.find(LOCAL_SIG)
        if idx == -1:
            idx2 = buf.find(CENTRAL_SIG)
            if idx2 != -1:
                return None
            window = buf[-3:]
            pos += len(chunk)
            continue
        return pos - len(window) + idx


def _salvage_one(sidecar_dir, path, stats):
    size = os.path.getsize(path)
    saved = skipped = torn = bad = 0
    offset = 0
    with open(path, "rb") as fh:
        while True:
            fh.seek(offset)
            head = fh.read(30)
            if len(head) < 30 or head[:4] != LOCAL_SIG:
                break
            (_ver, flags, method, _mt, _md, _crc,
             csize, _usize, nlen, elen) = struct.unpack("<HHHHHIIIHH", head[4:30])
            name = fh.read(nlen)
            fh.read(elen)
            data_start = offset + 30 + nlen + elen
            try:
                name_s = name.decode("utf-8")
            except UnicodeDecodeError:
                name_s = name.decode("utf-8", "replace")

            if flags & 0x8 or csize == 0:
                nxt = _find_next_header(fh, data_start)
                if nxt is None:
                    torn += 1
                    break
                csize = nxt - data_start
                offset = nxt
            else:
                offset = data_start + csize

            if data_start + csize > size:
                torn += 1
                break

            if not name_s.lower().endswith(".json"):
                skipped += 1
                continue

            fh.seek(data_start)
            raw = fh.read(csize)
            try:
                blob = zlib.decompress(raw, -15) if method == 8 else raw
                doc = json.loads(blob.decode("utf-8"))
                if not isinstance(doc, dict):
                    raise ValueError("not an object")
            except (zlib.error, ValueError, UnicodeDecodeError):
                bad += 1
                continue

            dest = safe_member_path(sidecar_dir, name_s)
            if dest is None:
                bad += 1
                continue
            if os.path.exists(dest):
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as out:
                out.write(blob)
            saved += 1

    pct = 100.0 * offset / size if size else 0
    print(f"  read {offset/1073741824:.1f} of {size/1073741824:.1f} GB ({pct:.0f}%)  "
          f"sidecars saved {saved:,}  media skipped {skipped:,}  "
          f"unparseable {bad:,}" + ("  [truncated tail]" if torn else ""))
    stats["saved"] += saved
    stats["skipped"] += skipped
    stats["bad"] += bad


def salvage(cfg, paths):
    sidecar_dir = str(cfg.sidecar_dir)
    os.makedirs(sidecar_dir, exist_ok=True)
    stats = {"saved": 0, "skipped": 0, "bad": 0}
    for p in paths:
        print(f"\n=== {os.path.basename(p)} ===", flush=True)
        try:
            _salvage_one(sidecar_dir, p, stats)
        except OSError as exc:
            print(f"  cannot read: {exc}")
    print(f"\nTOTAL sidecars recovered: {stats['saved']:,}")
    print(f"sidecars -> {sidecar_dir}")
    print("\nNothing was deleted. Re-run `gphotos ingest` on the completed "
          "archives when the download finishes; it supersedes this.")
    return 0
