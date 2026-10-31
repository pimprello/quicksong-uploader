#!/usr/bin/env python3
"""
One-time YouTube login for the QuickSong channel. Run this on YOUR computer:

    python tools/get_token.py path\\to\\client_secret.json

A browser opens — sign in with the QuickSong Gmail and pick the QuickSong channel.
It then prints the three values to paste into GitHub → Settings → Secrets → Actions:
    YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN
Standard library only, nothing to install.
"""
import http.server, json, secrets, sys, threading, urllib.parse, urllib.request, webbrowser

SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
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
        "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state})
    print("Opening your browser to sign in…\nIf it doesn't open, paste this link:\n" + url + "\n")
    webbrowser.open(url)
    while "code" not in result:
        srv.handle_request()

    data = urllib.parse.urlencode({"code": result["code"], "client_id": cid, "client_secret": csec,
                                   "redirect_uri": redirect, "grant_type": "authorization_code"}).encode()
    tok = json.load(urllib.request.urlopen("https://oauth2.googleapis.com/token", data))
    if "refresh_token" not in tok:
        sys.exit("No refresh token returned. Remove the app's access at myaccount.google.com/permissions and run again.")
    print("Add these as GitHub Actions secrets (repo → Settings → Secrets and variables → Actions):\n")
    print(f"YT_CLIENT_ID      = {cid}")
    print(f"YT_CLIENT_SECRET  = {csec}")
    print(f"YT_REFRESH_TOKEN  = {tok['refresh_token']}")
    print("\nKeep these private — anyone with them can upload to the channel.")


if __name__ == "__main__":
    main()
