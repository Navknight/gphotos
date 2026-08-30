#!/usr/bin/env python3
"""Parse every Takeout sidecar into one table, keeping ALL of it.

This is the step that was done wrong the first time. A previous run read each
*.supplemental-metadata.json for exactly two fields -- photoTakenTime and the
album folder -- wrote them to a two-column table, and then deleted the Takeout.
Everything else in those files went with it: GPS, captions, people tags,
favourites. Hence this script reads the whole record and writes it out flat, so
that if a later step turns out to need a field nobody thought about, the answer
is a re-parse and not a 236 GB re-download.

Output: takeout_metadata.tsv, one row per sidecar.

Two points worth knowing about the fields:

geoData vs geoDataExif -- geoDataExif is what the camera recorded; geoData is
what Google Photos displays, which the user may have edited by hand. They are
usually identical. Both are emitted so the embed step can prefer the camera's
value and fall back to Google's. Google writes 0.0/0.0 for "no location", which
is a real coordinate in the Gulf of Guinea; it is treated as absent here, on the
grounds that none of these photos were taken 500 km off the coast of Ghana.

photoTakenTime -- for files that arrived without EXIF (WhatsApp, screenshots,
downloads) this is frequently the *upload* time, not the capture time. It is
recorded faithfully and flagged downstream; it is not ground truth.
"""

from __future__ import annotations

import csv
import json
import os
import sys

COLUMNS = [
    "sidecar_path", "title", "album", "photo_taken_ts", "creation_ts",
    "lat", "lon", "alt", "exif_lat", "exif_lon", "exif_alt",
    "description", "favorited", "people", "url", "app_source", "origin",
]

SIDECAR_SUFFIXES = (".supplemental-metadata.json", ".json")


def origin_label(doc):
    """Reproduce the Go tool's gphotos: origin label.

    v0-go wrote this to XMP:Label, so matching its format keeps the files this
    pass touches consistent with the ones that tool already wrote.
    """
    o = doc.get("googlePhotosOrigin")
    if not isinstance(o, dict):
        return ""
    parts = []
    if isinstance(o.get("fromSharedAlbum"), dict):
        parts.append("fromSharedAlbum")
    if isinstance(o.get("webUpload"), dict):
        parts.append("webUpload")
    mob = o.get("mobileUpload")
    if isinstance(mob, dict):
        parts.append("mobileUpload")
    comp = o.get("composition")
    if isinstance(comp, dict) and comp.get("type"):
        parts.append("composition=" + str(comp["type"]))
    if isinstance(mob, dict):
        if mob.get("deviceType"):
            parts.append("deviceType=" + str(mob["deviceType"]))
        df = mob.get("deviceFolder")
        if isinstance(df, dict) and df.get("localFolderName"):
            parts.append("deviceFolder=" + str(df["localFolderName"]))
    return "gphotos:" + ",".join(parts) if parts else ""


def app_source_of(doc):
    src = doc.get("appSource")
    if isinstance(src, dict) and src.get("androidPackageName"):
        return str(src["androidPackageName"])
    return ""


def coord(block):
    """Pull (lat, lon, alt) out of a geoData block, treating 0/0 as absent."""
    if not isinstance(block, dict):
        return "", "", ""
    lat = block.get("latitude")
    lon = block.get("longitude")
    alt = block.get("altitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return "", "", ""
    if lat == 0 and lon == 0:
        return "", "", ""
    alt_s = f"{alt}" if isinstance(alt, (int, float)) and alt != 0 else ""
    return f"{lat!r}", f"{lon!r}", alt_s


def ts(block):
    if isinstance(block, dict):
        v = block.get("timestamp")
        if v not in (None, ""):
            return str(v)
    return ""


def album_of(rel):
    """The immediate parent directory is the album (or 'Photos from YYYY')."""
    parts = rel.split(os.sep)
    return parts[-2] if len(parts) >= 2 else ""


def sidecar_media_name(sidecar_rel, title):
    """The media file a sidecar describes, as a path relative to the Takeout."""
    d = os.path.dirname(sidecar_rel)
    base = os.path.basename(sidecar_rel)
    for suf in SIDECAR_SUFFIXES:
        if base.endswith(suf):
            stem = base[: -len(suf)]
            break
    else:
        stem = os.path.splitext(base)[0]
    # Google renames duplicates as "foo.jpg(1).json" describing "foo(1).jpg".
    if stem.endswith(")") and "(" in stem:
        head, _, tail = stem.rpartition("(")
        root, ext = os.path.splitext(head)
        if ext:
            stem = f"{root}({tail}{ext}"
    cands = [stem]
    if title and title != stem:
        cands.append(title)
    return [os.path.join(d, c) if d else c for c in cands]


def parse_sidecar_dir(sidecar_dir, out_path, albums_path):
    """Walk a directory of sidecars into the two TSVs. Returns (rows, albums, bad)."""
    rows = 0
    albums = {}
    bad = []

    with open(out_path, "w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n",
                       quoting=csv.QUOTE_MINIMAL)
        w.writerow(COLUMNS)

        for root, _dirs, files in os.walk(sidecar_dir):
            for name in sorted(files):
                if not name.lower().endswith(".json"):
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, sidecar_dir)
                try:
                    with open(full, encoding="utf-8") as jf:
                        doc = json.load(jf)
                except (OSError, ValueError) as exc:
                    bad.append((rel, str(exc)))
                    continue
                if not isinstance(doc, dict):
                    bad.append((rel, "not a JSON object"))
                    continue

                # An album's own metadata.json describes the album, not a photo:
                # it has a title but no photoTakenTime. Collect separately.
                if "photoTakenTime" not in doc and "title" in doc:
                    albums[album_of(rel)] = doc.get("title") or ""
                    continue

                lat, lon, alt = coord(doc.get("geoData"))
                elat, elon, ealt = coord(doc.get("geoDataExif"))

                people = doc.get("people")
                people_s = ""
                if isinstance(people, list):
                    people_s = "|".join(
                        p.get("name", "") for p in people
                        if isinstance(p, dict) and p.get("name")
                    )

                desc = doc.get("description") or ""
                # Newlines and tabs in a caption would corrupt the TSV. csv's
                # quoting handles them, but downstream awk/cut would still break,
                # so flatten them here instead.
                desc = " ".join(desc.split())

                w.writerow([
                    rel,
                    doc.get("title") or "",
                    album_of(rel),
                    ts(doc.get("photoTakenTime")),
                    ts(doc.get("creationTime")),
                    lat, lon, alt, elat, elon, ealt,
                    desc,
                    "1" if doc.get("favorited") else "",
                    people_s,
                    doc.get("url") or "",
                    app_source_of(doc),
                    origin_label(doc),
                ])
                rows += 1

    with open(albums_path, "w", newline="") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["folder", "album_title"])
        for folder, title in sorted(albums.items()):
            w.writerow([folder, title])

    return rows, albums, bad


def main(cfg, sidecar_dir=None):
    sidecar_dir = str(sidecar_dir or cfg.sidecar_dir)
    if not os.path.isdir(sidecar_dir):
        sys.exit(f"no sidecar directory at {sidecar_dir} -- run `gphotos ingest` first")
    cfg.mkdirs()
    out = cfg.derived / "takeout_metadata.tsv"
    albums_out = cfg.derived / "takeout_albums.tsv"

    rows, albums, bad = parse_sidecar_dir(sidecar_dir, out, albums_out)

    print(f"parsed {rows:,} photo sidecars -> {out}")
    print(f"       {len(albums):,} album metadata files -> {albums_out}")
    if bad:
        print(f"\n{len(bad)} sidecar(s) could not be parsed:")
        for rel, exc in bad[:20]:
            print(f"  {rel}: {exc}")
        if len(bad) > 20:
            print(f"  ... and {len(bad) - 20} more")
    return 0
