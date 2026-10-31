#!/usr/bin/env python3
"""
QuickSong renderer
------------------
Turns a folder of songs into vertical 1080x1920 visualiser Shorts.

For every song it:
  1. finds the catchiest ~30 s (the most repeated, high-energy section = the chorus/hook)
  2. makes it nightcore: 1.3x speed with the pitch raised to match, plus reverb and a polish chain
  3. takes the colour palette from the cover art
  4. renders a cover-art-centred visualiser (glow, circular spectrum, particles, title/artist)
  5. writes  <out>/<slug>.mp4  and  <out>/<slug>.json  (YouTube title / description / tags)

Usage:
  python render.py SONG_FOLDER [--out OUT_FOLDER] [--only NAME] [--fps 30]

Per-song info is read from (first match wins):
  - <song>.json next to the audio file
  - metadata.json / metadata.csv in the folder (keyed by file name)
  - the audio file's own tags (title, artist, embedded cover)
Recognised keys: title, artist, cover, hook_start (seconds in the ORIGINAL song), tags
Cover art: "cover" key, embedded art, <song>.jpg/.png, or cover.jpg/folder.jpg in the folder.
"""
import argparse, csv, io, json, math, os, re, subprocess, sys, unicodedata
from pathlib import Path

import numpy as np
import cairo
import librosa
from PIL import Image, ImageFilter, ImageOps
from pedalboard import (Pedalboard, Reverb, LowShelfFilter, HighShelfFilter,
                        Compressor, Limiter, Gain)

# ----------------------------------------------------------------- settings
W, H = 1080, 1920
SPEED = 1.3                 # nightcore: speed and pitch together
CLIP_SECONDS = 30.0         # length of the finished video
SR = 44100
AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".aiff", ".aif"}
IMG_EXT = [".jpg", ".jpeg", ".png", ".webp"]
CHANNEL_TAG = "@QuickSong"
FONT_TITLE = "Poppins"
FONT_BODY = "Inter"

TITLE_TEMPLATE = "{title} – {artist} (sped up) #shorts"
DESC_TEMPLATE = ("{title} by {artist} — sped up + reverb 🎧\n\n"
                 "New sped up songs every 30 minutes. Subscribe for more.\n\n"
                 "#spedup #nightcore #speedup #{artist_tag} #shorts")
BASE_TAGS = ["sped up", "nightcore", "sped up songs", "speed up", "reverb", "shorts"]


# ----------------------------------------------------------------- metadata
def slugify(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or "song"


def folder_metadata(folder):
    meta = {}
    j = folder / "metadata.json"
    if j.exists():
        data = json.loads(j.read_text(encoding="utf-8"))
        if isinstance(data, list):
            for row in data:
                key = row.get("file") or row.get("filename") or row.get("title")
                if key:
                    meta[Path(key).stem.lower()] = row
        elif isinstance(data, dict):
            for k, v in data.items():
                meta[Path(k).stem.lower()] = v
    c = folder / "metadata.csv"
    if c.exists():
        with c.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
                key = row.get("file") or row.get("filename") or row.get("title")
                if key:
                    meta.setdefault(Path(key).stem.lower(), row)
    return meta


def tag_metadata(path):
    """title / artist / embedded cover bytes from the file's tags."""
    out = {}
    try:
        import mutagen
        mf = mutagen.File(path)
        if mf is None:
            return out
        easy = mutagen.File(path, easy=True)
        if easy is not None and easy.tags:
            for k in ("title", "artist"):
                v = easy.tags.get(k)
                if v:
                    out[k] = v[0]
        tags = mf.tags
        if tags is not None:
            for key in list(tags.keys()):           # ID3 APIC
                if str(key).startswith("APIC"):
                    out["cover_bytes"] = tags[key].data
                    break
            if "covr" in tags:                      # MP4
                out["cover_bytes"] = bytes(tags["covr"][0])
            if hasattr(tags, "get") and tags.get("metadata_block_picture"):  # Ogg
                import base64
                from mutagen.flac import Picture
                out["cover_bytes"] = Picture(base64.b64decode(tags["metadata_block_picture"][0])).data
        if hasattr(mf, "pictures") and mf.pictures:  # FLAC
            out["cover_bytes"] = mf.pictures[0].data
    except Exception as e:
        print(f"   (couldn't read tags: {e})")
    return out


def find_cover(path, info, folder):
    if info.get("cover"):
        p = Path(info["cover"])
        p = p if p.is_absolute() else folder / p
        if p.exists():
            return Image.open(p).convert("RGB")
    if info.get("cover_bytes"):
        try:
            return Image.open(io.BytesIO(info["cover_bytes"])).convert("RGB")
        except Exception:
            pass
    for ext in IMG_EXT:
        for cand in (path.with_suffix(ext), path.with_suffix(ext.upper())):
            if cand.exists():
                return Image.open(cand).convert("RGB")
    for name in ("cover", "folder", "artwork", "front"):
        for ext in IMG_EXT:
            p = folder / f"{name}{ext}"
            if p.exists():
                return Image.open(p).convert("RGB")
    return None


# --- QuickSong downloader format: <audio file>.metadata.json + covers/ folder
SIDE_INDEX = {}


def index_sidecars(root):
    """Map every *.metadata.json under root by the audio name it describes."""
    SIDE_INDEX.clear()
    for p in root.rglob("*.metadata.json"):
        base = p.name[: -len(".metadata.json")]
        for key in {base.lower(), Path(base).stem.lower()}:
            SIDE_INDEX[key] = p


def deep_get(d, *names):
    """First non-empty value for any of names, searching nested dicts too."""
    names = [n.lower() for n in names]
    stack = [d]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if str(k).lower() in names and v not in (None, "", [], {}):
                    return v
            stack.extend(v for v in cur.values() if isinstance(v, dict))
    return None


def read_downloader_record(path):
    p = SIDE_INDEX.get(path.name.lower()) or SIDE_INDEX.get(path.stem.lower())
    if not p:
        return {}
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"   (bad metadata json {p.name}: {e})")
        return {}
    out = {"_record": rec, "_record_path": p}
    t = deep_get(rec, "title", "track_title", "song")
    a = deep_get(rec, "artist", "uploader", "creator", "user", "username")
    if isinstance(a, dict):
        a = a.get("username") or a.get("name")
    if t: out["title"] = str(t)
    if a: out["artist"] = str(a)
    yt = deep_get(rec, "youtube", "youtube_metadata", "prepared_youtube", "yt")
    yt = yt if isinstance(yt, dict) else rec
    yt_title = deep_get(yt, "youtube_title") or (deep_get(yt, "title") if yt is not rec else None)
    yt_desc = deep_get(yt, "youtube_description") or (deep_get(yt, "description") if yt is not rec else None)
    yt_tags = deep_get(yt, "youtube_tags") or (deep_get(yt, "tags") if yt is not rec else None)
    if yt_title: out["yt_title"] = str(yt_title)
    if yt_desc: out["yt_description"] = str(yt_desc)
    if yt_tags: out["yt_tags"] = yt_tags
    plays = deep_get(rec, "playback_count", "play_count", "plays", "view_count")
    try:
        out["plays"] = int(plays) if plays is not None else 0
    except (TypeError, ValueError):
        out["plays"] = 0
    art = deep_get(rec, "artwork_path", "cover_path", "artwork_file", "cover", "artwork", "thumbnail")
    if isinstance(art, str) and not art.startswith("http"):
        cand = Path(art)
        for c in (cand, p.parent / cand, p.parent / "covers" / cand.name):
            if c.exists():
                out["cover"] = str(c.resolve())
                break
    for k in ("hook_start",):
        v = deep_get(rec, k)
        if v not in (None, ""):
            out[k] = v
    return out


def cover_from_covers_dir(path, root):
    """Match a file in any covers/ folder by audio name or the trailing track ID."""
    stem = path.stem.lower()
    ids = re.findall(r"\d{5,}", stem)
    for d in [path.parent / "covers", path.parent.parent / "covers", root / "covers"]:
        if not d.is_dir():
            continue
        imgs = [p for p in d.iterdir() if p.suffix.lower() in IMG_EXT]
        for p in imgs:
            if p.stem.lower() == stem:
                return p
        for p in imgs:
            if ids and any(i in p.stem for i in ids):
                return p
        for p in imgs:
            if p.stem.lower() in stem or stem in p.stem.lower():
                return p
    return None


def gather_info(path, folder, fmeta, root=None):
    info = tag_metadata(path)
    rec = read_downloader_record(path)
    info.update({k: v for k, v in rec.items() if v not in (None, "")})
    if not info.get("cover"):
        c = cover_from_covers_dir(path, root or folder)
        if c:
            info["cover"] = str(c.resolve())
    row = fmeta.get(path.stem.lower())
    if row:
        info.update({k.lower(): v for k, v in row.items() if v not in (None, "")})
    side = path.with_suffix(".json")
    if side.exists() and not side.name.endswith(".metadata.json"):
        info.update(json.loads(side.read_text(encoding="utf-8")))
    # strip downloader-style " - 123456789" track IDs from fallback names
    if not info.get("title") or not info.get("artist"):
        stem = re.sub(r"\s*[-_]\s*\d{5,}$", "", path.stem)
        stem = re.sub(r"[_]+", " ", stem)
        if " - " in stem:
            a, t = stem.split(" - ", 1)
            info.setdefault("artist", a.strip())
            info.setdefault("title", t.strip())
        info.setdefault("title", stem.strip())
        info.setdefault("artist", "Unknown Artist")
    return info


# ----------------------------------------------------------------- best 30 s
def find_hook(y_mono, sr, window_s):
    """Start time (s, original song) of the most repeated + energetic window."""
    dur = len(y_mono) / sr
    if dur <= window_s + 1:
        return 0.0
    hop = 512
    tempo, beats = librosa.beat.beat_track(y=y_mono, sr=sr, hop_length=hop)
    if len(beats) < 32:
        beats = np.arange(0, librosa.time_to_frames(dur, sr=sr, hop_length=hop), 22)
    chroma = librosa.feature.chroma_cqt(y=y_mono, sr=sr, hop_length=hop)
    mfcc = librosa.feature.mfcc(y=y_mono, sr=sr, hop_length=hop, n_mfcc=13)
    rms = librosa.feature.rms(y=y_mono, hop_length=hop)[0]
    c = librosa.util.sync(chroma, beats, aggregate=np.median)
    m = librosa.util.sync(mfcc, beats, aggregate=np.mean)
    e = librosa.util.sync(rms[None, :], beats, aggregate=np.mean)[0]
    bt = librosa.frames_to_time(np.concatenate([[0], beats]), sr=sr, hop_length=hop)[:c.shape[1]]

    # stack 4 beats of chroma so we match phrases, not single chords
    feat = librosa.feature.stack_memory(c, n_steps=4, mode="edge")
    feat = feat / (np.linalg.norm(feat, axis=0, keepdims=True) + 1e-9)
    S = feat.T @ feat
    n = S.shape[0]
    idx = np.arange(n)
    S[np.abs(idx[:, None] - idx[None, :]) < 16] = 0        # ignore self / neighbours
    k = max(3, n // 20)
    rep = np.sort(S, axis=1)[:, -k:].mean(axis=1)            # how much this beat recurs

    def z(x):
        return (x - x.mean()) / (x.std() + 1e-9)
    energy = z(np.log(e + 1e-6))
    bright = z(m[1]) * 0.15                                  # chorus tends to be brighter
    score = 0.55 * z(rep) + 0.45 * energy + bright

    # intro/outro penalty
    pos = bt / dur
    score -= 1.2 * np.clip(0.1 - pos, 0, None) * 10
    score -= 1.0 * np.clip(pos - 0.9, 0, None) * 10

    best, best_t = -1e9, 0.0
    for i in range(n):
        t0 = bt[i]
        if t0 + window_s > dur:
            break
        sel = (bt >= t0) & (bt < t0 + window_s)
        w = score[sel]
        if len(w) < 4:
            continue
        # front-load: the hook should hit early in the clip
        weights = np.linspace(1.2, 0.8, len(w))
        s = float(np.average(w, weights=weights))
        if s > best:
            best, best_t = s, t0
    # start one bar early so the hook lands with a lead-in
    beat_len = 60.0 / float(np.atleast_1d(tempo)[0] or 120)
    return float(max(0.0, min(best_t - 2 * beat_len, dur - window_s)))


# ----------------------------------------------------------------- audio fx
def nightcore(seg, sr):
    """seg: (channels, samples). 1.3x speed with matching pitch rise + reverb + polish."""
    out = librosa.resample(seg, orig_sr=int(sr * SPEED), target_sr=sr, res_type="soxr_vhq")
    board = Pedalboard([
        LowShelfFilter(cutoff_frequency_hz=140, gain_db=3.0),    # restore body lost to pitch-up
        HighShelfFilter(cutoff_frequency_hz=9000, gain_db=-2.0), # tame chipmunk fizz
        Reverb(room_size=0.42, damping=0.55, wet_level=0.16, dry_level=0.86, width=1.0),
        Compressor(threshold_db=-16, ratio=2.5, attack_ms=8, release_ms=120),
        Gain(gain_db=2.0),
        Limiter(threshold_db=-1.6, release_ms=80),
    ])
    out = board(out.astype(np.float32), sr)
    n = out.shape[1]
    fi, fo = int(0.25 * sr), int(1.6 * sr)
    env = np.ones(n, dtype=np.float32)
    env[:fi] = np.linspace(0, 1, fi) ** 2
    env[-fo:] = np.linspace(1, 0, fo) ** 1.5
    return out * env


# ----------------------------------------------------------------- palette
import colorsys


def _hsv(c):
    return colorsys.rgb_to_hsv(*(np.asarray(c, float) / 255.0))


def _rgb(h, s, v):
    return tuple(int(round(x * 255)) for x in colorsys.hsv_to_rgb(h % 1.0, s, v))


def palette_from_cover(img):
    """Harmonious, moody colours taken from the cover's own dominant colours."""
    px = np.asarray(img.convert("RGB").resize((80, 80))).reshape(-1, 3).astype(np.float32)
    rng = np.random.default_rng(0)
    k = 8
    cent = px[rng.choice(len(px), k, replace=False)]
    for _ in range(25):
        lab = ((px[:, None] - cent[None]) ** 2).sum(-1).argmin(1)
        for j in range(k):
            if (lab == j).any():
                cent[j] = px[lab == j].mean(0)
    share = np.bincount(lab, minlength=k) / len(px)
    cols = []
    for j in np.argsort(-share):
        if share[j] < 0.02:
            continue
        h, s, v = _hsv(cent[j])
        cols.append(dict(h=h, s=s, v=v, w=share[j]))
    # weight by presence + a little colourfulness
    cols.sort(key=lambda c: c["w"] * (0.35 + c["s"]) * (0.3 + c["v"]), reverse=True)
    colourful = [c for c in cols if c["s"] > 0.18 and c["v"] > 0.15]
    mono = len(colourful) == 0
    base = colourful[:4] if not mono else cols[:3]

    blobs = []
    for i, c in enumerate(base):
        if mono:                                    # black & white / grey cover → elegant neutrals
            blobs.append(_rgb(0.62, 0.06, 0.18 + 0.12 * i))
        else:
            s = float(np.clip(c["s"] * 0.85, 0.30, 0.72))
            v = float(np.clip(c["v"] * 0.75, 0.26, 0.58))
            blobs.append(_rgb(c["h"], s, v))
    while len(blobs) < 4:                           # fill with gentle shifts of the main hue
        h, s, v = _hsv(blobs[0])
        blobs.append(_rgb(h + 0.035 * len(blobs), s * 0.85, v * (0.75 + 0.1 * len(blobs))))
    h0, s0, v0 = _hsv(blobs[0])
    return dict(
        blobs=blobs[:4],
        base=_rgb(h0, min(0.6, s0 * 0.9), 0.10),     # deep shadow tone
        bar=(255, 255, 255),
        accent=_rgb(h0, min(0.35, s0 * 0.5), 1.0),   # very light tint for progress fill
    )


# ----------------------------------------------------------------- render helpers
_SURF_BUFFERS = []


def np_to_surface(arr_rgba):
    """uint8 RGBA (premultiplied not required for opaque) → cairo surface."""
    h, w = arr_rgba.shape[:2]
    a = arr_rgba[..., 3:4].astype(np.uint16)
    bgra = np.empty_like(arr_rgba)
    bgra[..., 0] = (arr_rgba[..., 2] * a[..., 0] // 255)
    bgra[..., 1] = (arr_rgba[..., 1] * a[..., 0] // 255)
    bgra[..., 2] = (arr_rgba[..., 0] * a[..., 0] // 255)
    bgra[..., 3] = arr_rgba[..., 3]
    stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_ARGB32, w)
    buf = np.zeros((h, stride // 4, 4), dtype=np.uint8)
    buf[:, :w] = bgra
    surf = cairo.ImageSurface.create_for_data(memoryview(buf), cairo.FORMAT_ARGB32, w, h, stride)
    _SURF_BUFFERS.append(buf)
    return surf


def pil_to_surface(img):
    return np_to_surface(np.asarray(img.convert("RGBA")).copy())


def c01(rgb, a=None):
    r, g, b = (x / 255.0 for x in rgb)
    return (r, g, b) if a is None else (r, g, b, a)


def rounded_rect(ctx, x, y, w, h, r):
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()


def fit_font(ctx, text, family, weight, max_w, start, min_size):
    size = start
    while size > min_size:
        ctx.select_font_face(family, cairo.FONT_SLANT_NORMAL, weight)
        ctx.set_font_size(size)
        if ctx.text_extents(text).x_advance <= max_w:
            return size, text
        size -= 2
    ctx.select_font_face(family, cairo.FONT_SLANT_NORMAL, weight)
    ctx.set_font_size(min_size)
    while ctx.text_extents(text + "…").x_advance > max_w and len(text) > 3:
        text = text[:-1]
    return min_size, (text.rstrip() + "…")


# ----------------------------------------------------------------- analysis for visuals
def analyse(audio, sr, fps, nbars):
    mono = audio.mean(0)
    hop = sr // fps
    n_frames = int(math.ceil(len(mono) / hop))
    S = np.abs(librosa.stft(mono, n_fft=4096, hop_length=hop, center=True))[:, :n_frames]
    freqs = librosa.fft_frequencies(sr=sr, n_fft=4096)
    edges = np.geomspace(50, 12000, nbars + 1)
    bands = np.zeros((nbars, S.shape[1]), np.float32)
    for i in range(nbars):
        sel = (freqs >= edges[i]) & (freqs < edges[i + 1])
        bands[i] = S[sel].mean(0) if sel.any() else S[np.argmin(np.abs(freqs - edges[i]))]
    db = librosa.amplitude_to_db(bands, ref=np.max)
    lvl = np.clip((db + 58) / 58, 0, 1) ** 1.8
    lvl *= np.linspace(0.9, 1.35, nbars)[:, None]
    lvl = np.clip(lvl, 0, 1)
    # neighbour smoothing → flowing shape, not jagged noise
    k = np.array([0.2, 0.6, 0.2])
    lvl = np.apply_along_axis(lambda x: np.convolve(x, k, mode="same"), 0, lvl)
    sm = np.zeros_like(lvl)
    for t in range(lvl.shape[1]):
        prev = sm[:, t - 1] if t else 0
        cur = lvl[:, t]
        sm[:, t] = np.where(cur > prev, prev + (cur - prev) * 0.55, prev * 0.88 + cur * 0.12)
    bass = bands[edges[:-1] < 150].mean(0)
    lo, hi = np.percentile(bass, 10), np.percentile(bass, 98)
    bass = np.clip((bass - lo) / (hi - lo + 1e-9), 0, 1)
    bsm = np.zeros_like(bass)
    for t in range(len(bass)):
        p = bsm[t - 1] if t else 0
        bsm[t] = p + (bass[t] - p) * (0.45 if bass[t] > p else 0.10)
    pad = lambda x: np.pad(x, (0, max(0, n_frames - len(x))), mode="edge")[:n_frames]
    return sm[:, :n_frames], pad(bsm), n_frames


# ----------------------------------------------------------------- background
class MeshBackground:
    """Slow, liquid colour field (Apple-Music style) built from the cover palette."""
    def __init__(self, cover, pal, seed=3):
        self.lw, self.lh = 108, 192                  # low-res field, scaled up smoothly
        yy, xx = np.mgrid[0:self.lh, 0:self.lw].astype(np.float32)
        self.xx, self.yy = xx / self.lw, yy / self.lh
        self.cols = np.array([c01(c) for c in pal["blobs"]], np.float32)
        self.base = np.array(c01(pal["base"]), np.float32)
        rng = np.random.default_rng(seed)
        self.ph = rng.uniform(0, 2 * math.pi, (4, 2))
        self.fr = rng.uniform(0.05, 0.11, (4, 2))
        # blurred cover underlay keeps the background tied to the artwork's shapes
        under = ImageOps.fit(cover, (self.lw, self.lh), Image.LANCZOS).filter(ImageFilter.GaussianBlur(10))
        self.under = np.asarray(under).astype(np.float32) / 255.0
        self.surf = cairo.ImageSurface(cairo.FORMAT_RGB24, self.lw, self.lh)
        self.buf = np.ndarray((self.lh, self.surf.get_stride() // 4, 4), np.uint8, self.surf.get_data())
        # static film grain (kills banding, adds texture)
        g = (rng.normal(0, 1, (H, W)) * 5 + 128).clip(0, 255).astype(np.uint8)
        grain = np.zeros((H, W, 4), np.uint8)
        grain[..., :3] = g[..., None]
        grain[..., 3] = 14
        self.grain = np_to_surface(grain)
        # vignette (top & bottom darker so text and Shorts UI stay readable)
        v = np.zeros((H, W, 4), np.uint8)
        yv = np.linspace(0, 1, H)[:, None]
        xv = np.linspace(-1, 1, W)[None, :]
        a = 0.55 * np.clip(1 - yv / 0.18, 0, 1) ** 2 + 0.75 * np.clip((yv - 0.62) / 0.38, 0, 1) ** 1.6
        a = np.clip(a + 0.25 * xv ** 2, 0, 0.85)
        v[..., 3] = (a * 255).astype(np.uint8)
        self.vignette = np_to_surface(v)

    def frame(self, t, b):
        field = np.zeros((self.lh, self.lw, 3), np.float32)
        wsum = np.zeros((self.lh, self.lw, 1), np.float32)
        for i in range(4):
            cx = 0.5 + 0.42 * math.sin(t * self.fr[i, 0] * 2 * math.pi / 6 + self.ph[i, 0])
            cy = 0.5 + 0.40 * math.sin(t * self.fr[i, 1] * 2 * math.pi / 6 + self.ph[i, 1])
            d2 = (self.xx - cx) ** 2 + ((self.yy - cy) * 1.78) ** 2 * 0.35
            w = np.exp(-d2 / 0.09)[..., None]
            field += w * self.cols[i]
            wsum += w
        field = field / (wsum + 1e-4)
        mix = np.clip(wsum * 1.4, 0, 1)
        field = field * mix + self.base * (1 - mix)
        field = field * 0.78 + self.under * 0.22
        field *= 0.92 + 0.10 * b                      # breathe with the bass
        rgb = (np.clip(field, 0, 1) * 255).astype(np.uint8)
        self.buf[..., 0] = rgb[..., 2]
        self.buf[..., 1] = rgb[..., 1]
        self.buf[..., 2] = rgb[..., 0]
        self.surf.mark_dirty()
        return self.surf


# ----------------------------------------------------------------- the renderer
def render_video(audio, sr, cover, info, pal, out_mp4, fps=30):
    NB = 34                                           # bars per half (mirrored → 68)
    spec, bass, n_frames = analyse(audio, sr, fps, NB)
    total = len(audio[0]) / sr

    bg = MeshBackground(cover, pal)

    # layout (safe for Shorts UI: nothing important in the bottom ~360px or far right edge)
    M = 120
    CW = W - 2 * M                                    # 840 — cover and content width
    CX, CY = M, 300
    RAD = 30
    cov_img = ImageOps.fit(cover, (CW, CW), Image.LANCZOS)
    cov_surf = pil_to_surface(cov_img)

    # soft, deep shadow under the cover (pre-blurred once)
    pad = 160
    sh = Image.new("L", (CW + 2 * pad, CW + 2 * pad), 0)
    from PIL import ImageDraw
    ImageDraw.Draw(sh).rounded_rectangle([pad, pad + 30, pad + CW, pad + CW + 30], RAD, fill=170)
    sh = sh.filter(ImageFilter.GaussianBlur(48))
    sh_arr = np.zeros((sh.height, sh.width, 4), np.uint8)
    sh_arr[..., 3] = np.asarray(sh)
    shadow = np_to_surface(sh_arr)

    title, artist = info["title"], info["artist"]
    TY = CY + CW + 112                                # title baseline
    VIS_Y = TY + 190                                  # visualiser centre line
    VIS_H = 96                                        # max half-height
    PY = VIS_Y + VIS_H + 70                           # progress bar

    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    ctx = cairo.Context(surf)
    ctx.set_antialias(cairo.ANTIALIAS_BEST)

    # text never changes: measure once
    ctx.select_font_face(FONT_TITLE, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    t_size, t_txt = fit_font(ctx, title, FONT_TITLE, cairo.FONT_WEIGHT_BOLD, CW, 66, 42)
    a_size, a_txt = fit_font(ctx, artist, FONT_BODY, cairo.FONT_WEIGHT_NORMAL, CW, 40, 30)

    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
           "-f", "f32le", "-ar", str(sr), "-ac", "2", "-i", out_mp4 + ".pcm",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
           "-profile:v", "high", "-movflags", "+faststart",
           "-c:a", "aac", "-b:a", "256k", "-shortest", out_mp4]
    audio.T.astype(np.float32).tofile(out_mp4 + ".pcm")
    ff = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    nbar = NB * 2
    gap = CW / nbar
    bw = gap * 0.52
    for f in range(n_frames):
        t = f / fps
        b = float(bass[f])

        # background
        ctx.save()
        ctx.scale(W / bg.lw, H / bg.lh)
        ctx.set_source_surface(bg.frame(t, b), 0, 0)
        ctx.get_source().set_filter(cairo.FILTER_BEST)
        ctx.paint()
        ctx.restore()
        ctx.set_source_surface(bg.vignette, 0, 0); ctx.paint()

        # cover: shadow + art, a barely-there pulse on the kick
        s = 1.0 + 0.010 * b
        cw = CW * s
        x, y = W / 2 - cw / 2, CY + CW / 2 - cw / 2
        ctx.save()
        ctx.translate(W / 2, CY + CW / 2); ctx.scale(s, s); ctx.translate(-W / 2, -(CY + CW / 2))
        ctx.set_source_surface(shadow, CX - pad, CY - pad)
        ctx.paint_with_alpha(0.85)
        ctx.restore()
        ctx.save()
        rounded_rect(ctx, x, y, cw, cw, RAD * s)
        ctx.clip()
        ctx.translate(x, y); ctx.scale(s, s)
        ctx.set_source_surface(cov_surf, 0, 0)
        ctx.get_source().set_filter(cairo.FILTER_GOOD)
        ctx.paint()
        ctx.restore()
        ctx.set_line_width(1.5)
        ctx.set_source_rgba(1, 1, 1, 0.10)
        rounded_rect(ctx, x, y, cw, cw, RAD * s); ctx.stroke()

        # title + artist (left aligned to the cover)
        ctx.select_font_face(FONT_TITLE, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(t_size)
        ctx.set_source_rgba(1, 1, 1, 0.98)
        ctx.move_to(M, TY); ctx.show_text(t_txt)
        ctx.select_font_face(FONT_BODY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        ctx.set_font_size(a_size)
        ctx.set_source_rgba(1, 1, 1, 0.62)
        ctx.move_to(M, TY + 62); ctx.show_text(a_txt)

        # mirrored bar visualiser: bass in the middle, highs to the edges
        lv = spec[:, f]
        vals = np.concatenate([lv[::-1], lv])
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        ctx.set_line_width(bw)
        for i in range(nbar):
            v = float(vals[i])
            edge = 1 - abs((i + 0.5) / nbar - 0.5) * 2      # taper toward the sides
            hh = 3 + VIS_H * v * (0.55 + 0.45 * edge)
            bx = M + gap * (i + 0.5)
            ctx.set_source_rgba(1, 1, 1, 0.55 + 0.40 * min(1.0, v * 1.3))
            ctx.move_to(bx, VIS_Y - hh); ctx.line_to(bx, VIS_Y + hh)
            ctx.stroke()

        # progress bar + times
        p = min(1.0, t / total)
        ctx.set_line_width(5)
        ctx.set_source_rgba(1, 1, 1, 0.20)
        ctx.move_to(M, PY); ctx.line_to(M + CW, PY); ctx.stroke()
        ctx.set_source_rgba(*c01(pal["accent"], 0.95))
        ctx.move_to(M, PY); ctx.line_to(M + max(0.5, CW * p), PY); ctx.stroke()
        ctx.set_source_rgba(1, 1, 1, 1)
        ctx.arc(M + CW * p, PY, 9, 0, 2 * math.pi); ctx.fill()
        ctx.select_font_face(FONT_BODY, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        ctx.set_font_size(24)
        ctx.set_source_rgba(1, 1, 1, 0.55)
        tl = f"{int(t)//60}:{int(t)%60:02d}"
        tr = f"-{int(total - t)//60}:{int(total - t)%60:02d}"
        ctx.move_to(M, PY + 42); ctx.show_text(tl)
        e2 = ctx.text_extents(tr)
        ctx.move_to(M + CW - e2.x_advance, PY + 42); ctx.show_text(tr)

        ctx.set_source_surface(bg.grain, 0, 0); ctx.paint()

        fade = min(1.0, t / 0.45, (total - t) / 0.8)
        if fade < 1:
            ctx.set_source_rgba(0, 0, 0, 1 - max(0.0, fade)); ctx.paint()

        surf.flush()
        ff.stdin.write(surf.get_data())
    ff.stdin.close()
    rc = ff.wait()
    os.remove(out_mp4 + ".pcm")
    if rc != 0:
        raise RuntimeError("ffmpeg failed")


# ----------------------------------------------------------------- per song
def process(path, folder, out_dir, fmeta, fps, root=None, info=None):
    info = info or gather_info(path, folder, fmeta, root)
    title, artist = str(info["title"]).strip(), str(info["artist"]).strip()
    slug = slugify(f"{artist}-{title}")
    out_mp4 = str(out_dir / f"{slug}.mp4")
    print(f"♪ {artist} – {title}")

    y, sr = librosa.load(str(path), sr=SR, mono=False)
    if y.ndim == 1:
        y = np.stack([y, y])
    y_mono = librosa.to_mono(y)
    window = CLIP_SECONDS * SPEED
    if info.get("hook_start") not in (None, ""):
        start = float(info["hook_start"])
        print(f"   hook from metadata: {start:.1f}s")
    else:
        an = librosa.resample(y_mono, orig_sr=sr, target_sr=22050)
        start = find_hook(an, 22050, window)
        print(f"   hook detected at {int(start)//60}:{start%60:04.1f}")
    a, b = int(start * sr), int(min(len(y_mono), (start + window) * sr))
    seg = y[:, a:b]
    audio = nightcore(seg, sr)

    cover = find_cover(path, info, folder)
    if cover is None:
        print("   no cover art found — using a generated gradient")
        g = np.zeros((600, 600, 3), np.uint8)
        g[..., 0] = np.linspace(120, 40, 600)[:, None]
        g[..., 2] = np.linspace(200, 90, 600)[None, :]
        g[..., 1] = 50
        cover = Image.fromarray(g)
    pal = palette_from_cover(cover)

    render_video(audio, sr, cover, info, pal, out_mp4, fps=fps)

    artist_tag = re.sub(r"[^A-Za-z0-9]", "", artist.lower()) or "music"
    fmt = dict(title=title, artist=artist, artist_tag=artist_tag)
    extra = info.get("tags") or []
    if isinstance(extra, str):
        extra = [x.strip() for x in extra.split(",") if x.strip()]
    yt_tags = info.get("yt_tags") or []
    if isinstance(yt_tags, str):
        yt_tags = [x.strip() for x in re.split(r"[,#]", yt_tags) if x.strip()]
    meta = {
        "file": f"{slug}.mp4",
        "title": (info.get("yt_title") or TITLE_TEMPLATE.format(**fmt))[:100],
        "description": info.get("yt_description") or DESC_TEMPLATE.format(**fmt),
        "tags": list(dict.fromkeys(list(yt_tags) + [title, artist, f"{title} sped up", f"{artist} sped up"]
                                   + BASE_TAGS + list(extra)))[:30],
        "plays": info.get("plays", 0),
        "hook_start": round(start, 2),
        "palette": ["#%02x%02x%02x" % c for c in pal["blobs"]],
    }
    (out_dir / f"{slug}.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"   ✓ {out_mp4}")
    return out_mp4


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--out", default="out")
    ap.add_argument("--only", help="render only files whose name contains this")
    ap.add_argument("--fps", type=int, default=30)
    args = ap.parse_args()
    folder = Path(args.folder)
    out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)
    index_sidecars(folder)
    songs = sorted(p for p in folder.rglob("*") if p.suffix.lower() in AUDIO_EXT)
    if args.only:
        songs = [p for p in songs if args.only.lower() in p.name.lower()]
    metas = {}
    infos = {}
    for p in songs:
        fm = metas.setdefault(p.parent, folder_metadata(p.parent))
        infos[p] = gather_info(p, p.parent, fm, folder)
    # most-played tracks first, so the queue leads with the strongest songs
    songs.sort(key=lambda p: -int(infos[p].get("plays") or 0))
    print(f"{len(songs)} song(s) found")
    ok = 0
    for p in songs:
        try:
            process(p, p.parent, out_dir, metas[p.parent], args.fps, folder, infos[p])
            ok += 1
        except Exception as e:
            print(f"   ✗ {p.name}: {e}")
    print(f"done: {ok}/{len(songs)}")


if __name__ == "__main__":
    main()
