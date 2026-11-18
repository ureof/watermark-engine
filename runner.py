"""
=============================================================================
GITHUB ACTIONS SERVERLESS VIDEO PROCESSOR ENGINE
=============================================================================
Runs on GitHub Actions Ubuntu Runner with:
- 7 GB RAM
- 2 Fast vCPUs
- 1 Gbps direct Telegram DC connection
=============================================================================
"""

import os
import sys
import json
import time
import asyncio
import logging
import subprocess
from pathlib import Path
from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeFilename

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("GitHubActionRunner")

# Telegram Credentials
API_ID = int(os.environ.get("TELEGRAM_API_ID", "35537842"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "d97500b2596363b5ed28dc12af62ede8")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8901084274:AAGsXxNfC3zsVsHzbg_gsBQLUGFA3kn_8Ts")

# Working directory
WORKDIR = Path("/tmp/video_job") if os.name != "nt" else Path("C:/tmp/video_job")
WORKDIR.mkdir(parents=True, exist_ok=True)
SESSION_PATH = str(WORKDIR / "runner_session")


def probe_video_duration(file_path: str) -> float:
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", file_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(res.stdout.strip())
    except Exception as e:
        logger.warning(f"Duration probe error: {e}")
        return 0.0


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


async def update_tg_status(client: TelegramClient, chat_id: int, message_id: int, text: str):
    if not message_id or not chat_id:
        return
    try:
        await client.edit_message(chat_id, message_id, text, parse_mode="html")
    except Exception as e:
        logger.debug(f"Edit message failed: {e}")


async def main():
    # 1. Parse client payload from GitHub Event
    payload_str = os.environ.get("CLIENT_PAYLOAD", "{}")
    logger.info(f"Raw Client Payload: {payload_str}")
    try:
        payload = json.loads(payload_str)
    except Exception:
        # Fallback: check GITHUB_EVENT_PATH
        event_path = os.environ.get("GITHUB_EVENT_PATH")
        if event_path and os.path.exists(event_path):
            with open(event_path, "r", encoding="utf-8") as f:
                event_data = json.load(f)
                payload = event_data.get("client_payload", {})
        else:
            payload = {}

    job_id = payload.get("job_id", f"job_{int(time.time())}")
    chat_id = int(payload.get("chat_id", 0))
    msg_id = int(payload.get("status_message_id", 0)) or None
    media_msg_id = int(payload.get("media_message_id", 0)) or None
    limit_min = float(payload.get("limit_minutes", 10.0))
    custom_wm = str(payload.get("custom_wm", "")).strip()
    file_name = payload.get("file_name", "educational_video.mp4")

    if not chat_id:
        logger.error("Missing chat_id! Exiting.")
        sys.exit(1)

    logger.info(f"Starting Job {job_id} for Chat {chat_id} (Media: {media_msg_id}, Limit: {limit_min}m, WM: '{custom_wm}')")

    # 2. Connect Telethon Client
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    logger.info(f"Connected as @{me.username} ({me.id}) on GitHub Actions 7GB Cloud Runner!")

    input_file = WORKDIR / f"input_{file_name}"
    output_file = WORKDIR / f"cleaned_{file_name}"

    try:
        # 3. Locate media message
        candidate_ids = []
        if media_msg_id:
            candidate_ids.extend([media_msg_id, media_msg_id - 1, media_msg_id + 1])
        if msg_id:
            candidate_ids.extend([msg_id - 1, msg_id - 2, msg_id - 3])

        seen = set()
        clean_candidates = [x for x in candidate_ids if x > 0 and not (x in seen or seen.add(x))]
        logger.info(f"Looking up media in candidates: {clean_candidates}")

        # Resolve peer entity first if possible
        peer = None
        try:
            peer = await client.get_input_entity(chat_id)
        except Exception:
            try:
                peer = await client.get_entity(chat_id)
            except Exception:
                peer = chat_id

        media_msg = None
        for cid in clean_candidates:
            try:
                m = await client.get_messages(peer, ids=cid)
                if m and m.media:
                    media_msg = m
                    logger.info(f"Found media in Message ID {cid} via peer (Type: {type(m.media).__name__})")
                    break
            except Exception as e:
                logger.debug(f"Candidate {cid} via peer failed: {e}")

        if not media_msg:
            for cid in clean_candidates:
                try:
                    m = await client.get_messages(None, ids=cid)
                    if m and m.media:
                        media_msg = m
                        logger.info(f"Found media in Message ID {cid} via global lookup")
                        break
                except Exception:
                    pass

        if not media_msg or not media_msg.media:
            raise ValueError(f"پیام حاوی ویدیو در چت یافت نشد (شناسه‌های بررسی شده: {clean_candidates}). لطفاً مجدداً ویدیو را ارسال فرمایید.")

        # 4. Download video with live Telegram progress
        last_dl_ui = 0

        async def dl_ui(current, total):
            nonlocal last_dl_ui
            now = time.time()
            if now - last_dl_ui >= 3.0:
                pct = int((current / total) * 100) if total else 0
                cur_mb = current / (1024 * 1024)
                tot_mb = total / (1024 * 1024)
                text = (
                    f"📥 <b>در حال دانلود پرسرعت با سرور اختصاصی گیت‌هاب (۷ گیگ رم)...</b>\n"
                    f"📊 پیشرفت: <b>{pct}%</b> (<code>{cur_mb:.1f} MB</code> از <code>{tot_mb:.1f} MB</code>)\n"
                    f"⚡️ <i>سرعت دانلود بسیار بالا و بدون فریز</i>"
                )
                await update_tg_status(client, chat_id, msg_id, text)
                last_dl_ui = now

        def sync_dl_ui(c, t):
            asyncio.create_task(dl_ui(c, t))

        logger.info("Starting media download...")
        await client.download_media(media_msg, file=str(input_file), progress_callback=sync_dl_ui)

        if not input_file.exists() or input_file.stat().st_size == 0:
            raise FileNotFoundError("دانلود ناموفق بود یا فایل خالی است.")

        file_mb = input_file.stat().st_size / (1024 * 1024)
        logger.info(f"Downloaded file: {input_file} ({file_mb:.1f} MB)")

        # 5. Probe video
        duration_sec = probe_video_duration(str(input_file))
        width, height = probe_video_dimensions(str(input_file))
        splice_sec = min(duration_sec, limit_min * 60.0) if limit_min > 0 else duration_sec

        await update_tg_status(
            client, chat_id, msg_id,
            f"🟣 <b>در حال اعمال افکت بلور گوسی و درج واترمارک بنفش...</b>\n"
            f"⏱ <b>بازه تغییر:</b> تا دقیقه {int(limit_min) if limit_min > 0 else 'کل ویدیو'}\n"
            f"📊 <b>ابعاد:</b> <code>{width}x{height}</code> | حجم: <code>{file_mb:.1f} MB</code>\n"
            f"✨ <i>مابقی ویدیو بدون تغییر و با نهایت کیفیت متصل می‌شود.</i>"
        )

        # Bottom ~30% area - ensure even dimensions for FFmpeg YUV420p
        wm_x = int(width * 0.16) & ~1
        wm_y = int(height * 0.68) & ~1
        wm_w = int(width * 0.68) & ~1
        wm_h = int(height * 0.28) & ~1

        # Font resolution (support Persian and English)
        font_candidates = [
            "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
            "/usr/share/fonts/truetype/freefarsi/Nazli.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        font_file = ""
        for fc in font_candidates:
            if os.path.exists(fc):
                font_file = fc
                break
        font_param = f":fontfile='{font_file}'" if font_file else ""

        font_size = max(24, int(height * 0.065))
        safe_text = custom_wm.replace(":", r"\:").replace("'", "").replace("%", r"\%")

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

        # 6. FFmpeg Processing
        if limit_min <= 0 or splice_sec >= duration_sec - 10:
            logger.info("Processing entire video...")
            cmd = [
                "ffmpeg", "-y", "-threads", "2", "-i", str(input_file),
                "-filter_complex", filter_complex,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-c:a", "copy",
                str(output_file)
            ]
            subprocess.run(cmd, check=True)
        else:
            part_a = WORKDIR / "part_a.mp4"
            part_b = WORKDIR / "part_b.mp4"
            list_txt = WORKDIR / "concat_list.txt"

            logger.info(f"Encoding Part A (0 to {splice_sec}s)...")
            cmd_a = [
                "ffmpeg", "-y", "-threads", "2", "-i", str(input_file),
                "-t", str(splice_sec),
                "-filter_complex", filter_complex,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
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
            raise RuntimeError("خطا در ایجاد فایل خروجی FFmpeg.")

        out_mb = output_file.stat().st_size / (1024 * 1024)

        # 7. Upload to Telegram with live progress
        await update_tg_status(
            client, chat_id, msg_id,
            f"📤 <b>در حال ارسال ویدیوی جدید به چت ({out_mb:.1f} MB)...</b>\n"
            f"<i>واترمارک قبلی بلور شد و واترمارک بنفش شما قرار گرفت.</i>"
        )

        last_up_ui = 0

        async def up_ui(current, total):
            nonlocal last_up_ui
            now = time.time()
            if now - last_up_ui >= 3.0:
                pct = int((current / total) * 100) if total else 0
                c_mb = current / (1024 * 1024)
                t_mb = total / (1024 * 1024)
                text = (
                    f"📤 <b>در حال ارسال ویدیوی نهایی به چت...</b>\n"
                    f"📊 پیشرفت: <b>{pct}%</b> (<code>{c_mb:.1f} MB</code> از <code>{t_mb:.1f} MB</code>)"
                )
                await update_tg_status(client, chat_id, msg_id, text)
                last_up_ui = now

        def sync_up_ui(c, t):
            asyncio.create_task(up_ui(c, t))

        caption = (
            f"✅ <b>ویدیوی شما با موفقیت آماده شد!</b>\n\n"
            f"📁 <b>فایل:</b> <code>{file_name}</code>\n"
            f"⏱ <b>بازه تغییر:</b> {int(limit_min) if limit_min > 0 else 'کل'} دقیقه اول\n"
            f"✨ <b>افکت:</b> بلور گوسی ملایم روی واترمارک قبلی\n"
            f"🟣 <b>واترمارک جدید:</b> <code>{custom_wm if custom_wm else 'بدون متن'}</code>\n"
            f"🔊 <b>صدا و تصویر مابقی ویدیو:</b> بدون افت کیفیت و هماهنگ"
        )

        await client.send_file(
            chat_id,
            file=str(output_file),
            caption=caption,
            parse_mode="html",
            supports_streaming=True,
            progress_callback=sync_up_ui,
            attributes=[
                DocumentAttributeVideo(
                    duration=int(duration_sec),
                    w=width,
                    h=height,
                    supports_streaming=True
                ),
                DocumentAttributeFilename(file_name=f"blurred_{file_name}")
            ]
        )

        if msg_id:
            try:
                await client.delete_messages(chat_id, msg_id)
            except Exception:
                pass

        logger.info(f"Job {job_id} successfully completed on GitHub Actions runner!")

    except Exception as err:
        logger.error(f"Error in GitHub Actions Runner: {err}", exc_info=True)
        if msg_id:
            await update_tg_status(
                client, chat_id, msg_id,
                f"❌ <b>خطا در پردازش ویدیو:</b>\n<code>{err}</code>"
            )

    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
