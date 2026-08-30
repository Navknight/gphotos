#!/usr/bin/env python3
"""Make an Android phone a third replica of the archive, alongside the drive and Google.

Two hops, because there is usually only one USB cable and the drive and the
phone cannot be connected at the same time:

    gphotos phone stage --apply    drive plugged in -> ~/phone-staging
    gphotos phone push  --apply    phone plugged in -> /sdcard/Pictures/restored
    gphotos phone verify           compare every file's size on the device
    gphotos phone hash             print the on-device hashing script

Splitting it this way also buys the WhatsApp exclusion for free. `adb push` has
no --exclude, so filtering during the push would mean building a file list by
hand; rsync filters at the staging hop instead and hop 2 sends a tree that is
already correct.

The destination is Pictures/restored deliberately: Google Photos defaults new
device folders to backup OFF, so the whole archive does not get swept into the
cloud and re-uploaded as duplicates. Do NOT add a .nomedia there -- that would
hide them from the phone's own gallery too, which is the entire point of the
copy.

Album structure comes along free: each top-level directory in the archive is an
album, and gallery apps render directories as albums.

Stdlib + adb + rsync only. No Python dependencies, so this needs no extra.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_STAGE = "~/phone-staging/restored"

# WhatsApp media is excluded on purpose: those files are already on the device
# under Android/media/com.whatsapp, and copying them into Pictures/restored
# would duplicate them there.
#
# The pattern is the glob form of the WA regex: the -<8 digits>-WA<n> tail is
# the signature, and the prefix is NOT whitelisted. A prefix whitelist is what
# let null-20260513-WA0008.jpg escape an earlier delete pass and reach Google
# Photos.
WA_GLOB = "*-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-WA[0-9]*"

EXCLUDES = ["$RECYCLE.BIN", ".dtrash", ".dtrash_files", "_damaged",
            "Failed videos", "lost+found", "System Volume Information",
            ".claude", ".trashed-*", "*.MP~*", "_pc-backup", ".Trash-*",
            ".fseventsd", ".Spotlight-V100", WA_GLOB]

# The on-device hashing script. Runs entirely on the phone and writes one file,
# rather than streaming a line per file back over adb -- that per-line round
# trip over the USB link was the bottleneck, not the hashing itself. sha256sum
# and stat are batched 50-at-a-time across 4 parallel xargs workers, because a
# process per file is the slow pattern this project already learned to avoid
# with exiftool.
HASH_SCRIPT = r"""#!/system/bin/sh
ROOT="/sdcard"
CAND="/data/local/tmp/phone_candidates.txt"
SKIP="/data/local/tmp/phone_skip.txt"
TODO="/data/local/tmp/phone_todo.txt"
HASHOUT="/data/local/tmp/phone_hash_raw.txt"
SIZEOUT="/data/local/tmp/phone_size_raw.txt"

DIRS="
DCIM/Camera
DCIM/Restored
DCIM/Screenshots
Pictures/Restored
Pictures/Screenshots
Pictures/restored
Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images
Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Video
"

> "$CAND"
echo "$DIRS" | while IFS= read -r d; do
  [ -z "$d" ] && continue
  full="$ROOT/$d"
  [ -d "$full" ] || continue
  find "$full" -type f \( \
    -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" -o -iname "*.heic" \
    -o -iname "*.heif" -o -iname "*.gif" -o -iname "*.webp" -o -iname "*.bmp" \
    -o -iname "*.tif" -o -iname "*.tiff" -o -iname "*.dng" -o -iname "*.nef" \
    -o -iname "*.mp4" -o -iname "*.mov" -o -iname "*.m4v" -o -iname "*.avi" \
    -o -iname "*.mkv" -o -iname "*.3gp" -o -iname "*.wmv" \
  \) 2>/dev/null | sed "s|^$ROOT/||" >> "$CAND"
done

# comm, not a per-file grep: skipping with a subprocess per file is the pattern
# that made the first version take hours.
if [ -s "$SKIP" ]; then
  sort "$CAND" > "$CAND.sorted"
  sort "$SKIP" > "$SKIP.sorted"
  comm -23 "$CAND.sorted" "$SKIP.sorted" > "$TODO"
else
  cp "$CAND" "$TODO"
fi
echo "candidates: $(wc -l < "$CAND")  todo: $(wc -l < "$TODO")" >&2

sed "s#^#$ROOT/#" "$TODO" | tr '\n' '\0' | xargs -0 -n 50 -P 4 sha256sum > "$HASHOUT" 2>/dev/null
sed "s#^#$ROOT/#" "$TODO" | tr '\n' '\0' | xargs -0 -n 50 -P 4 stat -c '%s\t%n' > "$SIZEOUT" 2>/dev/null
echo "hashed: $(wc -l < "$HASHOUT")  sized: $(wc -l < "$SIZEOUT")" >&2
echo "pull them with: adb pull $HASHOUT; adb pull $SIZEOUT" >&2
"""


def _adb(*args, capture=True, check=False):
    return subprocess.run(["adb", *args], capture_output=capture, text=True,
                          check=check)


def _have_adb() -> bool:
    if shutil.which("adb") is None:
        return False
    return _adb("get-state").returncode == 0


def _gb(n) -> str:
    return f"{n / 1073741824:.1f} GB"


def stage(cfg, stage_dir: Path, apply: bool):
    src = cfg.require_archive()
    cfg.check_mount()
    if shutil.which("rsync") is None:
        sys.exit("rsync not found; it is what checksums each file it writes")

    excludes = [f"--exclude={e}" for e in EXCLUDES]
    wa = sum(1 for p in Path(src).rglob("*")
             if p.is_file() and p.match(WA_GLOB))
    total = sum(1 for p in Path(src).rglob("*") if p.is_file())
    print(f"archive: {total:,} files; WhatsApp files to skip: {wa:,}")

    # -a preserves mtimes, which `adb push --sync` needs in hop 2 to know what
    # it already sent. rsync checksums every file it writes and retries on
    # mismatch, which is the verification a flaky USB-SATA bridge requires.
    cmd = ["rsync", "-a", "--info=progress2", "--partial", *excludes,
           f"{src}/", f"{stage_dir}/"]
    if not apply:
        print("\ndry run:")
        subprocess.run(["rsync", "-an", "--stats", *excludes,
                        f"{src}/", f"{stage_dir}/"])
        print("\npass --apply to stage")
        return 0
    stage_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.run(cmd).returncode


def push(cfg, stage_dir: Path, dest: str, apply: bool):
    if not stage_dir.is_dir():
        sys.exit(f"REFUSING: {stage_dir} does not exist -- run `stage` first")
    if not _have_adb():
        sys.exit("REFUSING: no adb device (USB debugging on?)")

    need = sum(p.stat().st_size for p in stage_dir.rglob("*") if p.is_file())
    free_out = _adb("shell", "df", "/storage/emulated/0").stdout.splitlines()
    free = int(free_out[1].split()[3]) * 1024 if len(free_out) > 1 else 0
    have_out = _adb("shell", f"du -sk {dest} 2>/dev/null | cut -f1").stdout.strip()
    have = int(have_out or 0) * 1024 if have_out.isdigit() else 0
    # Only what is NOT already on the device still needs room. Comparing the
    # full staged size against free space refuses to resume a partial push:
    # after 77 of 123 GB had landed, the naive check saw 122 GB needed vs 99 GB
    # free and gave up, when only 46 GB actually remained.
    left = max(0, need - have)
    print(f"staged {_gb(need)}, on device {_gb(have)}, still to send {_gb(left)}")
    print(f"phone free {_gb(free)}")
    if free <= left:
        sys.exit("REFUSING: not enough free space on the phone")
    if not apply:
        print("\ndry run -- pass --apply to push")
        return 0

    parent = os.path.dirname(dest.rstrip("/")) or "/sdcard/Pictures"
    _adb("shell", "mkdir", "-p", parent)

    # One push of the whole tree. An earlier version looped over the staging
    # subdirectories to get per-album resume, but most of this library is loose
    # files directly under the root and a */ glob skips every one of them.
    # --sync compares timestamps across the whole tree, so re-running after a
    # wedge still resumes. -Z disables compression; these are already
    # JPEG/HEIC/MP4.
    #
    # adb renders its progress bar ONLY when stdout is a terminal -- through a
    # pipe it prints nothing until the final summary, so a 123 GB push looks
    # hung for an hour. pty.spawn gives it a terminal to write to.
    #
    # pty.spawn returns the raw wait status (exit code << 8), so passing it
    # straight to sys.exit truncates to the low byte: a real failure of 1
    # becomes 256 and is reported as exit 0. waitstatus_to_exitcode fixes it.
    import pty
    rc = 1
    for attempt in range(1, 6):
        if attempt > 1:
            print(f"--- attempt {attempt} (--sync resumes; waiting for device)")
            _adb("wait-for-device", capture=False)
        rc = os.waitstatus_to_exitcode(
            pty.spawn(["adb", "push", "--sync", "-Z", str(stage_dir), parent + "/"]))
        if rc == 0:
            break
        print(f"!! push exited {rc} -- the phone dropped off USB. Retrying.")

    # Nothing appears in the gallery until the media scanner indexes it. For
    # tens of thousands of files that takes a long while; it is not a failed
    # transfer.
    print("triggering media scan (slow -- give it time before judging the result)")
    if _adb("shell", "content", "call", "--uri", "content://media/external",
            "--method", "scan_volume").returncode != 0:
        print("  scan trigger unavailable; reboot the phone instead")
    return rc


def verify(cfg, stage_dir: Path, dest: str):
    """Compare every file's size on the device against staging.

    `adb push --sync` decides by mtime ALONE -- proven: a file truncated to 1 MB
    whose mtime matched a 5 MB local original was reported "0 files pushed, 1
    skipped" and left corrupt. adb sets mtime only after a file finishes, so an
    interrupted transfer usually does get re-sent, but "usually" is not a backup
    guarantee. This is the check that makes the parity claim real.
    """
    if not stage_dir.is_dir():
        sys.exit(f"REFUSING: {stage_dir} does not exist")
    if not _have_adb():
        sys.exit("REFUSING: no adb device")

    print("listing device (tens of thousands of files -- takes a minute)...")
    # A REAL tab byte, not the escape "\t": the device runs toybox stat, which
    # passes \t through literally instead of expanding it, so every line came
    # back as "273340\t./file.jpg" with no actual tab, nothing split, and the
    # device looked empty while holding all 29,349 files.
    out = _adb("shell",
               f"cd {dest} 2>/dev/null && find . -type f -exec stat -c '%s\t%n' {{}} +")
    dev = {}
    for ln in out.stdout.replace("\r", "").splitlines():
        size, _, name = ln.partition("\t")   # split on the FIRST tab only
        if name and size.strip().isdigit():
            dev[name] = int(size)

    loc = {}
    for p in stage_dir.rglob("*"):
        if p.is_file():
            loc["./" + str(p.relative_to(stage_dir))] = p.stat().st_size

    print(f"device: {len(dev):,} files, staging: {len(loc):,} files")
    bad = sorted(n for n, sz in loc.items() if dev.get(n) != sz)
    report = cfg.derived / "phone_mismatched.txt"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(bad) + ("\n" if bad else ""))

    if not bad:
        print("VERIFIED: every staged file is on the device at the correct size.")
        return 0
    missing = sum(1 for n in bad if n not in dev)
    print(f"{len(bad):,} file(s) bad: {missing:,} missing, "
          f"{len(bad)-missing:,} wrong-sized")
    for n in bad[:5]:
        print("   ", n)
    print(f"\n-> {report}")
    print("Delete exactly those on the device, then re-run `push` -- --sync "
          "re-sends them because they are then absent.")
    return 1


def main(cfg, args):
    stage_dir = Path(os.path.expanduser(args.stage_dir or DEFAULT_STAGE))
    if args.action == "stage":
        return stage(cfg, stage_dir, args.apply)
    if args.action == "push":
        return push(cfg, stage_dir, args.dest, args.apply)
    if args.action == "verify":
        return verify(cfg, stage_dir, args.dest)
    if args.action == "hash":
        print(HASH_SCRIPT)
        print("# Save this, then:", file=sys.stderr)
        print("#   adb push phone_hash.sh /data/local/tmp/ && "
              "adb shell sh /data/local/tmp/phone_hash.sh", file=sys.stderr)
        return 0
    raise AssertionError(args.action)
