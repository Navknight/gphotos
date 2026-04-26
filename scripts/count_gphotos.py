#!/usr/bin/env python3
import json, requests, time
from pathlib import Path
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata"]
TOKEN_FILE = Path.home() / ".config" / "gphotos_appcreated_token.json"
UPLOADER_TOKEN_FILE = Path.home() / ".config" / "gphotos_uploader_token.json"


def get_credentials():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_FILE.write_text(creds.to_json())
        return creds
    uploader = json.loads(UPLOADER_TOKEN_FILE.read_text())
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


def main():
    print("Authenticating...", flush=True)
    creds = get_credentials()
    headers = {"Authorization": f"Bearer {creds.token}"}
    url = "https://photoslibrary.googleapis.com/v1/mediaItems"

    total = photos = videos = 0
    page_token = None
    page = 0

    print("Counting...", flush=True)
    while True:
        params = {"pageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(url, headers=headers, params=params)
        if not resp.ok:
            print(f"Error {resp.status_code}: {resp.text}", flush=True)
            break
        data = resp.json()
        items = data.get("mediaItems", [])
        for item in items:
            if item.get("mimeType", "").startswith("video/"):
                videos += 1
            else:
                photos += 1
        total += len(items)
        page += 1
        if page % 20 == 0:
            print(f"  {total:,} items so far...", flush=True)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
        time.sleep(0.05)

    print(f"\nGoogle Photos library:")
    print(f"  Total  : {total:,}")
    print(f"  Photos : {photos:,}")
    print(f"  Videos : {videos:,}")


if __name__ == "__main__":
    main()
