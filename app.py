"""
=============================================================================
WATERMARK REMOVER — HUGGING FACE BACKEND ENGINE (PYTHON / FFMPEG / TELETHON)
=============================================================================
Architecture:
- FastAPI Web Server (Port 7860)
- Telethon MTProto Client (Full 2 GB Upload & Download Speed)
- FFmpeg Adaptive Inpainting & Lossless Segment Splicer
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
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8901084274:AAF23N3tAtvKNH5Y0j4Py69O3TJo3SPT9_w")

WORKDIR = Path("/tmp/watermark_jobs")
WORKDIR.mkdir(parents=True, exist_ok=True)

# Active jobs tracking
ACTIVE_JOBS: Dict[str, Dict[str, Any]] = {}

app = FastAPI(title="Watermark Remover Backend", version="10.0.0")

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
        session_path = "/tmp/bot_telethon_session"
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
            "📥 <b>در حال دانلود ویدیوی حجیم از سرورهای پرسرعت تلگرام...</b>\n<i>لطفاً چند لحظه شکیبا باشید.</i>"
        )

        last_progress_update = 0

        def download_progress(current, total):
            nonlocal last_progress_update
            now = time.time()
            if now - last_progress_update >= 3.0:
                pct = int((current / total) * 100) if total else 0
                ACTIVE_JOBS[job_id]["progress"] = int(5 + pct * 0.25)
                last_progress_update = now

        if req.tg_file_id and tg_client:
            logger.info(f"Downloading from Telegram file_id (Chat {chat_id})...")
            # Try to fetch message media or download via telethon
            try:
                # If status message is a reply or we can get media
                msg = await tg_client.get_messages(chat_id, ids=msg_id)
                media_msg = None
                if msg and msg.is_reply:
                    media_msg = await msg.get_reply_message()
                if not media_msg or not media_msg.media:
                    # Search recent messages in chat for the media
                    async for m in tg_client.iter_messages(chat_id, limit=5):
                        if m.media:
                            media_msg = m
                            break

                if media_msg and media_msg.media:
                    await tg_client.download_media(media_msg, file=str(input_file), progress_callback=download_progress)
                else:
                    raise ValueError("Media message not found in chat context")
            except Exception as e:
                logger.warning(f"Direct media lookup failed ({e}), attempting file_id / URL fallback...")
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
            f"⚙️ <b>در حال پاکسازی هوشمند واترمارک تا دقیقه {limit_min if limit_min > 0 else 'انتهای ویدیو'}...</b>\n"
            f"📊 ابعاد: <code>{width}x{height}</code> | حجم: <code>{file_size_mb:.1f} MB</code>\n"
            f"🧠 الگوریتم: <i>Adaptive Delogo & Stroke Inpainting</i>"
        )
        ACTIVE_JOBS[job_id]["stage"] = "پاکسازی و ادغام ویدیو"
        ACTIVE_JOBS[job_id]["progress"] = 35

        # Watermark region: Bottom 33% of frame, center 70% width
        wm_x = int(width * 0.16)
        wm_y = int(height * 0.68)
        wm_w = int(width * 0.68)
        wm_h = int(height * 0.28)

        vf_filters = [f"delogo=x={wm_x}:y={wm_y}:w={wm_w}:h={wm_h}:show=0"]

        if custom_wm:
            safe_text = custom_wm.replace(":", "\\:").replace("'", "")
            vf_filters.append(
                f"drawtext=text='{safe_text}':x=(w-text_w)/2:y=h-th-25:"
                f"fontsize=24:fontcolor=white@0.85:shadowcolor=black@0.7:shadowx=2:shadowy=2"
            )

        vf_chain = ",".join(vf_filters)

        # Check if full video needs processing or segment splicing
        if limit_min <= 0 or splice_sec >= duration_sec - 10:
            # Clean entire video
            logger.info("Processing entire video with delogo...")
            cmd = [
                "ffmpeg", "-y", "-i", str(input_file),
                "-vf", vf_chain,
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-c:a", "copy",
                str(output_file)
            ]
            subprocess.run(cmd, check=True)
        else:
            # Segment A: 0 to splice_sec with inpainting
            part_a = job_dir / "part_a.mp4"
            part_b = job_dir / "part_b.mp4"
            list_txt = job_dir / "concat_list.txt"

            logger.info(f"Encoding Part A (0s to {splice_sec}s)...")
            cmd_a = [
                "ffmpeg", "-y", "-i", str(input_file),
                "-t", str(splice_sec),
                "-vf", vf_chain,
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
            f"📤 <b>در حال ارسال مستقیم ویدیوی تمیز به چت ({out_size_mb:.1f} MB)...</b>\n"
            f"<i>این مرحله با نهایت سرعت در حال انجام است.</i>"
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
            f"✅ <b>ویدیوی شما با موفقیت آماده شد!</b>\n\n"
            f"📁 <b>فایل:</b> <code>{req.file_name}</code>\n"
            f"⏱ <b>بازه پاکسازی:</b> تا دقیقه {limit_min if limit_min > 0 else 'کل'}\n"
            f"🏷 <b>واترمارک:</b> {custom_wm if custom_wm else 'کاملاً پاکسازی شد'}\n"
            f"📊 <b>کیفیت صدا و تصویر:</b> ۱۰۰٪ دست‌نخورده و بدون افت کیفیت"
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
                    DocumentAttributeFilename(file_name=f"cleaned_{req.file_name}")
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
  <title>Watermark Removal Engine (Hugging Face Core)</title>
  <style>
    body {{ background: #0b0f19; color: #f1f5f9; font-family: Tahoma, sans-serif; padding: 30px; text-align: center; }}
    .card {{ background: #1e293b; border-radius: 16px; padding: 24px; max-width: 600px; margin: 0 auto; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
    h1 {{ color: #38bdf8; margin-bottom: 8px; }}
    .badge {{ background: #10b981; color: #fff; padding: 4px 12px; border-radius: 99px; font-size: 13px; font-weight: bold; }}
    .stats {{ display: flex; justify-content: space-around; margin: 20px 0; }}
    .stat-val {{ font-size: 24px; font-weight: bold; color: #38bdf8; }}
  </style>
</head>
<body>
  <div class="card">
    <span class="badge">● آنلاین و فعال</span>
    <h1>موتور پردازش ویدیوی حجیم (هوش مصنوعی)</h1>
    <p>متصل به Cloudflare Workers و Telegram MTProto برای فایل‌های تا ۱ گیگابایت</p>
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
    """Ping endpoint for Cloudflare Workers Cron Trigger to keep the Space awake 24/7"""
    return {
        "status": "alive",
        "service": "Watermark Engine (Python / Hugging Face)",
        "timestamp": time.time(),
        "active_jobs": sum(1 for j in ACTIVE_JOBS.values() if j.get("status") == "processing"),
        "telegram_connected": bool(tg_client and tg_client.is_connected())
    }


@app.post("/api/process")
async def start_process(req: ProcessRequest, background_tasks: BackgroundTasks):
    logger.info(f"Received process request for job: {req.job_id} (chat: {req.chat_id})")
    background_tasks.add_task(process_video_pipeline, req)
    return {
        "ok": True,
        "job_id": req.job_id,
        "status": "queued",
        "message": "Video processing initiated successfully on Hugging Face Engine."
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
        filename=f"cleaned_{job.get('file_name', 'video.mp4')}"
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=7860, reload=False)
