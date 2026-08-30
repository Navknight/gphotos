"""Sidecar parsing and the four-tier media<->JSON matcher.

Each test names the Takeout naming quirk it exists for. A regression in any of
them shows up as "metadata silently not applied", which is the failure mode this
whole project is built to notice.
"""

import csv

from gphotos import sidecars
from gphotos.organize import SidecarIndex, detect_album, scan


def _index(takeout):
    idx = SidecarIndex()
    for p in takeout.rglob("*.json"):
        idx.add(str(p))
    return idx


def _resolve(takeout, media_rel):
    idx = _index(takeout)
    hit = idx.resolve(str(takeout / media_rel))
    return None if hit is None else hit.rsplit("/", 1)[-1]


GP = "Google Photos"
Y = f"{GP}/Photos from 2021"


def test_tier1_exact_title(takeout):
    """The ordinary case: the sidecar's `title` names the file exactly."""
    assert _resolve(takeout, f"{Y}/IMG_0001.jpg") == \
        "IMG_0001.jpg.supplemental-metadata.json"


def test_motion_photo_mp_filed_under_jpg(takeout):
    """A Pixel .MP is described by the sidecar titled <name>.MP.jpg."""
    assert _resolve(takeout, f"{Y}/PXL_20210102_123456789.MP") == \
        "PXL_20210102_123456789.MP.jpg.supplemental-metadata.json"


def test_google_duplicate_rename(takeout):
    """IMG_0009.jpg(1).json describes IMG_0009(1).jpg, not IMG_0009.jpg."""
    assert _resolve(takeout, f"{Y}/IMG_0009(1).jpg") == "IMG_0009.jpg(1).json"


def test_truncated_title_prefix_match(takeout):
    """Google clipped the title, so the media name is only a prefix of it."""
    assert _resolve(takeout, f"{Y}/a_very_long_original_filename_that_go.jpg") \
        == "a_very_long_original_filename_that_go.json"


def test_edited_derivative_falls_back_to_original(takeout):
    """-edited has no sidecar of its own; tier 4 normalises the suffix away."""
    assert _resolve(takeout, f"{Y}/IMG_0011-edited.png") == \
        "IMG_0011.png.supplemental-metadata.json"


def test_album_metadata_json_is_not_a_photo_sidecar(takeout):
    """An album's own metadata.json describes the album and must never match."""
    idx = _index(takeout)
    for paths in list(idx.by_title.values()) + list(idx.by_key.values()):
        assert not any(p.endswith("/metadata.json") for p in paths)


def test_detect_album(takeout):
    root = str(takeout)
    assert detect_album(root, f"{root}/{GP}/Trip to Goa/IMG_0004.jpg") == "Trip to Goa"
    # "Photos from YYYY" is Takeout's name for "no album", not an album.
    assert detect_album(root, f"{root}/{Y}/IMG_0001.jpg") == ""


def test_zero_gps_is_absent(takeout, cfg, tmp_path):
    """Google writes 0.0/0.0 for "no location"; that is not a coordinate."""
    out = tmp_path / "meta.tsv"
    sidecars.parse_sidecar_dir(str(takeout), out, tmp_path / "albums.tsv")
    rows = {r["title"]: r for r in csv.DictReader(open(out), delimiter="\t")}
    assert rows["IMG_0003.jpg"]["lat"] == ""
    assert rows["IMG_0003.jpg"]["exif_lat"] == ""
    # ...while a real coordinate survives.
    assert rows["IMG_0001.jpg"]["lat"].startswith("15.2993")


def test_parse_keeps_the_fields_the_first_attempt_threw_away(takeout, tmp_path):
    """GPS, description, people and favorited are the whole reason for a re-parse."""
    out = tmp_path / "meta.tsv"
    sidecars.parse_sidecar_dir(str(takeout), out, tmp_path / "albums.tsv")
    rows = {r["title"]: r for r in csv.DictReader(open(out), delimiter="\t")}
    r = rows["IMG_0001.jpg"]
    assert r["description"] == "beach"
    assert r["people"] == "Asha"
    assert r["favorited"] == "1"
    assert r["photo_taken_ts"] == "1609459200"


def test_album_metadata_collected_separately(takeout, tmp_path):
    out, albums = tmp_path / "meta.tsv", tmp_path / "albums.tsv"
    _rows, found, _bad = sidecars.parse_sidecar_dir(str(takeout), out, albums)
    assert "Trip to Goa" in found.values()


def test_scan_pairs_every_media_file(takeout):
    items, _n = scan(str(takeout))
    # 14 media files in the fixture; each must be seen exactly once.
    assert len(items) == 14
    assert [i.path for i in items if i.sidecar is None] == []
    # The album copy of the Storage-saver pair has no sidecar file of its own,
    # but tier 1 matches it to the one whose title is its name -- which is right:
    # the two copies are the same photo and Google's metadata describes both.
    album_copy = next(i for i in items
                      if i.path.endswith("Trip to Goa/IMG_0012.jpg"))
    assert album_copy.sidecar.endswith(
        "IMG_0012.jpg.supplemental-metadata.json")
