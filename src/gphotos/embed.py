#!/usr/bin/env python3
"""Write Google's metadata into the files themselves, safely, and prove it stuck.

Reads embed_plan.tsv (built by plan.py) and for each row writes only the fields
the file is actually missing. Existing in-file metadata is never overwritten: a
camera's own EXIF is better evidence than Google's copy of it, and the planner is
responsible for having already excluded those fields.

The write path is deliberately roundabout, because the archive drive this was
built for sits behind a JMicron USB-SATA bridge that has previously aborted the
ext4 journal mid-write. Its failure mode is the nasty one -- extents get
allocated but never receive data, so a corrupted file has exactly the right size
and reads back as zeros. Size and mtime checks cannot see it. So, per file:

    1. read the original and hash it
    2. copy to fast local storage, and do all exiftool work there
    3. verify locally: the tags are present and the file still parses
    4. hash the modified version
    5. write back to a temp file ON the archive drive, fsync, rename into place
    6. re-read from the drive and confirm the hash matches step 4

Step 6 is the one that matters. It is the only check that catches a write the
bridge silently dropped. A file that fails it is restored from the copy still
sitting on local storage and recorded as failed.

Step 6 is what safety.verify_readback turns off. It is on by default and should
stay on; the switch exists for storage where the whole dance is provably
unnecessary, not as a way to make a slow run faster.

Every file produces a ledger row (old hash -> new hash -> status), so the run is
resumable, auditable, and diffable against the frozen hash manifest afterwards.
"""

from __future__ import annotations

import csv
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

from . import ledger
from .exiftool import VIDEO_EXT, work_extension

LEDGER_HEADER = ["path", "old_sha256", "new_sha256", "fields", "status", "note"]


def sha256(path, chunk=4 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def build_args(row, ext):
    """Exiftool arguments for one file, honouring what the planner asked for.

    Images take EXIF; videos take QuickTime plus XMP, because MP4 has no EXIF
    block and the QuickTime date atoms are what players and Immich actually read.
    """
    args = []
    fields = []
    video = ext in VIDEO_EXT

    date = row.get("set_date") or ""
    if date:
        if video:
            # -api QuickTimeUTC is set on the command line; these atoms are UTC.
            # MediaCreateDate/TrackCreateDate matter because several players and
            # Immich read those rather than QuickTime:CreateDate -- this mirrors
            # what v0-go already wrote.
            args += [f"-QuickTime:CreateDate={date}", f"-QuickTime:ModifyDate={date}",
                     f"-MediaCreateDate={date}", f"-TrackCreateDate={date}"]
            args += [f"-XMP:DateTimeOriginal={date}"]
        else:
            args += [f"-EXIF:DateTimeOriginal={date}", f"-EXIF:CreateDate={date}"]
            args += [f"-XMP:DateTimeOriginal={date}"]
        fields.append("date")

    lat = row.get("lat") or ""
    lon = row.get("lon") or ""
    if lat and lon:
        flat, flon = float(lat), float(lon)
        if video:
            args += [f"-XMP:GPSLatitude={flat}", f"-XMP:GPSLongitude={flon}"]
            # The ISO-6709 string is what QuickTime-aware players read.
            args += [f"-QuickTime:GPSCoordinates={flat} {flon}"]
        else:
            args += [
                f"-EXIF:GPSLatitude={abs(flat)}",
                f"-EXIF:GPSLatitudeRef={'N' if flat >= 0 else 'S'}",
                f"-EXIF:GPSLongitude={abs(flon)}",
                f"-EXIF:GPSLongitudeRef={'E' if flon >= 0 else 'W'}",
            ]
            alt = row.get("alt") or ""
            if alt:
                falt = float(alt)
                args += [f"-EXIF:GPSAltitude={abs(falt)}",
                         f"-EXIF:GPSAltitudeRef={'0' if falt >= 0 else '1'}"]
        fields.append("gps")

    desc = (row.get("description") or "").strip()
    if desc:
        if not video:
            args.append(f"-EXIF:ImageDescription={desc}")
        args.append(f"-XMP:Description={desc}")
        fields.append("desc")

    # Google's people tags. XMP:PersonInImage is the interoperable list tag --
    # digiKam, Lightroom and Immich all read it. Written with += because the
    # planner only schedules files that have none, so there is nothing to clobber
    # and each name becomes its own list entry rather than one joined string.
    people = (row.get("people") or "").strip()
    if people:
        names = [n.strip() for n in people.split("|") if n.strip()]
        for name in names:
            # PersonInImage is the face-region tag; Subject is the generic
            # keyword list. v0-go wrote both, and so do we -- some viewers index
            # only one of them.
            args.append(f"-XMP:PersonInImage+={name}")
            args.append(f"-XMP:Subject+={name}")
        if names:
            fields.append("people")

    # Google's star. XMP:Rating=5 is what v0-go used and what digiKam/Lightroom
    # read as "favourite".
    if (row.get("favorited") or "").strip():
        args.append("-XMP:Rating=5")
        fields.append("fav")

    url = (row.get("url") or "").strip()
    if url:
        args.append(f"-XMP:Source={url}")
    app = (row.get("app_source") or "").strip()
    if app:
        args.append(f"-XMP:CreatorTool={app}")
    origin = (row.get("origin") or "").strip()
    if origin:
        args.append(f"-XMP:Label={origin}")

    return args, fields


def verify_tags(path, fields, video, xmp_only=False):
    """Confirm the tags we asked for are actually readable back off the file."""
    want = []
    if "date" in fields:
        if xmp_only:
            want.append("-XMP:DateTimeOriginal")
        else:
            want.append("-QuickTime:CreateDate" if video else "-EXIF:DateTimeOriginal")
    if "gps" in fields and not xmp_only:
        want.append("-XMP:GPSLatitude" if video else "-EXIF:GPSLatitude")
    if "people" in fields:
        want.append("-XMP:PersonInImage")
    if not want:
        return True, ""
    out = subprocess.run(
        ["exiftool", "-m", "-s", "-s", "-s", *want, str(path)],
        capture_output=True, text=True,
    )
    vals = [ln for ln in out.stdout.splitlines() if ln.strip() and ln.strip() != "-"]
    if len(vals) < len(want):
        return False, f"expected {len(want)} tag(s), read back {len(vals)}"
    return True, ""


def embed_file(src, row, tmpdir, verify_readback=True):
    """The verified write, applied to one file already sitting at `src`.

    Returns (status, old_sha, new_sha, fields, note). This is the whole point of
    the module and the one function other commands (organize) must go through --
    reimplementing "just run exiftool on it" elsewhere throws away every step
    above.
    """
    src = str(src)
    ext = os.path.splitext(src)[1].lower()
    video = ext in VIDEO_EXT

    if not os.path.isfile(src):
        return ("MISSING", "", "", "", "not on drive")

    args, fields = build_args(row, ext)
    if not args:
        return ("NOOP", "", "", "", "nothing to write")

    old = sha256(src)
    # Remember the original mtime. shutil.copyfile (used for the write-back)
    # does not carry it, so without this every rewritten file ends up stamped
    # with the time of the run -- which is exactly as wrong as the Takeout
    # extraction date this project set out to fix.
    try:
        orig_mtime = os.stat(src).st_mtime
    except OSError:
        orig_mtime = None
    # A few files carry a .webp extension but are actually JPEG bytes (seen on
    # 2024 phone exports). exiftool picks its writer module from the extension,
    # so it refuses to write EXIF into "work.webp" even though the content is a
    # perfectly normal JPEG. Sniff the magic bytes and use the real extension
    # for the write-work-copy only -- the file in the archive keeps its original
    # (wrong) extension untouched.
    work = os.path.join(tmpdir, "work" + work_extension(src))
    shutil.copy2(src, work)

    cmd = ["exiftool", "-m", "-P", "-overwrite_original"]
    if video:
        cmd += ["-api", "QuickTimeUTC=1"]
    cmd += args + [work]
    res = subprocess.run(cmd, capture_output=True, text=True)
    xmp_only = False
    if res.returncode != 0:
        # A corrupt EXIF IFD makes exiftool refuse the whole write -- e.g.
        # "Error reading OtherImageStart data in ExifIFD" on files whose EXIF
        # offsets point outside the file. XMP lives in its own JPEG segment and
        # is usually still writable, so fall back to that rather than leaving
        # the file with no metadata at all. Less complete than EXIF, but these
        # files have no readable EXIF to preserve in the first place.
        xargs = [a for a in args if a.startswith("-XMP:")]
        if xargs:
            cmd2 = ["exiftool", "-m", "-P", "-overwrite_original"]
            if video:
                cmd2 += ["-api", "QuickTimeUTC=1"]
            cmd2 += xargs + [work]
            res2 = subprocess.run(cmd2, capture_output=True, text=True)
            if res2.returncode == 0:
                xmp_only = True
                res = res2
        if not xmp_only:
            return ("EXIFTOOL_FAIL", old, "", ",".join(fields),
                    (res.stderr or res.stdout).strip().replace("\t", " ")[:200])

    ok, why = verify_tags(work, fields, video, xmp_only)
    if not ok:
        return ("VERIFY_FAIL", old, "", ",".join(fields), why)

    new = sha256(work)

    # Write back through a temp file on the drive so the rename is atomic: a
    # crash mid-copy leaves the original intact rather than a truncated photo.
    dst_tmp = src + ".embedtmp"
    try:
        shutil.copyfile(work, dst_tmp)
        with open(dst_tmp, "rb+") as fh:
            os.fsync(fh.fileno())
        os.replace(dst_tmp, src)
    except OSError as exc:
        if os.path.exists(dst_tmp):
            os.unlink(dst_tmp)
        return ("WRITE_FAIL", old, new, ",".join(fields), str(exc)[:200])

    if verify_readback:
        # Drop the page cache for this file so the re-read comes off the platter,
        # not out of RAM. Without this the verification would happily confirm
        # bytes the bridge never actually stored.
        try:
            with open(src, "rb") as fh:
                os.posix_fadvise(fh.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        except (OSError, AttributeError):
            pass

        back = sha256(src)
        if back != new:
            # The drive did not store what we sent it. Put the original back.
            shutil.copyfile(work, src)
            return ("READBACK_MISMATCH", old, new, ",".join(fields),
                    f"drive returned {back[:16]}")

    # Set the capture date if the plan supplied one; otherwise put back the mtime
    # the file had before we touched it. Never leave it at "now".
    stamped = False
    if row.get("set_mtime"):
        try:
            ts = int(row["set_mtime"])
            os.utime(src, (ts, ts))
            stamped = True
        except (ValueError, OSError):
            pass
    if not stamped and orig_mtime is not None:
        try:
            os.utime(src, (orig_mtime, orig_mtime))
        except OSError:
            pass

    # Record the degraded path in the note so a later audit can tell which files
    # carry their metadata only in XMP because their EXIF was unwritable.
    note = "xmp_only" if xmp_only else ""
    if not verify_readback:
        note = (note + ",no_readback").lstrip(",")
    return ("OK", old, new, ",".join(fields), note)


def process(cfg, row, tmpdir, dry):
    rel = row["path"]
    src = os.path.join(str(cfg.archive), rel)
    ext = os.path.splitext(rel)[1].lower()

    if not os.path.isfile(src):
        return ("MISSING", "", "", "", "not on drive")
    if dry:
        args, fields = build_args(row, ext)
        if not args:
            return ("NOOP", "", "", "", "nothing to write")
        return ("DRY", "", "", ",".join(fields), " ".join(args[:3]) + " ...")
    return embed_file(src, row, tmpdir, cfg.safety.verify_readback)


def main(cfg, apply=False, limit=0, plan_path=None, ledger_path=None):
    archive = cfg.require_archive()
    cfg.mkdirs()
    dry = not apply
    # Overridable so a second plan (e.g. filename-derived dates) runs through
    # this same verified write path with its own ledger, rather than being
    # reimplemented.
    plan_file = plan_path or os.environ.get("EMBED_PLAN") \
        or cfg.derived / "embed_plan.tsv"
    ledger_file = ledger_path or os.environ.get("EMBED_LEDGER") \
        or cfg.ledgers / "embed_ledger.tsv"

    if not os.path.exists(plan_file):
        sys.exit(f"no plan at {plan_file} -- run `gphotos plan` first")
    cfg.check_mount()

    done = ledger.statuses(ledger_file)
    with open(plan_file, newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    todo = [r for r in rows if done.get(r["path"]) not in ledger.SETTLED]
    retries = sum(1 for r in todo if r["path"] in done)
    if limit:
        todo = todo[:limit]

    print(f"plan {len(rows):,} rows, {len(rows)-len(todo):,} settled, "
          f"{len(todo):,} to process{' (DRY RUN)' if dry else ''}")
    if retries:
        print(f"  ({retries:,} of those are retries of earlier failures)")
    if not cfg.safety.verify_readback and not dry:
        print("  !! safety.verify_readback is OFF -- writes are not read back")

    stats = {}
    scratch = cfg.data / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="embed-", dir=scratch) as tmpdir:
        out = ledger.Writer(ledger_file, LEDGER_HEADER) if not dry else None
        if out:
            out.__enter__()
        try:
            for i, row in enumerate(todo, 1):
                status, old, new, fields, note = process(cfg, row, tmpdir, dry)
                stats[status] = stats.get(status, 0) + 1
                if out:
                    out.write([row["path"], old, new, fields, status, note])
                if i % 200 == 0 or i == len(todo):
                    print(f"  {i:,}/{len(todo):,}  " +
                          "  ".join(f"{k}={v}" for k, v in sorted(stats.items())),
                          flush=True)
                if status in ("READBACK_MISMATCH", "WRITE_FAIL"):
                    print(f"  !! {status} on {row['path']}: {note}", flush=True)
        finally:
            if out:
                out.__exit__(None, None, None)

    print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    if not dry:
        print(f"ledger: {ledger_file}")
    else:
        print("\ndry run -- pass --apply to write")
    return 0
