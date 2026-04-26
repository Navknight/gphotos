#!/usr/bin/env python3
"""
Upload files from a local folder tree to Google Photos, mapping folders to albums.

- Each relative folder under --root becomes one Google Photos album.
- Files in each folder are uploaded and added to that album.
- A local state file prevents re-uploading unchanged files.
- Parallel uploads for speed, rclone-style verbose progress.

Requirements:
  pip install google-auth google-auth-oauthlib requests
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from typing import Any

# Make Ctrl+C actually kill the process on Windows.
signal.signal(signal.SIGINT, lambda *_: os._exit(1))

import google.auth.exceptions
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

try:
    from PIL import ExifTags, Image
except Exception:
    Image = None
    ExifTags = None

UPLOAD_ENDPOINT = "https://photoslibrary.googleapis.com/v1/uploads"
BATCH_CREATE_ENDPOINT = "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"
ALBUMS_ENDPOINT = "https://photoslibrary.googleapis.com/v1/albums"

SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
]

SUPPORTED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
    ".bmp", ".tif", ".tiff", ".avif",
    ".mp4", ".mp", ".mv", ".mp~2", ".mp~3", ".mov", ".m4v",
    ".avi", ".mkv", ".3gp", ".wmv",
    ".dng", ".nef",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_size(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.3f} GiB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.3f} MiB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.1f} KiB"
    return f"{n} B"


def fmt_eta(seconds: float) -> str:
    if seconds < 0 or seconds > 365 * 86400:
        return "-"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h{m:02d}m{s:02d}s"
    if m > 0:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def ts() -> str:
    return time.strftime("%Y/%m/%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class FileJob:
    path: Path
    rel_path: str
    album_title: str
    description: str
    size: int
    mtime_ns: int


def file_fingerprint(job: FileJob) -> str:
    return f"{job.path.name}-{job.size}-{job.mtime_ns}"


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state(state_file: Path) -> dict[str, Any]:
    if not state_file.exists():
        return {"files": {}, "albums": {}, "pending_tokens": {}, "upload_sessions": {}}
    try:
        with state_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"files": {}, "albums": {}, "pending_tokens": {}, "upload_sessions": {}}
        data.setdefault("files", {})
        data.setdefault("albums", {})
        data.setdefault("pending_tokens", {})
        data.setdefault("upload_sessions", {})
        return data
    except Exception:
        return {"files": {}, "albums": {}, "pending_tokens": {}, "upload_sessions": {}}


def save_state(state_file: Path, state: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    try:
        tmp.replace(state_file)
    except PermissionError:
        # On Windows, replace() fails with Access Denied when the destination
        # is momentarily locked by antivirus/indexer. Fall back to delete+rename.
        if state_file.exists():
            state_file.unlink()
        tmp.rename(state_file)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_credentials(client_secret: Path, token_file: Path) -> Credentials:
    creds: Credentials | None = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except google.auth.exceptions.RefreshError:
            creds = None
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
        creds = flow.run_local_server(port=0)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    return creds


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def api_request(
    session: AuthorizedSession,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    data: Any = None,
    retries: int = 5,
    timeout: int = 120,
) -> Any:
    for attempt in range(retries):
        try:
            resp = session.request(method, url, headers=headers, json=json_body, data=data, timeout=timeout)
        except Exception as exc:
            if attempt < (retries - 1):
                delay = min(2 ** attempt, 30)
                print(f"{ts()} WARN  : {method} {url} transport error (attempt {attempt + 1}/{retries}): {exc}; retrying in {delay}s")
                time.sleep(delay)
                continue
            raise RuntimeError(f"{method} {url} transport failed after retries: {exc}") from exc
        if resp.status_code < 400:
            if "application/json" in resp.headers.get("Content-Type", ""):
                return resp.json()
            return resp.text
        if resp.status_code in (429, 500, 502, 503, 504):
            delay = min(2 ** attempt, 30)
            print(f"{ts()} WARN  : {method} {url} HTTP {resp.status_code} (attempt {attempt + 1}/{retries}); retrying in {delay}s")
            time.sleep(delay)
            continue
        raise RuntimeError(f"{method} {url} failed ({resp.status_code}): {resp.text[:500]}")
    raise RuntimeError(f"{method} {url} failed after retries")


def _resumable_start_session(session: AuthorizedSession, path: Path, retries: int = 5) -> str:
    mime_type, _ = mimetypes.guess_type(str(path))
    if not mime_type:
        mime_type = "application/octet-stream"
    file_size = path.stat().st_size
    headers = {
        "Content-Length": "0",
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Content-Type": mime_type,
        "X-Goog-Upload-Raw-Size": str(file_size),
        "X-Goog-Upload-File-Name": path.name.encode("ascii", errors="replace").decode("ascii"),
    }
    for attempt in range(retries):
        try:
            resp = session.request("POST", UPLOAD_ENDPOINT, headers=headers, data=b"", timeout=60)
        except Exception as exc:
            if attempt < retries - 1:
                delay = min(2 ** attempt, 30)
                print(f"{ts()} WARN  : start session transport error (attempt {attempt + 1}/{retries}): {exc}; retrying in {delay}s")
                time.sleep(delay)
                continue
            raise RuntimeError(f"start session failed after retries: {exc}") from exc
        if resp.status_code < 400:
            session_url = resp.headers.get("X-Goog-Upload-URL", "")
            if not session_url:
                raise RuntimeError(f"start session returned no X-Goog-Upload-URL for {path}")
            return session_url
        if resp.status_code in (429, 500, 502, 503, 504):
            delay = min(2 ** attempt, 30)
            print(f"{ts()} WARN  : start session HTTP {resp.status_code} (attempt {attempt + 1}/{retries}); retrying in {delay}s")
            time.sleep(delay)
            continue
        raise RuntimeError(f"start session failed ({resp.status_code}): {resp.text[:500]}")
    raise RuntimeError(f"start session failed after retries for {path}")


def _resumable_query_offset(session: AuthorizedSession, session_url: str) -> int:
    headers = {
        "Content-Length": "0",
        "X-Goog-Upload-Command": "query",
    }
    resp = session.request("POST", session_url, headers=headers, data=b"", timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"query offset failed ({resp.status_code}): {resp.text[:300]}")
    offset_str = resp.headers.get("X-Goog-Upload-Size-Received", "0")
    return int(offset_str)


def _resumable_upload_finalize(
    session: AuthorizedSession, session_url: str, path: Path, offset: int, retries: int = 5,
) -> str:
    file_size = path.stat().st_size
    upload_timeout = max(300, 300 + file_size // (1024 * 1024))

    for attempt in range(retries):
        # Re-query offset on retry to get the authoritative position
        if attempt > 0:
            try:
                offset = _resumable_query_offset(session, session_url)
                print(f"{ts()} INFO  : Retry: server confirmed offset {offset} / {file_size}")
            except Exception:
                raise RuntimeError(f"Session expired during retry for {path}")

        remaining = file_size - offset
        if remaining == 0:
            # All bytes already on server — just finalize, no upload needed.
            headers = {
                "X-Goog-Upload-Command": "finalize",
                "X-Goog-Upload-Offset": str(offset),
                "Content-Length": "0",
            }
            try:
                resp = session.request("POST", session_url, headers=headers, data=b"", timeout=60)
            except Exception as exc:
                if attempt < retries - 1:
                    delay = min(2 ** attempt, 30)
                    print(f"{ts()} WARN  : upload finalize transport error (attempt {attempt + 1}/{retries}): {exc}; retrying in {delay}s")
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"upload finalize failed after retries: {exc}") from exc
        else:
            headers = {
                "X-Goog-Upload-Command": "upload, finalize",
                "X-Goog-Upload-Offset": str(offset),
                "Content-Length": str(remaining),
            }
            try:
                with path.open("rb") as f:
                    if offset > 0:
                        f.seek(offset)
                    resp = session.request("POST", session_url, headers=headers, data=f, timeout=upload_timeout)
            except Exception as exc:
                if attempt < retries - 1:
                    delay = min(2 ** attempt, 30)
                    print(f"{ts()} WARN  : upload finalize transport error (attempt {attempt + 1}/{retries}): {exc}; retrying in {delay}s")
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"upload finalize failed after retries: {exc}") from exc

        if resp.status_code < 400:
            token = resp.text.strip() if resp.text else ""
            if not token:
                raise RuntimeError(f"Upload finalize returned empty token for {path}")
            return token
        if resp.status_code in (429, 500, 502, 503, 504):
            delay = min(2 ** attempt, 30)
            print(f"{ts()} WARN  : upload finalize HTTP {resp.status_code} (attempt {attempt + 1}/{retries}); retrying in {delay}s")
            time.sleep(delay)
            continue
        raise RuntimeError(f"upload finalize failed ({resp.status_code}): {resp.text[:500]}")
    raise RuntimeError(f"upload finalize failed after retries for {path}")


def upload_resumable(
    session: AuthorizedSession,
    path: Path,
    fingerprint: str,
    upload_sessions: dict[str, str],
    state_lock: Lock,
    state: dict[str, Any],
    state_file: Path,
) -> str:
    # Check for an existing session URL from a previous run
    session_url = upload_sessions.get(fingerprint, "")
    offset = 0

    if session_url:
        try:
            offset = _resumable_query_offset(session, session_url)
            file_size = path.stat().st_size
            print(f"{ts()} INFO  : Resuming {path.name} from offset {offset} / {file_size}")
        except Exception:
            # Session expired — start fresh
            print(f"{ts()} INFO  : Previous session expired for {path.name}, starting fresh")
            with state_lock:
                upload_sessions.pop(fingerprint, None)
                save_state(state_file, state)
            session_url = ""
            offset = 0

    if not session_url:
        session_url = _resumable_start_session(session, path)
        with state_lock:
            upload_sessions[fingerprint] = session_url
            save_state(state_file, state)

    token = _resumable_upload_finalize(session, session_url, path, offset)

    # Clean up session entry on success
    with state_lock:
        upload_sessions.pop(fingerprint, None)
        save_state(state_file, state)

    return token


def batch_create(
    session: AuthorizedSession,
    album_id: str,
    items: list[tuple[FileJob, str]],
) -> list[tuple[FileJob, str]]:
    payload = {
        "albumId": album_id,
        "newMediaItems": [
            {
                "description": job.description,
                "simpleMediaItem": {
                    "uploadToken": upload_token,
                    "fileName": job.path.name,
                },
            }
            for job, upload_token in items
        ],
    }
    data = api_request(session, "POST", BATCH_CREATE_ENDPOINT, json_body=payload)
    results = data.get("newMediaItemResults", [])
    out: list[tuple[FileJob, str]] = []
    for i, result in enumerate(results):
        status = result.get("status", {})
        code = status.get("code", 0)
        if code != 0:
            msg = status.get("message", "unknown error")
            print(f"{ts()} ERROR : {items[i][0].path.name}: batch create failed ({code}: {msg})")
            continue
        media_item = result.get("mediaItem", {})
        media_id = media_item.get("id", "")
        if media_id:
            out.append((items[i][0], media_id))
    return out


def list_app_albums(session: AuthorizedSession) -> dict[str, str]:
    albums: dict[str, str] = {}
    page_token = None
    while True:
        params = {"pageSize": "50"}
        if page_token:
            params["pageToken"] = page_token
        query = "&".join(f"{k}={v}" for k, v in params.items())
        data = api_request(session, "GET", f"{ALBUMS_ENDPOINT}?{query}")
        for album in data.get("albums", []):
            title = album.get("title", "")
            album_id = album.get("id", "")
            if title and album_id:
                albums[title] = album_id
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return albums


def create_album(session: AuthorizedSession, title: str) -> str:
    payload = {"album": {"title": title}}
    data = api_request(session, "POST", ALBUMS_ENDPOINT, json_body=payload)
    album_id = data.get("id")
    if not album_id:
        raise RuntimeError(f"Album create returned no id for {title!r}")
    return album_id


# ---------------------------------------------------------------------------
# Description extraction (lazy, per-file at upload time)
# ---------------------------------------------------------------------------

def _decode_exif_text(value: Any) -> str:
    if isinstance(value, bytes):
        for prefix in (b"ASCII\x00\x00\x00", b"UNICODE\x00", b"JIS\x00\x00\x00\x00\x00"):
            if value.startswith(prefix):
                value = value[len(prefix):]
                break
        for encoding in ("utf-16-le", "utf-8", "latin-1"):
            try:
                out = value.decode(encoding, errors="ignore").strip("\x00").strip()
                if out:
                    return out
            except Exception:
                continue
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, tuple):
        try:
            return "".join(chr(v) for v in value if isinstance(v, int) and v != 0).strip()
        except Exception:
            return ""
    return ""


def description_from_exif(path: Path) -> str:
    if Image is None:
        return ""
    if path.suffix.lower() not in {".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".heic", ".heif"}:
        return ""
    try:
        with Image.open(path) as img:
            exif = img.getexif()
        if not exif:
            return ""
        for tag_name in ("ImageDescription", "XPComment", "UserComment"):
            for tag_id, raw in exif.items():
                if not ExifTags or ExifTags.TAGS.get(tag_id) != tag_name:
                    continue
                text = _decode_exif_text(raw)
                if text:
                    return text
    except Exception:
        return ""
    return ""


def description_from_sidecar(path: Path) -> str:
    candidates = [
        Path(str(path) + ".json"),
        path.with_suffix(path.suffix + ".json"),
        path.with_suffix(".json"),
    ]
    for candidate in candidates:
        if not candidate.exists() or not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for key in ("description", "caption"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def resolve_description(path: Path) -> str:
    text = description_from_exif(path)
    if text:
        return text
    return description_from_sidecar(path)


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def collect_jobs(root: Path, include_root_files: bool, album_prefix: str) -> list[FileJob]:
    jobs: list[FileJob] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        rel = path.relative_to(root)
        rel_dir = rel.parent.as_posix()
        if rel_dir == ".":
            if not include_root_files:
                continue
            album_title = f"{album_prefix}{root.name}"
        else:
            album_title = f"{album_prefix}{rel_dir}"
        st = path.stat()
        jobs.append(
            FileJob(
                path=path,
                rel_path=rel.as_posix(),
                album_title=album_title[:500],
                description="",
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
            )
        )
    return jobs


def should_skip(job: FileJob, files_state: dict[str, Any]) -> bool:
    entry = files_state.get(job.rel_path)
    if not isinstance(entry, dict):
        return False
    return (
        entry.get("size") == job.size
        and entry.get("mtime_ns") == job.mtime_ns
        and bool(entry.get("media_item_id"))
    )


# ---------------------------------------------------------------------------
# Progress tracker
# ---------------------------------------------------------------------------

class Progress:
    def __init__(self, total_files: int, total_bytes: int):
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.uploaded_files = 0
        self.uploaded_bytes = 0
        self.committed_files = 0
        self.failed_files = 0
        self.reused_tokens = 0
        self.start = time.time()
        self._last_summary = 0.0
        self._lock = Lock()

    def file_uploaded(self, job: FileJob, reused: bool = False) -> None:
        with self._lock:
            self.uploaded_files += 1
            self.uploaded_bytes += job.size
            if reused:
                self.reused_tokens += 1
            label = "Reused" if reused else "Uploaded"
            print(f"{ts()} INFO  : {job.path.name}: {label} ({fmt_size(job.size)})")
            self._maybe_summary()

    def file_failed(self, job: FileJob, exc: Exception) -> None:
        with self._lock:
            self.failed_files += 1
            print(f"{ts()} ERROR : {job.path.name}: {exc}")
            self._maybe_summary()

    def batch_committed(self, count: int, failures: int) -> None:
        with self._lock:
            self.committed_files += count
            self.failed_files += failures
            self._print_summary()

    def _maybe_summary(self) -> None:
        now = time.time()
        if now - self._last_summary >= 5.0:
            self._print_summary()

    def _print_summary(self) -> None:
        self._last_summary = time.time()
        elapsed = self._last_summary - self.start
        speed = self.uploaded_bytes / elapsed if elapsed > 0 else 0
        pct_bytes = (self.uploaded_bytes / self.total_bytes * 100) if self.total_bytes > 0 else 100
        remaining = ((self.total_bytes - self.uploaded_bytes) / speed) if speed > 0 else 0
        pct_files = (self.committed_files / self.total_files * 100) if self.total_files > 0 else 100
        print(
            f"Transferred:   {fmt_size(self.uploaded_bytes)} / {fmt_size(self.total_bytes)}, "
            f"{pct_bytes:.0f}%, {fmt_size(int(speed))}/s, ETA {fmt_eta(remaining)}"
        )
        print(
            f"Files:         {self.committed_files} / {self.total_files}, {pct_files:.0f}% | "
            f"Errors: {self.failed_files} | Elapsed: {fmt_eta(elapsed)}"
        )

    def final(self) -> None:
        with self._lock:
            self._print_summary()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    home = Path.home()
    p = argparse.ArgumentParser(description="Upload a folder tree to Google Photos with folder->album mapping.")
    p.add_argument("--root", default=str(home / "Downloads" / "out"), help="Root local folder to upload.")
    p.add_argument("--client-secret", required=True, help="OAuth client secret JSON.")
    p.add_argument("--token-file", default=str(home / ".config" / "gphotos_uploader_token.json"))
    p.add_argument("--state-file", default=str(home / ".config" / "gphotos_uploader_state.json"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--include-root-files", action="store_true", help="Upload files directly under --root.")
    p.add_argument("--album-prefix", default="")
    p.add_argument("--workers", type=int, default=16, help="Parallel upload threads (default: 16).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    client_secret = Path(args.client_secret).expanduser().resolve()
    token_file = Path(args.token_file).expanduser().resolve()
    state_file = Path(args.state_file).expanduser().resolve()

    if not root.is_dir():
        print(f"Root folder not found: {root}")
        return 1
    if not client_secret.exists():
        print(f"Client secret not found: {client_secret}")
        return 1

    # -- Scan --
    print(f"{ts()} INFO  : Scanning {root} ...")
    jobs = collect_jobs(root, args.include_root_files, args.album_prefix)
    if not jobs:
        print("No supported media files found.")
        return 0

    state = load_state(state_file)
    files_state: dict[str, Any] = state.setdefault("files", {})
    album_state: dict[str, Any] = state.setdefault("albums", {})
    pending_tokens: dict[str, Any] = state.setdefault("pending_tokens", {})
    upload_sessions: dict[str, str] = state.setdefault("upload_sessions", {})

    pending = [j for j in jobs if not should_skip(j, files_state)]
    print(f"{ts()} INFO  : Files: {len(jobs)} total, {len(jobs) - len(pending)} already uploaded, {len(pending)} pending ({fmt_size(sum(j.size for j in pending))})")

    if args.dry_run:
        for j in pending[:200]:
            print(f"  DRY RUN: {j.rel_path} -> [{j.album_title}]")
        if len(pending) > 200:
            print(f"  ... plus {len(pending) - 200} more")
        return 0
    if not pending:
        print("Nothing to upload.")
        return 0

    # -- Auth --
    creds = get_credentials(client_secret, token_file)
    session = AuthorizedSession(creds)

    # -- Albums --
    print(f"{ts()} INFO  : Fetching existing albums...")
    app_albums = list_app_albums(session)
    album_ids: dict[str, str] = {}
    for title in sorted({j.album_title for j in pending}):
        existing = app_albums.get(title) or album_state.get(title)
        if existing:
            album_ids[title] = existing
        else:
            print(f"{ts()} INFO  : Creating album: {title}")
            album_ids[title] = create_album(session, title)
            album_state[title] = album_ids[title]
            save_state(state_file, state)

    # -- Upload --
    progress = Progress(len(pending), sum(j.size for j in pending))
    state_lock = Lock()
    num_workers = max(1, args.workers)

    def get_cached_token(job: FileJob) -> str:
        entry = pending_tokens.get(job.rel_path)
        if not isinstance(entry, dict):
            return ""
        if entry.get("size") != job.size or entry.get("mtime_ns") != job.mtime_ns:
            return ""
        tok = entry.get("upload_token")
        created_at = entry.get("created_at_epoch", 0)
        if not isinstance(tok, str) or not tok:
            return ""
        if not isinstance(created_at, (int, float)) or (time.time() - float(created_at)) > 20 * 3600:
            return ""
        return tok

    def upload_one(job: FileJob) -> tuple[FileJob, str | None, bool]:
        job.description = resolve_description(job.path)
        cached = get_cached_token(job)
        if cached:
            progress.file_uploaded(job, reused=True)
            return (job, cached, True)
        try:
            fp = file_fingerprint(job)
            token = upload_resumable(
                session, job.path, fp,
                upload_sessions, state_lock, state, state_file,
            )
        except Exception as exc:
            progress.file_failed(job, exc)
            return (job, None, False)
        progress.file_uploaded(job)
        with state_lock:
            pending_tokens[job.rel_path] = {
                "size": job.size,
                "mtime_ns": job.mtime_ns,
                "album": job.album_title,
                "upload_token": token,
                "created_at_epoch": int(time.time()),
            }
            save_state(state_file, state)
        return (job, token, False)

    # Group by album, upload chunks of 50 in parallel, then batch-create.
    by_album: dict[str, list[FileJob]] = {}
    for j in pending:
        by_album.setdefault(j.album_title, []).append(j)

    for album_title, album_jobs in by_album.items():
        album_id = album_ids[album_title]
        print(f"{ts()} INFO  : Album [{album_title}]: {len(album_jobs)} files")

        for chunk_start in range(0, len(album_jobs), 50):
            chunk = album_jobs[chunk_start: chunk_start + 50]
            batch_tokens: list[tuple[FileJob, str]] = []

            with ThreadPoolExecutor(max_workers=num_workers) as pool:
                futures = {pool.submit(upload_one, j): j for j in chunk}
                for fut in as_completed(futures):
                    job, token, _ = fut.result()
                    if token:
                        batch_tokens.append((job, token))

            if not batch_tokens:
                continue

            print(f"{ts()} INFO  : Committing batch of {len(batch_tokens)} items to album...")
            try:
                created = batch_create(session, album_id, batch_tokens)
                created_paths = {j.rel_path for j, _ in created}
                for cj, media_id in created:
                    files_state[cj.rel_path] = {
                        "size": cj.size,
                        "mtime_ns": cj.mtime_ns,
                        "album": cj.album_title,
                        "media_item_id": media_id,
                    }
                    pending_tokens.pop(cj.rel_path, None)
                failures = sum(1 for j, _ in batch_tokens if j.rel_path not in created_paths)
                if failures > 0:
                    # Stale/invalid tokens — clear them so next run re-uploads.
                    for j, _ in batch_tokens:
                        if j.rel_path not in created_paths:
                            pending_tokens.pop(j.rel_path, None)
                            print(f"{ts()} WARN  : {j.path.name}: token rejected, will re-upload next run")
                progress.batch_committed(len(created), failures)
                with state_lock:
                    save_state(state_file, state)
            except Exception as exc:
                print(f"{ts()} ERROR : Batch create failed ({len(batch_tokens)} items): {exc}")
                # Clear all tokens in this batch so they get re-uploaded.
                for j, _ in batch_tokens:
                    pending_tokens.pop(j.rel_path, None)
                progress.batch_committed(0, len(batch_tokens))
                with state_lock:
                    save_state(state_file, state)

    progress.final()
    print(f"{ts()} INFO  : Done. State: {state_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
