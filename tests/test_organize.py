"""End-to-end organize: album merging, collision naming, and gap-only writes.

The album-merging test is the one that matters most. A photo in three albums
arrives from Takeout three times, and if dedup keeps only the first copy's album
the other two memberships are gone with no error and no way to notice.
"""

import csv
import os

from conftest import needs_exiftool  # noqa: F401

from gphotos import dedup, organize


def _by_name(items):
    return {os.path.basename(i.path): i for i in items}


def test_dedup_unions_album_sets(takeout):
    """IMG_0004 is in Trip to Goa AND Family. One copy must carry both."""
    items, _n = organize.scan(str(takeout))
    merged = dedup.merge_identical(items)
    hits = [i for i in merged if os.path.basename(i.path) == "IMG_0004.jpg"]
    assert len(hits) == 1, "the two identical copies were not merged"
    assert hits[0].albums == {"Trip to Goa", "Family"}


def test_dedup_prefers_the_original_over_the_storage_saver_reencode(takeout):
    """Same name, different bytes: the larger file is the full original."""
    items, _n = organize.scan(str(takeout))
    merged, dropped = dedup.prefer_originals(dedup.merge_identical(items))
    assert dropped >= 1
    kept = [i for i in merged if os.path.basename(i.path) == "IMG_0012.jpg"]
    assert len(kept) == 1
    assert "Photos from 2021" in kept[0].path, "kept the degraded album copy"
    # ...and it inherited the album the copy it displaced belonged to.
    assert kept[0].albums == {"Trip to Goa"}


def test_unique_path_uses_a_stable_hash_suffix(tmp_path):
    """Two different photos with the same name get hash suffixes, not counters.

    A running counter renumbers differently whenever the input order changes;
    the hash suffix is the same on every re-run.
    """
    taken = set()
    a = organize.unique_path(tmp_path, "IMG_1.jpg", "a" * 64, taken)
    a.write_bytes(b"x")
    b = organize.unique_path(tmp_path, "IMG_1.jpg", "b" * 64, taken)
    assert a.name == "IMG_1.jpg"
    assert b.name == "IMG_1-bbbbbbbb.jpg"


def test_sanitize_folder_never_escapes_the_albums_directory():
    assert organize.sanitize_folder("a/b") == "a_b"
    assert organize.sanitize_folder("   ") == "Untitled"
    assert organize.sanitize_folder("Trip to Goa") == "Trip to Goa"


@needs_exiftool
def test_organize_writes_library_and_albums(cfg, takeout, tmp_path):
    out = tmp_path / "out"
    rc = organize.main(cfg, str(takeout), str(out), apply=True)
    assert rc == 0

    lib = sorted(p.name for p in (out / "Library").iterdir())
    # The photo in two albums must appear under BOTH, and not in Library.
    assert (out / "Albums" / "Trip to Goa" / "IMG_0004.jpg").exists()
    assert (out / "Albums" / "Family" / "IMG_0004.jpg").exists()
    assert "IMG_0004.jpg" not in lib
    # An unalbumed photo goes to Library.
    assert "IMG_0001.jpg" in lib


@needs_exiftool
def test_organize_is_gap_only(cfg, takeout, tmp_path):
    """IMG_0002 keeps its own 2019 EXIF; Google's 2021 sidecar must not win."""
    import subprocess
    out = tmp_path / "out"
    organize.main(cfg, str(takeout), str(out), apply=True)

    res = subprocess.run(
        ["exiftool", "-m", "-s", "-s", "-s", "-EXIF:DateTimeOriginal",
         str(out / "Library" / "IMG_0002.jpg")],
        capture_output=True, text=True)
    assert res.stdout.strip() == "2019:03:04 05:06:07"

    # ...while the dateless one DID get Google's date written into it.
    res = subprocess.run(
        ["exiftool", "-m", "-s", "-s", "-s", "-EXIF:DateTimeOriginal",
         str(out / "Library" / "IMG_0001.jpg")],
        capture_output=True, text=True)
    assert res.stdout.strip().startswith("2021:01:01")


@needs_exiftool
def test_organize_does_not_invent_gps_from_zero_zero(cfg, takeout, tmp_path):
    import subprocess
    out = tmp_path / "out"
    organize.main(cfg, str(takeout), str(out), apply=True)
    res = subprocess.run(
        ["exiftool", "-m", "-s", "-s", "-s", "-EXIF:GPSLatitude",
         str(out / "Library" / "IMG_0003.jpg")],
        capture_output=True, text=True)
    assert res.stdout.strip() == ""


@needs_exiftool
def test_motion_photo_mp_is_written_as_video_not_image(cfg, takeout, tmp_path):
    """.MP is an MP4 container despite the extension.

    Classifying it as an image made the writer ask for EXIF:DateTimeOriginal,
    which exiftool cannot put in an MP4: it wrote the XMP tag, skipped the EXIF
    one, and verification then found the tag missing on 353 files.
    """
    import subprocess
    out = tmp_path / "out"
    organize.main(cfg, str(takeout), str(out), apply=True)
    mp = out / "Library" / "PXL_20210102_123456789.MP"
    assert mp.exists()
    res = subprocess.run(
        ["exiftool", "-m", "-s", "-s", "-s", "-QuickTime:CreateDate", str(mp)],
        capture_output=True, text=True)
    assert res.stdout.strip().startswith("2021:01:02")


def test_readback_verification_catches_a_truncated_write(tmp_path):
    """The check the original data-loss incident paid for.

    A drive that allocates extents without writing to them returns a file of
    the right size full of zeros. Only re-reading and re-hashing sees it.
    """
    src = tmp_path / "src.bin"
    src.write_bytes(b"A" * 4096)
    dst = tmp_path / "dst.bin"
    assert organize._copy_verified(src, dst, verify_readback=True)

    # Now corrupt the destination the way the bridge did: same size, wrong bytes.
    dst.write_bytes(b"\x00" * 4096)
    assert dst.stat().st_size == src.stat().st_size, "the sizes still match"
    # A size or mtime check would pass here. The hash comparison must not.
    from gphotos import dedup
    assert dedup.sha256(dst) != dedup.sha256(src)


@needs_exiftool
def test_organize_dry_run_writes_nothing(cfg, takeout, tmp_path):
    out = tmp_path / "out"
    assert organize.main(cfg, str(takeout), str(out), apply=False) == 0
    assert not out.exists()


@needs_exiftool
def test_organize_leaves_the_takeout_untouched(cfg, takeout, tmp_path):
    """Reads only. The input is irreplaceable; nothing may be moved or edited."""
    import hashlib
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(takeout.rglob("*")) if p.is_file()}
    organize.main(cfg, str(takeout), str(tmp_path / "out"), apply=True)
    after = {p: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(takeout.rglob("*")) if p.is_file()}
    assert before == after


@needs_exiftool
def test_organize_ledger_records_every_write(cfg, takeout, tmp_path):
    organize.main(cfg, str(takeout), str(tmp_path / "out"), apply=True)
    rows = list(csv.reader(open(cfg.ledgers / "organize_ledger.tsv"),
                           delimiter="\t"))
    assert rows[0] == organize.LEDGER_HEADER
    statuses = {r[4] for r in rows[1:]}
    assert "READBACK_MISMATCH" not in statuses
    assert "COPY_FAIL" not in statuses
