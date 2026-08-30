#!/usr/bin/env python3
"""Decide what to embed in which file, and refuse to overwrite anything.

Joins Google's sidecar metadata to the files in the archive and emits
embed_plan.tsv: one row per file, listing only the fields that file is actually
missing. This is where the "fill gaps only" rule lives -- a camera's own EXIF is
better evidence of when and where a photo was taken than Google's copy of it,
which may have been re-derived or hand-edited, so an existing value always wins.

The join runs media-first, in three passes of decreasing confidence, and every
row records which pass matched it so the result can be audited:

  sha256  the file on the drive is byte-identical to a file in the Takeout.
          Unambiguous. Covers most of the library.
  name    same basename and byte size, but different content. This is expected
          for ~1,745 files that an earlier session rewrote with EXIF dates --
          the bytes changed, the identity did not.
  none    no counterpart. Phone-only files Google never had, plus recycle-bin
          and trash content. These get no plan row.

photoTakenTime is used for the date, with one caveat that matters: for files
that arrived without EXIF -- WhatsApp images, screenshots, downloads -- Google
frequently records the *upload* time rather than the capture time. It is still
better than the Takeout extraction date currently sitting in the mtime, but it
is not ground truth, and rows relying on it are flagged `date_is_upload_guess`
so a later pass can revisit them.
"""

from __future__ import annotations

import csv
import os
import subprocess
import sys
from datetime import datetime, timezone

from .exiftool import absent
from .sidecars import sidecar_media_name

COLUMNS = ["path", "set_date", "lat", "lon", "alt", "description", "set_mtime",
           "matched_by", "album", "favorited", "people", "url", "app_source",
           "origin", "date_is_upload_guess"]

# Paths in the archive that are not photographs and must never be rewritten:
# a 6 GB Windows recycle bin inherited from the NTFS era, Android trash, files
# the user deleted on the phone, and the quarantine folder for truncated files.
# Left in, the plan spends its time embedding metadata into deleted junk -- and
# some of it has corrupt EXIF that makes exiftool fail anyway.
EXCLUDE_PREFIXES = ("$RECYCLE.BIN/", ".dtrash/", ".dtrash_files/", "_damaged/",
                    "Failed videos/", "lost+found/", "System Volume Information/",
                    ".claude/")


def is_excluded(rel):
    if rel.startswith(EXCLUDE_PREFIXES):
        return True
    base = os.path.basename(rel)
    return base.startswith(".trashed-")


def load_exif_state(exif_state_path, archive):
    """path -> (has_date, has_gps) for everything currently in the archive."""
    archive = str(archive)
    state = {}
    if not os.path.exists(exif_state_path):
        sys.exit(f"no EXIF inventory at {exif_state_path} -- cannot tell what is missing")
    with open(exif_state_path, encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            parts = ln.rstrip("\n").split("|")
            if len(parts) < 4:
                continue
            path, dto, cre, gps = parts[0], parts[1], parts[2], parts[3]
            if not path.startswith(archive + "/"):
                continue
            rel = path[len(archive) + 1:]
            has_date = not (absent(dto) and absent(cre))
            has_gps = not absent(gps)
            state[rel] = (has_date, has_gps)
    return state


def load_media_index(newhash_path, original_path):
    """Takeout media path -> sha256, and sha256 -> archive relpath."""
    tk = {}
    if os.path.exists(newhash_path):
        with open(newhash_path, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                p = ln.rstrip("\n").split("\t")
                if len(p) == 3:
                    tk[p[2]] = (p[0], int(p[1]))
    # Every index maps to a LIST, not a single path. The same photo genuinely
    # exists at more than one place in the archive -- 39,967 files share only
    # 39,372 distinct hashes, because an album copy and a root copy are the same
    # bytes. Keeping just the first match would embed metadata into one copy and
    # silently leave its twin bare, which defeats the point of the exercise.
    #
    # Basenames are lower-cased: the sidecar says IMG_3531.jpg, the drive says
    # IMG_3531.JPG, and on a case-sensitive filesystem those never meet.
    sha_to_drive = {}
    name_to_drive = {}
    basename_to_drive = {}
    with open(original_path, encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            p = ln.rstrip("\n").split("\t")
            if len(p) != 3:
                continue
            sha, size, rel = p[0], int(p[1]), p[2]
            sha_to_drive.setdefault(sha, []).append(rel)
            bn = os.path.basename(rel).lower()
            name_to_drive.setdefault((bn, size), []).append(rel)
            basename_to_drive.setdefault(bn, []).append(rel)
    return tk, sha_to_drive, name_to_drive, basename_to_drive


def probe_state(archive, rels, batch=150):
    """Read date/GPS presence for specific files, naming each one explicitly.

    Naming files matters: `exiftool -r` filters by extension while recursing, so
    a bulk scan of the tree never even opens the .MP motion-photo files. Passing
    the paths through -@ makes exiftool process them regardless of extension.
    """
    archive = str(archive)
    found = {}
    for i in range(0, len(rels), batch):
        chunk = rels[i:i + batch]
        args = "\n".join(os.path.join(archive, r) for r in chunk)
        res = subprocess.run(
            ["exiftool", "-fast2", "-q", "-q", "-m", "-f",
             "-p", "$FilePath|$DateTimeOriginal|$CreateDate|$GPSLatitude", "-@", "-"],
            input=args, capture_output=True, text=True,
        )
        for ln in res.stdout.splitlines():
            p = ln.rstrip("\n").split("|")
            if len(p) < 4 or not p[0].startswith(archive + "/"):
                continue
            rel = p[0][len(archive) + 1:]
            found[rel] = (not (absent(p[1]) and absent(p[2])),
                          not absent(p[3]))
    return found


def to_exif_date(ts):
    if not ts:
        return "", ""
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone()
    except (ValueError, OSError, OverflowError):
        return "", ""
    return dt.strftime("%Y:%m:%d %H:%M:%S"), str(int(ts))


def build_plan(archive, meta_path, newhash_path, original_path, exif_state_path,
               probe=True, verbose=True):
    """The whole join, returning (plan rows, unmatched sidecars, match counts).

    Split out from main() so tests can drive it against a fixture tree without
    the config object or the TSV writing.
    """
    def say(*a, **kw):
        if verbose:
            print(*a, **kw)

    state = load_exif_state(exif_state_path, archive)
    tk, sha_to_drive, name_to_drive, basename_to_drive = load_media_index(
        newhash_path, original_path)
    say(f"drive EXIF state: {len(state):,} files")
    say(f"takeout media:    {len(tk):,} paths")

    plan = {}
    unmatched = []
    match_counts = {"sha256": 0, "name": 0, "none": 0}
    need_desc = []
    resolved = []

    # Pass 1: work out which file in the archive each sidecar is describing.
    with open(meta_path, newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            cands = sidecar_media_name(row["sidecar_path"], row.get("title", ""))
            rels = None
            how = "none"
            for cand in cands:
                if cand in tk:
                    sha, size = tk[cand]
                    if sha in sha_to_drive:
                        rels, how = sha_to_drive[sha], "sha256"
                        break
                    hit = name_to_drive.get((os.path.basename(cand).lower(), size))
                    if hit:
                        rels, how = hit, "name"
                        break
            if rels is None:
                # Last resort: basename alone, ignoring the Takeout side entirely.
                # Indexed, not scanned -- 62k sidecars against 39k files is 2.4
                # billion comparisons if you do it the obvious way.
                for cand in cands:
                    hit = basename_to_drive.get(os.path.basename(cand).lower())
                    if hit:
                        rels, how = hit, "name"
                        break
            if rels is None:
                unmatched.append((row["sidecar_path"], row.get("title", "")))
                match_counts["none"] += 1
                continue
            match_counts[how] += 1
            # One sidecar can describe several identical files in the archive.
            # Every one of them needs the metadata, not just the first.
            for rel in rels:
                if is_excluded(rel):
                    continue
                resolved.append((rel, row, how))

    # A file missing from the inventory must NOT be treated as having no
    # metadata -- that would quietly turn fill-gaps-only into overwrite. The bulk
    # inventory does miss things: `exiftool -r` only recurses into extensions it
    # recognises, which silently drops every .MP motion-photo file, and a long
    # run can be killed part way. So probe the gaps directly instead of guessing.
    unknown = sorted({rel for rel, _r, _h in resolved if rel not in state})
    if unknown and probe:
        say(f"probing {len(unknown):,} files absent from the EXIF inventory...")
        state.update(probe_state(archive, unknown))
        still = [r for r in unknown if r not in state]
        if still:
            say(f"  {len(still):,} unreadable -- excluded from the plan entirely")

    # Pass 2: decide what each file is actually missing.
    for rel, row, how in resolved:
        if rel not in state:
            continue
        has_date, has_gps = state[rel]
        date_s, epoch = to_exif_date(row.get("photo_taken_ts", ""))

        # Prefer the camera's own coordinates over Google's display copy.
        lat = row.get("exif_lat") or row.get("lat") or ""
        lon = row.get("exif_lon") or row.get("lon") or ""
        alt = row.get("exif_alt") or row.get("alt") or ""

        set_date = "" if has_date else date_s
        set_lat = "" if has_gps else lat
        set_lon = "" if has_gps else lon
        set_alt = "" if has_gps else alt
        desc = (row.get("description") or "").strip()
        people = (row.get("people") or "").strip()

        if not (set_date or (set_lat and set_lon) or desc or people):
            continue

        if desc or people:
            need_desc.append(rel)

        # mtime is only corrected when we are also supplying the date; if the
        # file already carried its own EXIF, its mtime is not ours to touch.
        prev = plan.get(rel)
        entry = {
            "path": rel,
            "set_date": set_date,
            "lat": set_lat,
            "lon": set_lon,
            "alt": set_alt,
            "description": desc,
            "set_mtime": epoch if set_date else "",
            "matched_by": how,
            "album": row.get("album", ""),
            "favorited": row.get("favorited", ""),
            "people": row.get("people", ""),
            "url": row.get("url", ""),
            "app_source": row.get("app_source", ""),
            "origin": row.get("origin", ""),
            "date_is_upload_guess": "1" if (set_date and not has_date) else "",
        }
        # A file can appear in several albums, so several sidecars describe it.
        # Keep the richest row rather than whichever came last.
        rich = ("set_date", "lat", "description", "people")
        if prev is None or (sum(1 for k in rich if entry[k])
                            > sum(1 for k in rich if prev[k])):
            plan[rel] = entry

    # Descriptions and people are the two fields the bulk EXIF inventory does not
    # capture, so they get their own pass -- but only over files Google actually
    # has data for, which keeps it cheap. Both are read in one exiftool call.
    if need_desc and probe:
        targets = sorted({os.path.join(str(archive), r) for r in need_desc})
        say(f"checking existing description/people on {len(targets):,} files...")
        had_desc, had_people = set(), set()
        for i in range(0, len(targets), 200):
            chunk = targets[i:i + 200]
            res = subprocess.run(
                ["exiftool", "-m", "-fast2", "-q", "-q", "-f",
                 "-p", "$FilePath|$ImageDescription|$PersonInImage", *chunk],
                capture_output=True, text=True,
            )
            for ln in res.stdout.splitlines():
                parts = ln.rstrip("\n").split("|")
                if len(parts) < 3 or not parts[0].startswith(str(archive) + "/"):
                    continue
                rel = os.path.relpath(parts[0], str(archive))
                if not absent(parts[1]):
                    had_desc.add(rel)
                if not absent(parts[2]):
                    had_people.add(rel)
        for rel in had_desc:
            if rel in plan:
                plan[rel]["description"] = ""
        for rel in had_people:
            if rel in plan:
                plan[rel]["people"] = ""
        say(f"  {len(had_desc):,} already had a description, "
            f"{len(had_people):,} already had people -- left alone")

    rows = [r for r in plan.values()
            if r["set_date"] or (r["lat"] and r["lon"]) or r["description"]
            or r["people"]]
    return sorted(rows, key=lambda x: x["path"]), unmatched, match_counts


def main(cfg, exif_state=None):
    archive = cfg.require_archive()
    cfg.check_mount()
    cfg.mkdirs()

    meta = cfg.derived / "takeout_metadata.tsv"
    newhash = cfg.manifests / "takeout_new_hashes.tsv"
    original = cfg.manifests / "original_hashes.tsv"
    # Produced by:
    #   exiftool -fast2 -r -q -q -m -f \
    #     -p '$FilePath|$DateTimeOriginal|$CreateDate|$GPSLatitude'
    state_file = exif_state or os.environ.get("EXIF_STATE") \
        or cfg.manifests / "all_dates.txt"
    out = cfg.derived / "embed_plan.tsv"
    unmatched_out = cfg.derived / "embed_unmatched.tsv"

    if not os.path.exists(meta):
        sys.exit(f"no {meta} -- run `gphotos sidecars` first")

    rows, unmatched, match_counts = build_plan(
        archive, meta, newhash, original, state_file)

    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, delimiter="\t",
                           lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    with open(unmatched_out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["sidecar_path", "title"])
        w.writerows(unmatched)

    n_date = sum(1 for r in rows if r["set_date"])
    n_gps = sum(1 for r in rows if r["lat"] and r["lon"])
    n_desc = sum(1 for r in rows if r["description"])
    n_ppl = sum(1 for r in rows if r["people"])
    print("\nmatched: " + "  ".join(f"{k}={v:,}" for k, v in match_counts.items()))
    print(f"plan rows: {len(rows):,}  (dates {n_date:,}, gps {n_gps:,}, "
          f"desc {n_desc:,}, people {n_ppl:,})")
    print(f"-> {out}")
    print(f"unmatched sidecars: {len(unmatched):,} -> {unmatched_out}")
    return 0
