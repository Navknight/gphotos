#!/usr/bin/env python3
"""
1. Fetch GPhotos album structure -> album_db.json
2. Move/copy files in OneDrive/Pictures root into album subfolders.

Usage:
  python3 organize_by_album.py --build-db          # fetch from GPhotos, save DB
  python3 organize_by_album.py --organize           # move files per DB
  python3 organize_by_album.py --organize --dry-run # preview moves
  python3 organize_by_album.py --build-db --organize
"""

import argparse
import json
import shutil
import time
from pathlib import Path
from threading import Lock

import requests
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata"]
TOKEN_FILE = Path.home() / ".config" / "gphotos_appcreated_token.json"
UPLOADER_TOKEN = Path.home() / ".config" / "gphotos_uploader_token.json"

PICTURES_ROOT = Path(r"C:\Users\abhig\OneDrive\Pictures")
DB_FILE = Path(r"C:\Users\abhig\Github\gphotos\album_db.json")

SKIP_DIRS = {".claude", ".dtrash", ".dtrash_files", "batch_manifests", "Unsorted"}


def get_credentials() -> Credentials:
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
        return creds
    uploader = json.loads(UPLOADER_TOKEN.read_text())
    client_config = {"installed": {
        "client_id": uploader["client_id"],
        "client_secret": uploader["client_secret"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": uploader["token_uri"],
        "redirect_uris": ["http://localhost"],
    }}
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())
    return creds


def fetch_albums(session: AuthorizedSession) -> list[dict]:
    albums = []
    page_token = None
    while True:
        params = {"pageSize": 50}
        if page_token:
            params["pageToken"] = page_token
        resp = session.get("https://photoslibrary.googleapis.com/v1/albums", params=params)
        resp.raise_for_status()
        data = resp.json()
        albums.extend(data.get("albums", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
        time.sleep(0.05)
    return albums


def fetch_album_items(session: AuthorizedSession, album_id: str) -> list[str]:
    filenames = []
    page_token = None
    while True:
        body = {"albumId": album_id, "pageSize": 100}
        if page_token:
            body["pageToken"] = page_token
        for attempt in range(5):
            resp = session.post("https://photoslibrary.googleapis.com/v1/mediaItems:search", json=body)
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = 2 ** attempt
                print(f"    {resp.status_code} — retry in {wait}s...", flush=True)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break
        data = resp.json()
        for item in data.get("mediaItems", []):
            fn = item.get("filename")
            if fn:
                filenames.append(fn)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
        time.sleep(0.05)
    return filenames


def build_db(session: AuthorizedSession) -> dict:
    """Returns {album_title: [filename, ...]} and saves to DB_FILE."""
    print("Fetching albums...", flush=True)
    albums = fetch_albums(session)
    print(f"  {len(albums)} albums.", flush=True)

    db = {}  # album_title -> list of filenames
    for i, album in enumerate(albums):
        title = album.get("title", album["id"])
        print(f"  [{i+1}/{len(albums)}] {title}", flush=True)
        filenames = fetch_album_items(session, album["id"])
        db[title] = filenames

    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    DB_FILE.write_text(json.dumps(db, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nDB saved to {DB_FILE}", flush=True)
    print(f"  {len(db)} albums, {sum(len(v) for v in db.values()):,} total file entries.", flush=True)
    return db


def sanitize(name: str) -> str:
    return "".join(c if c not in r'\/:*?"<>|' else "_" for c in name)


def organize(db: dict, dry_run: bool) -> None:
    """Move root-level files in PICTURES_ROOT into album subfolders per db."""

    # Build reverse map: filename (lower) -> list of album titles
    # Skip "Pictures" album = root-level files, they stay at root
    file_to_albums: dict[str, list[str]] = {}
    for album_title, filenames in db.items():
        safe = sanitize(album_title)
        # Album named same as root folder = root files, skip (they stay at root)
        if safe == PICTURES_ROOT.name:
            continue
        for fn in filenames:
            file_to_albums.setdefault(fn.lower(), []).append(album_title)

    # Collect root-level files only (not in subdirs)
    root_files = [f for f in PICTURES_ROOT.iterdir()
                  if f.is_file() and f.name not in {".gitignore"}]
    print(f"Root-level files to process: {len(root_files):,}", flush=True)

    moved = skipped = conflicts = 0

    for src in root_files:
        albums_for_file = file_to_albums.get(src.name.lower(), [])

        if not albums_for_file:
            skipped += 1
            continue  # no album → stays at root

        for album_title in albums_for_file:
            safe = sanitize(album_title)
            dest_dir = PICTURES_ROOT / safe
            dest = dest_dir / src.name

            if dest == src:
                skipped += 1
                continue

            if dry_run:
                print(f"  MOVE {src.name} → {safe}/", flush=True)
                moved += 1
                continue

            dest_dir.mkdir(parents=True, exist_ok=True)

            if dest.exists():
                # Conflict: same filename already in dest folder
                stem = src.stem
                suffix = src.suffix
                dest = dest_dir / f"{stem}_dup{suffix}"
                conflicts += 1

            try:
                if len(albums_for_file) > 1:
                    if album_title == albums_for_file[0]:
                        shutil.move(str(src), str(dest))
                    else:
                        shutil.copy2(str(src), str(dest))
                else:
                    shutil.move(str(src), str(dest))
                moved += 1
            except FileNotFoundError:
                print(f"  SKIP (vanished): {src.name}", flush=True)
                skipped += 1
            except Exception as e:
                print(f"  ERROR {src.name}: {e}", flush=True)
                skipped += 1

        if moved % 500 == 0 and moved > 0:
            print(f"  {moved:,} files processed...", flush=True)

    print(f"\n{'DRY RUN — ' if dry_run else ''}Done.")
    print(f"  Moved/copied : {moved:,}")
    print(f"  Stayed root  : {skipped:,} (no album)")
    print(f"  Conflicts    : {conflicts:,} (renamed _dup)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-db", action="store_true", help="Fetch album DB from GPhotos")
    parser.add_argument("--organize", action="store_true", help="Move files per DB")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.build_db and not args.organize:
        parser.print_help()
        return

    db = None

    if args.build_db:
        print("Authenticating...", flush=True)
        creds = get_credentials()
        session = AuthorizedSession(creds)
        db = build_db(session)

    if args.organize:
        if db is None:
            if not DB_FILE.exists():
                print(f"No DB found at {DB_FILE}. Run --build-db first.")
                return
            db = json.loads(DB_FILE.read_text(encoding="utf-8"))
            print(f"Loaded DB: {len(db)} albums, {sum(len(v) for v in db.values()):,} entries.", flush=True)
        organize(db, args.dry_run)


if __name__ == "__main__":
    main()
