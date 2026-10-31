#!/usr/bin/env python3
"""
One-time YouTube login for the QuickSong channel.

EASY WAY: put client_secret.json in the same folder as this file, open this file in
Python IDLE and press F5. (Command line also works: python QuickSong_Login.py client_secret.json)

A browser opens — sign in with the QuickSong Gmail and pick the QuickSong channel.
It checks the channel you picked and only saves the login if it's the right one.
It then prints the three values to paste into GitHub → Settings → Secrets → Actions:
    YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN
Standard library only, nothing to install.
"""
from pathlib import Path
import http.server, json, secrets, sys, threading, urllib.parse, urllib.request, webbrowser

# The channel uploads must go to. The login refuses to finish on any other channel.
TARGET_CHANNEL_ID = "UC1wkRMYNFOyWZMEZkWlrWKQ"

# upload = post videos; readonly = lets the uploader check it's on the right channel first
SCOPE = ("https://www.googleapis.com/auth/youtube.upload "
         "https://www.googleapis.com/auth/youtube.readonly")


def which_channel(access_token):
    req = urllib.request.Request(
        "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
        headers={"Authorization": f"Bearer {access_token}"})
    items = json.load(urllib.request.urlopen(req)).get("items", [])
    if not items:
        return None, None
    return items[0]["id"], items[0]["snippet"]["title"]


def find_client_file():
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    here = Path(__file__).resolve().parent
    for folder in (here, here.parent, Path.home() / "Downloads"):
        found = sorted(folder.glob("client_secret*.json"), key=lambda p: -p.stat().st_mtime)
        if found:
            return found[0]
    return None


def main():
    path = find_client_file()
    if not path or not path.exists():
        print("Couldn't find client_secret.json.\nPut it in the same folder as this file, then press F5 again.")
        return
    print(f"Using {path.name}")
    cfg = json.load(open(path, encoding="utf-8"))
    cfg = cfg.get("installed") or cfg.get("web") or cfg
    cid, csec = cfg["client_id"], cfg["client_secret"]

    result = {}
    state = secrets.token_urlsafe(16)

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state and "code" in q:
                result["code"] = q["code"][0]
                msg = "QuickSong is connected. You can close this tab."
            else:
                msg = "Login failed: " + q.get("error", ["unknown"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<h2 style='font-family:sans-serif'>{msg}</h2>".encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "select_account consent",
        "include_granted_scopes": "false", "state": state})
    print("Opening your browser to sign in…\nIf it doesn't open, paste this link:\n" + url + "\n")
    webbrowser.open(url)
    while "code" not in result:
        srv.handle_request()

    data = urllib.parse.urlencode({"code": result["code"], "client_id": cid, "client_secret": csec,
                                   "redirect_uri": redirect, "grant_type": "authorization_code"}).encode()
    tok = json.load(urllib.request.urlopen("https://oauth2.googleapis.com/token", data))
    if "refresh_token" not in tok:
        print("No refresh token returned. Remove the app's access at myaccount.google.com/permissions and press F5 again.")
        return
    ch_id, ch_name = which_channel(tok["access_token"])
    if ch_id != TARGET_CHANNEL_ID:
        print("\n✗ WRONG CHANNEL — nothing was saved.\n")
        print(f"You picked:  {ch_name or 'the Gmail itself (it has no channel of its own)'}  ({ch_id or '-'})")
        print(f"Needed:      QuickSong  ({TARGET_CHANNEL_ID})\n")
        print("QuickSong is a 'brand account' channel, so Google treats it as its own account.")
        print("1. In this browser, go to youtube.com → your profile picture → Switch account → QuickSong.")
        print("2. Press F5 here again. On Google's 'Choose an account' screen, QuickSong should now appear")
        print("   as its own entry (with its channel picture). Click QuickSong, not the Gmail.")
        return
    print(f"\n✓ Logged in to the right channel: {ch_name} ({ch_id})")
    text = ("Add these 3 secrets in GitHub: your repo → Settings → Secrets and variables → Actions\n"
            "→ New repository secret. Name on the left, value on the right.\n\n"
            f"YT_CLIENT_ID\n{cid}\n\n"
            f"YT_CLIENT_SECRET\n{csec}\n\n"
            f"YT_REFRESH_TOKEN\n{tok['refresh_token']}\n\n"
            "Keep this private — anyone with these can upload to the channel. "
            "Delete this file once they're in GitHub.\n")
    out = path.parent / "QuickSong_GitHub_Secrets.txt"
    out.write_text(text, encoding="utf-8")
    print("\n✓ Connected!\n")
    print(text)
    print(f"Also saved to: {out}")


if __name__ == "__main__":
    main()
