#!/usr/bin/env python3
"""Decide which local files are GENUINELY absent from Google Photos, perceptually.

Content hashing already told us which local files have no byte-identical twin in
the Takeout. That is not the same as "not in Google Photos": Google re-encodes,
resizes and strips metadata, so the very same picture routinely lands with a
completely different sha256. Uploading those would create real duplicates in the
user's library, which can only be removed by hand.

So each candidate is compared to the whole library by PERCEPTUAL hash, which
survives re-encoding and rescaling:

  pHash (DCT-based)  - primary; robust to quality changes and mild resizing
  dHash (gradient)   - secondary; different failure modes, so agreement between
                       the two is much stronger evidence than either alone

A 64-bit hash pair plus Hamming distance gives a graded verdict rather than a
yes/no, because the cost of the two mistakes is not symmetric:

  DUPLICATE  (pHash <= 4 and dHash <= 6)  - confidently already in Google Photos
  REVIEW     (pHash <= 10)                - probably a duplicate, but shown so a
                                            human can decide before anything is
                                            skipped
  UNIQUE     (otherwise)                  - safe to upload

Videos are hashed from an extracted frame (10% into the clip, which avoids
black lead-ins) and additionally required to match on duration, since a single
frame is weak evidence on its own for video.

Both phases cache to disk and are resumable: hashing 36k files is expensive and
must survive a disconnect.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor

import imagehash
import numpy as np
from PIL import Image

try:
    # Pillow has no built-in HEIC decoder; without this every .heic silently
    # fails to hash and would be reported as "unique" for lack of a match.
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

Image.MAX_IMAGE_PIXELS = None

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".wmv",
             ".mp", ".mv", ".mp~2", ".mp~3", ".mp-edited"}
DUP_P, DUP_D, REVIEW_P = 4, 6, 10
DUR_TOL = 1.0


def video_frame(path):
    """A representative frame as a PIL image, plus duration. (None, dur) on failure.

    Taken 10% into the clip: frame 0 is often black or a fade-in, which would
    make unrelated videos collide on an all-dark hash.

    Note the contract: this returns (None, duration) when extraction fails,
    never a bare None. Testing the tuple for truthiness therefore never catches
    a failure and feeds None straight to phash().
    """
    try:
        d = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30).stdout.strip()
        dur = float(d) if d and d != "N/A" else 0.0
    except Exception:
        dur = 0.0
    seek = max(0.0, dur * 0.1)
    # NOT tempfile.mktemp(): it only checks for collision at call time, so with
    # a process pool two workers can be handed the same name, overwrite each
    # other's extracted frame, and delete a file another worker is still
    # reading. That silently failed 2,807 files on the first run. mkstemp gives
    # each worker an exclusively-created file.
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        # PNG, not JPEG. Motion-photo frames are often tagged full-range YUV,
        # which ffmpeg's mjpeg encoder refuses outright ("Non full-range YUV is
        # non-standard") -- that silently killed 2,807 .MP files. PNG skips the
        # JPEG encoder and has no such constraint.
        #
        # Two seeks: 10% in (avoids black lead-ins), falling back to frame 0,
        # because on very short clips the 10% seek can land past the only
        # decodable keyframe.
        for ss in (f"{seek:.2f}", "0"):
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                            "-ss", ss, "-i", path, "-frames:v", "1",
                            "-vf", "scale=256:-1", "-y", tmp],
                           capture_output=True, timeout=90)
            if os.path.getsize(tmp) > 0:
                im = Image.open(tmp)
                im.load()      # decode before the file is removed
                return im, dur
    except Exception:
        pass
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return None, dur


def hash_one(path):
    """(phash_int, dhash_int, duration) for one file, or None if unreadable."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in VIDEO_EXT:
            im, dur = video_frame(path)
            if im is None:
                return None
        else:
            im = Image.open(path)
            # JPEG draft mode decodes at 1/8 scale straight from the DCT
            # coefficients -- far cheaper than a full decode, and the hashes
            # only need a 32x32 thumbnail anyway.
            try:
                im.draft("L", (64, 64))
            except Exception:
                pass
            im.load()
            dur = 0.0
        im = im.convert("L")
        p = int(str(imagehash.phash(im, hash_size=8)), 16)
        d = int(str(imagehash.dhash(im, hash_size=8)), 16)
        return p, d, dur
    except Exception:
        return None


def _job(args):
    key, path = args
    return key, hash_one(path)


def build_index(files, cache_path, workers, label):
    """{key: [phash, dhash, dur]} for many files, cached and resumable."""
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            for line in f:
                try:
                    k, p, d, du = line.rstrip("\n").split("\t")
                    cache[k] = [int(p), int(d), float(du)]
                except ValueError:
                    pass
    todo = [(k, v) for k, v in files.items() if k not in cache]
    print(f"{label}: {len(files):,} files | cached {len(cache):,} | "
          f"to hash {len(todo):,}", flush=True)
    if not todo:
        return cache
    n = 0
    with open(cache_path, "a", encoding="utf-8") as out, \
            ProcessPoolExecutor(max_workers=workers) as ex:
        for key, res in ex.map(_job, todo, chunksize=16):
            n += 1
            if res is None:
                continue
            p, d, du = res
            cache[key] = [p, d, du]
            out.write(f"{key}\t{p}\t{d}\t{du}\n")
            if n % 2000 == 0:
                out.flush()
                print(f"  {label}: {n:,}/{len(todo):,}", flush=True)
    return cache


def _read_manifest(path, root):
    """{sha: absolute path} from a sha\\tsize\\trelpath TSV."""
    out = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            x = line.rstrip("\n").split("\t")
            if len(x) == 3:
                out[x[0]] = os.path.join(str(root), x[2])
    return out


def main(cfg, args):
    workers = args.workers or max(1, (os.cpu_count() or 2) - 2)
    cfg.mkdirs()

    lib_files = _read_manifest(args.library, args.library_root)
    cand_root = cfg.archive or "."
    cand_files = _read_manifest(args.candidates, cand_root)
    cand_rel = {k: os.path.relpath(v, str(cand_root)) for k, v in cand_files.items()}
    if args.limit:
        cand_files = dict(list(cand_files.items())[:args.limit])
    if cfg.archive is not None:
        cfg.check_mount()

    lib = build_index(lib_files, cfg.derived / "phash_library.tsv", workers,
                      "library")
    cand = build_index(cand_files, cfg.derived / "phash_candidates.tsv", workers,
                       "candidates")

    lib_keys = [k for k in lib if k in lib_files]
    if not lib_keys:
        print("empty library index")
        return 1
    LP = np.array([lib[k][0] for k in lib_keys], dtype=np.uint64)
    LD = np.array([lib[k][1] for k in lib_keys], dtype=np.uint64)
    LU = np.array([lib[k][2] for k in lib_keys], dtype=np.float64)

    dup = review = uniq = 0
    rows = []
    for k, path in cand_files.items():
        if k not in cand:
            continue
        p, d, du = cand[k]
        pd = np.bitwise_count((LP ^ np.uint64(p)).astype(np.uint64))
        i = int(np.argmin(pd))
        best_p = int(pd[i])
        best_d = int(np.bitwise_count((LD[i:i + 1] ^ np.uint64(d)).astype(np.uint64))[0])
        is_video = os.path.splitext(path)[1].lower() in VIDEO_EXT
        # one frame is thin evidence for a video, so demand the durations agree
        dur_ok = (not is_video) or (du > 0 and abs(LU[i] - du) <= DUR_TOL)

        if best_p <= DUP_P and best_d <= DUP_D and dur_ok:
            v = "DUPLICATE"; dup += 1
        elif best_p <= REVIEW_P and dur_ok:
            v = "REVIEW"; review += 1
        else:
            v = "UNIQUE"; uniq += 1
        rows.append((v, best_p, best_d, cand_rel[k],
                     os.path.relpath(lib_files[lib_keys[i]], str(args.library_root))))

    out = cfg.derived / "upload_candidates.tsv"
    with open(out, "w", encoding="utf-8") as f:
        f.write("verdict\tphash_dist\tdhash_dist\tpath\tnearest_in_gphotos\n")
        for r in sorted(rows, key=lambda r: (r[0], r[1])):
            f.write("\t".join(str(c) for c in r) + "\n")

    total = dup + review + uniq
    print(f"\nchecked {total:,} candidates against {len(lib_keys):,} library photos")
    print(f"  DUPLICATE (already in Google Photos): {dup:,}")
    print(f"  REVIEW    (probably duplicate)      : {review:,}")
    print(f"  UNIQUE    (safe to upload)          : {uniq:,}")
    print(f"\nwrote {out}")
    print("\nOnly UNIQUE should be uploaded. REVIEW is a human-review bucket by")
    print("design: a wrong call there puts a visible duplicate in the library")
    print("that can only be removed by hand.")
    return 0
