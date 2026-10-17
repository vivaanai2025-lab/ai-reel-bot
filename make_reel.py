"""
AI Reel Bot
Takes the script/keywords/caption from Make, then:
  1) makes a voiceover (free, edge-tts)
  2) gets visuals, trying in order: stock video (Pixabay/Pexels) -> your own 'clips' folder
     -> free AI images with slow zoom -> gradient background (so it never fails)
  3) stitches them to 1080x1920, adds on-screen text + voice (FFmpeg)
  4) uploads the video to Cloudinary (if keys are set)
  5) sends the video link + final caption back to Make (if webhook is set)
"""
import asyncio
import glob
import io
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime

import requests
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
VOICE = "en-IN-PrabhatNeural"   # Indian English male. Female option: en-IN-NeerjaNeural
WORK = "work"
OUTPUT = "reel.mp4"
AI_OK = True       # switched off automatically if the AI image service fails
SOURCES = set()   # which stock sites were used (for the caption credit)
DISABLED = set()  # stock sites that failed this run - we stop asking them

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AIReelBot/1.0)"}
EMOJI = re.compile("[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0F]")


# ---------- helpers ----------
def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def http_get(url, headers=None, **kw):
    """GET that waits and retries if a site says 'rate limit' (HTTP 429)."""
    h = {**HEADERS, **(headers or {})}
    r = None
    for attempt in range(3):
        r = requests.get(url, headers=h, **kw)
        if r.status_code == 429 and attempt < 2:
            wait = 15 * (attempt + 1)
            print(f"Rate limit from {url.split('/')[2]} - waiting {wait}s, then retrying")
            r.close()
            time.sleep(wait)
            continue
        break
    return r


def strip_emoji(text):
    return EMOJI.sub("", text).strip()


def clean_for_voice(text):
    text = strip_emoji(text)
    text = re.sub(r"[#*_<>\[\]{}|]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def as_list(value, pattern=r"[,\n]"):
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [x.strip() for x in re.split(pattern, value) if x.strip()]
    return []


def load_payload():
    raw = os.environ.get("PAYLOAD", "").strip()
    data = None
    if raw and raw != "null":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
    if not data:
        print("No data from Make - using sample_payload.json (test mode)")
        with open("sample_payload.json", encoding="utf-8") as f:
            data = json.load(f)
    return data


# ---------- 1) voiceover ----------
async def _edge(text, out):
    import edge_tts
    await edge_tts.Communicate(text, VOICE, rate="+5%").save(out)


def make_voice(text, out):
    try:
        asyncio.run(_edge(text, out))
        if os.path.getsize(out) < 1000:
            raise RuntimeError("audio file is empty")
        print("Voice made with edge-tts")
    except Exception as e:
        print(f"edge-tts failed ({e}). Falling back to gTTS.")
        from gtts import gTTS
        gTTS(text, lang="en", tld="co.in").save(out)
        print("Voice made with gTTS")


def audio_seconds(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


# ---------- 2) stock videos ----------
def find_pexels_clip(query, used_ids):
    r = http_get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": os.environ["PEXELS_API_KEY"]},
        params={"query": query, "orientation": "portrait", "per_page": 8},
        timeout=30,
    )
    r.raise_for_status()
    for video in r.json().get("videos", []):
        if video["id"] in used_ids:
            continue
        files = [
            f for f in video.get("video_files", [])
            if f.get("file_type") == "video/mp4"
            and f.get("width") and f.get("height") and f["height"] > f["width"]
        ]
        if not files:
            continue
        files.sort(key=lambda f: abs(f["width"] - 1080))  # closest to 1080 wide
        used_ids.add(video["id"])
        return files[0]["link"]
    return None


def find_pixabay_clip(query, used_ids):
    r = http_get(
        "https://pixabay.com/api/videos/",
        params={"key": os.environ["PIXABAY_API_KEY"], "q": query[:100],
                "per_page": 10, "safesearch": "true"},
        timeout=30,
    )
    r.raise_for_status()
    for hit in r.json().get("hits", []):
        uid = f"pixabay-{hit['id']}"
        if uid in used_ids:
            continue
        variants = [v for v in hit.get("videos", {}).values()
                    if v.get("url") and v.get("width") and v.get("height")]
        if not variants:
            continue
        # pick the size closest to Full HD (avoids huge 4K files)
        variants.sort(key=lambda v: abs(max(v["width"], v["height"]) - 1920))
        used_ids.add(uid)
        return variants[0]["url"]
    return None


def find_clip(query, used_ids):
    finders = []
    if os.environ.get("PIXABAY_API_KEY"):
        finders.append(("Pixabay", find_pixabay_clip))
    if os.environ.get("PEXELS_API_KEY"):
        finders.append(("Pexels", find_pexels_clip))
    if not finders and "nokey" not in DISABLED:
        print("No stock-video key set - using 'clips' folder, AI images or gradients instead")
        DISABLED.add("nokey")
    for name, finder in finders:
        if name in DISABLED:
            continue
        try:
            link = finder(query, used_ids)
        except Exception as e:
            print(f"{name} problem: {e} - skipping {name} for the rest of this run")
            DISABLED.add(name)
            continue
        if link:
            SOURCES.add(name)
            return link
    return None


def download(url, path):
    r = http_get(url, stream=True, timeout=120)
    r.raise_for_status()
    with open(path, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)


def find_local_clip(keyword, used_ids):
    """Pick a clip from the repo's 'clips' folder. Best filename match wins."""
    exts = (".mp4", ".mov", ".webm", ".m4v")
    files = [f for f in glob.glob("clips/*") if f.lower().endswith(exts)]
    if not files:
        return None
    fresh = [f for f in files if f not in used_ids] or files   # reuse only if we must
    words = set(re.findall(r"[a-z]+", keyword.lower()))

    def score(f):
        return len(words & set(re.findall(r"[a-z]+", os.path.basename(f).lower())))

    best = max(score(f) for f in fresh)
    choice = random.choice([f for f in fresh if score(f) == best])
    used_ids.add(choice)
    return choice


def ai_image(keyword, index):
    """Free AI image (Pollinations, no key needed). Returns a file path or None."""
    global AI_OK
    if not AI_OK:
        return None
    prompt = (f"{keyword}, cinematic vertical photo, modern technology theme, "
              "no text, no logo, no watermark")
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt)
           + f"?width={W}&height={H}&nologo=true&seed={random.randint(1, 99999)}")
    for attempt in range(2):
        try:
            r = requests.get(url, timeout=90)
            if r.status_code == 200 and len(r.content) > 20000:
                img = Image.open(io.BytesIO(r.content)).convert("RGB")
                path = f"{WORK}/raw_{index}.jpg"
                img.save(path, quality=92)
                print(f"Scene {index + 1}: AI image for '{keyword}'")
                return path
            print(f"AI image attempt {attempt + 1} failed (status {r.status_code})")
        except Exception as e:
            print(f"AI image attempt {attempt + 1} failed ({e})")
    AI_OK = False
    print("AI images not available. Using gradient backgrounds from now on.")
    return None


PALETTES = [((15, 32, 80), (120, 40, 160)), ((10, 60, 90), (20, 150, 160)),
            ((60, 20, 90), (200, 60, 110)), ((15, 25, 45), (45, 90, 200))]


def gradient_image(index):
    """Last resort: a clean gradient background. This can never fail."""
    top, bottom = PALETTES[index % len(PALETTES)]
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        d.line([(0, y), (W, y)],
               fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    path = f"{WORK}/raw_{index}.png"
    img.save(path)
    print(f"Scene {index + 1}: gradient background")
    return path


def get_visual(keyword, used_ids, index):
    """Returns (file_path, 'video' or 'image').
    Order: stock video (Pixabay/Pexels) -> your 'clips' folder -> AI image -> gradient."""
    for query in (keyword, "artificial intelligence technology"):
        link = find_clip(query, used_ids)
        if link:
            path = f"{WORK}/raw_{index}.mp4"
            try:
                download(link, path)
                print(f"Scene {index + 1}: stock video for '{query}'")
                return path, "video"
            except Exception as e:
                print(f"Download failed ({e}) - trying the next option")
    local = find_local_clip(keyword, used_ids)
    if local:
        print(f"Scene {index + 1}: clip from 'clips' folder ({os.path.basename(local)})")
        return local, "video"
    path = ai_image(keyword, index)
    if path:
        return path, "image"
    return gradient_image(index), "image"


def make_scene(src, seconds, out):
    run([
        "ffmpeg", "-y", "-stream_loop", "-1", "-i", src, "-t", f"{seconds:.2f}",
        "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,"
               f"crop={W}:{H},fps=30,setsar=1",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", out,
    ])


def make_image_scene(src, seconds, out, index):
    """Turns a still image into a video with a slow zoom (Ken Burns effect)."""
    frames = int(seconds * 30) + 1
    if index % 2 == 0:
        zoom = "min(zoom+0.0007,1.2)"                        # slow zoom in
    else:
        zoom = "if(eq(on,1),1.2,max(zoom-0.0007,1.0))"       # slow zoom out
    vf = (f"scale=1620:2880:force_original_aspect_ratio=increase,crop=1620:2880,"
          f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
          f"d={frames}:s={W}x{H}:fps=30,setsar=1,format=yuv420p")
    run(["ffmpeg", "-y", "-i", src, "-t", f"{seconds:.2f}", "-vf", vf,
         "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-pix_fmt", "yuv420p", out])


# ---------- 3) on-screen text ----------
def make_text_png(text, path):
    font = ImageFont.truetype(FONT, 76)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    lines, current = [], ""
    for word in text.split():
        test = (current + " " + word).strip()
        if d.textlength(test, font=font) <= W - 220:
            current = test
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)

    line_h = 96
    block_h = line_h * len(lines)
    y0 = 1150 - block_h // 2          # lower-middle, clear of Instagram buttons
    max_w = max(d.textlength(l, font=font) for l in lines)
    pad = 32
    d.rounded_rectangle(
        [(W - max_w) / 2 - pad, y0 - pad, (W + max_w) / 2 + pad, y0 + block_h + pad - 18],
        radius=32, fill=(0, 0, 0, 170),
    )
    for i, line in enumerate(lines):
        w = d.textlength(line, font=font)
        d.text(((W - w) / 2, y0 + i * line_h), line, font=font, fill=(255, 255, 255, 255))
    img.save(path)


def text_windows(n, total):
    shares = [0.10, 0.30, 0.33, 0.27] if n == 4 else [1 / n] * n
    windows, start = [], 0.0
    for s in shares:
        end = start + s * total
        windows.append((start, end))
        start = end
    return windows


# ---------- main ----------
def main():
    os.makedirs(WORK, exist_ok=True)
    p = load_payload()

    script = clean_for_voice(p.get("voiceover_script") or p.get("script") or "")
    if not script:
        sys.exit("ERROR: no voiceover_script was received")
    texts = [strip_emoji(t) for t in as_list(p.get("on_screen_text"), r"\s*\|\s*|\n")][:5]
    keywords = as_list(p.get("scene_keywords"))[:6] or ["artificial intelligence technology"]
    hashtags = [t if t.startswith("#") else "#" + t
                for t in as_list(p.get("hashtags"), r"[,\s]+")]

    # voice
    voice_path = f"{WORK}/voice.mp3"
    make_voice(script, voice_path)
    total = audio_seconds(voice_path) + 0.6
    print(f"Reel length: {total:.1f} seconds")

    # scenes
    used, scene_files = set(), []
    per_scene = total / len(keywords)
    for i, kw in enumerate(keywords):
        raw, kind = get_visual(kw, used, i)
        scene = f"{WORK}/scene_{i}.mp4"
        if kind == "video":
            make_scene(raw, per_scene, scene)
        else:
            make_image_scene(raw, per_scene, scene, i)
        scene_files.append(scene)

    with open(f"{WORK}/list.txt", "w") as f:
        for s in scene_files:
            f.write(f"file '{os.path.basename(s)}'\n")
    base = f"{WORK}/base.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", f"{WORK}/list.txt",
         "-c", "copy", base])

    # text overlays + voice
    inputs = ["-i", base]
    filters, prev = [], "0:v"
    if texts:
        for i, (text, (a, b)) in enumerate(zip(texts, text_windows(len(texts), total))):
            png = f"{WORK}/text_{i}.png"
            make_text_png(text, png)
            inputs += ["-i", png]
            out = f"v{i + 1}"
            filters.append(
                f"[{prev}][{i + 1}:v]overlay=0:0:enable='between(t,{a:.2f},{b:.2f})'[{out}]"
            )
            prev = out
    audio_index = len(texts) + 1
    cmd = ["ffmpeg", "-y", *inputs, "-i", voice_path]
    if filters:
        cmd += ["-filter_complex", ";".join(filters), "-map", f"[{prev}]"]
    else:
        cmd += ["-map", "0:v"]
    cmd += ["-map", f"{audio_index}:a", "-af", "apad", "-t", f"{total:.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", OUTPUT]
    run(cmd)
    print(f"Video ready: {OUTPUT} ({os.path.getsize(OUTPUT) / 1e6:.1f} MB)")

    # final caption for Instagram
    caption = (p.get("caption") or "").strip()
    credit_line = f"\n\nStock footage: {' & '.join(sorted(SOURCES))}" if SOURCES else ""
    final_caption = f"{caption}\n\n{' '.join(hashtags)}{credit_line}".strip()

    # upload
    video_url = None
    keys = ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET")
    if all(os.environ.get(k) for k in keys):
        import cloudinary
        import cloudinary.uploader
        cloudinary.config(
            cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"],
            api_key=os.environ["CLOUDINARY_API_KEY"],
            api_secret=os.environ["CLOUDINARY_API_SECRET"],
            secure=True,
        )
        res = cloudinary.uploader.upload(
            OUTPUT, resource_type="video", folder="ai_reels",
            public_id="reel_" + datetime.utcnow().strftime("%Y%m%d_%H%M%S"),
        )
        video_url = res["secure_url"]
        print("Uploaded to Cloudinary:", video_url)
    else:
        print("Cloudinary keys not set - skipping upload (test mode)")

    # tell Make
    hook = os.environ.get("MAKE_WEBHOOK_URL", "").strip()
    if hook and video_url:
        try:
            r = requests.post(hook, json={
                "video_url": video_url,
                "caption": final_caption,
                "source_name": p.get("source_name", ""),
                "source_link": p.get("link", ""),
            }, timeout=30)
            print("Sent to Make, status:", r.status_code)
        except Exception as e:
            print("Could not reach Make:", e)
    else:
        print("Make webhook not set or no video link - skipping")


if __name__ == "__main__":
    main()
