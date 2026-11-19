#!/usr/bin/env python3
"""
Telegram Bot  ->  YouTube (Unlisted) uploader   [Koyeb ke liye]

Bot ko ye bhejo:
  * Telegram link          https://t.me/channel/123  ya  https://t.me/c/123456/789
  * Google Drive link      https://drive.google.com/file/d/ID/view
  * Seedha video forward / upload
Optional title:            <link> | Mera Title

Environment variables (Koyeb me daalne hain):
  TG_API_ID, TG_API_HASH, TG_SESSION_STRING, BOT_TOKEN, OWNER_ID, YT_TOKEN_JSON
"""

import asyncio
import gc
import json
import mimetypes
import os
import random
import re
import signal
import unicodedata
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Windows console/log file cp1252 hoti hai, emoji ya special naam par print crash ho jaata hai
for _st in (sys.stdout, sys.stderr):
    try:
        _st.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from telethon import Button, TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl import types
from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    InputStickerSetEmpty,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaWebPage,
    PeerChannel,
)

# ---------------- CONFIG ----------------
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
# Ek ya zyada Telegram session string, comma se alag:  string1,string2
SESSION_STRINGS = [s for s in re.split(r"[,\s]+", os.environ["TG_SESSION_STRING"].strip()) if s]
BOT_TOKEN = os.environ["BOT_TOKEN"]
# Ek ya zyada user ID, comma se alag:  123456789,987654321
OWNER_IDS = {int(x) for x in re.split(r"[,\s]+", os.environ["OWNER_ID"].strip()) if x}
# PC par chalane ke liye: env variable na ho to bot ke folder wali yt_token.json file padh leta hai
APP_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
YT_TOKEN_JSON = os.environ.get("YT_TOKEN_JSON") or open(
    os.path.join(APP_DIR, "yt_token.json"), encoding="utf-8").read()

PORT = int(os.getenv("PORT", "8000"))
# Speed settings: env variable se badal sakte ho (phone/kam RAM par kam rakho, PC par badha sakte ho)
WORKERS = int(os.getenv("DL_WORKERS", "12"))             # ek saath kitne download requests
UP_CONNECTIONS = int(os.getenv("UP_CONNECTIONS", "4"))   # Telegram upload connections
UP_WORKERS = int(os.getenv("UP_WORKERS", "8"))           # har connection par ek saath kitne parts
START_TIME = time.time()
UPLOAD_CLIENTS = []                      # main() me bharta hai
CANCEL_EVT = threading.Event()           # cancel dabane par set hota hai
CURRENT = {"task": None, "batch": None, "compose": None}  # abhi chal raha kaam
STATE = {"ready": False, "last_ok": time.time()}   # health + watchdog
BG_TASKS = []                            # background tasks ka reference
CANCEL_BTN = [[Button.inline("❌ Cancel", b"cancel")]]
PRIVACY = "unlisted"
CATEGORY_ID = "22"
UPLOAD_CHUNK = int(os.getenv("YT_CHUNK_MB", "16")) * 1024 * 1024   # YouTube chunk (256KB ka multiple)
# Download size limit (GB). PC par disk jitni ho utni; env variable MAX_SIZE_GB se badlo
MAX_SIZE = int(float(os.getenv("MAX_SIZE_GB", "4")) * 1024 ** 3)
TG_LIMIT = 2000 * 1024 * 1024            # Telegram bot ek file max ~2GB hi bhej sakta hai
# Temporary download folder. Doosri drive (jaise D:) par rakhna ho to DOWNLOAD_DIR env variable set karo
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR") or os.path.join(tempfile.gettempdir(), "yt_uploader")
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
# ----------------------------------------

# t.me/c/ID/9-30   ya   t.me/c/ID/TOPIC/9-30
RANGE_RE = re.compile(r"t\.me/(c/\d+|[A-Za-z0-9_]+)/(?:(\d+)/)?(\d+)\s*[-–]\s*(\d+)")
MAX_RANGE = 500                          # ek baar me max kitne messages ki range

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", ".m4v", ".mpeg", ".mpg", ".3gp", ".ts"}

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Custom thumbnail aur word-replace rules (restart ke baad bhi bachne ke liye Saved Messages me save hote hain)
THUMB_TG = os.path.join(DOWNLOAD_DIR, "thumb_tg.jpg")   # Telegram ke liye chhota (320px, <200KB)
THUMB_YT = os.path.join(DOWNLOAD_DIR, "thumb_yt.jpg")   # YouTube ke liye bada (<2MB)
SETTINGS = {"rules": [], "thumb_msg_id": None, "bot_sessions": []}
SETTINGS_MSG = {"id": None}
SETTINGS_TAG = "#ytbot_settings"
THUMB_TAG = "#ytbot_thumb"


# ---------- /help ka text ----------
BOT_COMMANDS = [
    ("help", "Saare commands aur use karne ka tareeka"),
    ("status", "Bot live hai? Kya chal raha hai, queue kitni"),
    ("sessions", "Kaun se Telegram accounts se messages padhe ja rahe hain"),
    ("cancel", "Chal raha kaam rokna aur queue saaf karna"),
    ("setthumb", "Thumbnail set karna (photo ke saath)"),
    ("thumb", "Abhi ka thumbnail dekhna"),
    ("delthumb", "Thumbnail hatana"),
    ("replace", "Words badalna ya hatana"),
    ("replacelist", "Saare replace rules dekhna"),
    ("delreplace", "Ek replace rule hatana"),
    ("clearreplace", "Saare replace rules hatana"),
]

HELP_1 = """📖 BOT KAISE USE KAREIN (1/2)

━━ 1. UPLOAD KARNA ━━
Bot ko ye 3 me se kuch bhi bhejo:

• Telegram link
  https://t.me/c/123456/789
  (private channel ke liye aapka session wala account us channel me member hona chahiye)
  Link ya range me video, photo, voice, sticker, GIF, audio, file aur normal text message sab chalte hain. Sab apni original type me hi bheje jate hain (photo photo ki tarah, voice voice ki tarah).

• Google Drive link
  https://drive.google.com/file/d/ID/view
  (file "Anyone with the link" share honi chahiye)

• Seedha video ya file (PDF, ZIP wagairah) bot ko bhejo ya forward karo

Phir bot puchega kahan upload karna hai:
▶️ YouTube  |  📨 Telegram  |  ✅ Dono
YouTube par video Unlisted jaati hai. PDF jaisi non-video file YouTube par nahi jaati, wo sirf Telegram par jati hai.

━━ 2. TITLE DENA (optional) ━━
Link ke baad | laga kar title likho:
https://t.me/c/123456/789 | Biology Class 1

Ye YouTube title aur Telegram caption bane ga. Na do to original caption ya file ka naam use hota hai.

━━ 3. RANGE (kai files ek saath) ━━
Message number ki range do:
https://t.me/c/123456/9-30
(9 se 30 tak ke saare messages)

Topic wale group/channel me:
https://t.me/c/123456/5/9-30
(5 = topic ka number, 9-30 = range)

• Ek baar me max 500 messages
• Destination sirf ek baar puchega
• Pehle poori range ek baar me scan hoti hai. Jin messages me kuch bhejne layak nahi (service msg, doosre topic ka) wo turant skip ho jate hain
• Baaki ek ke baad ek chalte hain
• Ek hi status message hamesha edit hota rehta hai: abhi kaun si file (jaise 8/33), uski progress aur ✅ ❌ ⏭️ count
• End me wahi message summary ban jata hai (YouTube links alag message me aate hain)
• Range me title nahi lagta"""

HELP_2 = """📖 BOT KAISE USE KAREIN (2/2)

━━ 4. KAAM KO CONTROL KARNA ━━
/status
Bot live hai ya nahi, kitni der se chal raha hai, abhi kya ho raha hai (speed ke saath), queue me kitne kaam hain aur range kitni poori hui.

/cancel
Chal raha kaam rok deta hai aur queue ke saare kaam hata deta hai.

❌ Cancel button
Har status message ke neeche hota hai. Sirf chal raha kaam rokta hai. Range chal rahi ho to uski baaki files bhi skip ho jati hain.

━━ 5. THUMBNAIL ━━
/setthumb
Thumbnail set karne ke 2 tareeke:
  (a) Photo bhejo aur caption me /setthumb likho
  (b) Pehle se bheji photo par reply karke /setthumb likho
/thumb  → abhi ka thumbnail dekho
/delthumb  → wahi thumbnail hata do

Set hone ke baad har upload (Telegram aur YouTube dono) me lagta hai. Hatane par original thumbnail wapas lagta hai.
Note: YouTube par custom thumbnail ke liye channel verified hona chahiye (youtube.com/verify).

━━ 6. WORDS BADALNA / HATANA ━━
/replace purana => naya
Jaise:  /replace Biology => Bio

Word hatane ke liye naya khali chhodo:
/replace @download_it_bot =>

Ek saath kai rules (har line ek rule):
/replace [@download_it_bot 720p] =>
Class => Lecture

/replacelist  → saare rules dekho
/delreplace 2  → number se rule hatao
/delreplace Class  → word se rule hatao
/clearreplace  → saare rules hatao

Ye file ke naam, caption aur YouTube title par lagta hai. Capital/small ka farq nahi padta. Link ke saath jo title aap khud dete ho (link | Title) usme replace nahi lagta.

━━ 7. DHYAN RAKHNE WALI BAATEN ━━
• Thumbnail aur replace rules aapke Saved Messages me save rehte hain, restart ke baad bhi bache rehte hain. Wo messages delete mat karna.
• Bot restart ya redeploy hone par chalte kaam aur queue ruk jate hain. Bot "🟢 Bot online" ka message bhejta hai, tab dobara link bhejna padta hai.
• Roz YouTube par kuch hi videos ja sakte hain (API limit).
• Sirf OWNER_ID wale log hi bot use kar sakte hain.

Ye help dobara dekhne ke liye /help bhejo."""


# ---------- health check server (Koyeb web service ke liye zaroori) ----------
class Health(BaseHTTPRequestHandler):
    def _send(self, with_body):
        body = b"ok" if STATE["ready"] else b"starting"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if with_body:
            self.wfile.write(body)

    def do_GET(self):
        self._send(True)

    def do_HEAD(self):   # UptimeRobot jaise monitor HEAD bhejte hain
        self._send(False)

    def log_message(self, *a):
        pass


def start_health():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Health)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # Render apna URL RENDER_EXTERNAL_URL me deta hai: khud ko ping karte rehna (sleep se bachne ke liye)
    ext = os.getenv("RENDER_EXTERNAL_URL")
    if ext:
        def self_ping():
            import urllib.request

            while True:
                time.sleep(600)
                try:
                    urllib.request.urlopen(ext, timeout=20).read()
                except Exception:
                    pass

        threading.Thread(target=self_ping, daemon=True).start()


# ---------- helpers ----------
def human(n):
    for u in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


class Progress:
    def __init__(self, label):
        self.label = label
        self.text = f"{label}: shuru..."
        self.t, self.last = time.time(), 0

    def __call__(self, done, total):
        now = time.time()
        speed = (done - self.last) / max(now - self.t, 1e-6) if now - self.t >= 1 else None
        if speed is not None:
            self.t, self.last = now, done
            self._speed = speed
        pct = done / total * 100 if total else 0
        self.text = f"{self.label}: {pct:.0f}%  {human(done)}/{human(total)}  {human(getattr(self, '_speed', 0))}/s"


class Note:
    """Sirf text dikhane wala chhota progress (Drive download / text bhejte waqt)."""

    def __init__(self, text):
        self.text = text


async def edit_ref(bot, ref, text, buttons=None):
    """ref = (chat_id, message_id). Kabhi crash nahi karta. buttons=None => buttons hat jate hain."""
    if not ref:
        return False
    try:
        await bot.edit_message(ref[0], ref[1], text[:4000], buttons=buttons, parse_mode=None)
        return True
    except Exception as e:
        return "not modified" in str(e).lower()


async def status_updater(bot, sref, compose, stop):
    last = ""
    while not stop.is_set():
        await asyncio.sleep(4)
        if stop.is_set():
            break
        try:
            txt = compose()
        except Exception:
            continue
        if txt and txt != last:
            if await edit_ref(bot, sref, txt, CANCEL_BTN):
                last = txt


# ---------- parallel Telegram download ----------
async def parallel_download(client, media, path, size, prog, workers=WORKERS):
    CHUNK = 1024 * 1024
    total_chunks = (size + CHUNK - 1) // CHUNK
    per = (total_chunks + workers - 1) // workers
    with open(path, "wb") as f:
        f.truncate(size)
    done = [0]

    async def worker(i):
        start = i * per
        n = min(per, total_chunks - start)
        if n <= 0:
            return
        with open(path, "r+b") as f:
            f.seek(start * CHUNK)
            async for chunk in client.iter_download(
                media, offset=start * CHUNK, limit=n, request_size=CHUNK, file_size=size
            ):
                f.write(chunk)
                done[0] += len(chunk)
                prog(min(done[0], size), size)

    await asyncio.gather(*[worker(i) for i in range(workers)])
    prog(size, size)


def parse_tg_link(link):
    m = re.search(r"t\.me/c/(\d+)/(?:\d+/)?(\d+)", link)
    if m:
        return int(m.group(1)), int(m.group(2)), True
    m = re.search(r"t\.me/([A-Za-z0-9_]+)/(?:\d+/)?(\d+)", link)
    if m:
        return m.group(1), int(m.group(2)), False
    raise ValueError("Telegram link samajh nahi aaya")


def link_topic(link):
    """t.me/c/ID/TOPIC/MSG link se topic id nikalta hai (normal t.me/c/ID/MSG link me None)."""
    m = re.search(r"t\.me/c/\d+/(\d+)/\d+", link) or re.search(r"t\.me/(?!c/)[A-Za-z0-9_]+/(\d+)/\d+", link)
    return int(m.group(1)) if m else None


def msg_topic_id(m):
    """Forum group me message kis topic ka hai (General topic = 1)."""
    r = getattr(m, "reply_to", None)
    if not r:
        return 1
    return getattr(r, "reply_to_top_id", None) or getattr(r, "reply_to_msg_id", None)


def classify(m):
    """Message ka type: photo / video / videonote / gif / sticker / voice / audio / file / text.
    Kuch bhejne layak nahi (service msg, poll, location...) to None."""
    if not m or getattr(m, "action", None):
        return None
    media = getattr(m, "media", None)
    if isinstance(media, MessageMediaPhoto):
        return "photo" if isinstance(media.photo, types.Photo) else None
    if isinstance(media, MessageMediaDocument):
        d = media.document
        if not isinstance(d, types.Document):
            return None
        attrs = {type(a): a for a in d.attributes}
        if DocumentAttributeSticker in attrs:
            return "sticker"
        if DocumentAttributeAnimated in attrs:
            return "gif"
        v = attrs.get(DocumentAttributeVideo)
        if v is not None:
            return "videonote" if getattr(v, "round_message", False) else "video"
        a = attrs.get(DocumentAttributeAudio)
        if a is not None:
            return "voice" if getattr(a, "voice", False) else "audio"
        return "file"
    if media is not None and not isinstance(media, MessageMediaWebPage):
        return None   # poll, location, contact...
    return "text" if (getattr(m, "message", "") or "").strip() else None


async def resolve_entity(user, chat, private):
    key = PeerChannel(chat) if private else chat
    try:
        return await user.get_entity(key)
    except ValueError:
        await user.get_dialogs()   # entity cache bharne ke liye
        return await user.get_entity(key)


async def find_tg_message(users, link, topic=None, reasons=None):
    """Link wala message dhundta hai. Nahi mile to (None, None).
    topic diya ho to sirf us topic ke messages maane jate hain."""
    chat, msg_id, private = parse_tg_link(link)
    for n, user in enumerate(users, 1):  # jis account se message mil jaye, usse download
        try:
            entity = await resolve_entity(user, chat, private)
            m = await user.get_messages(entity, ids=msg_id)
        except Exception as e:
            if reasons is not None:
                reasons.append(f"Session {n}: {type(e).__name__}: {str(e)[:90]}")
            continue
        if not classify(m):
            if reasons is not None:
                reasons.append(f"Session {n}: channel mila par message khaali/delete ya bhejne layak nahi")
            continue
        if topic and msg_topic_id(m) != topic:
            if reasons is not None:
                reasons.append(f"Session {n}: message doosre topic ka hai")
            continue
        return m, user
    return None, None


async def scan_range(users, base, topic, a, b, reasons=None):
    """Range ke messages 100-100 ke batch me ek saath laata hai (message ke hisaab se alag-alag
    nahi), isliye skip wale messages bahut fast nikal jate hain.
    Return: ([(msg_id, user, entity), ...], skip_count)"""
    private = base.startswith("c/")
    chat = int(base[2:]) if private else base
    topic = int(topic) if topic else None
    ids = list(range(a, b + 1))
    seen, valid = set(), {}
    for n, user in enumerate(users, 1):
        remaining = [i for i in ids if i not in seen]
        if not remaining:
            break
        try:
            entity = await resolve_entity(user, chat, private)
        except Exception as e:
            if reasons is not None:
                reasons.append(f"Session {n}: {type(e).__name__}: {str(e)[:90]}")
            continue
        for k in range(0, len(remaining), 100):
            try:
                msgs = await user.get_messages(entity, ids=remaining[k:k + 100])
            except Exception as e:
                if reasons is not None:
                    reasons.append(f"Session {n}: {type(e).__name__}: {str(e)[:90]}")
                continue
            for m in msgs:
                if not m:
                    continue
                seen.add(m.id)
                if not classify(m):
                    continue
                if topic and msg_topic_id(m) != topic:
                    continue
                valid[m.id] = (user, entity)
    out = [(i, valid[i][0], valid[i][1]) for i in sorted(valid)]
    return out, len(ids) - len(out)


def clean_attrs(doc):
    """Original ke attributes (voice, sticker, gif, duration...) jo dobara bhejte waqt lagane hain."""
    out = []
    for a in doc.attributes:
        if isinstance(a, DocumentAttributeFilename):
            continue   # naam hum khud (replace rules ke baad) set karte hain
        if isinstance(a, DocumentAttributeVideo):
            a = DocumentAttributeVideo(
                duration=a.duration, w=a.w, h=a.h,
                round_message=getattr(a, "round_message", None), supports_streaming=True,
            )
        elif isinstance(a, DocumentAttributeSticker):
            a = DocumentAttributeSticker(alt=a.alt, stickerset=InputStickerSetEmpty())
        out.append(a)
    return out


async def download_message(client, msg, info, kind):
    size = msg.file.size or 0
    if size > MAX_SIZE:
        raise RuntimeError(f"File {human(size)} hai, bot ki size limit ({human(MAX_SIZE)}) se badi. MAX_SIZE_GB badha sakte ho.")
    name = msg.file.name or f"tg_{msg.id}{msg.file.ext or '.mp4'}"
    path = os.path.join(DOWNLOAD_DIR, re.sub(r'[\\/:*?"<>|]', "_", name))
    prog = Progress("⬇️ Download")
    info["p"] = prog
    try:
        if kind == "photo":   # photo ka size andaaza hota hai, isliye seedha download_media
            await client.download_media(msg, file=path, progress_callback=prog)
        else:
            await parallel_download(client, msg.document or msg.media, path, size, prog)
    except BaseException:   # cancel ya error: adhoori file hata do
        safe_delete(path)
        raise

    # original caption, attributes (duration/voice/sticker/gif) aur thumbnail (Telegram par dobara bhejne ke liye)
    meta = {"caption": (msg.message or "").strip(), "attrs": [], "thumb": None}
    if msg.document and kind != "photo":
        meta["attrs"] = clean_attrs(msg.document)
    if kind in ("video", "file", "audio"):
        try:
            meta["thumb"] = await client.download_media(msg, file=path + ".thumb.jpg", thumb=-1)
        except Exception:
            pass
    return path, os.path.splitext(os.path.basename(path))[0], meta


# ---------- Drive ----------
def drive_id(link):
    for pat in (r"/d/([A-Za-z0-9_-]{10,})", r"[?&]id=([A-Za-z0-9_-]{10,})"):
        m = re.search(pat, link)
        if m:
            return m.group(1)
    raise ValueError("Drive link se file ID nahi mili")


def drive_download_sync(link):
    import gdown

    out = gdown.download(id=drive_id(link), output=DOWNLOAD_DIR + os.sep, quiet=True)
    if not out:
        raise RuntimeError("Drive download fail. File 'Anyone with the link' share honi chahiye.")
    if os.path.getsize(out) > MAX_SIZE:
        os.remove(out)
        raise RuntimeError(f"File bot ki size limit ({human(MAX_SIZE)}) se badi hai.")
    return out, os.path.splitext(os.path.basename(out))[0]


# ---------- YouTube ----------
def yt_upload_sync(path, title, prog, thumb=None):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    prog.text = "⬆️ YouTube upload: Google se login ho raha hai..."
    t0 = time.time()
    creds = Credentials.from_authorized_user_info(json.loads(YT_TOKEN_JSON), SCOPES)
    if not creds.valid:
        creds.refresh(Request())
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    print(f"YT: login ho gaya ({time.time() - t0:.1f}s)", flush=True)
    prog.text = f"⬆️ YouTube upload: pehla hissa ({human(UPLOAD_CHUNK)}) bhej raha hoon... (chunk poora hone par % dikhega)"

    body = {
        "snippet": {"title": title[:100], "categoryId": CATEGORY_ID},
        "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(path, chunksize=UPLOAD_CHUNK, resumable=True, mimetype="video/*")
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    total = os.path.getsize(path)
    resp, retries = None, 0
    while resp is None:
        if CANCEL_EVT.is_set():
            raise RuntimeError("Cancel kiya gaya")
        try:
            status, resp = req.next_chunk()
            if status:
                prog(int(status.progress() * total), total)
            retries = 0
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and retries < 10:
                retries += 1
                wait = min(2 ** retries, 60) + random.random()
                print(f"YT: server error {e.resp.status}, retry {retries}/10", flush=True)
                prog.text = f"⚠️ YouTube server error {e.resp.status}, {wait:.0f}s baad dobara ({retries}/10)"
                time.sleep(wait)
                continue
            raise
        except (ConnectionError, TimeoutError, OSError) as e:
            if retries >= 10:
                raise
            retries += 1
            wait = min(2 ** retries, 60)
            print(f"YT: network error {type(e).__name__}, retry {retries}/10", flush=True)
            prog.text = f"⚠️ Internet/connection dikkat ({type(e).__name__}), {wait}s baad dobara ({retries}/10)"
            time.sleep(wait)
    prog(total, total)
    vid = resp["id"]
    note = ""
    if thumb and os.path.exists(thumb):
        try:
            yt.thumbnails().set(
                videoId=vid, media_body=MediaFileUpload(thumb, mimetype="image/jpeg")
            ).execute()
        except Exception as e:
            note = f"\n⚠️ YouTube thumbnail set nahi hua: {str(e)[:120]}"
    return f"https://youtu.be/{vid}", note


def safe_delete(path):
    gc.collect()
    for _ in range(10):
        try:
            if os.path.exists(path):
                os.remove(path)
            return
        except PermissionError:
            time.sleep(1)
            gc.collect()


# ---------- settings: thumbnail + word replace ----------
def build_thumbs(src):
    """Ek image se YouTube (bada) aur Telegram (chhota) thumbnail banata hai."""
    from PIL import Image

    im = Image.open(src).convert("RGB")

    yt = im.copy()
    yt.thumbnail((1280, 720))
    q = 90
    while True:
        yt.save(THUMB_YT, "JPEG", quality=q)
        if os.path.getsize(THUMB_YT) <= 1900 * 1024 or q <= 40:
            break
        q -= 10

    tg = im.copy()
    tg.thumbnail((320, 320))
    q = 85
    while True:
        tg.save(THUMB_TG, "JPEG", quality=q)
        if os.path.getsize(THUMB_TG) <= 190 * 1024 or q <= 40:
            break
        q -= 10


async def save_settings(user):
    text = SETTINGS_TAG + "\n" + json.dumps(
        {
            "rules": SETTINGS["rules"],
            "thumb_msg_id": SETTINGS["thumb_msg_id"],
            "bot_sessions": SETTINGS["bot_sessions"],
        },
        ensure_ascii=False,
    )
    try:
        if SETTINGS_MSG["id"]:
            await user.edit_message("me", SETTINGS_MSG["id"], text)
        else:
            m = await user.send_message("me", text)
            SETTINGS_MSG["id"] = m.id
        return True
    except Exception as e:
        if "not modified" in str(e).lower():
            return True
        try:   # purana settings message delete ho gaya ho to naya banao
            m = await user.send_message("me", text)
            SETTINGS_MSG["id"] = m.id
            return True
        except Exception as e2:
            print("Settings save nahi hui:", e2)
            return False


async def load_settings(user):
    try:
        async for m in user.iter_messages("me", search=SETTINGS_TAG, limit=10):
            if m.message and m.message.startswith(SETTINGS_TAG):
                data = json.loads(m.message[len(SETTINGS_TAG):].strip() or "{}")
                SETTINGS["rules"] = [list(x) for x in data.get("rules", [])]
                SETTINGS["thumb_msg_id"] = data.get("thumb_msg_id")
                SETTINGS["bot_sessions"] = [s for s in data.get("bot_sessions", []) if isinstance(s, str) and s]
                SETTINGS_MSG["id"] = m.id
                break
        tid = SETTINGS.get("thumb_msg_id")
        if tid:
            m = await user.get_messages("me", ids=tid)
            if m and m.media:
                src = await user.download_media(m, file=os.path.join(DOWNLOAD_DIR, "thumb_src"))
                if src:
                    build_thumbs(src)
                    safe_delete(src)
            else:
                SETTINGS["thumb_msg_id"] = None
        print(f"Settings load: {len(SETTINGS['rules'])} replace rules, thumbnail={'haan' if os.path.exists(THUMB_TG) else 'nahi'}")
    except Exception as e:
        print("Settings load nahi hui:", e)


async def set_thumb(user, bot, media_msg):
    src = await bot.download_media(media_msg, file=os.path.join(DOWNLOAD_DIR, "thumb_new"))
    if not src:
        raise RuntimeError("Image download nahi hui")
    build_thumbs(src)
    safe_delete(src)
    old = SETTINGS.get("thumb_msg_id")
    m = await user.send_file("me", THUMB_YT, caption=THUMB_TAG)
    SETTINGS["thumb_msg_id"] = m.id
    if old:
        try:
            await user.delete_messages("me", [old])
        except Exception:
            pass
    return await save_settings(user)


async def del_thumb(user):
    tid = SETTINGS.get("thumb_msg_id")
    had = bool(tid) or os.path.exists(THUMB_TG)
    if tid:
        try:
            await user.delete_messages("me", [tid])
        except Exception:
            pass
    SETTINGS["thumb_msg_id"] = None
    safe_delete(THUMB_TG)
    safe_delete(THUMB_YT)
    await save_settings(user)
    return had


def parse_rules(text):
    """Har line:  purana => naya   (naya khali ho ya sirf purana likho to wo word hata diya jayega)"""
    rules = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "=>" in line:
            old, new = line.split("=>", 1)
        elif "|" in line:
            old, new = line.split("|", 1)
        else:
            old, new = line, ""
        old, new = old.strip(), new.strip()
        if old:
            rules.append([old, new])
    return rules


_IGNORE = {ord(c) for c in "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad\ufe0e\ufe0f"}


def _norm(text):
    """Text ko 'seedha' roop me badalta hai (𝐃𝐎𝐖𝐍𝐋𝐎𝐀𝐃 -> download, NBSP -> space, chhupe zero-width
    characters hata deta hai) aur har naye character ka original position bhi return karta hai."""
    out, idx = [], []
    for i, ch in enumerate(text):
        if ord(ch) in _IGNORE:
            continue
        for c in unicodedata.normalize("NFKC", ch).casefold():
            out.append(c)
            idx.append(i)
    return "".join(out), idx


def _rule_pattern(old, name_mode=False):
    norm, _ = _norm(old)
    norm = " ".join(norm.split())
    sep = r"[\s_.\-]+" if name_mode else r"\s+"
    return re.compile(sep.join(re.escape(p) for p in norm.split(" ")))


def smart_sub(text, name_mode=False):
    """Rules lagata hai. Fancy/bold Unicode letters, alag tarah ke spaces aur chhupe characters ka farq nahi padta;
    sirf match hua hissa badalta hai, baaki text jaisa tha waisa rehta hai."""
    for old, new in SETTINGS["rules"]:
        try:
            pat = _rule_pattern(old, name_mode)
        except Exception:
            continue
        if not pat.pattern:
            continue
        norm, idx = _norm(text)
        for m in reversed(list(pat.finditer(norm))):
            if m.end() == m.start():
                continue
            a, b = idx[m.start()], idx[m.end() - 1] + 1
            text = text[:a] + new + text[b:]
    return text


def apply_rules_raw(text):
    """Sirf rules lagata hai (spaces/lines ko nahi chhedta)."""
    return smart_sub(text)


def apply_rules(text):
    if not text:
        return text
    text = apply_rules_raw(text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return text.strip()


def apply_rules_name(text):
    """File ke naam par rules: naam me space ki jagah _ . - bhi ho to bhi match hota hai."""
    text = smart_sub(text, name_mode=True)
    text = re.sub(r"[ _]{2,}", " ", text)
    return text.strip(" ._-")


def rename_with_rules(path):
    """File ka naam bhi rules se badalta hai (PDF/video sab; Telegram par file ka naam wahi dikhta hai)."""
    base, ext = os.path.splitext(os.path.basename(path))
    nb = re.sub(r'[\\/:*?"<>|]', "_", apply_rules_name(base)).strip(" ._")
    if not nb or nb == base:
        return path
    new_path = os.path.join(os.path.dirname(path), nb + ext)
    try:
        if os.path.exists(new_path):
            return path
        os.rename(path, new_path)
        return new_path
    except OSError:
        return path


# ---------- fast Telegram upload (parallel parts) ----------
async def fast_upload(clients, path, prog, workers=UP_WORKERS):
    """File ke parts kai connections par ek saath bhejta hai. Chhoti file (<10MB) ke liye path hi return karta hai."""
    from telethon.errors import FloodWaitError
    from telethon.tl.functions.upload import SaveBigFilePartRequest
    from telethon.tl.types import InputFileBig

    size = os.path.getsize(path)
    if size <= 10 * 1024 * 1024:
        return path
    part_size = 512 * 1024
    parts = (size + part_size - 1) // part_size
    file_id = int.from_bytes(os.urandom(8), "big", signed=True)
    sems = [asyncio.Semaphore(workers) for _ in clients]
    done = [0]

    async def send(ci, i, data):
        try:
            for attempt in range(5):
                try:
                    await clients[ci](SaveBigFilePartRequest(file_id, i, parts, data))
                    break
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds + 1)
                except Exception:
                    if attempt == 4:
                        raise
                    await asyncio.sleep(1 + attempt)
            done[0] += len(data)
            prog(min(done[0], size), size)
        finally:
            sems[ci].release()

    tasks = []
    try:
        with open(path, "rb") as f:
            for i in range(parts):
                data = f.read(part_size)
                ci = i % len(clients)
                await sems[ci].acquire()
                tasks.append(asyncio.create_task(send(ci, i, data)))
        await asyncio.gather(*tasks)
    except BaseException:   # cancel ya error: baaki parts rok do
        for t in tasks:
            t.cancel()
        raise
    prog(size, size)
    return InputFileBig(id=file_id, parts=parts, name=os.path.basename(path))


def probe_video(path):
    """Duration/size nikalta hai (hachoir se), taaki Telegram par 00:00 na dikhe."""
    try:
        from hachoir.metadata import extractMetadata
        from hachoir.parser import createParser

        parser = createParser(path)
        if not parser:
            return None
        with parser:
            m = extractMetadata(parser)
        if not m:
            return None
        dur = int(m.get("duration").total_seconds()) if m.has("duration") else 0
        w = int(m.get("width")) if m.has("width") else 0
        h = int(m.get("height")) if m.has("height") else 0
        return DocumentAttributeVideo(duration=dur, w=w, h=h, supports_streaming=True)
    except Exception:
        return None


# ---------- job processing ----------
def tally(b):
    return f"✅ {b['ok']}   ❌ {b['fail']}   🛑 {b['cancel']}   ⏭️ {b['skip']}"


async def batch_done(bot, batch, outcome, edit=True):
    """Range ke har kaam ke baad count update; sab khatam hone par wahi status message summary ban jata hai."""
    batch[outcome] += 1
    batch["done"] += 1
    if batch["done"] < batch["total"]:
        if edit:
            await edit_ref(
                bot, batch["status"],
                f"📦 Range {batch['range']}  →  {batch['label']}\n"
                f"⏳ {batch['done']}/{batch['total']} ho chuki, agli shuru ho rahi hai...\n{tally(batch)}",
                CANCEL_BTN,
            )
        return
    lines = [
        f"🏁 Range {batch['range']} poora hua  →  {batch['label']}",
        f"📁 Total files/messages: {batch['total']}",
        f"✅ Ho gaye: {batch['ok']}",
        f"❌ Fail: {batch['fail']}",
        f"🛑 Cancel: {batch['cancel']}",
        f"⏭️ Skip (kuch bhejne layak nahi / doosre topic ka): {batch['skip']}",
    ]
    if batch["tg_only"]:
        lines.append(f"ℹ️ {batch['tg_only']} item video nahi the, wo sirf Telegram par gaye")
    if batch["errors"]:
        lines += ["", "❌ Errors:"] + batch["errors"][:10]
    await edit_ref(bot, batch["status"], "\n".join(lines))

    chunks, cur = [], ""
    for i, item in enumerate(batch["yt_links"], 1):
        entry = f"{i}. {item}\n\n"
        if len(cur) + len(entry) > 3500:
            chunks.append(cur)
            cur = ""
        cur += entry
    if cur:
        chunks.append(cur)
    for c in chunks:
        try:
            await bot.send_message(batch["chat"], "▶️ YouTube links (Unlisted):\n\n" + c,
                                   parse_mode=None, link_preview=False)
        except Exception:
            pass


async def send_text(bot, chat_id, msg):
    """Normal text message bhejta hai (bold/link wagairah formatting ke saath, agar rules ne text nahi badla)."""
    text = msg.message or ""
    preview = isinstance(msg.media, MessageMediaWebPage)
    if apply_rules_raw(text) == text:
        try:
            await bot.send_message(chat_id, text, formatting_entities=msg.entities or None,
                                   parse_mode=None, link_preview=preview)
            return
        except Exception:
            pass   # entities me dikkat ho to plain text bhej do
        plain = text
    else:
        plain = apply_rules(text)   # text badla, isliye formatting nahi lag sakti
    await bot.send_message(chat_id, plain or text, parse_mode=None, link_preview=preview)


def send_kwargs(kind, meta):
    """Message ki type ke hisaab se send_file ke options (photo photo rahe, voice voice, sticker sticker...)."""
    kw = {}
    if meta.get("attrs"):
        kw["attributes"] = list(meta["attrs"])
    if kind in ("video", "file") and os.path.exists(THUMB_TG):
        kw["thumb"] = THUMB_TG                  # aapka custom thumbnail
    elif kind in ("video", "file", "audio") and meta.get("thumb"):
        kw["thumb"] = meta["thumb"]             # original ka thumbnail
    if kind == "voice":
        kw["voice_note"] = True
    if kind == "videonote":
        kw["video_note"] = True
    return kw


async def run_job(users, bot, job):
    ev, kind, payload = job["ev"], job["kind"], job["payload"]
    title, dest, batch = job["title"], job["dest"], job.get("batch")
    chat_id = ev.chat_id
    if batch and batch.get("cancelled"):   # range cancel ho chuki hai
        await batch_done(bot, batch, "cancel", edit=False)
        return

    sref = batch["status"] if batch else job.get("status")
    if not sref:
        m = await ev.reply("⏳ Shuru ho raha hai...", buttons=CANCEL_BTN)
        sref = (m.chat_id, m.id)
    info = {"p": None, "name": ""}   # abhi ka kaam (download/upload yahan likhte hain)

    def compose():
        lines = []
        if batch:
            lines.append(f"📦 Range {batch['range']}  →  {batch['label']}")
            lines.append(f"▶️ Chal raha hai: {batch['done'] + 1}/{batch['total']}")
        if info["name"]:
            lines.append(f"📄 {info['name']}")
        lines.append(info["p"].text if info["p"] else "⏳ Shuru ho raha hai...")
        if batch:
            lines.append(tally(batch))
        return "\n".join(lines)

    CURRENT["compose"] = compose
    stop = asyncio.Event()
    updater = asyncio.create_task(status_updater(bot, sref, compose, stop))
    await edit_ref(bot, sref, compose(), CANCEL_BTN)

    path = None
    meta = {}
    results = []
    outcome = "fail"
    mid = payload[2] if kind == "tgr" else None
    try:
        loop = asyncio.get_running_loop()
        mkind, msg, client = None, None, None
        if kind == "tgr":   # range ka ek message (taaza fetch, taaki file reference purana na ho)
            client = payload[0]
            msg = await client.get_messages(payload[1], ids=payload[2])
        elif kind == "tg":
            reasons = []
            msg, client = await find_tg_message(users, payload, link_topic(payload), reasons)
            if not msg:
                raise RuntimeError(
                    "Message nahi mila. Har session ka natija:\n" + "\n".join(reasons)
                    + "\n(Sessions kaun se account ke hain dekhne ke liye /sessions bhejo)"
                )
        elif kind == "media":   # forwarded/uploaded
            msg, client = payload, bot
        if kind != "drive":
            mkind = classify(msg)
            if not mkind:
                outcome = "skip"
                return
            mid = msg.id
            info["name"] = f"msg {msg.id} · {mkind}"

        if mkind == "text":
            info["p"] = Note("📨 Text bhej raha hoon...")
            await send_text(bot, chat_id, msg)
            if dest in ("yt", "both"):
                results.append("ℹ️ Ye text hai, YouTube par nahi ja sakta, isliye sirf Telegram par bheja")
                if batch:
                    batch["tg_only"] += 1
            results.append("📨 Telegram par bhej diya")
        else:
            if kind == "drive":
                info["p"] = Note("⬇️ Drive se download ho raha hai...")
                path, _t = await loop.run_in_executor(None, drive_download_sync, payload)
                mkind = "file"
            else:
                path, _t, meta = await download_message(client, msg, info, mkind)

            # word-replace rules: file ka naam, caption aur YouTube title par
            path = rename_with_rules(path)
            default_title = os.path.splitext(os.path.basename(path))[0]
            info["name"] = os.path.basename(path) + (f"  (msg {mid})" if mid else "")
            caption = apply_rules((meta.get("caption") or "").strip())
            yt_title = title or (caption.splitlines()[0] if caption else "") or default_title
            # photo/voice/sticker/gif par file ka naam caption nahi banta
            tg_caption = title or caption or (default_title if mkind in ("video", "file", "audio") else "")

            ext = os.path.splitext(path)[1].lower()
            if mkind == "file" and (ext in VIDEO_EXTS or (mimetypes.guess_type(path)[0] or "").startswith("video/")):
                mkind = "video"
            is_video = mkind in ("video", "videonote")
            if not is_video and dest in ("yt", "both"):
                results.append("ℹ️ Ye video nahi hai, YouTube par sirf video jaati hai, isliye sirf Telegram par bheji")
                if batch:
                    batch["tg_only"] += 1
                dest = "tg"

            fsize = os.path.getsize(path)
            if fsize > TG_LIMIT and dest in ("tg", "both"):
                if dest == "both" and is_video:
                    results.append("ℹ️ File Telegram bot ki 2GB limit se badi hai, isliye sirf YouTube par gayi")
                    dest = "yt"
                else:
                    raise RuntimeError(f"File {human(fsize)} hai. Telegram bot 2GB se badi file nahi bhej sakta.")

            if dest in ("tg", "both"):
                tgp = Progress("📨 Telegram upload")
                info["p"] = tgp
                kw = send_kwargs(mkind, meta)
                if mkind == "video" and not any(isinstance(x, DocumentAttributeVideo) for x in kw.get("attributes", [])):
                    v = await loop.run_in_executor(None, probe_video, path)
                    if v:
                        kw["attributes"] = kw.get("attributes", []) + [v]
                force_doc = (mkind == "file")   # photo photo ki tarah, baaki apni type me; sirf PDF/ZIP jaisi file document
                try:
                    handle = await fast_upload(UPLOAD_CLIENTS or [bot], path, tgp)
                    await bot.send_file(
                        chat_id, handle, caption=tg_caption[:1024], parse_mode=None,
                        force_document=force_doc, **kw,
                    )
                except Exception:
                    # fast tareeka fail ho to normal upload
                    tgp = Progress("📨 Telegram upload")
                    info["p"] = tgp
                    await bot.send_file(
                        chat_id, path, caption=tg_caption[:1024], parse_mode=None,
                        force_document=force_doc, progress_callback=tgp, **kw,
                    )
                results.append("📨 Telegram par bhej diya")

            if dest in ("yt", "both"):
                up = Progress("⬆️ YouTube upload")
                info["p"] = up
                url, note = await loop.run_in_executor(
                    None, yt_upload_sync, path, yt_title, up, THUMB_YT if os.path.exists(THUMB_YT) else None
                )
                results.append(f"▶️ YouTube (Unlisted): {url}{note}")
                if batch:
                    batch["yt_links"].append(f"{yt_title}\n{url}")

        outcome = "ok"
        if not batch:
            stop.set()
            updater.cancel()
            await edit_ref(bot, sref, "✅ Ho gaya\n" + "\n".join(results))
    except asyncio.CancelledError:
        outcome = "cancel"
        if not batch:
            await edit_ref(bot, sref, "🛑 Cancel kar diya.")
    except Exception as e:
        if CANCEL_EVT.is_set():
            outcome = "cancel"
            if not batch:
                await edit_ref(bot, sref, "🛑 Cancel kar diya.")
        else:
            err = (str(e) or type(e).__name__)[:500]
            if batch:
                batch["errors"].append(f"msg {mid}: {err[:150]}")
            else:
                done = ("\n\nPehle ho chuka:\n" + "\n".join(results)) if results else ""
                await edit_ref(bot, sref, f"❌ Error: {err[:800]}{done}")
    finally:
        stop.set()
        updater.cancel()
        if path:
            safe_delete(path)
        if meta.get("thumb"):
            safe_delete(meta["thumb"])
        if batch:
            try:
                await batch_done(bot, batch, outcome)
            except Exception as e:
                print("batch_done error:", repr(e), flush=True)


async def watchdog(bot, users):
    """Har minute Telegram se connection check. Atak jaye (health server 'ok' de raha ho tab bhi)
    to reconnect try, phir process restart (Render khud dobara chala deta hai)."""
    fails = 0
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.wait_for(bot.get_me(), 60)
            await asyncio.wait_for(users[0].get_me(), 60)
            fails = 0
            STATE["last_ok"] = time.time()
        except Exception as e:
            fails += 1
            print(f"Watchdog: connection check fail #{fails}: {e!r}", flush=True)
            if fails >= 2:
                for c in [bot, *users]:
                    try:
                        if not c.is_connected():
                            await c.connect()
                    except Exception:
                        pass
            t = CURRENT.get("task")
            busy = bool(t and not t.done())   # upload/download chal raha ho to internet bhara hota hai, ping slow ho sakti hai
            if fails >= (30 if busy else 5):
                print("Watchdog: bot atak gaya, restart kar raha hoon...", flush=True)
                os._exit(1)


async def login_bot(saved=None):
    """Bot login. Saved session ho to naya login (ImportBotAuthorization) nahi hota, jisse
    Telegram ka FloodWait nahi lagta. FloodWait aaye to crash hone ki jagah wait karke retry."""
    from telethon import errors

    fails = 0
    while True:
        use = saved if (saved and fails < 3) else None   # saved session 3 baar fail ho to naya login
        c = TelegramClient(StringSession(use), API_ID, API_HASH, connection_retries=-1, retry_delay=2)
        try:
            await c.start(bot_token=BOT_TOKEN)
            return c
        except errors.FloodWaitError as e:
            print(f"Telegram ne bot login rok diya, {e.seconds}s wait karke dobara koshish...", flush=True)
            try:
                await c.disconnect()
            except Exception:
                pass
            await asyncio.sleep(e.seconds + 5)
        except Exception as e:
            print("Bot login error:", repr(e), flush=True)
            fails += 1
            try:
                await c.disconnect()
            except Exception:
                pass
            await asyncio.sleep(10)


async def main():
    start_health()
    users = [TelegramClient(StringSession(s), API_ID, API_HASH, connection_retries=-1, retry_delay=2)
             for s in SESSION_STRINGS]
    for n, u in enumerate(users, 1):
        await u.connect()
        if not await u.is_user_authorized():
            # start() yahan phone number maangke atak jata, isliye seedha saaf error
            sys.exit(
                f"ERROR: Session {n} galat/adhoori/expire hai (string poori copy nahi hui, "
                f"ya Telegram me terminate ho gayi). gen_session.py se nayi string banao."
            )
    SESSION_INFO = []
    for n, u in enumerate(users, 1):
        me = await u.get_me()
        SESSION_INFO.append(f"Session {n}: {me.first_name or ''} {me.last_name or ''} (id {me.id})".replace("  ", " "))
        print(SESSION_INFO[-1], flush=True)
    if len({(await u.get_me()).id for u in users}) < len(users):
        print("WARNING: do session string same account ki hain!", flush=True)
    settings_user = users[0]
    await load_settings(settings_user)   # thumbnail + replace rules + bot sessions wapas load

    # Bot (aur upload ke extra connections) ki session Saved Messages me save hoti hai, taaki
    # har restart par naya login na ho (naya login baar-baar karne se Telegram FloodWait deta hai).
    saved = SETTINGS.get("bot_sessions", [])
    bot = await login_bot(saved[0] if saved else None)
    UPLOAD_CLIENTS.append(bot)
    for i in range(1, UP_CONNECTIONS):
        c = await login_bot(saved[i] if len(saved) > i else None)
        UPLOAD_CLIENTS.append(c)
    print(f"Upload connections: {len(UPLOAD_CLIENTS)}", flush=True)

    current = [c.session.save() for c in UPLOAD_CLIENTS]
    if current != saved:
        SETTINGS["bot_sessions"] = current
        await save_settings(settings_user)

    # Telegram ke "/" menu me commands dikhane ke liye
    try:
        from telethon.tl.functions.bots import SetBotCommandsRequest
        from telethon.tl.types import BotCommand, BotCommandScopeDefault

        await bot(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(), lang_code="",
            commands=[BotCommand(command=c, description=d) for c, d in BOT_COMMANDS],
        ))
    except Exception as e:
        print("Command menu set nahi hua:", e)

    queue: asyncio.Queue = asyncio.Queue()
    pending = {}      # button dabane ka wait kar rahe requests
    counter = [0]

    async def ask_destination(event, kind, payload, title, prompt="Kahan upload karna hai?"):
        counter[0] += 1
        key = str(counter[0])
        pending[key] = (event.message, kind, payload, title)
        await event.reply(
            prompt,
            buttons=[
                [Button.inline("▶️ YouTube", f"yt:{key}".encode()),
                 Button.inline("📨 Telegram", f"tg:{key}".encode())],
                [Button.inline("✅ Dono", f"both:{key}".encode())],
            ],
        )

    @bot.on(events.NewMessage(incoming=True))
    async def handler(event):
        if event.sender_id not in OWNER_IDS:
            return  # baaki sab ignore
        text = (event.raw_text or "").strip()

        if text.startswith("/") and not text.startswith("/start"):
            return  # baaki commands ke apne handler hain

        if text.startswith("/start"):
            await event.reply(
                "👋 Mujhe Telegram link, Drive link ya video/file bhejo. Phir main puchunga ki "
                "YouTube (Unlisted), Telegram ya dono me upload karna hai.\n\n"
                "Saare commands aur unka poora tareeka dekhne ke liye /help bhejo.",
                parse_mode=None,
            )
            return

        k = classify(event.message)
        if k and k != "text":   # video, photo, voice, sticker, gif, file...
            await ask_destination(event, "media", event.message, text or None)
        else:
            title = None
            if "|" in text:
                text, title = [x.strip() for x in text.split("|", 1)]
            rng = RANGE_RE.search(text)
            if rng:
                base, topic = rng.group(1), rng.group(2)
                a, b = int(rng.group(3)), int(rng.group(4))
                if a > b:
                    a, b = b, a
                if b - a + 1 > MAX_RANGE:
                    await event.reply(f"Ek baar me maximum {MAX_RANGE} messages ki range de sakte ho.")
                    return
                topic_txt = f" (topic {topic})" if topic else ""
                await ask_destination(
                    event, "range", (base, topic, a, b), None,
                    prompt=f"Range {a}-{b}{topic_txt}: {b - a + 1} messages. Pehle scan karunga (jin me kuch bhejne layak nahi wo turant skip), baaki ek ke baad ek bhejunga.\nKahan upload karna hai?",
                )
            elif "t.me/" in text:
                await ask_destination(event, "tg", text, title)
            elif "drive.google.com" in text or "docs.google.com" in text:
                await ask_destination(event, "drive", text, title)
            else:
                await event.reply("Sirf Telegram (t.me) ya Google Drive link / video bhejo.")

    @bot.on(events.CallbackQuery)
    async def on_choice(event):
        if event.sender_id not in OWNER_IDS:
            await event.answer("Aap allowed nahi ho.", alert=True)
            return
        try:
            dest, key = event.data.decode().split(":", 1)
        except Exception:
            return
        item = pending.pop(key, None)
        if not item:
            await event.answer("Ye request purani ho gayi, dobara bhejo.", alert=True)
            return
        await event.answer()
        label = {"yt": "YouTube", "tg": "Telegram", "both": "YouTube + Telegram"}.get(dest, dest)
        ev_msg, kind, payload, title = item
        sref = (event.chat_id, event.query.msg_id)   # ye wahi message hai jo ab status message banega

        if kind == "range":
            base, topic, a, b = payload
            await edit_ref(bot, sref, f"🔎 Range {a}-{b} scan ho rahi hai...")
            reasons = []
            try:
                valid, skipped = await scan_range(users, base, topic, a, b, reasons)
            except Exception as e:
                await edit_ref(bot, sref, f"❌ Range scan fail: {str(e)[:300]}")
                return
            if not valid:
                await edit_ref(bot, sref, f"Range {a}-{b} me bhejne layak koi message nahi mila ({skipped} skip)."
                               + ("\n\nSession ka natija:\n" + "\n".join(reasons[:6]) if reasons else ""))
                return
            batch = {
                "total": len(valid), "done": 0, "ok": 0, "skip": skipped, "fail": 0, "cancel": 0,
                "range": f"{a}-{b}", "label": label, "status": sref, "chat": ev_msg.chat_id,
                "errors": [], "yt_links": [], "tg_only": 0,
            }
            ahead = queue.qsize()
            for mid, user, entity in valid:
                await queue.put({"ev": ev_msg, "kind": "tgr", "payload": (user, entity, mid),
                                 "title": None, "dest": dest, "batch": batch, "status": None})
            await edit_ref(
                bot, sref,
                f"📥 Range {a}-{b}: {len(valid)} files {label} ke liye queue me add ho gayi "
                f"({skipped} skip)." + (f"\nAage {ahead} kaam hain." if ahead else "")
                + "\nIs hi message me progress dikhegi.",
            )
            return

        await queue.put({"ev": ev_msg, "kind": kind, "payload": payload, "title": title,
                         "dest": dest, "batch": None, "status": sref})
        n = queue.qsize()
        await edit_ref(bot, sref, f"📥 {label} ke liye queue me add ho gaya" + (f" (aage {n - 1} kaam hain)" if n > 1 else ""))

    async def do_cancel(clear_queue):
        """Chal raha kaam rokta hai. clear_queue=True ho to queue ke saare kaam bhi hata deta hai."""
        CANCEL_EVT.set()
        removed = 0
        if clear_queue:
            while not queue.empty():
                try:
                    job = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                queue.task_done()
                removed += 1
                if job.get("batch"):
                    await batch_done(bot, job["batch"], "cancel", edit=False)
                elif job.get("status"):
                    await edit_ref(bot, job["status"], "🛑 Cancel kar diya (queue se hata diya).")
        b = CURRENT.get("batch")
        if b:
            b["cancelled"] = True   # is range ke baaki messages bhi skip
        t = CURRENT.get("task")
        running = bool(t and not t.done())
        if running:
            t.cancel()
        return running, removed

    @bot.on(events.NewMessage(pattern=r"^/cancel", incoming=True))
    async def cancel_cmd(event):
        if event.sender_id not in OWNER_IDS:
            return
        running, removed = await do_cancel(True)
        if running or removed:
            await event.reply(f"🛑 Cancel ho gaya. Chal raha kaam roka, queue ke {removed} kaam hata diye.")
        else:
            await event.reply("Koi kaam chal nahi raha.")

    def fmt_uptime(sec):
        sec = int(sec)
        d, r = divmod(sec, 86400)
        h, r = divmod(r, 3600)
        m, _ = divmod(r, 60)
        return (f"{d}d " if d else "") + (f"{h}h " if h else "") + f"{m}m"

    @bot.on(events.NewMessage(pattern=r"^/status", incoming=True))
    async def status_cmd(event):
        if event.sender_id not in OWNER_IDS:
            return
        t = CURRENT.get("task")
        running = bool(t and not t.done())
        lines = [f"🟢 Bot LIVE hai (chal raha: {fmt_uptime(time.time() - START_TIME)})"]
        lines.append(f"🩺 Telegram connection OK ({int(time.time() - STATE['last_ok'])}s pehle check hua)")
        if running:
            comp = CURRENT.get("compose")
            now = comp() if comp else "shuru ho raha hai..."
            lines.append(f"⚙️ Abhi kaam chal raha hai:\n{now}")
        else:
            lines.append("😴 Abhi koi kaam nahi chal raha (idle)")
        lines.append(f"📋 Queue me: {queue.qsize()} kaam")
        lines.append(f"📡 Upload connections: {len(UPLOAD_CLIENTS)}")
        await event.reply("\n".join(lines))

    # ---------- thumbnail commands ----------
    @bot.on(events.NewMessage(pattern=r"^/setthumb(?:\s|$)", incoming=True))
    async def setthumb_cmd(event):
        if event.sender_id not in OWNER_IDS:
            return
        target = event.message if event.message.media else None
        if not target and event.message.is_reply:
            target = await event.message.get_reply_message()
        ok = bool(
            target and (
                target.photo
                or (target.document and (target.document.mime_type or "").startswith("image/"))
            )
        )
        if not ok:
            await event.reply(
                "Thumbnail set karne ke liye ek photo bhejo jiske caption me /setthumb likha ho, "
                "ya kisi photo par reply karke /setthumb likho."
            )
            return
        wait = await event.reply("⏳ Thumbnail ban raha hai...")
        try:
            saved = await set_thumb(settings_user, bot, target)
            await wait.delete()
            extra = "" if saved else "\n⚠️ Ye Saved Messages me save nahi ho paya, restart ke baad hat jayega."
            await event.reply(
                "✅ Thumbnail set ho gaya. Ab ke saare uploads (Telegram aur YouTube) me ye lagega.\n"
                "Hatane ke liye: /delthumb" + extra,
                file=THUMB_TG,
            )
        except Exception as e:
            await wait.edit(f"❌ Thumbnail set nahi hua: {str(e)[:300]}")

    @bot.on(events.NewMessage(pattern=r"^/thumb(?:\s|$)", incoming=True))
    async def thumb_cmd(event):
        if event.sender_id not in OWNER_IDS:
            return
        if os.path.exists(THUMB_TG):
            await event.reply("🖼️ Abhi ka thumbnail (hatane ke liye /delthumb):", file=THUMB_TG)
        else:
            await event.reply("Koi thumbnail set nahi hai. Photo bhejo aur caption me /setthumb likho.")

    @bot.on(events.NewMessage(pattern=r"^/delthumb(?:\s|$)", incoming=True))
    async def delthumb_cmd(event):
        if event.sender_id not in OWNER_IDS:
            return
        had = await del_thumb(settings_user)
        await event.reply("🗑️ Thumbnail hata diya." if had else "Koi thumbnail set nahi tha.")

    # ---------- word replace commands ----------
    def format_rules():
        if not SETTINGS["rules"]:
            return "Abhi koi replace rule nahi hai."
        lines = []
        for i, (old, new) in enumerate(SETTINGS["rules"], 1):
            lines.append(f"{i}. {old}  →  {new if new else '(hata diya jayega)'}")
        return "\n".join(lines)

    @bot.on(events.NewMessage(pattern=r"^/replace(?:\s|$)", incoming=True))
    async def replace_cmd(event):
        if event.sender_id not in OWNER_IDS:
            return
        parts = (event.raw_text or "").split(None, 1)
        new_rules = parse_rules(parts[1]) if len(parts) > 1 else []
        if not new_rules:
            await event.reply(
                "Aise likho (har line ek rule):\n"
                "/replace purana => naya\n"
                "/replace @channel =>\n\n"
                "Naya khali chhodoge (ya sirf purana likhoge) to wo word hat jayega. "
                "Capital/small ka farq nahi padta.\n"
                "Dekhne ke liye /replacelist, hatane ke liye /delreplace ya /clearreplace"
            )
            return
        for old, new in new_rules:
            SETTINGS["rules"] = [r for r in SETTINGS["rules"] if r[0].lower() != old.lower()]
            SETTINGS["rules"].append([old, new])
        saved = await save_settings(settings_user)
        await event.reply(
            f"✅ {len(new_rules)} rule add hua.\n\n{format_rules()}\n\n"
            "Ye file ke naam, caption aur YouTube title par lagega."
            + ("" if saved else "\n⚠️ Saved Messages me save nahi hua, restart ke baad hat jayega.")
        )

    @bot.on(events.NewMessage(pattern=r"^/replacelist(?:\s|$)", incoming=True))
    async def replacelist_cmd(event):
        if event.sender_id not in OWNER_IDS:
            return
        await event.reply("🔤 Replace rules:\n" + format_rules())

    @bot.on(events.NewMessage(pattern=r"^/delreplace(?:\s|$)", incoming=True))
    async def delreplace_cmd(event):
        if event.sender_id not in OWNER_IDS:
            return
        parts = (event.raw_text or "").split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if not arg:
            await event.reply("Kaun sa rule hatana hai? Number ya word likho:\n/delreplace 2\n/delreplace @channel\n\n" + format_rules())
            return
        before = len(SETTINGS["rules"])
        if arg.isdigit() and 1 <= int(arg) <= before:
            SETTINGS["rules"].pop(int(arg) - 1)
        else:
            SETTINGS["rules"] = [r for r in SETTINGS["rules"] if r[0].lower() != arg.lower()]
        if len(SETTINGS["rules"]) == before:
            await event.reply("Ye rule mila nahi.\n\n" + format_rules())
            return
        await save_settings(settings_user)
        await event.reply("🗑️ Rule hata diya.\n\n" + format_rules())

    @bot.on(events.NewMessage(pattern=r"^/clearreplace(?:\s|$)", incoming=True))
    async def clearreplace_cmd(event):
        if event.sender_id not in OWNER_IDS:
            return
        SETTINGS["rules"] = []
        await save_settings(settings_user)
        await event.reply("🗑️ Saare replace rules hata diye.")

    @bot.on(events.NewMessage(pattern=r"^/sessions(?:\s|$)", incoming=True))
    async def sessions_cmd(event):
        if event.sender_id not in OWNER_IDS:
            return
        lines = ["👤 Message padhne wale accounts:"] + SESSION_INFO
        ids = {x.split("(id ")[-1] for x in SESSION_INFO}
        if len(ids) < len(SESSION_INFO):
            lines.append("\n⚠️ Do session string same account ki hain, doosre account ki nayi string banao (gen_session.py).")
        await event.reply("\n".join(lines))

    @bot.on(events.NewMessage(pattern=r"^/help(?:\s|$)", incoming=True))
    async def help_cmd(event):
        if event.sender_id not in OWNER_IDS:
            return
        await event.reply(HELP_1, parse_mode=None, link_preview=False)
        await event.reply(HELP_2, parse_mode=None, link_preview=False)

    @bot.on(events.CallbackQuery(data=b"cancel"))
    async def cancel_btn(event):
        if event.sender_id not in OWNER_IDS:
            await event.answer("Aap allowed nahi ho.", alert=True)
            return
        running, _ = await do_cancel(False)
        await event.answer("Cancel kar raha hoon..." if running else "Koi kaam nahi chal raha.")

    async def worker():
        while True:
            job = await queue.get()
            try:
                CANCEL_EVT.clear()
                CURRENT["batch"] = job.get("batch")
                task = asyncio.create_task(run_job(users, bot, job))
                CURRENT["task"] = task
                await task
            except BaseException as e:
                if not isinstance(e, asyncio.CancelledError):
                    print("Job error:", repr(e), flush=True)
            finally:
                CURRENT["task"] = None
                CURRENT["compose"] = None
                queue.task_done()

    BG_TASKS.append(asyncio.create_task(worker()))
    BG_TASKS.append(asyncio.create_task(watchdog(bot, users)))
    STATE["ready"] = True
    STATE["last_ok"] = time.time()
    print("Bot chal raha hai...")

    # Bot band hone par (Render restart/deploy) notice bhejo
    async def on_term():
        for oid in OWNER_IDS:
            try:
                await bot.send_message(
                    oid, "🔴 Bot band ho raha hai (Render restart/deploy). Chal raha kaam aur queue ruk jayenge."
                )
            except Exception:
                pass
        os._exit(0)

    try:
        asyncio.get_running_loop().add_signal_handler(
            signal.SIGTERM, lambda: asyncio.create_task(on_term())
        )
    except (NotImplementedError, RuntimeError, AttributeError):
        pass

    # Bot chalu hone par notice bhejo
    for oid in OWNER_IDS:
        try:
            await bot.send_message(
                oid,
                "🟢 Bot online ho gaya (naya start/restart).\n"
                "Pehle ka koi kaam ya queue chal rahi thi to wo ruk chuki hai, dobara bhejna padega.",
            )
        except Exception:
            pass
    await asyncio.gather(*[u.run_until_disconnected() for u in users], bot.run_until_disconnected())


if __name__ == "__main__":
    asyncio.run(main())
