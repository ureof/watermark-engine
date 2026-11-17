"""
=============================================================================
WATERMARK REPLACEMENT ENGINE (BLUR + PURPLE WATERMARK + 2GB TELETHON)
=============================================================================
Architecture:
- FastAPI Web Server (Port 7860)
- Telethon MTProto Client (Full 2 GB Upload & Download Speed)
- FFmpeg Video Engine:
  * Blur Effect over Previous Watermark Region
  * Electric Purple (0xA855F7) Watermark Text Overlay
  * Temporal Splicing (First N Minutes Processed, Remainder Lossless -c copy)
- Cloudflare Keep-Alive Endpoint (/health)
=============================================================================
"""

import os
import sys
import time
import asyncio
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from telethon import TelegramClient, utils
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeFilename

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("WatermarkEngine")

# Credentials
API_ID = int(os.environ.get("TELEGRAM_API_ID", "35537842"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "d97500b2596363b5ed28dc12af62ede8")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8901084274:AAGsXxNfC3zsVsHzbg_gsBQLUGFA3kn_8Ts")

import tempfile
WORKDIR = Path(os.environ.get("WORKDIR", tempfile.gettempdir())) / "watermark_jobs"
WORKDIR.mkdir(parents=True, exist_ok=True)

# Active jobs tracking
ACTIVE_JOBS: Dict[str, Dict[str, Any]] = {}

app = FastAPI(title="Watermark Blur & Purple Overlay Engine", version="11.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Telethon client instance
tg_client: Optional[TelegramClient] = None


@app.on_event("startup")
async def startup_event():
    global tg_client
    logger.info("Initializing Telethon Client for 2GB MTProto operations...")
    try:
        session_path = str(WORKDIR / "bot_telethon_session")
        tg_client = TelegramClient(session_path, API_ID, API_HASH)
        await tg_client.start(bot_token=BOT_TOKEN)
        me = await tg_client.get_me()
        logger.info(f"Telethon bot successfully connected as @{me.username} ({me.id})")
    except Exception as e:
        logger.error(f"Failed to start Telethon client: {e}")


@app.on_event("shutdown")
async def shutdown_event():
    global tg_client
    if tg_client and tg_client.is_connected():
        await tg_client.disconnect()


class ProcessRequest(BaseModel):
    job_id: str
    chat_id: int
    status_message_id: Optional[int] = None
    media_message_id: Optional[int] = None
    tg_file_id: Optional[str] = None
    limit_minutes: float = 10.0
    custom_wm: str = ""
    source_url: str = ""
    file_name: str = "video.mp4"


def probe_video_duration(file_path: str) -> float:
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", file_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(res.stdout.strip())
    except Exception as e:
        logger.warning(f"ffprobe duration error: {e}, fallback to 5400s")
        return 5400.0


def probe_video_dimensions(file_path: str) -> tuple[int, int]:
    try:
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0", file_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        w, h = map(int, res.stdout.strip().split("x"))
        return w, h
    except Exception:
        return 854, 480


async def update_tg_status(chat_id: int, message_id: Optional[int], text: str):
    if not tg_client or not message_id:
        return
    try:
        await tg_client.edit_message(chat_id, message_id, text, parse_mode="html")
    except Exception as e:
        logger.debug(f"Could not edit message: {e}")


async def process_video_pipeline(req: ProcessRequest):
    job_id = req.job_id
    chat_id = req.chat_id
    msg_id = req.status_message_id
    limit_min = req.limit_minutes
    custom_wm = req.custom_wm.strip()

    job_dir = WORKDIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    input_file = job_dir / f"input_{req.file_name}"
    output_file = job_dir / f"cleaned_{req.file_name}"

    ACTIVE_JOBS[job_id] = {
        "status": "processing",
        "stage": "دانلود ویدیو",
        "progress": 5,
        "start_time": time.time(),
        "file_name": req.file_name,
    }

    try:
        # Step 1: Download the video
        await update_tg_status(
            chat_id, msg_id,
            "📥 <b>در حال دانلود ویدیو از سرورهای تلگرام...</b>\n<i>پشتیبانی کامل از فایل‌های بالای ۱ گیگابایت</i>"
        )

        last_progress_update = 0

        def download_progress(current, total):
            nonlocal last_progress_update
            now = time.time()
            if now - last_progress_update >= 3.0:
                pct = int((current / total) * 100) if total else 0
                ACTIVE_JOBS[job_id]["progress"] = int(5 + pct * 0.25)
                last_progress_update = now

        if tg_client:
            logger.info(f"Downloading from Telegram (Chat {chat_id}, MediaMsg {req.media_message_id}, StatusMsg {msg_id})...")
            try:
                media_msg = None
                candidate_ids = []

                if req.media_message_id:
                    candidate_ids.extend([req.media_message_id, req.media_message_id - 1, req.media_message_id + 1])
                if msg_id:
                    candidate_ids.extend([msg_id - 1, msg_id - 2, msg_id - 3, msg_id + 1])

                # Deduplicate while preserving order
                seen = set()
                clean_candidates = [x for x in candidate_ids if x > 0 and not (x in seen or seen.add(x))]
                logger.info(f"Searching for media message in candidate IDs: {clean_candidates}")

                # Priority 1: Fetch globally by message ID (No GetHistoryRequest, allowed for bot users)
                for cid in clean_candidates:
                    try:
                        m = await tg_client.get_messages(None, ids=cid)
                        if m and m.media:
                            media_msg = m
                            logger.info(f"Found media message globally with ID {cid} (Type: {type(m.media).__name__})")
                            break
                    except Exception as err:
                        logger.debug(f"Global lookup for msg {cid} failed: {err}")

                # Priority 2: Check reply-to from status message
                if not media_msg and msg_id:
                    try:
                        st_msg = await tg_client.get_messages(None, ids=msg_id)
                        if st_msg and st_msg.is_reply and st_msg.reply_to_msg_id:
                            m = await tg_client.get_messages(None, ids=st_msg.reply_to_msg_id)
                            if m and m.media:
                                media_msg = m
                                logger.info(f"Found media message via reply_to_msg_id ({st_msg.reply_to_msg_id})")
                    except Exception as err:
                        logger.debug(f"Reply-to lookup failed: {err}")

                # Priority 3: Lookup with chat_id for candidates
                if not media_msg:
                    for cid in clean_candidates:
                        try:
                            m = await tg_client.get_messages(chat_id, ids=cid)
                            if m and m.media:
                                media_msg = m
                                logger.info(f"Found media message with chat_id for ID {cid}")
                                break
                        except Exception:
                            pass

                if media_msg and media_msg.media:
                    await tg_client.download_media(media_msg, file=str(input_file), progress_callback=download_progress)
                else:
                    raise ValueError(f"پیام حاوی ویدیو در چت یافت نشد (شناسه‌های بررسی شده: {clean_candidates}). لطفاً مجدداً ویدیو را ارسال فرمایید.")
            except Exception as e:
                logger.warning(f"Direct media lookup failed ({e}), attempting fallback...")
                if req.source_url:
                    import requests
                    with requests.get(req.source_url, stream=True) as r:
                        r.raise_for_status()
                        with open(input_file, "wb") as f:
                            for chunk in r.iter_content(chunk_size=1024 * 1024):
                                f.write(chunk)
                else:
                    raise

        if not input_file.exists() or input_file.stat().st_size == 0:
            raise FileNotFoundError("ویدیو دانلود نشد یا فایل خالی است.")

        file_size_mb = input_file.stat().st_size / (1024 * 1024)
        logger.info(f"Input file ready: {input_file} ({file_size_mb:.1f} MB)")

        # Step 2: Probe Video
        duration_sec = probe_video_duration(str(input_file))
        width, height = probe_video_dimensions(str(input_file))
        logger.info(f"Video info: {width}x{height}, duration: {duration_sec:.1f}s")

        # Step 3: Determine Splicing & Filters
        splice_sec = min(duration_sec, limit_min * 60.0) if limit_min > 0 else duration_sec

        await update_tg_status(
            chat_id, msg_id,
            f"🟣 <b>در حال اعمال افکت بلور و درج واترمارک بنفش...</b>\n"
            f"⏱ <b>بازه:</b> تا دقیقه {limit_min if limit_min > 0 else 'انتهای ویدیو'}\n"
            f"📊 <b>ابعاد:</b> <code>{width}x{height}</code> | حجم: <code>{file_size_mb:.1f} MB</code>\n"
            f"✨ <i>مابقی ویدیو بدون کوچکترین تغییر و با سرعت فوق‌العاده متصل می‌شود.</i>"
        )
        ACTIVE_JOBS[job_id]["stage"] = "بلور و درج واترمارک بنفش"
        ACTIVE_JOBS[job_id]["progress"] = 35

        # Watermark region: Bottom 30% of frame, center 70% width
        wm_x = int(width * 0.16)
        wm_y = int(height * 0.68)
        wm_w = int(width * 0.68)
        wm_h = int(height * 0.28)

        # Detect font path
        font_candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]
        font_file = next((f for f in font_candidates if os.path.exists(f)), "")
        font_param = ""
        if font_file:
            escaped_font_path = font_file.replace(":", "\\:")
            font_param = f":fontfile='{escaped_font_path}'"

        font_size = max(24, int(height * 0.065))
        safe_text = custom_wm.replace(":", r"\:").replace("'", "").replace("%", r"\%")

        # Filter Complex:
        # 1. Crop watermark area and apply heavy smooth Gaussian boxblur (boxblur=24:6)
        # 2. Overlay blurred box over original video
        # 3. Draw user's custom watermark with vibrant electric purple color (0xA855F7) and crisp border (if provided)
        if custom_wm:
            filter_complex = (
                f"[0:v] split [main][to_blur]; "
                f"[to_blur] crop={wm_w}:{wm_h}:{wm_x}:{wm_y}, boxblur=24:6 [blurred]; "
                f"[main][blurred] overlay={wm_x}:{wm_y} [bg]; "
                f"[bg] drawtext=text='{safe_text}'{font_param}:x=(w-text_w)/2:y={wm_y}+({wm_h}-text_h)/2:"
                f"fontsize={font_size}:fontcolor=0xA855F7:shadowcolor=black@0.9:shadowx=2:shadowy=2:borderw=1.5:bordercolor=black@0.7"
            )
        else:
            filter_complex = (
                f"[0:v] split [main][to_blur]; "
                f"[to_blur] crop={wm_w}:{wm_h}:{wm_x}:{wm_y}, boxblur=24:6 [blurred]; "
                f"[main][blurred] overlay={wm_x}:{wm_y}"
            )

        # If processing whole video
        if limit_min <= 0 or splice_sec >= duration_sec - 10:
            logger.info("Processing entire video with blur + purple watermark...")
            cmd = [
                "ffmpeg", "-y", "-i", str(input_file),
                "-filter_complex", filter_complex,
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-c:a", "copy",
                str(output_file)
            ]
            subprocess.run(cmd, check=True)
        else:
            # Segment A: 0 to splice_sec with blur + purple overlay
            part_a = job_dir / "part_a.mp4"
            part_b = job_dir / "part_b.mp4"
            list_txt = job_dir / "concat_list.txt"

            logger.info(f"Encoding Part A (0s to {splice_sec}s)...")
            cmd_a = [
                "ffmpeg", "-y", "-i", str(input_file),
                "-t", str(splice_sec),
                "-filter_complex", filter_complex,
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-c:a", "copy",
                str(part_a)
            ]
            subprocess.run(cmd_a, check=True)

            logger.info(f"Lossless Stream Copying Part B ({splice_sec}s to end)...")
            cmd_b = [
                "ffmpeg", "-y", "-ss", str(splice_sec),
                "-i", str(input_file),
                "-c", "copy",
                str(part_b)
            ]
            subprocess.run(cmd_b, check=True)

            # Concat demuxer
            with open(list_txt, "w", encoding="utf-8") as f:
                f.write(f"file '{part_a.resolve().as_posix()}'\n")
                f.write(f"file '{part_b.resolve().as_posix()}'\n")

            logger.info("Concatenating Part A and Part B...")
            cmd_concat = [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", str(list_txt),
                "-c", "copy",
                str(output_file)
            ]
            subprocess.run(cmd_concat, check=True)

        if not output_file.exists():
            raise RuntimeError("خطا در ایجاد فایل خروجی نهایی.")

        out_size_mb = output_file.stat().st_size / (1024 * 1024)
        logger.info(f"Output video created: {output_file} ({out_size_mb:.1f} MB)")

        # Step 4: Upload Cleaned Video to Telegram Chat (Up to 2 GB)
        await update_tg_status(
            chat_id, msg_id,
            f"📤 <b>در حال ارسال مستقیم ویدیوی جدید به چت ({out_size_mb:.1f} MB)...</b>\n"
            f"<i>واترمارک قبلی بلور شد و واترمارک بنفش شما قرار گرفت.</i>"
        )
        ACTIVE_JOBS[job_id]["stage"] = "ارسال به تلگرام"
        ACTIVE_JOBS[job_id]["progress"] = 80

        last_up_update = 0

        def upload_progress(current, total):
            nonlocal last_up_update
            now = time.time()
            if now - last_up_update >= 3.0:
                pct = int((current / total) * 100) if total else 0
                ACTIVE_JOBS[job_id]["progress"] = int(80 + pct * 0.18)
                last_up_update = now

        caption_text = (
            f"✅ <b>ویدیوی شما آماده شد!</b>\n\n"
            f"📁 <b>فایل:</b> <code>{req.file_name}</code>\n"
            f"⏱ <b>بازه تغییر:</b> تا دقیقه {limit_min if limit_min > 0 else 'کل'}\n"
            f"✨ <b>افکت:</b> بلور ملایم روی واترمارک قبلی\n"
            f"🟣 <b>واترمارک جدید:</b> <code>{custom_wm if custom_wm else 'بدون متن'}</code> (رنگ بنفش)\n"
            f"🔊 <b>صدا و تصویر مابقی ویدیو:</b> ۱۰۰٪ دست‌نخورده و بدون افت کیفیت"
        )

        if tg_client:
            await tg_client.send_file(
                chat_id,
                file=str(output_file),
                caption=caption_text,
                parse_mode="html",
                supports_streaming=True,
                progress_callback=upload_progress,
                attributes=[
                    DocumentAttributeVideo(
                        duration=int(duration_sec),
                        w=width,
                        h=height,
                        supports_streaming=True
                    ),
                    DocumentAttributeFilename(file_name=f"blurred_{req.file_name}")
                ]
            )

            # Delete the status message
            if msg_id:
                try:
                    await tg_client.delete_messages(chat_id, msg_id)
                except Exception:
                    pass

        ACTIVE_JOBS[job_id]["status"] = "completed"
        ACTIVE_JOBS[job_id]["stage"] = "تکمیل شد"
        ACTIVE_JOBS[job_id]["progress"] = 100
        ACTIVE_JOBS[job_id]["output_path"] = str(output_file)

        logger.info(f"Job {job_id} successfully completed!")

    except Exception as err:
        logger.error(f"Error in processing pipeline: {err}", exc_info=True)
        ACTIVE_JOBS[job_id]["status"] = "failed"
        ACTIVE_JOBS[job_id]["error"] = str(err)
        if tg_client and chat_id:
            await tg_client.send_message(
                chat_id,
                f"❌ <b>خطا در پردازش ویدیو:</b>\n<code>{err}</code>",
                parse_mode="html"
            )


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    total_jobs = len(ACTIVE_JOBS)
    completed = sum(1 for j in ACTIVE_JOBS.values() if j.get("status") == "completed")
    active = sum(1 for j in ACTIVE_JOBS.values() if j.get("status") == "processing")

    return f"""<!DOCTYPE html>
<html lang="fa" dir="rtl">
<head>
  <meta charset="UTF-8">
  <title>Watermark Blur & Purple Engine</title>
  <style>
    body {{ background: #0b0f19; color: #f1f5f9; font-family: Tahoma, sans-serif; padding: 30px; text-align: center; }}
    .card {{ background: #1e293b; border-radius: 16px; padding: 24px; max-width: 600px; margin: 0 auto; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
    h1 {{ color: #a855f7; margin-bottom: 8px; }}
    .badge {{ background: #9333ea; color: #fff; padding: 4px 12px; border-radius: 99px; font-size: 13px; font-weight: bold; }}
    .stats {{ display: flex; justify-content: space-around; margin: 20px 0; }}
    .stat-val {{ font-size: 24px; font-weight: bold; color: #c084fc; }}
  </style>
</head>
<body>
  <div class="card">
    <span class="badge">● آنلاین و فعال</span>
    <h1>موتور بلور و واترمارک بنفش (هوشمند)</h1>
    <p>متصل به Cloudflare Workers برای ویدیوهای بالای ۱ گیگابایت</p>
    <div class="stats">
      <div><div class="stat-val">{active}</div><div>در حال پردازش</div></div>
      <div><div class="stat-val">{completed}</div><div>تکمیل شده</div></div>
      <div><div class="stat-val">{total_jobs}</div><div>کل درخواست‌ها</div></div>
    </div>
  </div>
</body>
</html>"""


@app.get("/health")
async def health_check():
    """Keep-alive ping endpoint for Cloudflare Workers Cron Trigger"""
    return {
        "status": "alive",
        "service": "Watermark Blur & Purple Engine",
        "mode": "blur_plus_purple_overlay",
        "timestamp": time.time(),
        "active_jobs": sum(1 for j in ACTIVE_JOBS.values() if j.get("status") == "processing"),
        "telegram_connected": bool(tg_client and tg_client.is_connected())
    }


@app.post("/api/process")
async def start_process(req: ProcessRequest, background_tasks: BackgroundTasks):
    logger.info(f"Received process request: job={req.job_id}, limit={req.limit_minutes}m, wm='{req.custom_wm}'")
    background_tasks.add_task(process_video_pipeline, req)
    return {
        "ok": True,
        "job_id": req.job_id,
        "status": "queued",
        "message": "Blur & Purple watermark process initiated."
    }


@app.get("/download/{job_id}")
async def download_file(job_id: str):
    job = ACTIVE_JOBS.get(job_id)
    if not job or "output_path" not in job:
        raise HTTPException(status_code=404, detail="فایل یافت نشد یا هنوز آماده نشده است.")

    file_path = job["output_path"]
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="فایل از حافظه موقت پاک شده است.")

    return FileResponse(
        file_path,
        media_type="video/mp4",
        filename=f"blurred_{job.get('file_name', 'video.mp4')}"
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=7860, reload=False)
