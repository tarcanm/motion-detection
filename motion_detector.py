#!/usr/bin/env python3
"""
Motion Detector v2 — Frame-diff algılama + 10s video + ses + Telegram.
Reads config from /opt/data/.motion_config.json (auto-created with defaults).

Signals:
  SIGTERM — Graceful shutdown
  SIGUSR1 — Toggle sensitivity (normal ↔ sensitive)

Config (.motion_config.json):
  enabled: true/false
  sensitivity: 1-10 (5=default)
  min_interval_sec: 10
  resolution_width: 640
  resolution_height: 480
  alert_cooldown_sec: 15
"""
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ─── Paths ─────────────────────────────────────────────────────
CONFIG_PATH = Path("/opt/data/.motion_config.json")
PID_PATH = Path("/tmp/motion_detector.pid")
LOG_PATH = Path("/tmp/motion_detector.log")
FRAME_DIR = Path("/tmp/motion_frames")

running = True
detection_count = 0
last_alert_time = 0

VIDEO_DURATION = 10      # seconds
PRE_BLUR = 21            # GaussianBlur kernel size

# Telegram
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '8588379897')

# Try to load .env for background processes that don't inherit Hermes env
_env_path = Path("/opt/data/.env")
if _env_path.exists() and not os.environ.get('TELEGRAM_BOT_TOKEN'):
    try:
        for _line in _env_path.read_text().splitlines():
            _line = _line.strip()
            if _line.startswith('TELEGRAM_BOT_TOKEN='):
                _val = _line.split('=', 1)[1].strip().strip('"').strip("'")
                if _val:
                    os.environ['TELEGRAM_BOT_TOKEN'] = _val
                    break
    except Exception:
        pass


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] {msg}"
    print(entry, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(entry + "\n")


def load_config() -> dict:
    defaults = {
        "enabled": True,
        "sensitivity": 5,
        "min_interval_sec": 10,
        "resolution_width": 640,
        "resolution_height": 480,
        "alert_cooldown_sec": 15,
    }
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            defaults.update(cfg)
        except Exception:
            log("⚠ Config parse error, using defaults")
    # Write back merged config
    CONFIG_PATH.write_text(json.dumps(defaults, indent=2))
    return defaults


def get_motion_pixels(level: int) -> int:
    """Sensitivity 1-10 → threshold in pixels (lower = more sensitive)."""
    levels = {1: 10000, 2: 7000, 3: 5000, 4: 4000, 5: 3000,
              6: 2000, 7: 1500, 8: 1000, 9: 700, 10: 500}
    return levels.get(level, 3000)


def send_telegram(msg, media_path=None):
    """Send text + optional file to Telegram via Bot API."""
    bot = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    if not bot:
        log("⚠ No TELEGRAM_BOT_TOKEN")
        return

    # Send text
    try:
        subprocess.run(
            ["curl", "-s", "-X", "POST",
             f"https://api.telegram.org/bot{bot}/sendMessage",
             "-d", f"chat_id={CHAT_ID}",
             "-d", f"text={msg}",
             "-d", "parse_mode=Markdown",
             "--max-time", "10"],
            capture_output=True
        )
    except Exception as e:
        log(f"⚠ TG text failed: {e}")

    # Send media
    if media_path and Path(media_path).exists():
        try:
            subprocess.run(
                ["curl", "-s", "-X", "POST",
                 f"https://api.telegram.org/bot{bot}/sendVideo",
                 "-F", f"chat_id={CHAT_ID}",
                 "-F", f"video=@{media_path}",
                 "-F", "supports_streaming=True",
                 "--max-time", "60"],
                capture_output=True
            )
            Path(media_path).unlink(missing_ok=True)
            log(f"  TG video sent: {Path(media_path).name}")
        except Exception as e:
            log(f"⚠ TG video failed: {e}")


def capture_media(ts: str) -> dict:
    """
    Capture 10s video (with audio) + snapshot via ffmpeg.
    Works without OpenCV — grabs directly from /dev/video0 + hw:1,0.
    """
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    paths = {"snapshot": "", "video": ""}

    snapshot_path = str(FRAME_DIR / f"snap_{ts}.jpg")
    video_path = str(FRAME_DIR / f"clip_{ts}.mp4")

    # Snapshot (1 frame, full res MJPEG)
    try:
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", "1280x720",
            "-i", "/dev/video0",
            "-vframes", "1", "-q:v", "2",
            snapshot_path
        ], capture_output=True, timeout=10)
        if Path(snapshot_path).stat().st_size > 1000:
            paths["snapshot"] = snapshot_path
            log(f"  Snapshot: {Path(snapshot_path).name}")
    except Exception as e:
        log(f"⚠ Snapshot failed: {e}")

    # Video (10s, 640x480, H264 + AAC audio from C922 mic)
    try:
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "v4l2", "-input_format", "mjpeg",
            "-video_size", "640x480", "-framerate", "15",
            "-i", "/dev/video0",
            "-f", "alsa", "-i", "hw:1,0",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
            "-c:a", "aac", "-b:a", "64k",
            "-t", str(VIDEO_DURATION),
            "-movflags", "+faststart",
            video_path
        ], capture_output=True, timeout=VIDEO_DURATION + 10)
        if Path(video_path).stat().st_size > 10000:
            paths["video"] = video_path
            log(f"  Video: {Path(video_path).name}")
    except Exception as e:
        log(f"⚠ Video failed: {e}")

    return paths


def signal_handler(signum, frame):
    global running
    if signum == signal.SIGTERM:
        log("⏹ SIGTERM, shutting down...")
        running = False
    elif signum == signal.SIGUSR1:
        cfg = load_config()
        new = 8 if cfg.get("sensitivity", 5) < 6 else 3
        cfg["sensitivity"] = new
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        log(f"🔀 Sensitivity toggled to {new}/10")


def detect_motion():
    """Frame-diff detection loop. On motion: ffmpeg capture + Telegram."""
    global running, detection_count, last_alert_time

    cfg = load_config()
    width = cfg["resolution_width"]
    height = cfg["resolution_height"]
    sensitivity = cfg["sensitivity"]
    cooldown = cfg["alert_cooldown_sec"]
    threshold = get_motion_pixels(sensitivity)

    log(f"🚀 Motion Detector v2 (frame-diff)")
    log(f"   Resolution: {width}x{height}")
    log(f"   Sensitivity: {sensitivity}/10 ({threshold}px)")
    log(f"   Cooldown: {cooldown}s | Video: {VIDEO_DURATION}s")

    try:
        import cv2
    except ImportError:
        log("❌ OpenCV not installed")
        return

    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, 15)

    if not cap.isOpened():
        log("❌ Cannot open /dev/video0")
        return

    ret, prev_frame = cap.read()
    if not ret:
        log("❌ Cannot read first frame")
        cap.release()
        return

    prev_gray = cv2.cvtColor(prev_frame, cv2.COLOR_BGR2GRAY)
    prev_gray = cv2.GaussianBlur(prev_gray, (PRE_BLUR, PRE_BLUR), 0)
    frame_count = 0

    log("✅ Monitoring...")

    while running:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.5)
            continue

        frame_count += 1
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (PRE_BLUR, PRE_BLUR), 0)

        # Frame difference
        diff = cv2.absdiff(prev_gray, gray)
        _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        thresh = cv2.dilate(thresh, None, iterations=2)
        changed = cv2.countNonZero(thresh)

        if changed > threshold:
            now = time.time()
            if now - last_alert_time > cooldown:
                detection_count += 1
                last_alert_time = now
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")

                log(f"📸 Motion #{detection_count} ({changed}px) [{ts}]")

                # Release camera so ffmpeg can use /dev/video0
                cap.release()

                # Capture snapshot + video+audio via ffmpeg
                media = capture_media(ts)

                # Send to Telegram
                ts_human = datetime.now().strftime("%d.%m.%Y %H:%M")
                caption = (
                    f"🚨 *Hareket Algılandı!*\n"
                    f"├ Zaman: `{ts_human}`\n"
                    f"├ Piksel: `{changed}px`\n"
                    f"└ Duyarlılık: {sensitivity}/10"
                )

                # Send snapshot first
                if media["snapshot"]:
                    send_telegram(caption, media["snapshot"])
                    time.sleep(1)

                # Send video (has audio embedded)
                if media["video"]:
                    send_telegram("🎥 10sn kayıt", media["video"])

                log(f"  ✅ Done for motion #{detection_count}")

                # Re-open camera
                cap = cv2.VideoCapture(0)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                cap.set(cv2.CAP_PROP_FPS, 15)
                if not cap.isOpened():
                    log("❌ Failed to re-open camera")
                    running = False
                    return

                # Read first frame for new reference
                ret, prev_frame = cap.read()
                if ret:
                    prev_gray = cv2.cvtColor(prev_frame, cv2.COLOR_BGR2GRAY)
                    prev_gray = cv2.GaussianBlur(prev_gray, (PRE_BLUR, PRE_BLUR), 0)
                    frame_count = 0

                continue

        prev_gray = gray

        if frame_count % 500 == 0:
            log(f"❤ Frames:{frame_count} Detections:{detection_count}")

        time.sleep(0.03)

    cap.release()
    log("⏹ Stopped")


def main():
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGUSR1, signal_handler)

    FRAME_DIR.mkdir(parents=True, exist_ok=True)

    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text().strip())
            with open(f"/proc/{pid}/status") as f:
                if "motion_detector" in f.read():
                    log(f"⚠ Already running (PID {pid})")
                    sys.exit(1)
        except Exception:
            pass

    PID_PATH.write_text(str(os.getpid()))

    try:
        detect_motion()
    except KeyboardInterrupt:
        log("⏹ Interrupted")
    finally:
        PID_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
