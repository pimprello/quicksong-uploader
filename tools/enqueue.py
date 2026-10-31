#!/usr/bin/env python3
"""
Adds rendered videos to the upload queue (the "queue" release on GitHub).

  python tools/enqueue.py OUT_FOLDER --repo OWNER/quicksong-uploader

Needs the GitHub CLI (gh) logged in. Each <slug>.mp4 + <slug>.json pair from render.py
is uploaded as  <batch>-<NNN>__<slug>.mp4/.json  so new batches go to the back of the line
and, inside a batch, the most-played songs go first. Already-queued songs are skipped.
"""
import argparse, datetime, json, subprocess, sys
from pathlib import Path

TAG = "queue"


def gh(*a, check=True):
    r = subprocess.run(["gh", *a], capture_output=True, text=True)
    if check and r.returncode:
        sys.exit(f"gh {' '.join(a)}\n{r.stderr}")
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--repo", required=True)
    args = ap.parse_args()
    folder = Path(args.folder)

    if gh("release", "view", TAG, "-R", args.repo, check=False).returncode:
        gh("release", "create", TAG, "-R", args.repo, "--title", "Upload queue",
           "--notes", "Videos waiting to be uploaded. Managed automatically — one is posted every 30 minutes.",
           "--prerelease")
    queued = json.loads(gh("release", "view", TAG, "-R", args.repo, "--json", "assets").stdout)["assets"]
    queued_slugs = {a["name"].split("__", 1)[-1].removeprefix("FAILED_") for a in queued}

    vids = []
    for mp4 in sorted(folder.glob("*.mp4")):
        meta = mp4.with_suffix(".json")
        if f"{mp4.name}" in queued_slugs:
            print(f"skip (already queued): {mp4.name}")
            continue
        plays = 0
        if meta.exists():
            plays = int(json.loads(meta.read_text(encoding="utf-8")).get("plays") or 0)
        vids.append((plays, mp4, meta))
    vids.sort(key=lambda x: -x[0])
    if not vids:
        print("Nothing new to queue.")
        return

    batch = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M")
    tmp = folder / "_queue_upload"
    tmp.mkdir(exist_ok=True)
    for i, (_, mp4, meta) in enumerate(vids, 1):
        stem = f"{batch}-{i:03d}__{mp4.stem}"
        files = []
        for src, ext in ((mp4, ".mp4"), (meta, ".json")):
            if src.exists():
                dst = tmp / (stem + ext)
                dst.write_bytes(src.read_bytes())
                files.append(str(dst))
        gh("release", "upload", TAG, "-R", args.repo, *files)
        for f in files:
            Path(f).unlink()
        print(f"queued {i}/{len(vids)}: {mp4.stem}")
    tmp.rmdir()
    total = len([a for a in queued if a["name"].endswith(".mp4")]) + len(vids)
    hrs = total * 0.5
    print(f"\n{len(vids)} added. {total} in queue ≈ {hrs:.1f} hours of uploads at one every 30 min.")


if __name__ == "__main__":
    main()
