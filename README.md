# gphotos

Turn a Google Takeout into a photo library you actually own — and, optionally,
keep an existing curated archive's embedded metadata in sync with Google's
sidecars without ever overwriting what your camera already recorded.

Google Takeout hands you your photos with their metadata stripped out of the
files and dumped into JSON sidecars beside them, one full copy of every photo in
every album it belongs to. This tool pairs them back up, deduplicates, writes the
metadata into the files where it belongs, and proves it stuck.

Requires **Python 3.11+** and **exiftool**. Nothing else, for the basic path.

---

## Quickstart

```bash
# 1. install exiftool
sudo apt install libimage-exiftool-perl     # Debian/Ubuntu
sudo pacman -S perl-image-exiftool          # Arch
brew install exiftool                       # macOS

# 2. install gphotos
pip install .

# 3. unzip your Takeout, then look at what would happen
gphotos organize ~/Downloads/Takeout

# 4. do it
gphotos organize ~/Downloads/Takeout -o ~/Photos --apply
```

You get:

```
~/Photos/
├── Library/          every distinct photo, exactly once
└── Albums/
    ├── Trip to Goa/  a copy for each album it belonged to
    └── Family/
```

No config file, no account, no OAuth, no network. The Takeout is only read —
nothing in it is moved, renamed or deleted.

---

## Safety

This tool writes to image files. Read this section before pointing it at
anything irreplaceable.

**It never overwrites metadata a file already has.** If your camera recorded a
capture date, that date wins over Google's — Google's copy may have been
re-derived at upload or hand-edited in the Photos app, and for anything that
arrived without EXIF (WhatsApp, screenshots, downloads) Google frequently
records the *upload* time rather than the capture time. Only genuinely empty
fields are filled. This rule is called **gap-only** and it is not configurable.

**Every write is read back.** After each file is written, the page cache for it
is dropped and the file is re-read off the device and re-hashed. This exists
because of a real incident: a USB-SATA bridge that aborted the ext4 journal
mid-write, allocating extents that never received data. The result is a file of
exactly the right size that reads back as zeros. **Size and mtime checks cannot
see it.** A file that fails the re-read is restored from the scratch copy and
recorded as failed, never silently accepted.

You can turn this off with `safety.verify_readback = false` or
`--no-verify-readback`. Do that only for storage where the whole dance is
provably unnecessary — never to make a slow run faster.

**Everything is dry-run by default.** `organize` and `embed` do nothing without
`--apply`. `gphotos upload --run` additionally refuses to start until a single
`--test` upload has succeeded and been recorded.

**Every run leaves a ledger.** An append-only TSV under `<data>/ledgers/`, one
row per file: old hash, new hash, fields written, status. Runs resume from it,
audits diff against it, and it is greppable at 3am. A file that failed and later
succeeded appears twice; the last row wins.

**The destructive commands need to be switched on.** `plan`, `embed`, `verify`
and `audit` modify or inspect an existing library *in place*. They refuse to run
unless `paths.archive` is set, and `paths.archive` is empty by default. Nothing
you do with `organize` can reach them.

---

## Commands

| Command | What it does | Writes to |
|---|---|---|
| `organize <takeout>` | Extracted Takeout → `Library/` + `Albums/`. The headline command. | `paths.output` |
| `ingest <archive>…` | Read Takeout zips/tars **without extracting the media**: saves every sidecar, hashes every media file and discards the bytes. | `<data>/manifests`, `<data>/takeout_sidecars` |
| `salvage <file>…` | Pull sidecars out of a zip that never finished downloading, by walking local file headers. | `<data>/takeout_sidecars` |
| `sidecars` | Parse every ingested sidecar into one flat table — all fields, not the two you need today. | `<data>/derived` |
| `plan` | Decide what to embed in which archive file. Lists only fields the file is missing. | `<data>/derived` |
| `embed [--apply]` | Write the plan, through the verified write path. | your archive |
| `verify` | Re-read every file the ledger calls OK and confirm it still hashes correctly and still parses. | `<data>/derived` |
| `audit` | Six independent read-only questions over the whole archive: what has no date, what could be fixed from sidecars, where mtime disagrees with EXIF, and so on. | `<data>/derived` |
| `check-takeout` | Are the Takeout archives safe to delete? Four passes of decreasing confidence, because a raw hash diff once reported 4,285 missing files where the true number was zero. | `<data>/derived` |
| `quality` | Files where the Takeout copy may be a better original than the local one. A shortlist, not a verdict. | `<data>/derived` |
| `dates [--apply]` | Propose filename-date regexes for still-undated files. Saves nothing until you type `APPLY`. | `<data>/date_patterns.json` |
| `upload` | *[extra]* Send local-only files to Google Photos. | Google |
| `phash` | *[extra]* Perceptual dedup: which local files is Google *really* missing? | `<data>/derived` |
| `phone` | *[extra]* Mirror the archive to an Android phone over adb. | your phone |

Global flags: `--config FILE`, `--data DIR`, `--archive DIR`,
`--no-verify-readback`.

---

## Configuration

Optional. Precedence, highest first: **CLI flag → environment variable → config
file → built-in default.**

The config file is `$GPHOTOS_CONFIG`, else `./gphotos.toml`, else
`~/.config/gphotos/config.toml`. A missing file is not an error.

```toml
[paths]
data   = "~/.local/share/gphotos"   # $GPHOTOS_DATA   — manifests, ledgers, tokens
output = "./Output"                 # $GPHOTOS_OUTPUT — where organize writes
archive = ""                        # $GPHOTOS_ARCHIVE — empty = organize-only mode

[safety]
verify_readback = true              # $GPHOTOS_VERIFY_READBACK
require_mount_uuid = ""             # $GPHOTOS_REQUIRE_MOUNT_UUID

[google]                            # the upload extra only
client_secret_file = "~/.config/gphotos/client_secret.json"  # $GPHOTOS_CLIENT_SECRET
```

`gphotos.example.toml` is the same thing with every option explained.

`require_mount_uuid` guards the case where a removable drive comes back on a
different device node holding different data. Find yours with
`findmnt -n -o UUID --target /path/to/archive`. Empty disables the check.

### What lives where

Nothing personal ever goes in the repo. The data directory holds it all:

```
~/.local/share/gphotos/
├── manifests/   original_hashes.tsv (freeze it 0444), takeout_new_hashes.tsv, all_dates.txt
├── ledgers/     embed_ledger.tsv, upload_ledger.tsv, organize_ledger.tsv, ingest_done.txt
├── derived/     takeout_metadata.tsv, embed_plan.tsv, audit_*.tsv
├── tokens/      OAuth tokens, chmod 600
└── logs/
```

`original_hashes.tsv` is the reconciliation baseline: sha256 of every file
*before* any metadata was embedded. Freeze it read-only (`chmod 444`). It is the
identity Google knows your files by, and once you have rewritten them their
current hashes are useless for that comparison.

Credentials live in `~/.config/gphotos/`, separate from the data directory, so
the data can be backed up without dragging secrets along.

---

## The incremental workflow

This is the advanced path: you already have a curated library and you want each
month's Takeout folded into it without disturbing what is already there.

```bash
export GPHOTOS_ARCHIVE=/srv/photos/Pictures     # or set paths.archive

# 1. Read the archives without extracting 236 GB you do not have room for.
gphotos ingest ~/Downloads/takeout-*.zip

# 2. Parse every sidecar field, not the two you need today.
gphotos sidecars

# 3. Inventory what the archive already carries. Feed paths through -@;
#    `exiftool -r` filters by extension while recursing and silently never
#    opens .MP motion-photo files.
find "$GPHOTOS_ARCHIVE" -type f > /tmp/allfiles.txt
exiftool -fast2 -q -q -m -f \
  -p '$FilePath|$DateTimeOriginal|$CreateDate|$GPSLatitude' \
  -@ /tmp/allfiles.txt > ~/.local/share/gphotos/manifests/all_dates.txt

# 4. Plan, read it, then write.
gphotos plan
gphotos embed                    # dry run
gphotos embed --apply --limit 20 # twenty files, then look at them
gphotos embed --apply            # the rest

# 5. Prove it.
gphotos verify
gphotos check-takeout            # exit 0 = the archives are safe to delete
```

Do steps 4 and 5 in that order every time. **Never delete source data before the
data derived from it has been verified** — a previous run of this project read
each sidecar for two fields, deleted the Takeout, and paid for the rest with a
236 GB re-download.

`docs/procedure.md` is the long-form version with the reasoning.

---

## Optional extras

Each is a separate dependency set, lazily imported. A base install never sees
them, and never prompts you for OAuth.

```bash
pip install "gphotos[upload]"    # google-auth, google-auth-oauthlib, requests
pip install "gphotos[phash]"     # pillow, imagehash, numpy, pillow-heif
pip install "gphotos[all]"
```

### `upload` — send local-only files to Google Photos

Needs an OAuth **Desktop app** client from the Google Cloud console with the
Photos Library API enabled. Download the JSON and point
`google.client_secret_file` at it (`chmod 600`).

```bash
gphotos upload --auth                 # consent, once
gphotos upload --test path/to/one.jpg # exactly one file
# -- open the Photos app and look at it --
gphotos upload --run                  # refuses without a passed --test
gphotos upload --readback             # confirm what actually landed
```

Uploads land in the main library with no album: since the April 2025 API
restriction an app may only add to albums it created. `--root DIR` creates its
own albums, one per folder.

Re-use the same OAuth client across runs — a new client id permanently loses
access to data an earlier one created.

### `phash` — which files is Google *really* missing?

Never upload from a raw hash diff. Google re-encodes on upload, so a photo it
already holds comes back with a completely different sha256; and once you have
run `embed`, most of your library no longer hashes to what Google is holding
either. Uploading on that basis creates thousands of real duplicates that can
only be removed by hand.

```bash
gphotos phash --candidates cands.tsv \
              --library lib.tsv --library-root ~/gphotos-library
```

Verdicts are `DUPLICATE` / `REVIEW` / `UNIQUE`. Upload only `UNIQUE`. `REVIEW`
is a human-review bucket by design.

Video needs `ffmpeg` and `ffprobe` on PATH.

### `phone` — mirror to an Android device

Needs `adb` and `rsync`. Two hops, because the drive and the phone usually
cannot share the one cable:

```bash
gphotos phone stage --apply    # drive connected  -> ~/phone-staging
gphotos phone push  --apply    # phone connected  -> /sdcard/Pictures/restored
gphotos phone verify           # compare every file's size on the device
```

`verify` is not optional. `adb push --sync` decides what to re-send by **mtime
alone**: a file truncated to 1 MB whose mtime matched a 5 MB original was
reported "0 files pushed, 1 skipped" and left corrupt on the device.

`Pictures/restored` is deliberate — Google Photos defaults new device folders to
backup OFF, so the copy is not swept back into the cloud as duplicates.

---

## Tests

```bash
pip install pytest
python3 tests/fixtures/build.py    # generates the mini Takeout
pytest
```

`tests/fixtures/mini_takeout/` is 14 hand-built media files, every one a known
Takeout trap: a `.MP` motion photo (an MP4 container despite the extension,
whose sidecar is filed under the `.jpg` title), JPEG bytes named `.webp`, a
video whose sidecar has no `photoTakenTime` at all, a photo whose camera EXIF
contradicts a later sidecar date, the same photo in two albums, `0.0/0.0` GPS,
and a Storage-saver re-encode sharing a name with its original.

They are generated rather than committed: binary blobs nobody can read in a diff
are a bad way to describe what a test is testing.

`dates.py` and `dedup.py` each carry an assert-based self-check:
`python3 -m gphotos.dates`, `python3 -m gphotos.dedup`.

### Before pushing a fork

Personal data must never reach git. The `.gitignore` denies by pattern rather
than by name, so nothing new can sneak in — but verify it:

```bash
git ls-files | grep -E '\.tsv$|token|secret'     # must be empty
git clone . /tmp/clonecheck
grep -rE '/home/[a-z]+|/mnt/|[0-9]{12}' /tmp/clonecheck --include='*.py'
```

---

## Why Python, and what happened to the Go version

This repo previously held a 2,800-line Go CLI. It is tagged `v0-go` and
recoverable at any time; `git show v0-go:core/scanner/scanner.go` still works.

Its four-tier sidecar matcher was the best code in it and is ported forward
here. So are its `Library/`+`Albums/` layout, hash-suffix collision naming,
album-set union on merge, and learned filename-date patterns.

What it got wrong, and why the Python pipeline became the core instead: it
unconditionally overwrote existing EXIF dates with Google's sidecar time, had no
read-back verification, had no preference for originals over Storage-saver
re-encodes, and its dedup tiebreak `if a.Acc < b.Acc { return a.Acc < b.Acc }`
was a tautology that collapsed the comparison to path length. Porting nine
hard-won correctness fixes into Go was a bigger and riskier change than writing
one new `organize` command on top of logic already proven against 33,000 real
files.

---

## License

Apache-2.0. See `LICENSE`.
