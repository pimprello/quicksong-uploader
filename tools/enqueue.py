#!/usr/bin/env python3
"""
Adds rendered videos to the upload queue (the "queue" branch of the repo).

  python tools/enqueue.py OUT_FOLDER [--remote URL]

Each <slug>.mp4 + <slug>.json pair from render.py is added as
<batch>-<NNN>__<slug>.mp4/.json: new batches go to the back of the line and, inside a batch,
the most-played songs go first. Songs already waiting are skipped.

The queue branch is kept as ONE commit (rebuilt on every push) so videos that were already
uploaded don't stay in the repo's history and the repo never grows out of control.
Videos are pushed one at a time to keep each push small. Needs only git.
"""
import argparse, datetime, json, subprocess, sys, tempfile
from pathlib import Path

BRANCH = "queue"


def git(repo, *a, check=True, inp=None):
    r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, input=inp)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(a[:2])}: {r.stderr.strip()}")
    return r.stdout.strip()


def fetch_tip(repo):
    r = subprocess.run(["git", "-C", str(repo), "fetch", "-q", "--filter=blob:none", "--depth=1",
                        "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}"],
                       capture_output=True, text=True)
    if r.returncode:
        return None                       # branch doesn't exist yet
    return git(repo, "rev-parse", f"refs/remotes/origin/{BRANCH}")


def tree_entries(repo, tip):
    if not tip:
        return {}
    out = git(repo, "ls-tree", tip)
    ents = {}
    for line in out.splitlines():
        meta, name = line.split("\t", 1)
        ents[name] = meta
    return ents


def push_files(repo, add, message):
    """add: {queue_name: local_path}. Rebuilds the queue as one commit; retries if the
    uploader removed something in the meantime."""
    blobs = {n: git(repo, "hash-object", "-w", str(p)) for n, p in add.items()}
    for attempt in range(6):
        tip = fetch_tip(repo)
        ents = tree_entries(repo, tip)
        for n, sha in blobs.items():
            ents[n] = f"100644 blob {sha}"
        tree = git(repo, "mktree", inp="".join(f"{m}\t{n}\n" for n, m in sorted(ents.items())))
        commit = git(repo, "commit-tree", tree, "-m", message)
        lease = f"--force-with-lease=refs/heads/{BRANCH}:{tip or ''}"
        r = subprocess.run(["git", "-C", str(repo), "push", "-q", lease, "origin",
                            f"{commit}:refs/heads/{BRANCH}"], capture_output=True, text=True)
        if r.returncode == 0:
            return
        print(f"   push retry ({r.stderr.strip()[:120]})")
    raise RuntimeError("could not push to the queue")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--remote", default=None, help="repo URL (default: this repo's origin)")
    args = ap.parse_args()
    folder = Path(args.folder)
    here = Path(__file__).resolve().parent.parent
    remote = args.remote or git(here, "remote", "get-url", "origin")

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        git(repo, "init", "-q")
        git(repo, "remote", "add", "origin", remote)
        git(repo, "config", "user.name", "QuickSong")
        git(repo, "config", "user.email", "quicksong-bot@users.noreply.github.com")
        tip = fetch_tip(repo)
        waiting = {n.split("__", 1)[-1] for n in tree_entries(repo, tip)}

        vids = []
        for mp4 in sorted(folder.glob("*.mp4")):
            if mp4.name in waiting:
                print(f"skip (already queued): {mp4.name}")
                continue
            meta = mp4.with_suffix(".json")
            plays = int(json.loads(meta.read_text(encoding="utf-8")).get("plays") or 0) if meta.exists() else 0
            vids.append((plays, mp4, meta))
        vids.sort(key=lambda x: -x[0])
        if not vids:
            print("Nothing new to queue.")
            return

        batch = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M")
        for i, (_, mp4, meta) in enumerate(vids, 1):
            stem = f"{batch}-{i:03d}__{mp4.stem}"
            add = {stem + ".mp4": mp4}
            if meta.exists():
                add[stem + ".json"] = meta
            push_files(repo, add, f"Queue: {len(vids)} added in batch {batch}")
            print(f"queued {i}/{len(vids)}: {mp4.stem}")

        total = len([n for n in tree_entries(repo, fetch_tip(repo)) if n.endswith(".mp4")])
        print(f"\n{len(vids)} added. {total} waiting ≈ {total / 2:.1f} hours of uploads at one every 30 min.")


if __name__ == "__main__":
    main()
