# The monthly Takeout procedure

The reason this file exists: a Takeout was once downloaded, parsed for two
fields, and deleted. The other fields in those sidecars — GPS, captions, people,
favourites — went with it, and getting them back cost a 236 GB re-download. The
rule that would have prevented it is step 7, and it is the only step here that
is genuinely non-negotiable.

> **Never delete source data before the data derived from it has been verified.**

This is the long-form version of the incremental workflow in the README. It
assumes you have set `paths.archive` (or `$GPHOTOS_ARCHIVE`) to an existing
curated library. If you only want "unzip a Takeout, get a browsable folder",
you want `gphotos organize` and none of this.

---

## What each artefact is for

| File | Role |
|---|---|
| `manifests/original_hashes.tsv` | sha256 of every file *before* any metadata was embedded. Read-only (`chmod 444`). The reconciliation baseline, and the identity Google knows your files by. |
| `manifests/takeout_new_hashes.tsv` | sha256 of everything in the newest Takeout, produced by `ingest` without extracting a byte of media. |
| `ledgers/embed_ledger.tsv` | old hash → new hash for every file rewritten. The bridge from `original_hashes.tsv` to the current state of the drive. |
| `derived/takeout_metadata.tsv` | every field of every sidecar, flat. Re-derivable only from a Takeout you still have. |

If an indexer (Immich, digiKam, Photoprism) points at the archive, it must never
own, move, or rename anything there. Curate with the filesystem, then rescan.

---

## 1. Request the export

Google Takeout → deselect all → **Google Photos** only → 50 GB splits.

Expect the export to be considerably larger than your library, because Takeout
writes a full copy of every photo into *every* album folder it belongs to. One
export here summed to 236.5 GB extracted across 62,848 entries for only 35,675
distinct files.

Do not size the disk from the library size, and do not trust a projection from a
previous export either — estimating 236 GB when the answer was 185 GB is what
turned a comfortable margin into a full disk.

## 2. Check space, and plan to ingest as you go

Browsers download all the archives **in parallel**, so they finish at roughly
the same time and peak disk usage is the whole export rather than one archive.
Ingest and delete each archive the moment it completes; that returns its space
immediately and is what keeps the rest of the download alive.

If a media indexer is scanning at the same time, its transcode cache can eat
tens of GB at speed. Let the scan finish first, or turn transcoding off.

## 3. Ingest without extracting

```bash
gphotos ingest ~/Downloads/takeout-*.zip
```

Never extracts media. Saves every `.json` sidecar and streams every media file
through sha256, discarding the bytes. Resumable per archive: an archive is
recorded done only after being read end to end, so an interrupted run re-reads
at most one.

If a download stalled part way, `gphotos salvage file.crdownload` walks the
local file headers from byte zero and recovers the sidecars from whatever
finished. It is a salvage path, not a substitute — re-run `ingest` on the
completed archive when you have it.

## 4. Verify the download is the data you think it is

```bash
cd ~/.local/share/gphotos/manifests
LC_ALL=C sort -u -k1,1 takeout_hash_manifest.tsv > /tmp/old.tsv
LC_ALL=C sort -u -k1,1 takeout_new_hashes.tsv    > /tmp/new.tsv
join -t$'\t' -v1 /tmp/old.tsv /tmp/new.tsv | wc -l   # in old, gone from Google
join -t$'\t' -v2 /tmp/old.tsv /tmp/new.tsv | wc -l   # new since last time
```

`LC_ALL=C` is not optional. `sort` uses locale collation and `join`/`comm`
compare bytes; mixing them silently produces wrong answers, which has already
happened once on this project.

## 5. Parse the sidecars — all of them

```bash
gphotos sidecars
```

Writes `derived/takeout_metadata.tsv`: photoTakenTime, creationTime, geoData,
geoDataExif, description, favorited, people, url, appSource, origin.
Everything, not the two fields someone happens to need today.

## 6. Plan, then embed

```bash
find "$GPHOTOS_ARCHIVE" -type f > /tmp/allfiles.txt
exiftool -fast2 -q -q -m -f \
  -p '$FilePath|$DateTimeOriginal|$CreateDate|$GPSLatitude' \
  -@ /tmp/allfiles.txt > ~/.local/share/gphotos/manifests/all_dates.txt

gphotos plan
gphotos embed                      # dry run
gphotos embed --apply --limit 20   # twenty files; go and look at them
gphotos embed --apply
```

**Do not use `exiftool -r` for that inventory.** While recursing, exiftool only
opens files whose extension it recognises, and `.MP` — the video half of every
Google motion photo — is not on that list. It is skipped silently, with no
warning and a zero exit status. On one drive here that was 3,825 files, all of
which carry dates and GPS and read fine when named explicitly. Feeding paths
through `-@` bypasses the extension filter.

Run the inventory detached (`setsid … & disown`) too: a `nohup … &` inside a
wrapper that exits gets killed part way through, and truncated output looks
exactly like a completed run.

`gphotos plan` probes anything absent from the inventory rather than assuming it
has no metadata — that assumption is what would turn fill-gaps-only into
overwrite. It writes only fields a file is missing; existing EXIF always wins,
because the camera is better evidence than Google's copy of it.

`gphotos embed` copies each file to local scratch, edits it there, verifies the
tags read back, writes it to a temp file on the archive drive, renames it into
place, drops the page cache, and re-reads to confirm the hash. That last check
is the point: a failing USB bridge allocates extents that never receive data, so
a corrupted file has the correct size and reads back as zeros. **Size and mtime
comparisons cannot detect it.** Anything that fails is restored from the scratch
copy and marked in the ledger.

## 7. Verify — then, and only then, delete

```bash
gphotos verify
awk -F'\t' 'NR>1 && $5!="OK" && $5!="NOOP"' \
  ~/.local/share/gphotos/ledgers/embed_ledger.tsv     # must be empty
gphotos check-takeout                                  # exit 0 = safe to delete
```

Investigate every non-`OK` row before going further. Once the ledger is clean
the sidecars have been consumed into the files and the archives can go.

**This is the step that was skipped.** The sidecars were deleted while the
derived data was still two fields wide.

`gphotos check-takeout` will not accept a raw sha256 diff as the answer, for
three reasons: Google re-encodes on upload so a photo it already holds comes
back with different bytes; three separate tools invented three collision-rename
conventions (`X(1).jpg`, `X_1.jpg`) and they stack; and Takeout splits motion
photos into a still plus a standalone video, so the video looks absent while the
archive holds the combined original. A raw diff once reported 4,285 missing
files where the true number was zero.

## 8. Audit what is still unfixed

```bash
gphotos audit
```

Six independent questions — what has no usable date, what could be fixed from a
sidecar, same for GPS and people, where mtime disagrees with the embedded date,
where the embedded date disagrees with Google's, and what has no sidecar at all.
Independent on purpose, so one wrong answer cannot hide another.

For what is left dateless, filenames are often the only remaining evidence:

```bash
gphotos dates --apply
```

It clusters the undated files by filename shape, proposes a regex and a date
format for each cluster, shows what each would produce for real files, and saves
nothing until you type `APPLY`. The gate is deliberate: a wrong pattern stamps a
plausible wrong date on thousands of files, and a wrong date is far harder to
notice later than a missing one.

---

## Closing the local → Google direction

Ingesting a Takeout only fixes *Google → local*. The other direction needs its
own pass, and it is the one that cannot be fully automated.

### 9. Work out what Google is actually missing

```bash
gphotos phash --candidates cands.tsv --library lib.tsv --library-root ~/gphotos-library
```

**Do not use a raw hash diff for this either.** Two independent reasons, either
of which alone makes it wrong:

1. Google re-encodes on upload, so a photo it already holds returns with a
   different sha256 and looks absent.
2. **The embed pass rewrote your own files.** After step 6, most of the library
   no longer hashes to what Google is holding.

Reconcile against `original_hashes.tsv` (frozen pre-embed) plus
`embed_ledger.tsv` to map current files back to it. Never use current on-disk
hashes. On one run the raw figure was 5,433 files / 21.2 GB; perceptual
reconciliation cut it to 2,529. Run it against the newest Takeout, never a
stale one.

Getting this wrong uploads twenty thousand photos Google already has, and
undoing that in the app is manual, one at a time.

Videos are assessed separately, because perceptual-hashing one needs a frame
extracted first, and duration must additionally match within a second — a single
frame is weak evidence, and two clips of the same scene can share an opening
frame.

Only `UNIQUE` is uploaded. `REVIEW` is a deliberate human-review bucket.

Excluded by rule, not by accident: `$RECYCLE.BIN`, `.dtrash`, `.dtrash_files`,
`.trashed-*` (deleted on the phone — deleted *deliberately*, do not resurrect
them) and `_damaged`.

### 10. Upload from the PC, per file, with a ledger

```bash
gphotos upload --auth
gphotos upload --test path/to/one.jpg
# -- open the Photos app and look at it --
gphotos upload --run
```

**Do not bulk-copy the library to a phone and let the app back it up.** The app
backs up whole *device folders*, so everything in a watched folder is swept in
with no per-file control — and after the embed pass most of the library looks
new to Google. That is the recipe for twenty thousand duplicates. PC upload is
explicit per file and can be logged.

Each file is logged `BYTES_SENT` *before* the create call and again after, so a
crash mid-run can never make a sent file look un-sent. `--run` is resumable and
skips anything already `OK`, and refuses to start at all without a passed
`--test` on record.

Re-use the same OAuth client across runs: a newly created client id permanently
loses access to previously app-created data.

Uploads land in the **main library with no album** — since the April 2025 API
restriction an app may only add to albums it created, and your Google albums
were made in the phone app.

### 11. Confirm twice

```bash
gphotos upload --readback
```

Because these uploads *are* app-created, the API can read them back immediately,
which confirms the transfer. It does **not** confirm how the library treats
them, so also check that the *next* Takeout contains them exactly once.
