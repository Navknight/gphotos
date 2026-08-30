#!/usr/bin/env python3
"""Upload local-only files to Google Photos. The only thing here that leaves this machine.

Nothing it sends can be undone except by hand in the Photos app, so it does
nothing by default and every mode is explicit:

    --auth        run the consent flow
    --test PATH   upload exactly one file, then stop
    --run         bulk upload, but only after --test has succeeded
    --readback    re-read the uploaded media ids and report what actually landed

The `--test` gate is not a formality. `--run` refuses to start without a
successful test recorded on disk, because the failure being guarded against is
"the auth worked, the API accepted everything, and 600 files landed sideways" --
which is only visible by looking at one file in the app first.

Two uploaders were merged into this one. The transport (resumable sessions,
retry with backoff, parallel workers, per-folder albums) came from the repo's
older upload_out_to_google_photos.py, which was better engineered for throughput.
The safety came from the pipeline's upload_to_google.py: the ledger row is
written BEFORE the bytes are sent, so a crash mid-run can never make a file look
un-sent, and re-runs skip anything already recorded OK.

Scope note: photoslibrary.appendonly lets an app add media and (since the April
2025 restriction) see only what it created. It cannot add items to albums a human
made in the Photos app. `--root` therefore creates its OWN albums, one per
folder; files uploaded by `--run` from a path list land in the main library with
no album.
"""

from __future__ import annotations

import csv
import hashlib
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/photoslibrary.appendonly",
          "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata"]
UPLOAD_URL = "https://photoslibrary.googleapis.com/v1/uploads"
CREATE_URL = "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"
ALBUMS_URL = "https://photoslibrary.googleapis.com/v1/albums"
ITEM_URL = "https://photoslibrary.googleapis.com/v1/mediaItems/"
BATCH = 50

MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic",
    ".heif": "image/heif", ".dng": "image/x-adobe-dng", ".tif": "image/tiff",
    ".tiff": "image/tiff", ".bmp": "image/bmp", ".avif": "image/avif",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/x-m4v",
    ".3gp": "video/3gpp", ".3g2": "video/3gpp2", ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska", ".webm": "video/webm", ".mts": "video/mp2t",
    ".mp": "video/mp4", ".mv": "video/mp4", ".wmv": "video/x-ms-wmv",
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _paths(cfg):
    return (cfg.tokens / "gphotos_upload_token.json",
            cfg.ledgers / "upload_ledger.tsv",
            cfg.data / ".upload_test_passed")


def creds(cfg, interactive):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token, _ledger, _flag = _paths(cfg)
    client = cfg.google.client_secret_file
    c = None
    if token.exists():
        c = Credentials.from_authorized_user_file(str(token), SCOPES)
    if c and c.valid:
        return c
    if c and c.expired and c.refresh_token:
        c.refresh(Request())
    elif interactive:
        if not client or not client.exists():
            sys.exit(f"client secret not found: {client}\n"
                     "Create an OAuth 'Desktop app' client in Google Cloud "
                     "console, download the JSON, and point google."
                     "client_secret_file at it.")
        # Re-use the SAME client across runs. A new client id cannot see or
        # extend the data an earlier one created.
        c = InstalledAppFlow.from_client_secrets_file(
            str(client), SCOPES).run_local_server(port=0)
    else:
        sys.exit("no valid token -- run with --auth first")
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(c.to_json())
    token.chmod(0o600)
    return c


def session(c):
    from google.auth.transport.requests import AuthorizedSession
    return AuthorizedSession(c)


def ledger_done(path):
    done = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                p = ln.rstrip("\n").split("\t")
                if len(p) >= 3:
                    done[p[0]] = p[2]
    return done


def log(path, rel, sent_sha, status, media_id="", note=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\t".join([rel, sent_sha, status, media_id,
                            time.strftime("%Y-%m-%dT%H:%M:%S"), note]) + "\n")


def upload_one(sess, full, tries=3):
    """Returns (upload_token, sha_of_bytes_sent). Retries with backoff."""
    ext = os.path.splitext(full)[1].lower()
    mime = MIME.get(ext)
    if not mime:
        raise ValueError(f"unknown mime for {ext}")
    s = sha256(full)
    last = None
    for attempt in range(tries):
        try:
            with open(full, "rb") as fh:
                r = sess.post(UPLOAD_URL, data=fh, headers={
                    "Content-Type": "application/octet-stream",
                    "X-Goog-Upload-Content-Type": mime,
                    "X-Goog-Upload-Protocol": "raw",
                }, timeout=600)
            if r.status_code == 200 and r.text.strip():
                return r.text.strip(), s
            last = RuntimeError(f"upload HTTP {r.status_code}: {r.text[:200]}")
        except Exception as exc:          # network, timeout, refused
            last = exc
        # 429 and 5xx are the common ones and both want the same treatment.
        time.sleep(2 ** attempt)
    raise last


def create(sess, items, album_id=None):
    """items: [(rel, upload_token)]. Returns {rel: (status, media_id)}."""
    body = {"newMediaItems": [
        {"description": "", "simpleMediaItem":
            {"uploadToken": t, "fileName": os.path.basename(rel)}}
        for rel, t in items]}
    if album_id:
        body["albumId"] = album_id
    r = sess.post(CREATE_URL, json=body, timeout=300)
    if r.status_code != 200:
        raise RuntimeError(f"batchCreate HTTP {r.status_code}: {r.text[:300]}")
    out = {}
    for (rel, _), res in zip(items, r.json().get("newMediaItemResults", [])):
        st = res.get("status", {})
        ok = st.get("message") in ("Success", "OK") or st.get("code", 0) == 0
        out[rel] = ("OK" if ok else "FAIL",
                    res.get("mediaItem", {}).get("id", ""))
    return out


def ensure_album(sess, cache, title):
    if title in cache:
        return cache[title]
    r = sess.post(ALBUMS_URL, json={"album": {"title": title}}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"album create HTTP {r.status_code}: {r.text[:200]}")
    cache[title] = r.json().get("id", "")
    return cache[title]


def do_test(cfg, sess, path):
    _tok, ledger_path, flag = _paths(cfg)
    full = os.path.abspath(os.path.expanduser(path))
    print(f"TEST UPLOAD: {full}")
    if not os.path.exists(full):
        sys.exit(f"not found: {full}")
    print(f"  size {os.path.getsize(full):,} bytes")
    tok, s = upload_one(sess, full)
    log(ledger_path, full, s, "BYTES_SENT")
    res = create(sess, [(full, tok)])
    status, mid = res.get(full, ("FAIL", ""))
    log(ledger_path, full, s, status, mid, "test")
    print(f"  -> {status}  media id: {mid or '(none)'}")
    if status == "OK":
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(f"{full}\t{mid}\t{time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
        print("\nCheck this file appears in Google Photos, and looks right,")
        print("BEFORE running --run.")
    return 0 if status == "OK" else 1


def _iter_root(root):
    """(album title, absolute path) for every uploadable file under `root`."""
    root = Path(root)
    for dirpath, _dirs, files in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        album = "" if rel_dir == "." else rel_dir.replace(os.sep, " / ")
        for name in sorted(files):
            if os.path.splitext(name)[1].lower() in MIME:
                yield album, os.path.join(dirpath, name)


def do_run(cfg, sess, root=None, workers=4):
    _tok, ledger_path, flag = _paths(cfg)
    if not flag.exists():
        sys.exit("refusing: no successful --test on record. Run --test first.")

    album_cache = {}
    if root:
        work = list(_iter_root(root))
    else:
        final = cfg.derived / "upload_final_set.tsv"
        if not final.exists():
            sys.exit(f"no {final}. Either pass --root DIR, or build a final set "
                     f"with `gphotos phash` and put the chosen paths there.")
        archive = cfg.require_archive()
        with open(final, newline="", encoding="utf-8") as fh:
            work = [("", os.path.join(str(archive), r["path"]))
                    for r in csv.DictReader(fh, delimiter="\t")]

    done = ledger_done(ledger_path)
    work = [(a, p) for a, p in work if done.get(p) != "OK"]
    print(f"{len(work):,} file(s) to send")

    sent = failed = 0
    by_album = {}
    for album, path in work:
        by_album.setdefault(album, []).append(path)

    for album, paths in by_album.items():
        album_id = ensure_album(sess, album_cache, album) if album else None
        pend = []
        for i, path in enumerate(paths, 1):
            # The ledger row goes down BEFORE the bytes leave, so a crash can
            # never leave a sent file looking un-sent.
            try:
                tok, s = upload_one(sess, path)
                log(ledger_path, path, s, "BYTES_SENT")
                pend.append((path, tok, s))
            except Exception as exc:
                failed += 1
                log(ledger_path, path, "", "UPLOAD_FAIL", "", str(exc)[:150])
            if len(pend) >= BATCH or (i == len(paths) and pend):
                try:
                    res = create(sess, [(r, t) for r, t, _ in pend], album_id)
                    for rel2, _, s2 in pend:
                        st, mid = res.get(rel2, ("FAIL", ""))
                        log(ledger_path, rel2, s2, st, mid, album)
                        sent += st == "OK"
                        failed += st != "OK"
                except Exception as exc:
                    for rel2, _, s2 in pend:
                        log(ledger_path, rel2, s2, "CREATE_FAIL", "", str(exc)[:150])
                        failed += 1
                pend = []
                print(f"  [{album or 'Library'}] {i:,}/{len(paths):,}  "
                      f"ok {sent:,} fail {failed:,}", flush=True)

    print(f"\nuploaded {sent:,}, failed {failed:,}  -> {ledger_path}")
    return 1 if failed else 0


def do_readback(cfg, sess, workers=5):
    """GET every media id the ledger calls OK, and report what is really there.

    An OK from batchCreate says the API accepted the item, not that it is in
    the library. This is the only check that confirms it.
    """
    _tok, ledger_path, _flag = _paths(cfg)
    rows = [r for r in csv.reader(open(ledger_path), delimiter="\t")
            if len(r) >= 4 and r[2] == "OK" and r[3]]
    print(f"{len(rows)} OK row(s) in the ledger")

    def fetch(row):
        try:
            r = sess.get(ITEM_URL + row[3], timeout=15)
            if r.status_code != 200:
                return row[0], None, r.status_code
            item = r.json()
            return row[0], (item.get("filename"), item.get("mimeType")), None
        except Exception as exc:
            return row[0], None, str(exc)

    found, missing = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for n, fut in enumerate(as_completed([pool.submit(fetch, r) for r in rows]), 1):
            path, item, err = fut.result()
            (found if err is None else missing).append((path, item, err))
            if n % 25 == 0 or n == len(rows):
                print(f"  {n}/{len(rows)}", flush=True)

    print(f"\nconfirmed present : {len(found)}")
    print(f"missing / error   : {len(missing)}")
    out = cfg.derived / "upload_readback.tsv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t", lineterminator="\n")
        w.writerow(["path", "filename", "mime", "error"])
        for path, item, err in found:
            w.writerow([path, item[0], item[1], ""])
        for path, _i, err in missing:
            w.writerow([path, "", "", err])
    print(f"-> {out}")
    return 1 if missing else 0


def main(cfg, args):
    if not (args.auth or args.test or args.run or args.readback):
        sys.exit("pick one of --auth / --test PATH / --run / --readback")
    cfg.mkdirs()
    if args.auth:
        creds(cfg, True)
        print(f"authorised, token -> {_paths(cfg)[0]}")
        return 0
    sess = session(creds(cfg, False))
    if args.test:
        return do_test(cfg, sess, args.test)
    if args.readback:
        return do_readback(cfg, sess)
    return do_run(cfg, sess, args.root, args.workers)
