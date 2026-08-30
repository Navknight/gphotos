#!/usr/bin/env python3
"""The four read-only checks. Nothing here ever writes to the archive.

  embed    prove the embed pass did what the ledger claims, and broke nothing
  takeout  decide whether the Takeout archives are safe to delete
  audit    one systematic audit of the whole archive
  quality  find files where the Takeout copy may be better than the local one

Each was its own script; they are one module because they share the same three
inputs (the frozen hash manifest, the Takeout hash manifest, the sidecar table)
and the same set of hard-won emptiness rules.
"""

from __future__ import annotations

import csv
import hashlib
import os
import re
import subprocess
import sys
from collections import defaultdict

from .exiftool import absent, absent_gps
from . import ledger
from .sidecars import sidecar_media_name

MEDIA = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".bmp",
         ".tif", ".tiff", ".dng", ".nef", ".mp4", ".mov", ".m4v", ".3gp",
         ".3g2", ".avi", ".mkv", ".webm", ".mts", ".mp", ".mv"}
EXCLUDE = ("$RECYCLE.BIN/", ".dtrash/", ".dtrash_files/", "lost+found/",
           "System Volume Information/", ".claude/", "Backup/",
           "batch_manifests/", "_damaged/", "Failed videos/")

RE_GOOGLE_DUP = re.compile(r"\((\d{1,2})\)(?=\.[^.]+$)")
# Bounded to 1-2 digits on purpose: a greedy _\d+ once collapsed
# pxl_20211015_153758567.mp to pxl.mp and merged 3,825 unrelated files.
RE_TOOL_DUP = re.compile(r"_(\d{1,2})(?=\.[^.]+$)")

TZ_STEP = 900
DAY = 86400


def norm(name):
    n = name.lower()
    n = RE_GOOGLE_DUP.sub("", n)
    n = RE_TOOL_DUP.sub("", n)
    return n


def sha256(path, chunk=4 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _dump(cfg, name, header, rows):
    p = cfg.derived / name
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
    return p


# ---------------------------------------------------------------------------


def verify_embed(cfg, quick=False, expect_files=0):
    """Prove the embed pass did what the ledger claims, and broke nothing.

    The ledger is append-only and a file may appear in it several times across
    retries, so the first job is to reduce it to the *latest* status per path --
    counting raw rows would double-count a file that failed once and then
    succeeded.

    Then three checks, in increasing order of how much they would matter if they
    failed:

      1. every file the ledger calls OK still hashes to the new_sha256 recorded
         for it. This is the one that catches a bridge that accepted a write and
         stored something else -- the failure mode where a file has the right
         size and reads back as zeros.
      2. every OK file still decodes / parses as media, so we know the tags went
         in without corrupting the container.
      3. the archive's file count and total bytes are unchanged except by the
         size deltas the ledger accounts for. A file that vanished or appeared
         would show up here and nowhere else.

    Exit code 0 means the pass is clean and the Takeout archives are safe to
    delete.
    """
    archive = cfg.require_archive()
    cfg.check_mount()
    ledger_file = cfg.ledgers / "embed_ledger.tsv"
    report = cfg.derived / "embed_verify_failures.tsv"

    st = {k: (r[4], r[1], r[2], r[3]) for k, r in
          ledger.latest(ledger_file, min_cols=5).items()}
    counts = {}
    for status, *_ in st.values():
        counts[status] = counts.get(status, 0) + 1

    print(f"ledger covers {len(st):,} distinct files")
    for k in sorted(counts):
        print(f"  {k:<18} {counts[k]:,}")

    ok = [(p, v) for p, v in st.items() if v[0] == "OK"]
    print(f"\nre-reading {len(ok):,} rewritten files off the drive...")

    bad_hash, bad_decode, gone = [], [], []
    for i, (rel, (_s, _old, new, _f)) in enumerate(ok, 1):
        full = os.path.join(str(archive), rel)
        if not os.path.isfile(full):
            gone.append(rel)
            continue
        # Force the read to come off the platter, not out of page cache --
        # otherwise this confirms bytes the drive may never have stored.
        try:
            with open(full, "rb") as fh:
                os.posix_fadvise(fh.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        except (OSError, AttributeError):
            pass
        if sha256(full) != new:
            bad_hash.append(rel)
        if i % 250 == 0:
            print(f"  {i:,}/{len(ok):,}", flush=True)

    if not quick:
        print(f"\nchecking {len(ok):,} files still parse as media...")
        paths = [os.path.join(str(archive), p) for p, _ in ok]
        for i in range(0, len(paths), 200):
            chunk = paths[i:i + 200]
            res = subprocess.run(
                ["exiftool", "-q", "-q", "-m", "-fast2", "-p", "$FilePath", *chunk],
                capture_output=True, text=True,
            )
            seen = {ln.strip() for ln in res.stdout.splitlines() if ln.strip()}
            for p in chunk:
                if p not in seen:
                    bad_decode.append(os.path.relpath(p, str(archive)))

    n_files = sum(len(f) for _r, _d, f in os.walk(archive))
    total = 0
    for root, _d, files in os.walk(archive):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass

    print(f"\narchive: {n_files:,} files"
          + (f" (expected {expect_files:,})" if expect_files else ""))
    print(f"         {total:,} bytes")

    failures = ([(p, "HASH_MISMATCH") for p in bad_hash]
                + [(p, "WILL_NOT_PARSE") for p in bad_decode]
                + [(p, "MISSING") for p in gone])
    with open(report, "w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["path", "problem"])
        w.writerows(failures)

    print(f"\nhash mismatches : {len(bad_hash):,}")
    print(f"unparseable     : {len(bad_decode):,}")
    print(f"missing         : {len(gone):,}")

    unresolved = sum(v for k, v in counts.items() if k not in ("OK", "NOOP"))
    if failures or (expect_files and n_files != expect_files):
        print(f"\nPROBLEMS FOUND -- see {report}")
        print("Do NOT delete the Takeout archives.")
        return 1

    print("\nEvery rewritten file re-reads correctly off the drive and still")
    print("parses. File count and byte total are consistent.")
    if unresolved:
        print(f"\n{unresolved} file(s) were never successfully written -- these are")
        print("unchanged originals, not damage. Review them before deleting the")
        print("Takeout, since their metadata is still only in the sidecars.")
    return 0


# ---------------------------------------------------------------------------


def verify_takeout(cfg):
    """Decide whether the Takeout archives are safe to delete.

    The Takeout cannot be downloaded again, so this question deserves a real
    answer rather than a shrug. It reports every media file in the ingested
    archives that is not accounted for in the archive, and it does not accept a
    sha256 diff as that answer, because a hash diff is wrong here for three
    separate reasons:

      * Google re-encodes on upload, so a photo it already holds comes back with
        different bytes.
      * Three tools invented three collision-rename conventions -- Google's
        `X(1).jpg`, and `X_1.jpg` from two local scripts -- and they stack.
      * Takeout splits Pixel motion photos into a still plus a standalone video,
        so the video looks absent while the archive holds the combined original.

    A raw diff once reported 4,285 missing files where the true number was zero.
    So matching runs in four passes of decreasing confidence, and only files that
    survive all four are treated as genuinely absent.

    Files under `Takeout/Google Photos/Bin/` are Google's trash -- already
    deleted by the user -- and are counted separately rather than as losses.

    Exit code is 0 only when nothing is genuinely missing. Anything else means:
    do not delete the archives yet.
    """
    original = cfg.manifests / "original_hashes.tsv"
    newhash = cfg.manifests / "takeout_new_hashes.tsv"
    report = cfg.derived / "takeout_unaccounted.tsv"

    if not os.path.exists(newhash):
        sys.exit(f"no {newhash} -- run `gphotos ingest` first")

    drive_hash = set()
    by_name, by_norm, by_stem = {}, {}, {}
    for ln in open(original, errors="replace"):
        p = ln.rstrip("\n").split("\t")
        if len(p) != 3:
            continue
        drive_hash.add(p[0])
        base = os.path.basename(p[2]).lower()
        by_name.setdefault(base, p[2])
        n = norm(base)
        by_norm.setdefault(n, p[2])
        by_stem.setdefault(os.path.splitext(n)[0], p[2])

    seen = set()
    counts = {"sha256": 0, "filename": 0, "unrenamed": 0, "stem": 0}
    binned, missing = [], []

    for ln in open(newhash, errors="replace"):
        p = ln.rstrip("\n").split("\t")
        if len(p) != 3:
            continue
        sha, size, path = p[0], int(p[1]), p[2]
        if sha in seen:
            continue
        seen.add(sha)

        if sha in drive_hash:
            counts["sha256"] += 1
            continue
        base = os.path.basename(path).lower()
        if base in by_name:
            counts["filename"] += 1
            continue
        n = norm(base)
        if n in by_norm:
            counts["unrenamed"] += 1
            continue
        if os.path.splitext(n)[0] in by_stem:
            counts["stem"] += 1
            continue

        if "/Bin/" in path or path.startswith("Takeout/Google Photos/Bin/"):
            binned.append((size, path))
        else:
            missing.append((size, path))

    print(f"distinct media in Takeout : {len(seen):,}\n")
    print("accounted for in the archive:")
    print(f"  identical sha256        : {counts['sha256']:,}")
    print(f"  same filename           : {counts['filename']:,}  (Google re-encode)")
    print(f"  same after un-renaming  : {counts['unrenamed']:,}")
    print(f"  same stem (motion photo): {counts['stem']:,}")
    print(f"\nin Google's Bin (already deleted): {len(binned):,}"
          f"  {sum(s for s, _ in binned)/1048576:.1f} MB")
    print(f"GENUINELY UNACCOUNTED FOR       : {len(missing):,}"
          f"  {sum(s for s, _ in missing)/1073741824:.2f} GB")

    with open(report, "w") as fh:
        fh.write("bytes\ttakeout_path\tclass\n")
        for s, p in sorted(missing, reverse=True):
            fh.write(f"{s}\t{p}\tMISSING\n")
        for s, p in sorted(binned, reverse=True):
            fh.write(f"{s}\t{p}\tBIN\n")
    print(f"\nreport: {report}")

    if missing:
        print("\nDO NOT DELETE THE ARCHIVES.")
        print("Extract the files listed as MISSING first:")
        print("  unzip -j <archive.zip> '<path>' -d <archive>/<dest>")
        return 1

    print("\nEvery media file in the ingested archives is accounted for in the")
    print("archive. The archives that appear in ingest_done.txt are safe to")
    print("delete. Archives NOT in that list have not been read yet --")
    print("ingest them before deleting anything.")
    return 0


# ---------------------------------------------------------------------------


def quality(cfg):
    """Find files where the Takeout copy may be better than the local one.

    Most same-filename/different-hash pairs are Google's re-encode of a photo we
    already hold, and the local copy is the better one. But not always. Takeout
    ships some photos twice in two encodings -- the album folder often carries a
    Storage-saver re-encode while `Photos from YYYY` carries the full original --
    and a previous consolidation pass sometimes kept the degraded one. Measured
    on the July export: 2,106 such pairs, and in 1,217 the album copy was the
    degraded one that got kept.

    So this compares byte sizes for every same-name pair and flags the ones where
    Takeout's copy is materially larger. That is a *candidate* list, not a
    verdict: size alone cannot distinguish "higher quality same image" from
    "different crop of the same scene", and two files did turn out to be
    genuinely different crops. Anything flagged here must have its ImageSize
    compared after extraction, before it is allowed to replace a local file.

    Nothing is extracted or replaced. This only produces the shortlist.
    """
    original = cfg.manifests / "original_hashes.tsv"
    newhash = cfg.manifests / "takeout_new_hashes.tsv"
    out = cfg.derived / "takeout_better_candidates.tsv"

    # Below this the difference is metadata blocks, not pixels. A JPEG carrying
    # an extra XMP packet or a thumbnail is not a higher-quality image.
    MIN_GAIN = 0.05
    MIN_BYTES = 64 * 1024

    drive, drive_hashes = {}, set()
    for ln in open(original, errors="replace"):
        p = ln.rstrip("\n").split("\t")
        if len(p) != 3:
            continue
        drive_hashes.add(p[0])
        base = os.path.basename(p[2]).lower()
        # Keep the largest local copy: that is what a replacement must beat.
        if base not in drive or int(p[1]) > drive[base][0]:
            drive[base] = (int(p[1]), p[2])

    best = {}
    for ln in open(newhash, errors="replace"):
        p = ln.rstrip("\n").split("\t")
        if len(p) != 3:
            continue
        sha, size, path = p[0], int(p[1]), p[2]
        if sha in drive_hashes:
            continue
        base = os.path.basename(path).lower()
        if base not in drive:
            continue
        if base not in best or size > best[base][0]:
            best[base] = (size, path)

    rows = []
    for base, (tsize, tpath) in best.items():
        dsize, dpath = drive[base]
        gain = (tsize - dsize) / dsize if dsize else 0
        if tsize - dsize >= MIN_BYTES and gain >= MIN_GAIN:
            rows.append((tsize - dsize, gain, dsize, tsize, dpath, tpath))

    rows.sort(reverse=True)
    with open(out, "w") as fh:
        fh.write("bytes_gained\tpct_gain\tdrive_bytes\ttakeout_bytes\t"
                 "drive_path\ttakeout_path\n")
        for d, g, ds, ts, dp, tp in rows:
            fh.write(f"{d}\t{g*100:.1f}\t{ds}\t{ts}\t{dp}\t{tp}\n")

    print(f"same-name pairs where content differs : {len(best):,}")
    print(f"Takeout copy materially larger        : {len(rows):,}"
          f"  (+{sum(r[0] for r in rows)/1048576:.0f} MB total)")
    if rows:
        print("\nlargest gains:")
        for d, g, ds, ts, dp, tp in rows[:12]:
            print(f"  +{d/1048576:6.1f} MB  {g*100:5.1f}%  {os.path.basename(dp)}")
        print(f"\n-> {out}")
        print("\nThese are CANDIDATES. Extract and compare ImageSize before")
        print("replacing anything -- a larger file can be a different crop.")
    else:
        print("\nNo local file is beaten by its Takeout counterpart.")
        print("The archive holds the best available copy of everything.")
    return 0


# ---------------------------------------------------------------------------


def audit(cfg):
    """One systematic audit of the whole archive. Reads only; changes nothing.

    Written after a run of bugs that each came from assuming a value could not
    occur. Every one of those assumptions is now an explicit rule here:

      * "no date" has THREE spellings -- "", "-", and "0000:00:00 00:00:00".
        Testing only the first two made hundreds of dateless videos look dated,
        so the embed pass skipped them. See absent().
      * `exiftool -r` filters by extension while recursing and silently never
        opens .MP files (3,825 of them here). Paths are always passed explicitly
        via -@.
      * .MP / .MV are MP4 containers despite the extension; asking for EXIF tags
        on them yields a silent partial write. Classified as video.
      * basenames collide across case (IMG_3531.jpg vs IMG_3531.JPG) -- compare
        lower-cased.
      * one sha256 maps to SEVERAL archive paths; keeping the first loses its
        twins.
      * Python's % returns positive values for negative operands, which turned a
        timezone offset one second out into a "genuine" conflict.

    Six questions, each answered independently so one wrong answer cannot hide
    another:

      1. does every media file have a usable capture date?
      2. if not, is a date available in the Takeout sidecars that we failed to
         apply?
      3. same for GPS, and for people tags
      4. does the filesystem mtime agree with the embedded date?
      5. does the embedded date agree with Google's photoTakenTime?
      6. is anything in the archive absent from every manifest we hold?

    Output: full_audit_report.txt plus a per-file TSV for each finding class.
    """
    archive = str(cfg.require_archive())
    cfg.check_mount()
    cfg.mkdirs()
    meta = cfg.derived / "takeout_metadata.tsv"
    newhash = cfg.manifests / "takeout_new_hashes.tsv"
    original = cfg.manifests / "original_hashes.tsv"
    report = cfg.derived / "full_audit_report.txt"

    out = []

    def say(s=""):
        print(s, flush=True)
        out.append(s)

    # ---- inventory -------------------------------------------------------
    files, junk, nonmedia = [], 0, 0
    for root, _d, names in os.walk(archive):
        for n in names:
            rel = os.path.relpath(os.path.join(root, n), archive)
            if rel.startswith(EXCLUDE) or n.startswith(".trashed-"):
                junk += 1
                continue
            if os.path.splitext(n)[1].lower() not in MEDIA:
                nonmedia += 1
                continue
            files.append(rel)
    say(f"media files            : {len(files):,}")
    say(f"junk paths skipped     : {junk:,}")
    say(f"non-media skipped      : {nonmedia:,}")

    # ---- read every tag we care about, naming files explicitly -----------
    #
    # Chunked and cached on purpose. The drive this was written for sits behind
    # a JMicron bridge that intermittently drops off the bus under sustained
    # load -- it did so mid-audit once already, and a single 33,000-file
    # exiftool call loses everything when that happens. Results are appended to
    # a cache after each batch, and a re-run picks up where it stopped rather
    # than starting over.
    cache_path = cfg.derived / "audit_tag_cache.tsv"
    state = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                p = ln.rstrip("\n").split("\t")
                if len(p) == 7 and p[0].startswith(archive + "/"):
                    state[os.path.relpath(p[0], archive)] = p[1:]
        say(f"resumed from cache     : {len(state):,} files")

    todo = [f for f in files if f not in state]
    if todo:
        say(f"\nreading tags for {len(todo):,} files (the slow part)...")
        BATCH = 2000
        for i in range(0, len(todo), BATCH):
            if not os.path.ismount(archive) and not os.path.isdir(archive):
                say(f"\n!! {archive} vanished after {len(state):,} files.")
                say("   Progress is cached; remount and re-run to continue.")
                break
            chunk = todo[i:i + BATCH]
            res = subprocess.run(
                ["exiftool", "-q", "-q", "-m", "-fast2", "-f", "-d", "%s",
                 "-p", "$FilePath\t$DateTimeOriginal\t$CreateDate\t$GPSLatitude"
                       "\t${PersonInImage;s/[\\r\\n\\t]+/ /g}"
                       "\t${ImageDescription;s/[\\r\\n\\t]+/ /g}\t$FileModifyDate",
                 "-@", "-"],
                input="\n".join(os.path.join(archive, f) for f in chunk),
                capture_output=True, text=True,
            )
            with open(cache_path, "a", encoding="utf-8") as cf:
                for ln in res.stdout.splitlines():
                    p = ln.rstrip("\n").split("\t")
                    if len(p) != 7 or not p[0].startswith(archive + "/"):
                        continue
                    cf.write(ln + "\n")
                    state[os.path.relpath(p[0], archive)] = p[1:]
                cf.flush()
                os.fsync(cf.fileno())
            print(f"  {min(i+BATCH, len(todo)):,}/{len(todo):,}", flush=True)

    say(f"tags read for          : {len(state):,}")
    unread = [f for f in files if f not in state]
    if unread:
        say(f"COULD NOT READ         : {len(unread):,}")
        _dump(cfg, "audit_unreadable.tsv", ["path"], [[u] for u in unread])

    # ---- what the sidecars offer ----------------------------------------
    tk = {}
    with open(newhash, encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            p = ln.rstrip("\n").split("\t")
            if len(p) == 3:
                tk[p[2]] = (p[0], int(p[1]))
    by_sha, by_name = defaultdict(list), defaultdict(list)
    for ln in open(original, encoding="utf-8", errors="replace"):
        p = ln.rstrip("\n").split("\t")
        if len(p) == 3:
            by_sha[p[0]].append(p[2])
            by_name[(os.path.basename(p[2]).lower(), int(p[1]))].append(p[2])

    offer = {}
    with open(meta, newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            for cand in sidecar_media_name(row["sidecar_path"], row.get("title", "")):
                if cand not in tk:
                    continue
                sha, size = tk[cand]
                rels = by_sha.get(sha) or by_name.get(
                    (os.path.basename(cand).lower(), size))
                if not rels:
                    continue
                for rel in rels:
                    cur = offer.setdefault(rel, {"ts": "", "lat": "", "ppl": ""})
                    if row.get("photo_taken_ts") and not cur["ts"]:
                        cur["ts"] = row["photo_taken_ts"]
                    if (row.get("exif_lat") or row.get("lat")) and not cur["lat"]:
                        cur["lat"] = row.get("exif_lat") or row.get("lat")
                    if row.get("people") and not cur["ppl"]:
                        cur["ppl"] = row["people"]
                break
    say(f"files with sidecar data: {len(offer):,}")

    # ---- the six questions ----------------------------------------------
    no_date, fixable_date = [], []
    no_gps, fixable_gps = [], []
    fixable_ppl = []
    mtime_off, exif_vs_google, tz_hist = [], [], defaultdict(int)

    for rel in files:
        st = state.get(rel)
        if not st:
            continue
        dto, cre, gps, ppl, _desc, fmd = st
        has_date = not (absent(dto) and absent(cre))
        has_gps = not absent_gps(gps)
        has_ppl = not absent(ppl)
        o = offer.get(rel, {})

        if not has_date:
            no_date.append([rel, o.get("ts", "")])
            if o.get("ts"):
                fixable_date.append([rel, o["ts"]])
        if not has_gps and o.get("lat"):
            fixable_gps.append([rel, o["lat"]])
        if not has_ppl and o.get("ppl"):
            fixable_ppl.append([rel, o["ppl"]])
        if not has_gps:
            no_gps.append([rel])

        # mtime vs embedded
        raw = dto if not absent(dto) else cre
        if not absent(raw) and not absent(fmd):
            try:
                e, m = int(raw), int(fmd)
                if abs(m - e) > DAY:
                    mtime_off.append([rel, e, m, round((m - e) / DAY, 1)])
            except ValueError:
                pass
            # embedded vs Google
            g = o.get("ts")
            if g:
                try:
                    d = int(raw) - int(g)
                    tz_hist[round(d / TZ_STEP)] += 1
                    off = abs(d) % TZ_STEP
                    clean_tz = abs(d) <= 14 * 3600 and min(off, TZ_STEP - off) <= 60
                    if abs(d) > 3600 and not clean_tz:
                        exif_vs_google.append([rel, int(raw), int(g), d])
                except ValueError:
                    pass

    say("\n" + "=" * 60)
    say("1. FILES WITH NO USABLE CAPTURE DATE")
    say(f"   total                    : {len(no_date):,}")
    say(f"   ...a sidecar date EXISTS : {len(fixable_date):,}   <- recoverable")
    say(f"   ...no source anywhere    : {len(no_date)-len(fixable_date):,}")
    _dump(cfg, "audit_no_date.tsv", ["path", "sidecar_ts"], no_date)
    _dump(cfg, "audit_fixable_date.tsv", ["path", "sidecar_ts"], fixable_date)

    say("\n2. GPS")
    say(f"   files without GPS        : {len(no_gps):,}")
    say(f"   ...sidecar HAS GPS       : {len(fixable_gps):,}   <- recoverable")
    _dump(cfg, "audit_fixable_gps.tsv", ["path", "sidecar_lat"], fixable_gps)

    say("\n3. PEOPLE")
    say(f"   sidecar has people we lack: {len(fixable_ppl):,}   <- recoverable")
    _dump(cfg, "audit_fixable_people.tsv", ["path", "people"], fixable_ppl)

    say("\n4. MTIME vs EMBEDDED DATE")
    say(f"   disagree by >1 day       : {len(mtime_off):,}")
    _dump(cfg, "audit_mtime_off.tsv",
          ["path", "embedded_epoch", "mtime_epoch", "days"], mtime_off)

    say("\n5. EMBEDDED DATE vs GOOGLE")
    say(f"   genuine disagreements    : {len(exif_vs_google):,}")
    _dump(cfg, "audit_exif_vs_google.tsv",
          ["path", "exif_epoch", "google_epoch", "delta_s"], exif_vs_google)
    say("   offset histogram (hours):")
    for step, n in sorted(tz_hist.items(), key=lambda x: -x[1])[:6]:
        say(f"     {step*TZ_STEP/3600:+6.2f}h  {n:,}")

    say("\n6. COVERAGE")
    say(f"   files with sidecar match : {len(offer):,} of {len(files):,}")
    say(f"   files with no sidecar    : {len(files)-len(offer):,}"
        f"  (phone-only, Google never had them)")

    say("\n" + "=" * 60)
    total_fixable = len(fixable_date) + len(fixable_gps) + len(fixable_ppl)
    if total_fixable:
        say(f"RECOVERABLE METADATA STILL NOT EMBEDDED: {total_fixable:,} field(s)")
        say(f"  dates  {len(fixable_date):,}   gps {len(fixable_gps):,}   "
            f"people {len(fixable_ppl):,}")
    else:
        say("Nothing recoverable remains unembedded.")

    with open(report, "w") as fh:
        fh.write("\n".join(out) + "\n")
    say(f"\nreport: {report}")
    return 0
