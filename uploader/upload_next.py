#!/usr/bin/env python3
"""
Uploads ONE video from the queue to YouTube, then removes it from the queue.
Runs inside GitHub Actions every 30 minutes. Standard library only (fast start).

Queue = the GitHub release tagged "queue". Each video is two assets:
    <order>__<slug>.mp4   and   <order>__<slug>.json   (title / description / tags)
Assets are uploaded in name order, so the order prefix decides what goes out next.
"""
import csv, datetime, json, os, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

REPO = os.environ["REPO"]
QUEUE_TAG = "queue"
PRIVACY = os.environ.get("PRIVACY", "public")
CATEGORY = os.environ.get("CATEGORY_ID", "10")      # 10 = Music


def gh(*args, check=True):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def gh_json(path, method="GET"):
    out = gh("api", "-X", method, "-H", "Accept: application/vnd.github+json", path)
    return json.loads(out) if out.strip() else None


def queue_assets():
    try:
        rel = gh_json(f"repos/{REPO}/releases/tags/{QUEUE_TAG}")
    except RuntimeError:
        return None, []
    assets = []
    page = 1
    while True:
        batch = gh_json(f"repos/{REPO}/releases/{rel['id']}/assets?per_page=100&page={page}")
        assets += batch
        if len(batch) < 100:
            break
        page += 1
    return rel, assets


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
            sys.exit("::error::YouTube login expired (invalid_grant). Run tools/get_token.py again and "
                     "update the YT_REFRESH_TOKEN secret. If this keeps happening every 7 days, set the "
                     "OAuth consent screen to 'In production'.")
        sys.exit(f"::error::Token refresh failed: {e.code} {body}")


def download(asset, dest):
    gh("release", "download", QUEUE_TAG, "-R", REPO, "-p", asset["name"], "-O", dest, "--clobber")
    return dest


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
            "privacyStatus": PRIVACY,
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

    for attempt in range(4):
        try:
            with open(video_path, "rb") as f:
                put = urllib.request.Request(location, data=f.read(), method="PUT",
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


def log_upload(video_id, title, fname):
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    with open("uploaded.csv", "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([now, video_id, title, fname])
    subprocess.run(["git", "config", "user.name", "quicksong-bot"], check=True)
    subprocess.run(["git", "config", "user.email", "quicksong-bot@users.noreply.github.com"], check=True)
    subprocess.run(["git", "add", "uploaded.csv"], check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"Uploaded {fname} → https://youtu.be/{video_id}"], check=True)
    for _ in range(3):
        if subprocess.run(["git", "push", "-q"]).returncode == 0:
            return
        subprocess.run(["git", "pull", "-q", "--rebase"])
    print("::warning::Could not push uploaded.csv (video was still uploaded)")


def main():
    rel, assets = queue_assets()
    by_name = {a["name"]: a for a in assets}
    videos = sorted(n for n in by_name if n.endswith(".mp4") and not n.startswith("FAILED_"))
    print(f"Queue: {len(videos)} video(s) waiting")
    if not videos:
        print("Nothing to upload — queue is empty.")
        return
    name = videos[0]
    meta_name = name[:-4] + ".json"
    meta = {}
    if meta_name in by_name:
        download(by_name[meta_name], "meta.json")
        meta = json.load(open("meta.json", encoding="utf-8"))
    if not meta.get("title"):
        meta["title"] = name.split("__", 1)[-1][:-4].replace("-", " ").title()

    print(f"Uploading: {meta['title']}")
    download(by_name[name], "video.mp4")
    token = access_token()
    try:
        vid = youtube_upload(token, "video.mp4", meta)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if any(k in body for k in ("quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded")):
            sys.exit(f"::warning::YouTube limit reached, will retry next run: {body[:300]}")
        if e.code in (400, 403):
            # bad file or rejected metadata: park it so the queue keeps moving
            for n in (name, meta_name):
                if n in by_name:
                    gh("api", "-X", "PATCH", f"repos/{REPO}/releases/assets/{by_name[n]['id']}",
                       "-f", f"name=FAILED_{n}", check=False)
            sys.exit(f"::error::YouTube rejected {name}: {e.code} {body[:500]}")
        raise

    print(f"✓ https://youtu.be/{vid}  ({PRIVACY})")
    for n in (name, meta_name):
        if n in by_name:
            gh("api", "-X", "DELETE", f"repos/{REPO}/releases/assets/{by_name[n]['id']}", check=False)
    log_upload(vid, meta["title"], name)
    print(f"Left in queue: {len(videos) - 1}")


if __name__ == "__main__":
    main()
