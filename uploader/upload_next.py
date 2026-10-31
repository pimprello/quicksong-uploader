#!/usr/bin/env python3
"""
Uploads ONE video from the queue to YouTube, then removes it from the queue.
Runs inside GitHub Actions every 30 minutes. Standard library + gh CLI only (fast start).

Queue = the "queue" branch of this repo. Each video is two files at the branch root:
    <order>__<slug>.mp4   and   <order>__<slug>.json   (title / description / tags)
Files go out in name order, so the order prefix decides what is posted next.
The workflow only ever reads and deletes on that branch; new videos are added by
tools/enqueue.py, which also squashes the branch so old videos don't pile up in history.
"""
import csv, datetime, json, os, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

REPO = os.environ["REPO"]
BRANCH = "queue"
PRIVACY = os.environ.get("PRIVACY", "public")
CATEGORY = os.environ.get("CATEGORY_ID", "10")      # 10 = Music


def gh(*args, check=True, binary=False):
    r = subprocess.run(["gh", *args], capture_output=True, text=not binary)
    if check and r.returncode != 0:
        err = r.stderr if not binary else r.stderr.decode(errors="replace")
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {err.strip()}")
    return r


def queue_files():
    r = gh("api", f"repos/{REPO}/git/trees/{BRANCH}", check=False)
    if r.returncode != 0:
        return {}
    tree = json.loads(r.stdout)
    return {e["path"]: e["sha"] for e in tree.get("tree", []) if e["type"] == "blob"}


def download(path, dest):
    r = gh("api", "-H", "Accept: application/vnd.github.raw",
           f"repos/{REPO}/contents/{urllib.parse.quote(path)}?ref={BRANCH}", binary=True)
    with open(dest, "wb") as f:
        f.write(r.stdout)
    return dest


def remove(path, sha, note):
    for attempt in range(3):
        r = gh("api", "-X", "DELETE", f"repos/{REPO}/contents/{urllib.parse.quote(path)}",
               "-f", f"message={note}", "-f", f"sha={sha}", "-f", f"branch={BRANCH}", check=False)
        if r.returncode == 0:
            return True
        time.sleep(3)
        sha = queue_files().get(path)
        if not sha:
            return True
    print(f"::warning::Could not remove {path} from the queue: {r.stderr.strip()}")
    return False


def access_token():
    data = urllib.parse.urlencode({
        "client_id": os.environ["YT_CLIENT_ID"],
        "client_secret": os.environ["YT_CLIENT_SECRET"],
        "refresh_token": os.environ["YT_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }).encode()
    try:
        with urllib.request.urlopen("https://oauth2.googleapis.com/token", data, timeout=30) as r:
            return json.load(r)["access_token"]
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if "invalid_grant" in body:
            sys.exit("::error::YouTube login expired (invalid_grant). Open QuickSong_Login.py in IDLE, press F5, "
                     "and paste the new YT_REFRESH_TOKEN into the GitHub secret. If this happens every 7 days, "
                     "publish the Google app (Audience page → Publish app).")
        sys.exit(f"::error::Token refresh failed: {e.code} {body}")


def youtube_upload(token, video_path, meta):
    size = os.path.getsize(video_path)
    body = {
        "snippet": {
            "title": meta.get("title", "")[:100],
            "description": meta.get("description", "")[:5000],
            "tags": meta.get("tags", [])[:30],
            "categoryId": CATEGORY,
        },
        "status": {
            "privacyStatus": meta.get("privacy") or PRIVACY,
            "selfDeclaredMadeForKids": False,
            "embeddable": True,
        },
    }
    init = urllib.request.Request(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Type": "video/mp4",
                 "X-Upload-Content-Length": str(size)})
    with urllib.request.urlopen(init, timeout=60) as r:
        location = r.headers["Location"]
    with open(video_path, "rb") as f:
        payload = f.read()
    for attempt in range(4):
        try:
            put = urllib.request.Request(location, data=payload, method="PUT",
                                         headers={"Authorization": f"Bearer {token}",
                                                  "Content-Type": "video/mp4",
                                                  "Content-Length": str(size)})
            with urllib.request.urlopen(put, timeout=600) as r:
                return json.load(r)["id"]
        except urllib.error.HTTPError as e:
            if e.code in (500, 502, 503, 504) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise


def log_row(video_id, title, fname):
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
    with open("uploaded.csv", "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([now, video_id, title, fname])
    subprocess.run(["git", "config", "user.name", "quicksong-bot"], check=True)
    subprocess.run(["git", "config", "user.email", "quicksong-bot@users.noreply.github.com"], check=True)
    subprocess.run(["git", "add", "uploaded.csv"], check=True)
    msg = f"Uploaded {fname} → https://youtu.be/{video_id}" if video_id != "FAILED" else f"Failed {fname}"
    subprocess.run(["git", "commit", "-q", "-m", msg], check=True)
    for _ in range(3):
        if subprocess.run(["git", "push", "-q"]).returncode == 0:
            return
        subprocess.run(["git", "pull", "-q", "--rebase"])
    print("::warning::Could not push uploaded.csv (the upload itself still happened)")


def main():
    files = queue_files()
    videos = sorted(p for p in files if p.endswith(".mp4"))
    print(f"Queue: {len(videos)} video(s) waiting")
    if not videos:
        print("Nothing to upload — queue is empty.")
        return
    name = videos[0]
    meta_name = name[:-4] + ".json"
    meta = {}
    if meta_name in files:
        meta = json.load(open(download(meta_name, "meta.json"), encoding="utf-8"))
    if not meta.get("title"):
        meta["title"] = name.split("__", 1)[-1][:-4].replace("-", " ").title()

    print(f"Uploading: {meta['title']}")
    download(name, "video.mp4")
    token = access_token()
    try:
        vid = youtube_upload(token, "video.mp4", meta)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if any(k in body for k in ("quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded")):
            sys.exit(f"::warning::YouTube limit reached — will retry next run: {body[:300]}")
        if e.code in (400, 403):
            # bad file or rejected metadata: drop it so the queue keeps moving, and log it
            for n in (name, meta_name):
                if n in files:
                    remove(n, files[n], f"Drop rejected video {name}")
            log_row("FAILED", meta["title"], name)
            sys.exit(f"::error::YouTube rejected {name}: {e.code} {body[:500]}")
        raise

    print(f"✓ https://youtu.be/{vid}  ({meta.get('privacy') or PRIVACY})")
    for n in (name, meta_name):
        if n in files:
            remove(n, files[n], f"Uploaded {name}")
    log_row(vid, meta["title"], name)
    print(f"Left in queue: {len(videos) - 1}")


if __name__ == "__main__":
    main()
