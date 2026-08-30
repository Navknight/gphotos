"""Subcommands. Extras are imported lazily so a base install never sees their deps.

The commands split into two groups, and the split is a safety boundary rather
than a taxonomy:

  organize / ingest / sidecars   read a Takeout, write somewhere new. Safe: they
                                 never modify an existing library.
  plan / embed / verify / audit  operate on an EXISTING curated archive, in
                                 place. These need paths.archive set, refuse to
                                 run when it is not mounted, and default to a
                                 dry run.
"""

from __future__ import annotations

import argparse
import sys

from . import config, exiftool

EXTRA_HINT = {
    "upload": 'pip install "gphotos[upload]"',
    "phash": 'pip install "gphotos[phash]"',
}


def _lazy(module, extra):
    """Import an extras module, turning a missing dependency into an instruction."""
    try:
        from importlib import import_module
        return import_module(f".extras.{module}", __package__)
    except ImportError as exc:
        sys.exit(f"{exc}\n\nThis command needs an optional dependency:\n"
                 f"    {EXTRA_HINT[extra]}")


def build_parser():
    p = argparse.ArgumentParser(
        prog="gphotos",
        description="Organize a Google Takeout, and keep an existing photo "
                    "archive's metadata in sync with it.")
    p.add_argument("--config", metavar="FILE", help="config file to use")
    p.add_argument("--data", metavar="DIR", help="override paths.data")
    p.add_argument("--archive", metavar="DIR", help="override paths.archive")
    p.add_argument("--no-verify-readback", action="store_true",
                   help="skip the post-write re-read. Do not use on removable "
                        "or USB-bridged storage; it is the only check that "
                        "catches a silently dropped write.")
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("organize", help="extracted Takeout -> Library/ + Albums/")
    o.add_argument("takeout", help="directory of an extracted Takeout")
    o.add_argument("-o", "--output", help="where to write (default paths.output)")
    o.add_argument("--apply", action="store_true", help="actually write files")
    o.add_argument("--limit", type=int, default=0, help="stop after N photos")
    o.add_argument("-v", "--verbose", action="store_true")

    i = sub.add_parser("ingest", help="read Takeout archives without extracting media")
    i.add_argument("archives", nargs="+", help="zip or tar Takeout archives")

    s = sub.add_parser("salvage", help="pull sidecars from a partial .crdownload zip")
    s.add_argument("archives", nargs="+")

    sc = sub.add_parser("sidecars", help="parse ingested sidecars into one table")
    sc.add_argument("--dir", help="sidecar directory (default <data>/takeout_sidecars)")

    pl = sub.add_parser("plan", help="decide what to embed, overwriting nothing")
    pl.add_argument("--exif-state", help="path to the bulk EXIF inventory")

    e = sub.add_parser("embed", help="write the planned metadata, verified")
    e.add_argument("--apply", action="store_true", help="actually write (default dry run)")
    e.add_argument("--limit", type=int, default=0)
    e.add_argument("--plan", dest="plan_path")
    e.add_argument("--ledger", dest="ledger_path")

    v = sub.add_parser("verify", help="prove the embed pass did what it claims")
    v.add_argument("--quick", action="store_true", help="skip the re-parse check")
    v.add_argument("--expect-files", type=int, default=0,
                   help="fail if the archive does not hold exactly N files")

    sub.add_parser("audit", help="full read-only audit of the archive")
    sub.add_parser("check-takeout", help="are the Takeout archives safe to delete?")
    sub.add_parser("quality", help="Takeout copies that may beat the local ones")

    d = sub.add_parser("dates", help="propose filename-date patterns for undated files")
    d.add_argument("list", nargs="?",
                   help="TSV of paths (default <data>/derived/audit_no_date.tsv)")
    d.add_argument("--apply", action="store_true",
                   help="offer to save the patterns (still requires typing APPLY)")

    u = sub.add_parser("upload", help="[extra] upload local-only files to Google Photos")
    u.add_argument("--auth", action="store_true")
    u.add_argument("--test", metavar="PATH")
    u.add_argument("--run", action="store_true")
    u.add_argument("--readback", action="store_true",
                   help="re-read uploaded media ids and report what landed")
    u.add_argument("--root", help="upload a folder tree, one album per folder")
    u.add_argument("--workers", type=int, default=4)

    ph = sub.add_parser("phash", help="[extra] perceptual dedup against a library")
    ph.add_argument("--candidates", required=True,
                    help="TSV of sha\\tsize\\trelpath to assess")
    ph.add_argument("--library", required=True,
                    help="TSV of sha\\tsize\\trelpath Google already holds")
    ph.add_argument("--library-root", required=True)
    ph.add_argument("--workers", type=int, default=0)
    ph.add_argument("--limit", type=int, default=0)

    pn = sub.add_parser("phone", help="[extra] mirror the archive to an Android phone")
    pn.add_argument("action", choices=["stage", "push", "verify", "hash"])
    pn.add_argument("--stage-dir", help="staging directory (default ~/phone-staging)")
    pn.add_argument("--dest", default="/sdcard/Pictures/restored")
    pn.add_argument("--apply", action="store_true")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    cfg = config.load(args)

    needs_exiftool = args.cmd in ("organize", "plan", "embed", "verify", "audit")
    if needs_exiftool and not exiftool.available():
        sys.exit("exiftool not found on PATH. It is the one hard dependency:\n"
                 "  Debian/Ubuntu  apt install libimage-exiftool-perl\n"
                 "  Arch           pacman -S perl-image-exiftool\n"
                 "  macOS          brew install exiftool")

    if args.cmd == "organize":
        from . import organize
        return organize.main(cfg, args.takeout, args.output, args.apply,
                             args.verbose, args.limit)
    if args.cmd == "ingest":
        from . import ingest
        return ingest.ingest(cfg, args.archives)
    if args.cmd == "salvage":
        from . import ingest
        return ingest.salvage(cfg, args.archives)
    if args.cmd == "sidecars":
        from . import sidecars
        return sidecars.main(cfg, args.dir)
    if args.cmd == "plan":
        from . import plan
        return plan.main(cfg, args.exif_state)
    if args.cmd == "embed":
        from . import embed
        return embed.main(cfg, args.apply, args.limit, args.plan_path,
                          args.ledger_path)
    if args.cmd == "verify":
        from . import verify
        return verify.verify_embed(cfg, args.quick, args.expect_files)
    if args.cmd == "audit":
        from . import verify
        return verify.audit(cfg)
    if args.cmd == "check-takeout":
        from . import verify
        return verify.verify_takeout(cfg)
    if args.cmd == "quality":
        from . import verify
        return verify.quality(cfg)
    if args.cmd == "dates":
        from . import dates
        import csv
        src = args.list or cfg.derived / "audit_no_date.tsv"
        try:
            with open(src, newline="", encoding="utf-8") as fh:
                rows = list(csv.reader(fh, delimiter="\t"))
        except OSError as exc:
            sys.exit(f"{exc}\nRun `gphotos audit` first, or pass a TSV of paths.")
        paths = [r[0] for r in rows[1:] if r and r[0]]
        return dates.learn(cfg, paths, args.apply)

    if args.cmd == "upload":
        return _lazy("upload", "upload").main(cfg, args)
    if args.cmd == "phash":
        return _lazy("phash", "phash").main(cfg, args)
    if args.cmd == "phone":
        from .extras import phone
        return phone.main(cfg, args)
    raise AssertionError(f"unhandled command {args.cmd}")


if __name__ == "__main__":
    sys.exit(main())
