"""The gap-only rule and absent()-detection, which is where the real damage lives.

A bug here does not crash: it silently overwrites a camera's own capture date
with Google's upload time, across thirty thousand files, and nothing complains.
So these test the negative -- what must NOT be planned -- more than the positive.
"""

import pytest
from conftest import needs_exiftool  # noqa: F401

from gphotos import plan, sidecars
from gphotos.exiftool import absent, absent_gps


@pytest.mark.parametrize("value", ["", "-", "0000:00:00 00:00:00", "   ", None])
def test_absent_knows_all_three_spellings_of_nothing(value):
    """"" / "-" / "0000..." all mean no date. Missing one skipped 100s of videos."""
    assert absent(value)


def test_absent_accepts_a_real_date():
    assert not absent("2019:03:04 05:06:07")


def test_absent_gps_treats_zero_zero_as_no_location():
    """0 deg 0' 0" is the Gulf of Guinea. No photo here was taken there."""
    assert absent_gps("0 deg 0' 0.00\"")
    assert absent_gps("-")
    assert not absent_gps("15 deg 17' 57.48\" N")


def _fake_archive(tmp_path, takeout):
    """A stand-in archive holding copies of the fixture media, plus its manifests."""
    import hashlib
    import shutil
    archive = tmp_path / "archive"
    archive.mkdir()
    original = tmp_path / "original_hashes.tsv"
    newhash = tmp_path / "takeout_new_hashes.tsv"
    orig_rows, new_rows = [], []
    for src in sorted(takeout.rglob("*")):
        if not src.is_file() or src.name.endswith(".json"):
            continue
        rel = src.name
        dst = archive / rel
        if dst.exists():
            continue
        shutil.copy2(src, dst)
        sha = hashlib.sha256(dst.read_bytes()).hexdigest()
        size = dst.stat().st_size
        orig_rows.append(f"{sha}\t{size}\t{rel}\n")
        new_rows.append(f"{sha}\t{size}\t"
                        f"{src.relative_to(takeout.parent)}\n")
    original.write_text("".join(orig_rows))
    newhash.write_text("".join(new_rows))
    return archive, original, newhash


@needs_exiftool
def test_plan_never_overwrites_an_existing_date(tmp_path, takeout):
    """IMG_0002 already carries camera EXIF; Google's later date must not win."""
    archive, original, newhash = _fake_archive(tmp_path, takeout)
    meta = tmp_path / "meta.tsv"
    sidecars.parse_sidecar_dir(str(takeout), meta, tmp_path / "albums.tsv")

    # The bulk inventory deliberately covers nothing, forcing the probe path --
    # which is the path that must not guess "no metadata" for what it cannot see.
    state = tmp_path / "all_dates.txt"
    state.write_text("")

    rows, _unmatched, _counts = plan.build_plan(
        archive, meta, newhash, original, state, verbose=False)
    by_path = {r["path"]: r for r in rows}

    # IMG_0002.jpg has its own EXIF date -> no date may be planned for it.
    assert by_path.get("IMG_0002.jpg", {}).get("set_date", "") == ""
    # IMG_0001.jpg has none -> the sidecar date IS planned.
    assert by_path["IMG_0001.jpg"]["set_date"].startswith("20")
    assert by_path["IMG_0001.jpg"]["date_is_upload_guess"] == "1"


@needs_exiftool
def test_plan_never_plans_zero_gps(tmp_path, takeout):
    """0.0/0.0 in a sidecar must produce no GPS row at all."""
    archive, original, newhash = _fake_archive(tmp_path, takeout)
    meta = tmp_path / "meta.tsv"
    sidecars.parse_sidecar_dir(str(takeout), meta, tmp_path / "albums.tsv")
    state = tmp_path / "all_dates.txt"
    state.write_text("")

    rows, _u, _c = plan.build_plan(archive, meta, newhash, original, state,
                                   verbose=False)
    by_path = {r["path"]: r for r in rows}
    assert by_path["IMG_0003.jpg"]["lat"] == ""
    assert by_path["IMG_0001.jpg"]["lat"].startswith("15.2993")


@needs_exiftool
def test_plan_carries_description_and_people(tmp_path, takeout):
    archive, original, newhash = _fake_archive(tmp_path, takeout)
    meta = tmp_path / "meta.tsv"
    sidecars.parse_sidecar_dir(str(takeout), meta, tmp_path / "albums.tsv")
    state = tmp_path / "all_dates.txt"
    state.write_text("")
    rows, _u, _c = plan.build_plan(archive, meta, newhash, original, state,
                                   verbose=False)
    r = {x["path"]: x for x in rows}["IMG_0001.jpg"]
    assert r["description"] == "beach"
    assert r["people"] == "Asha"
    assert r["favorited"] == "1"


def test_unreadable_file_is_not_treated_as_having_no_metadata(tmp_path, takeout):
    """A file absent from the inventory and unprobeable gets NO plan row.

    Guessing "it has nothing" is exactly what turns fill-gaps-only into
    overwrite, so absence of evidence must mean absence of a plan row.
    """
    archive, original, newhash = _fake_archive(tmp_path, takeout)
    meta = tmp_path / "meta.tsv"
    sidecars.parse_sidecar_dir(str(takeout), meta, tmp_path / "albums.tsv")
    state = tmp_path / "all_dates.txt"
    state.write_text("")
    # probe=False simulates every probe failing.
    rows, _u, _c = plan.build_plan(archive, meta, newhash, original, state,
                                   probe=False, verbose=False)
    assert rows == []


def test_excluded_paths_never_reach_the_plan():
    for rel in ("$RECYCLE.BIN/x.jpg", ".dtrash/x.jpg", "_damaged/x.jpg",
                "lost+found/x.jpg", "album/.trashed-1-x.jpg"):
        assert plan.is_excluded(rel), rel
    assert not plan.is_excluded("Trip to Goa/IMG_0001.jpg")
