#!/usr/bin/env python3
"""
Download Google Photos items missing from local PC, preserving album structure.
Each GPhotos album becomes a subfolder. Items in no album go to Unsorted/.

Usage:
  python3 download_missing.py [--dry-run] [--workers N] [--dest PATH]
"""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

import requests
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata"]
TOKEN_FILE = Path.home() / ".config" / "gphotos_appcreated_token.json"
UPLOADER_TOKEN = Path.home() / ".config" / "gphotos_uploader_token.json"

DEFAULT_DEST = Path(r"C:\Users\abhig\OneDrive\Pictures")
PC_PICTURES = Path(r"C:\Users\abhig\OneDrive\Pictures")


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
        if not resp.ok:
            print(f"Error fetching albums: {resp.status_code} {resp.text[:200]}")
            break
        data = resp.json()
        albums.extend(data.get("albums", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
        time.sleep(0.05)
    return albums


def fetch_album_items(session: AuthorizedSession, album_id: str) -> list[dict]:
    items = []
    page_token = None
    while True:
        body = {"albumId": album_id, "pageSize": 100}
        if page_token:
            body["pageToken"] = page_token
        resp = session.post("https://photoslibrary.googleapis.com/v1/mediaItems:search", json=body)
        if not resp.ok:
            print(f"Error fetching album items: {resp.status_code} {resp.text[:200]}")
            break
        data = resp.json()
        items.extend(data.get("mediaItems", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
        time.sleep(0.05)
    return items


def fetch_all_items(session: AuthorizedSession) -> list[dict]:
    items = []
    page_token = None
    page = 0
    while True:
        params = {"pageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        resp = session.get("https://photoslibrary.googleapis.com/v1/mediaItems", params=params)
        if not resp.ok:
            print(f"Error: {resp.status_code} {resp.text[:200]}")
            break
        data = resp.json()
        items.extend(data.get("mediaItems", []))
        page += 1
        if page % 20 == 0:
            print(f"  Fetched {len(items):,} items...", flush=True)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
        time.sleep(0.05)
    return items


def build_pc_filenames(root: Path) -> set[str]:
    print(f"Scanning PC: {root}...", flush=True)
    names = {f.name.lower() for f in root.rglob("*") if f.is_file()}
    print(f"  {len(names):,} files on PC.", flush=True)
    return names


def download_item(item: dict, dest_folder: Path, session: requests.Session,
                  lock: Lock, stats: dict, dry_run: bool) -> None:
    filename = item.get("filename") or item["id"]
    mime = item.get("mimeType", "")
    base_url = item.get("baseUrl", "")
    out_path = dest_folder / filename

    if dry_run:
        with lock:
            stats["downloaded"] += 1
        return

    if out_path.exists():
        with lock:
            stats["skipped"] += 1
        return

    if not base_url:
        with lock:
            stats["failed"] += 1
        return

    dl_url = base_url + ("=dv" if mime.startswith("video/") else "=d")

    for attempt in range(4):
        try:
            resp = session.get(dl_url, timeout=300, stream=True)
            if resp.status_code in (500, 502, 503, 504):
                wait = 2 ** attempt
                time.sleep(wait)
                continue
            resp.raise_for_status()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
            size = out_path.stat().st_size
            with lock:
                stats["downloaded"] += 1
                stats["bytes"] += size
                if stats["downloaded"] % 100 == 0:
                    gb = stats["bytes"] / (1 << 30)
                    print(f"  Downloaded {stats['downloaded']:,} files ({gb:.2f} GB)...", flush=True)
            return
        except Exception as e:
            if attempt == 3:
                with lock:
                    stats["failed"] += 1
                print(f"  FAIL {filename}: {e}", flush=True)
            else:
                time.sleep(2 ** attempt)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--dest", default=str(DEFAULT_DEST))
    args = parser.parse_args()

    dest = Path(args.dest)

    print("Authenticating...", flush=True)
    creds = get_credentials()
    session = AuthorizedSession(creds)

    # Fetch albums
    print("Fetching albums...", flush=True)
    albums = fetch_albums(session)
    print(f"  {len(albums)} albums found.", flush=True)

    # Fetch all items to find unsorted ones
    print("Fetching all items...", flush=True)
    all_items = fetch_all_items(session)
    print(f"  {len(all_items):,} total items.", flush=True)

    # Build PC filename set
    pc_files = build_pc_filenames(PC_PICTURES)

    # Build map: item_id -> item (for unsorted detection)
    all_item_ids = {i["id"] for i in all_items}

    dl_session = requests.Session()
    dl_session.headers["Authorization"] = f"Bearer {creds.token}"
    lock = Lock()
    stats = {"downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}

    covered_ids: set[str] = set()

    # Process each album
    for album in albums:
        album_title = album.get("title", album["id"])
        # Sanitize folder name
        safe_title = "".join(c if c not in r'\/:*?"<>|' else "_" for c in album_title)
        # Album named same as dest root = root-level files, go to dest directly
        album_folder = dest if safe_title == dest.name else dest / safe_title

        print(f"\nAlbum: {album_title}", flush=True)
        items = fetch_album_items(session, album["id"])
        missing = [i for i in items if i.get("filename", "").lower() not in pc_files]
        covered_ids.update(i["id"] for i in items)

        print(f"  {len(items)} items, {len(missing)} missing from PC", flush=True)

        if not missing:
            continue

        if args.dry_run:
            for i in missing[:5]:
                print(f"    {i.get('filename')}", flush=True)
            if len(missing) > 5:
                print(f"    ... +{len(missing)-5} more", flush=True)
            stats["downloaded"] += len(missing)
            continue

        album_folder.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(download_item, item, album_folder, dl_session, lock, stats, False): item
                       for item in missing}
            for fut in as_completed(futures):
                fut.result()

    # Unsorted items (not in any album)
    unsorted = [i for i in all_items
                if i["id"] not in covered_ids
                and i.get("filename", "").lower() not in pc_files]

    print(f"\nUnsorted (no album): {len(unsorted)} missing from PC", flush=True)
    if unsorted:
        unsorted_folder = dest / "Unsorted"
        if not args.dry_run:
            unsorted_folder.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(download_item, item, unsorted_folder, dl_session, lock, stats, args.dry_run): item
                       for item in unsorted}
            for fut in as_completed(futures):
                fut.result()

    gb = stats["bytes"] / (1 << 30)
    print(f"\n{'DRY RUN - ' if args.dry_run else ''}Done.")
    print(f"  {'Would download' if args.dry_run else 'Downloaded'}: {stats['downloaded']:,} files ({gb:.2f} GB)")
    print(f"  Skipped : {stats['skipped']:,}")
    print(f"  Failed  : {stats['failed']:,}")


if __name__ == "__main__":
    main()
