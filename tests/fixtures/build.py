#!/usr/bin/env python3
"""Build tests/fixtures/mini_takeout/ -- ~12 files, every one of them a known trap.

Regenerated rather than committed as binaries: the media are minimal but real
(exiftool must actually be able to open and write them), and generating them
keeps the repo free of opaque blobs nobody can inspect in a diff.

Run:  python3 tests/fixtures/build.py
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).parent / "mini_takeout"
GP = ROOT / "Takeout" / "Google Photos"


def jpeg(path: Path, marker: bytes = b"\x00"):
    """A minimal but genuinely decodable 1x1 baseline JPEG.

    `marker` perturbs a byte in the scan data so two fixtures can differ in
    content while sharing a name -- which is the Storage-saver case.
    """
    soi = bytes.fromhex("ffd8")
    app0 = bytes.fromhex("ffe000104a46494600010100000100010000")
    dqt = bytes.fromhex("ffdb0043") + b"\x00" + b"\x10" * 64   # 2+1+64 = 0x43
    sof = bytes.fromhex("ffc0000b08000100010101110000")        # 1x1, 1 component
    dht = bytes.fromhex("ffc40014") + b"\x00" + b"\x00" * 16 + b"\x00"
    sos = bytes.fromhex("ffda0008010100003f00")
    data = soi + app0 + dqt + sof + dht + sos + marker + bytes.fromhex("ffd9")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def png(path: Path, marker: bytes = b"\xff"):
    """A minimal 1x1 PNG. `marker` is the pixel colour, so copies differ."""
    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00" + marker * 3)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def mp4(path: Path, marker: bytes = b"\x00"):
    """A tiny but structurally valid MP4 -- enough for exiftool to write atoms.

    `marker` fills the mdat so two clips are not byte-identical; without it the
    fixture's .MP and .mp4 dedup into one file and the motion-photo case
    disappears from the test set.
    """
    def box(tag, payload=b""):
        return struct.pack(">I", 8 + len(payload)) + tag + payload
    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41")
    # An empty moov is unusual but valid enough for exiftool's QuickTime writer.
    moov = box(b"moov", box(b"mvhd", b"\x00" * 100))
    mdat = box(b"mdat", marker * 16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ftyp + moov + mdat)


def sidecar(path: Path, title, taken=None, lat=None, lon=None, desc="",
            people=(), favorited=False, extra=None):
    doc = {"title": title}
    if taken is not None:
        doc["photoTakenTime"] = {"timestamp": str(taken)}
        doc["creationTime"] = {"timestamp": str(taken)}
    # 0.0/0.0 is Google's spelling of "no location". It is a real coordinate in
    # the Gulf of Guinea, and it must be read as absent.
    doc["geoData"] = {"latitude": lat if lat is not None else 0.0,
                      "longitude": lon if lon is not None else 0.0,
                      "altitude": 0.0}
    doc["geoDataExif"] = dict(doc["geoData"])
    if desc:
        doc["description"] = desc
    if people:
        doc["people"] = [{"name": n} for n in people]
    if favorited:
        doc["favorited"] = True
    if extra:
        doc.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2))


def stamp_exif(path: Path, date: str):
    """Give a fixture its own camera EXIF, so gap-only has something to protect."""
    subprocess.run(["exiftool", "-m", "-P", "-overwrite_original",
                    f"-EXIF:DateTimeOriginal={date}",
                    f"-EXIF:CreateDate={date}", str(path)],
                   capture_output=True, check=False)


def build():
    if ROOT.exists():
        shutil.rmtree(ROOT)
    year = GP / "Photos from 2021"
    trip = GP / "Trip to Goa"
    fam = GP / "Family"

    # 1. Plain photo, sidecar has a date and real GPS, file has nothing.
    #    The straightforward case everything else is measured against.
    jpeg(year / "IMG_0001.jpg")
    sidecar(year / "IMG_0001.jpg.supplemental-metadata.json", "IMG_0001.jpg",
            taken=1609459200, lat=15.2993, lon=74.1240, desc="beach",
            people=["Asha"], favorited=True)

    # 2. Photo that ALREADY has camera EXIF, with a sidecar claiming a
    #    different (later, upload-time) date. Gap-only must leave the file's
    #    own date alone. This is the case v0-go got wrong.
    jpeg(year / "IMG_0002.jpg", b"\x01")
    stamp_exif(year / "IMG_0002.jpg", "2019:03:04 05:06:07")
    sidecar(year / "IMG_0002.jpg.supplemental-metadata.json", "IMG_0002.jpg",
            taken=1609459200)

    # 3. GPS written as 0.0/0.0 -- Google's "no location". Must read as absent,
    #    not as a coordinate off the coast of Ghana.
    jpeg(year / "IMG_0003.jpg", b"\x02")
    sidecar(year / "IMG_0003.jpg.json", "IMG_0003.jpg", taken=1609462800)

    # 4+5. The SAME photo in two albums. Dedup must keep one copy carrying the
    #      union of both album names.
    jpeg(trip / "IMG_0004.jpg", b"\x03")
    jpeg(fam / "IMG_0004.jpg", b"\x03")
    sidecar(trip / "IMG_0004.jpg.supplemental-metadata.json", "IMG_0004.jpg",
            taken=1609466400, lat=15.5, lon=74.0)
    sidecar(fam / "IMG_0004.jpg.supplemental-metadata.json", "IMG_0004.jpg",
            taken=1609466400, lat=15.5, lon=74.0)

    # 6. Pixel motion photo. The .MP is an MP4 container despite the extension,
    #    and its sidecar is filed under the .jpg title -- tier 1's .MP rule.
    mp4(year / "PXL_20210102_123456789.MP", b"\x0a")
    sidecar(year / "PXL_20210102_123456789.MP.jpg.supplemental-metadata.json",
            "PXL_20210102_123456789.MP.jpg", taken=1609590896)

    # 7. Zero-date video: the sidecar has no photoTakenTime at all, so the only
    #    date available is the one in the filename.
    mp4(year / "VID_20210115_101112.mp4", b"\x0b")
    sidecar(year / "VID_20210115_101112.mp4.json", "VID_20210115_101112.mp4")

    # 8. A file named .webp that is actually JPEG bytes. exiftool picks its
    #    writer from the extension and refuses EXIF on a .webp, so the scratch
    #    copy must be renamed by magic-byte sniff.
    jpeg(year / "work.webp", b"\x04")
    sidecar(year / "work.webp.supplemental-metadata.json", "work.webp",
            taken=1609470000)

    # 9. Google's duplicate rename: the sidecar "IMG_0009.jpg(1).json"
    #    describes the file "IMG_0009(1).jpg", not "IMG_0009.jpg".
    jpeg(year / "IMG_0009(1).jpg", b"\x05")
    sidecar(year / "IMG_0009.jpg(1).json", "IMG_0009(1).jpg", taken=1609473600)

    # 10. Truncated sidecar title: Google clipped the title, so the media name
    #     is only a PREFIX of it. Tier 3, per-directory prefix matching.
    jpeg(year / "a_very_long_original_filename_that_go.jpg", b"\x06")
    sidecar(year / "a_very_long_original_filename_that_go.json",
            "a_very_long_original_filename_that_google_truncated.jpg",
            taken=1609477200)

    # 11. An -edited derivative with no sidecar of its own. Tier 4 normalises
    #     the suffix away and finds the original's.
    png(year / "IMG_0011-edited.png", b"\x40")
    png(year / "IMG_0011.png", b"\x80")
    sidecar(year / "IMG_0011.png.supplemental-metadata.json", "IMG_0011.png",
            taken=1609480800)

    # 12+13. Storage-saver pair: same name, different bytes, the album copy
    #        materially smaller. The larger original must win, inheriting the
    #        smaller one's album.
    (trip / "IMG_0012.jpg").parent.mkdir(parents=True, exist_ok=True)
    jpeg(trip / "IMG_0012.jpg", b"\x08")
    big = year / "IMG_0012.jpg"
    jpeg(big, b"\x09")
    big.write_bytes(big.read_bytes() + b"\x00" * 200_000)  # the full original
    sidecar(year / "IMG_0012.jpg.supplemental-metadata.json", "IMG_0012.jpg",
            taken=1609484400)

    # An album's own metadata.json -- describes the album, never a photo.
    (trip / "metadata.json").write_text(json.dumps({"title": "Trip to Goa"}))

    n = sum(1 for p in ROOT.rglob("*") if p.is_file())
    media = sum(1 for p in ROOT.rglob("*")
                if p.is_file() and not p.name.endswith(".json"))
    print(f"built {ROOT}: {media} media file(s), {n - media} sidecar(s)")


if __name__ == "__main__":
    if shutil.which("exiftool") is None:
        sys.exit("exiftool is needed to stamp the fixture that must keep its "
                 "own EXIF")
    build()
