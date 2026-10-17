"""
AI Reel Bot - version 2
Takes the script/keywords/caption from Make, then:
  1) makes a natural Hindi voiceover  (Gemini voice -> edge-tts Hindi -> gTTS Hindi)
  2) gets 6-12 different visuals, trying: stock video (Pixabay/Pexels) -> your 'clips' folder
     -> free AI images with slow zoom -> gradient (so it never fails)
  3) joins them with animated transitions
  4) adds a presenter (cut-outs of YOUR photos from the 'poses' folder) that pops in,
     changes pose/side every scene and moves with the voice
  5) adds animated on-screen text, optional background music (from 'music' folder)
  6) uploads the video to Cloudinary and sends the link + caption to Make
"""
import asyncio
import base64
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
import wave
from datetime import datetime

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

W, H = 1080, 1920
FPS = 30
PRES_H = 1250          # height of the strip at the bottom where the presenter lives
XFADE_T = 0.4          # transition length in seconds
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
WORK = "work"
OUTPUT = "reel.mp4"
EDGE_VOICE = os.environ.get("EDGE_VOICE", "hi-IN-MadhurNeural")   # Hindi male. Female: hi-IN-SwaraNeural
AI_OK = True           # switched off automatically if the AI image service fails
SOURCES = set()        # which stock sites were used (for the caption credit)
DISABLED = set()       # services that failed this run - we stop asking them

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AIReelBot/2.0)"}
EMOJI = re.compile("[\U00010000-\U0010FFFF\u2600-\u27BF\uFE0F]")
TRANSITIONS = ["fade", "slideleft", "circleopen", "wipeup", "zoomin",
               "slideright", "smoothleft", "fadeblack"]
POSE_KEYS = ["smiling", "explaining", "sad", "standing"]


# ---------- helpers ----------
def run(cmd):
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        print("FFmpeg problem:\n" + (e.stderr or b"").decode(errors="ignore")[-1500:])
        raise


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


def ease(x):
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3


# ---------- 1) voiceover ----------
def gemini_tts(text, out_wav):
    """Natural Hindi voice from Gemini (free key from Google AI Studio). Returns True if made."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return False
    model = os.environ.get("GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview")
    voice = os.environ.get("GEMINI_VOICE", "Charon")
    prompt = ("Speak in natural, warm, energetic Hindi, like a popular Indian tech YouTuber "
              "talking to friends. Say English tech words like AI clearly in English.\n\n" + text)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }
    r = None
    for attempt in range(3):
        r = requests.post(url, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                          json=body, timeout=180)
        if r.status_code == 200:
            break
        if r.status_code in (429, 500, 503) and attempt < 2:
            time.sleep(20 * (attempt + 1))
            continue
        raise RuntimeError(f"Gemini voice error {r.status_code}: {r.text[:200]}")
    parts = r.json()["candidates"][0]["content"]["parts"]
    inline = next((p["inlineData"] for p in parts if "inlineData" in p), None)
    if not inline:
        raise RuntimeError("Gemini returned no audio")
    m = re.search(r"rate=(\d+)", inline.get("mimeType", ""))
    rate = int(m.group(1)) if m else 24000
    with wave.open(out_wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(base64.b64decode(inline["data"]))
    return True


async def _edge(text, out):
    import edge_tts
    await edge_tts.Communicate(text, EDGE_VOICE, rate="+0%").save(out)


def make_voice(text):
    wav = f"{WORK}/voice.wav"
    try:
        if gemini_tts(text, wav):
            print("Voice made with Gemini")
            return wav
    except Exception as e:
        print(f"Gemini voice failed ({e}). Trying edge-tts.")
    mp3 = f"{WORK}/voice.mp3"
    try:
        asyncio.run(_edge(text, mp3))
        if os.path.getsize(mp3) < 1000:
            raise RuntimeError("audio file is empty")
        print(f"Voice made with edge-tts ({EDGE_VOICE})")
        return mp3
    except Exception as e:
        print(f"edge-tts failed ({e}). Falling back to gTTS.")
    from gtts import gTTS
    gTTS(text, lang="hi").save(mp3)
    print("Voice made with gTTS")
    return mp3


def audio_seconds(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        check=True, capture_output=True, text=True).stdout.strip()
    return float(out)


def audio_envelope(path, total):
    """Loudness (0..1) for every video frame - the presenter moves with this."""
    tmp = f"{WORK}/env.wav"
    run(["ffmpeg", "-y", "-i", path, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", tmp])
    with wave.open(tmp) as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    hop = 16000 // FPS
    frames = int(total * FPS) + 2
    env = np.zeros(frames, dtype=np.float32)
    for i in range(frames):
        seg = data[i * hop:(i + 1) * hop]
        if len(seg):
            env[i] = float(np.sqrt(np.mean(seg ** 2)))
    nz = env[env > 0]
    peak = float(np.percentile(nz, 95)) if len(nz) else 1.0
    env = np.clip(env / max(peak, 1e-6), 0, 1)
    return np.convolve(env, np.ones(5) / 5, mode="same")


# ---------- 2) visuals ----------
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
        files = [f for f in video.get("video_files", [])
                 if f.get("file_type") == "video/mp4" and f.get("width") and f.get("height")
                 and f["height"] > f["width"]]
        if not files:
            continue
        files.sort(key=lambda f: abs(f["width"] - 1080))
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
    exts = (".mp4", ".mov", ".webm", ".m4v")
    files = [f for f in glob.glob("clips/*") if f.lower().endswith(exts)]
    if not files:
        return None
    fresh = [f for f in files if f not in used_ids] or files
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
              "no text, no logo, no watermark, no people")
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
            ((60, 20, 90), (200, 60, 110)), ((15, 25, 45), (45, 90, 200)),
            ((20, 70, 60), (90, 160, 120)), ((70, 30, 30), (200, 110, 60))]


def gradient_image(index):
    top, bottom = PALETTES[index % len(PALETTES)]
    t = np.linspace(0, 1, H, dtype=np.float32)[:, None, None]
    arr = (np.array(top, dtype=np.float32) * (1 - t) + np.array(bottom, dtype=np.float32) * t)
    arr = np.repeat(arr, W, axis=1).astype(np.uint8)
    path = f"{WORK}/raw_{index}.png"
    Image.fromarray(arr, "RGB").save(path)
    print(f"Scene {index + 1}: gradient background")
    return path


def get_visual(keyword, used_ids, index):
    """Returns (file_path, 'video' or 'image')."""
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
    run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", src, "-t", f"{seconds:.2f}",
         "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1",
         "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", out])


def make_image_scene(src, seconds, out, index):
    """Still image -> video with a slow zoom (Ken Burns effect)."""
    frames = int(seconds * FPS) + 1
    if index % 2 == 0:
        zoom = "min(zoom+0.0007,1.2)"
    else:
        zoom = "if(eq(on,1),1.2,max(zoom-0.0007,1.0))"
    vf = (f"scale=1620:2880:force_original_aspect_ratio=increase,crop=1620:2880,"
          f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
          f"d={frames}:s={W}x{H}:fps={FPS},setsar=1,format=yuv420p")
    run(["ffmpeg", "-y", "-i", src, "-t", f"{seconds:.2f}", "-vf", vf, "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", out])


def xfade_concat(files, length, t, out):
    """Join scene clips with animated transitions (fade, slide, circle, wipe, zoom...)."""
    cmd = ["ffmpeg", "-y"]
    for f in files:
        cmd += ["-i", f]
    chain, prev = [], "0:v"
    for k in range(1, len(files)):
        tr = TRANSITIONS[(k - 1) % len(TRANSITIONS)]
        offset = k * (length - t)
        chain.append(f"[{prev}][{k}:v]xfade=transition={tr}:duration={t}:offset={offset:.3f}[x{k}]")
        prev = f"x{k}"
    cmd += ["-filter_complex", ";".join(chain), "-map", f"[{prev}]", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", out]
    run(cmd)


# ---------- 3) presenter (your photos) ----------
def find_pose_files():
    files = {}
    for f in sorted(glob.glob("poses/*")):
        if not f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        name = os.path.basename(f).lower()
        for k in POSE_KEYS:
            if k in name and k not in files:
                files[k] = f
    return files


def largest_part(rgba):
    """Keep only the main person (removes other people / stray bits in the background)."""
    import cv2
    arr = np.array(rgba)
    a = arr[:, :, 3]
    mask = (a > 128).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n <= 2:
        return rgba
    biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    keep = cv2.dilate((lab == biggest).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
    arr[:, :, 3] = np.where(keep, a, 0)
    return Image.fromarray(arr, "RGBA")


def rounded_card(img, width=900):
    """Backup look if background removal is not available: photo in a rounded frame."""
    img = img.copy()
    scale = width / img.width
    img = img.resize((width, int(img.height * scale)), Image.LANCZOS)
    if img.height > 1150:
        img = img.crop((0, 0, width, 1150))
    pad = 10
    card = Image.new("RGBA", (img.width + 2 * pad, img.height + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle([(0, 0), (card.width - 1, card.height - 1)],
                                           radius=60, fill=(255, 255, 255, 255))
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([(0, 0), (img.width - 1, img.height - 1)], radius=52, fill=255)
    card.paste(img, (pad, pad), mask)
    return card


def build_presenter_image(path, session):
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    img.thumbnail((1400, 1400))
    try:
        if session is None:
            raise RuntimeError("background remover not installed")
        from rembg import remove
        cut = remove(img, session=session).convert("RGBA")
        cut = largest_part(cut)
        coverage = float((np.array(cut)[:, :, 3] > 128).mean())
        if coverage < 0.08 or coverage > 0.97:
            raise RuntimeError(f"cut-out looks wrong (covers {coverage:.0%})")
        cut = cut.crop(cut.getbbox())
        w, h = cut.size
        if h / w > 1.5:                       # full-body photo -> keep waist-up
            cut = cut.crop((0, 0, w, int(h * 0.58)))
        cut.putalpha(cut.split()[3].filter(ImageFilter.GaussianBlur(1.2)))
    except Exception as e:
        print(f"  cut-out not used for {os.path.basename(path)} ({e}) - using framed photo")
        cut = rounded_card(img)
    w, h = cut.size
    s = min(1100 / h, 840 / w)            # same presenter size for every photo
    return cut.resize((max(2, int(w * s)), max(2, int(h * s))), Image.LANCZOS)


def load_presenters():
    files = find_pose_files()
    if not files:
        print("No photos in 'poses' folder - making the reel without a presenter")
        return {}
    session = None
    try:
        from rembg import new_session
        session = new_session(os.environ.get("REMBG_MODEL", "u2net_human_seg"))
    except Exception as e:
        print(f"Background remover unavailable ({e})")
    poses = {}
    for k, f in files.items():
        poses[k] = build_presenter_image(f, session)
        os.makedirs("preview", exist_ok=True)           # so you can check the cut-outs
        bg = Image.new("RGBA", poses[k].size, (40, 40, 40, 255))
        bg.alpha_composite(poses[k])
        bg.convert("RGB").save(f"preview/presenter_{k}.jpg", quality=90)
        print(f"Presenter ready: {k}")
    return poses


def pose_plan(n, mood, poses):
    avail = list(poses)
    def pick(name):
        return name if name in poses else avail[abs(hash(name)) % len(avail)]
    mood = mood if mood in poses else "explaining"
    cycle = ["explaining", "standing", mood, "explaining"]
    plan = []
    for i in range(n):
        plan.append("smiling" if i in (0, n - 1) else cycle[(i - 1) % len(cycle)])
    return [pick(p) for p in plan]


SIDES = [0, -150, 150, 0, 150, -150]


def presenter_frame(f, poses, plan, per, env):
    t = f / FPS
    n = len(plan)
    s = min(int(t / per), n - 1)
    local = t - s * per
    e = float(env[min(f, len(env) - 1)])
    dx_cur = SIDES[s % len(SIDES)]
    dx_prev = SIDES[(s - 1) % len(SIDES)] if s > 0 else dx_cur

    def draw(key, dx, pop_t):
        base = poses[key]
        sc = (0.90 + 0.10 * ease(pop_t / 0.30)) * (1 + 0.025 * e)
        w, h = max(2, int(base.width * sc)), max(2, int(base.height * sc))
        im = base.resize((w, h), Image.BILINEAR)
        canvas = Image.new("RGBA", (W, PRES_H), (0, 0, 0, 0))
        canvas.paste(im, (int((W - w) / 2 + dx), int(PRES_H - h + 24 - 14 * e)), im)
        return canvas

    dx = dx_prev + (dx_cur - dx_prev) * ease(local / 0.35)
    cur = draw(plan[s], dx, local)
    if s > 0 and local < 0.25 and plan[s - 1] != plan[s]:
        prev = draw(plan[s - 1], dx_prev, per)
        cur = Image.blend(prev, cur, ease(local / 0.25))
    return cur


# ---------- 4) text, background shading, final video ----------
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
    y0 = 520 - block_h // 2                  # upper-middle: above the presenter
    max_w = max(d.textlength(l, font=font) for l in lines)
    pad = 32
    d.rounded_rectangle(
        [(W - max_w) / 2 - pad, y0 - pad, (W + max_w) / 2 + pad, y0 + block_h + pad - 18],
        radius=32, fill=(0, 0, 0, 175))
    for i, line in enumerate(lines):
        w = d.textlength(line, font=font)
        d.text(((W - w) / 2, y0 + i * line_h), line, font=font, fill=(255, 255, 255, 255))
    img.save(path)


def make_vignette(path):
    y = np.linspace(0, 1, H, dtype=np.float32)
    alpha = np.clip((y - 0.30) / 0.70, 0, 1) ** 1.5 * 190
    arr = np.zeros((H, W, 4), dtype=np.uint8)
    arr[:, :, 3] = alpha[:, None].astype(np.uint8)
    Image.fromarray(arr, "RGBA").save(path)


def text_windows(n, total):
    start, end = 0.5, total - 1.0
    seg = (end - start) / n
    return [(start + i * seg, start + (i + 1) * seg - 0.15) for i in range(n)]


def compose_final(base, voice, total, texts, poses, plan, per, env, music):
    vignette = f"{WORK}/vignette.png"
    make_vignette(vignette)
    loop = ["-loop", "1", "-framerate", str(FPS), "-t", f"{total:.2f}"]
    cmd = ["ffmpeg", "-y", "-i", base, *loop, "-i", vignette]
    idx = 2
    pres_idx = None
    if poses:
        cmd += ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{PRES_H}",
                "-framerate", str(FPS), "-i", "pipe:0"]
        pres_idx, idx = idx, idx + 1
    text_idx = []
    for i, t in enumerate(texts):
        png = f"{WORK}/text_{i}.png"
        make_text_png(t, png)
        cmd += [*loop, "-i", png]
        text_idx.append(idx)
        idx += 1
    cmd += ["-i", voice]
    a_idx, idx = idx, idx + 1
    if music:
        cmd += ["-stream_loop", "-1", "-i", music]
        m_idx, idx = idx, idx + 1

    fc = ["[0:v][1:v]overlay=0:0[v0]"]
    prev = "v0"
    if pres_idx is not None:
        fc.append(f"[{prev}][{pres_idx}:v]overlay=0:{H - PRES_H}[vp]")
        prev = "vp"
    for i, (ti, (a, b)) in enumerate(zip(text_idx, text_windows(len(texts), total))):
        fc.append(f"[{ti}:v]format=rgba,fade=t=in:st={a:.2f}:d=0.3:alpha=1,"
                  f"fade=t=out:st={b - 0.3:.2f}:d=0.3:alpha=1[t{i}]")
        fc.append(f"[{prev}][t{i}]overlay=x=0:y='if(lt(t,{a:.2f}),0,90*pow(1-clip((t-{a:.2f})/0.35,0,1),2))'"
                  f":enable='between(t,{a:.2f},{b:.2f})'[vt{i}]")
        prev = f"vt{i}"
    if music:
        fc.append(f"[{a_idx}:a]apad[va];[{m_idx}:a]volume=0.12[ma];"
                  f"[va][ma]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
    else:
        fc.append(f"[{a_idx}:a]apad[aout]")

    cmd += ["-filter_complex", ";".join(fc), "-map", f"[{prev}]", "-map", "[aout]",
            "-t", f"{total:.2f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", OUTPUT]

    if not poses:
        run(cmd)
        return
    log = open(f"{WORK}/final_ffmpeg.log", "wb")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log)
    frames = int(total * FPS) + 1
    try:
        for f in range(frames):
            proc.stdin.write(presenter_frame(f, poses, plan, per, env).tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    code = proc.wait()
    log.close()
    if code != 0:
        print(open(f"{WORK}/final_ffmpeg.log", errors="ignore").read()[-2000:])
        raise RuntimeError("final video step failed")


# ---------- main ----------
def main():
    os.makedirs(WORK, exist_ok=True)
    p = load_payload()

    script = clean_for_voice(p.get("voiceover_script") or p.get("script") or "")
    if not script:
        sys.exit("ERROR: no voiceover_script was received")
    texts = [strip_emoji(t) for t in as_list(p.get("on_screen_text"), r"\s*\|\s*|\n")][:6]
    keywords = as_list(p.get("scene_keywords"))[:10] or ["artificial intelligence technology"]
    mood = str(p.get("avatar_pose") or "").strip().lower()
    hashtags = [t if t.startswith("#") else "#" + t for t in as_list(p.get("hashtags"), r"[,\s]+")]

    voice = make_voice(script)
    total = audio_seconds(voice) + 0.8
    print(f"Reel length: {total:.1f} seconds")
    env = audio_envelope(voice, total)

    n = max(6, min(12, round(total / 4.5)))
    per = total / n
    length = per + XFADE_T
    print(f"Scenes: {n} (about {per:.1f}s each)")
    used, scene_files = set(), []
    for i in range(n):
        raw, kind = get_visual(keywords[i % len(keywords)], used, i)
        scene = f"{WORK}/scene_{i}.mp4"
        if kind == "video":
            make_scene(raw, length, scene)
        else:
            make_image_scene(raw, length, scene, i)
        scene_files.append(scene)
    base = f"{WORK}/base.mp4"
    xfade_concat(scene_files, length, XFADE_T, base)

    poses = load_presenters()
    plan = pose_plan(n, mood, poses) if poses else []
    music_files = [f for f in glob.glob("music/*") if f.lower().endswith((".mp3", ".wav", ".m4a"))]
    music = random.choice(music_files) if music_files else None
    compose_final(base, voice, total, texts, poses, plan, per, env, music)
    print(f"Video ready: {OUTPUT} ({os.path.getsize(OUTPUT) / 1e6:.1f} MB)")

    caption = (p.get("caption") or "").strip()
    credit_line = f"\n\nStock footage: {' & '.join(sorted(SOURCES))}" if SOURCES else ""
    final_caption = f"{caption}\n\n{' '.join(hashtags)}{credit_line}".strip()

    video_url = None
    keys = ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET")
    if all(os.environ.get(k) for k in keys):
        import cloudinary
        import cloudinary.uploader
        cloudinary.config(cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"],
                          api_key=os.environ["CLOUDINARY_API_KEY"],
                          api_secret=os.environ["CLOUDINARY_API_SECRET"], secure=True)
        res = cloudinary.uploader.upload(
            OUTPUT, resource_type="video", folder="ai_reels",
            public_id="reel_" + datetime.utcnow().strftime("%Y%m%d_%H%M%S"))
        video_url = res["secure_url"]
        print("Uploaded to Cloudinary:", video_url)
    else:
        print("Cloudinary keys not set - skipping upload (test mode)")

    hook = os.environ.get("MAKE_WEBHOOK_URL", "").strip()
    if hook and video_url:
        try:
            r = requests.post(hook, json={"video_url": video_url, "caption": final_caption,
                                          "source_name": p.get("source_name", ""),
                                          "source_link": p.get("link", "")}, timeout=30)
            print("Sent to Make, status:", r.status_code)
        except Exception as e:
            print("Could not reach Make:", e)
    else:
        print("Make webhook not set or no video link - skipping")


if __name__ == "__main__":
    main()
