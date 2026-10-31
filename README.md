# QuickSong uploader

Posts one QuickSong Short to YouTube every 30 minutes (48 a day) from a queue stored in this repo.

```
your downloader ──► Claude renders (render.py) ──► queue (the "queue" branch)
                                                        │  every 30 min
                                                        ▼
                                   GitHub Actions ──► YouTube (QuickSong channel)
```

- **Queue** = the `queue` branch (switch branch at the top of the repo page). Each video is an
  `.mp4` + `.json` pair. Each upload deletes its pair, and every new batch rebuilds the branch as a
  single commit, so posted videos don't pile up in the repo's history.
- **Log** = `uploaded.csv` — every upload with its YouTube link.
- **Order** = most-played songs first inside each batch; new batches join the back of the line.

## One-time setup (≈15 minutes)

### 1. The QuickSong Google account
1. Create the separate Gmail and, signed in with it, create the **QuickSong** YouTube channel.

### 2. Google Cloud (signed in as the QuickSong Gmail)
1. Go to <https://console.cloud.google.com> → **New project** → name it `quicksong`.
2. **APIs & Services → Library** → enable **YouTube Data API v3**.
3. **APIs & Services → OAuth consent screen** → *External* → fill in the app name/email →
   add the scope `.../auth/youtube.upload` → add the QuickSong Gmail as a test user.
4. **Publish the app** (Publishing status → *In production*). Skip this and Google logs the
   uploader out every 7 days. You'll see an "unverified app" warning when you sign in — that's
   fine for your own channel; click *Advanced → Go to quicksong*.
5. **Credentials → Create credentials → OAuth client ID → Desktop app** → download the JSON
   (save it as `client_secret.json`, never commit it).

### 3. Get the YouTube login token (on your PC)
Put `client_secret.json` next to `tools/get_token.py` (or `QuickSong_Login.py`), open it in
Python IDLE and press **F5**. Sign in with the QuickSong Gmail and pick the QuickSong channel.
It prints three values and saves them to `QuickSong_GitHub_Secrets.txt`.

### 4. GitHub
1. Create a **private** repo called `quicksong-uploader` and put these files in it.
2. **Settings → Secrets and variables → Actions → New repository secret**, add:
   `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`.
3. **Settings → Actions → General → Workflow permissions** → *Read and write permissions*.
4. Test it: **Actions → QuickSong upload → Run workflow**.

### 5. Public vs private uploads
New Google Cloud projects that haven't passed YouTube's API audit can only upload **private**
videos. If your first test upload comes out private (or locked), apply for the audit:
<https://support.google.com/youtube/contact/yt_api_form>. Until then you can set the repo
variable `PRIVACY` to `private` or `unlisted` (Settings → Secrets and variables → Actions →
Variables) and publish manually.

## Controls (repo variables)
| Variable      | Default  | What it does                                |
|---------------|----------|---------------------------------------------|
| `PAUSED`      | —        | `true` stops uploads until you remove it    |
| `PRIVACY`     | `public` | `public`, `unlisted` or `private`           |
| `CATEGORY_ID` | `10`     | YouTube category (10 = Music)               |

## Adding videos
Claude does this for you each day. To do it yourself:
```
pip install -r tools/requirements.txt          # once (also needs ffmpeg)
python tools/render.py path\to\downloads --out out
python tools/enqueue.py out
```
`render.py` reads the downloader's `<audio>.metadata.json` files and the `covers/` folder,
picks the hook, makes it 1.3× nightcore with reverb, and renders a 1080×1920 30-second Short
coloured from the cover art. A video that YouTube rejects is removed from the queue and logged
as `FAILED` in `uploaded.csv`, so one bad file never blocks the rest. A video's `.json` can
include `"privacy": "private"` to override the default for just that upload.

## Good to know
- GitHub sometimes starts scheduled runs a few minutes late; uploads stay about 30 min apart.
- Each run takes under a minute, so 48 runs a day stays inside the free 2,000 Actions
  minutes a month for private repos.
- YouTube's default API quota allows about 100 uploads a day; 48 uses about half.
