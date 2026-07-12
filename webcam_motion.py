#!/usr/bin/env python3
"""
Webcam Motion Detection v7 - 10s video + audio + Telegram notification
- Circular frame buffer for pre-motion capture
- Frame differencing motion detection
- On motion: 5s pre + 5s post = 10s video
- Audio from C922 microphone (hw:1,0)
- Sends notification + video to Telegram automatically
"""
import cv2
import time
import os
import sys
import subprocess
import threading
import tempfile
import shutil
import requests
import json
from collections import deque
from datetime import datetime

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# ─── Configuration ─────────────────────────────────────────────
CAMERA_ID = 0
MOTION_PIXELS = 3000         # Min changed pixels to trigger
THRESHOLD_VAL = 25           # Binary threshold for frame diff
COOLDOWN = 15                # Seconds between alerts
FRAME_WIDTH = 640
FRAME_HEIGHT = 480
FPS = 15
PRE_SECONDS = 5              # Seconds before motion to include
POST_SECONDS = 5             # Seconds after motion to include
PRE_FRAMES = PRE_SECONDS * FPS      # 75 frames
POST_FRAMES = POST_SECONDS * FPS     # 75 frames
AUDIO_DEVICE = "hw:1,0"      # C922 microphone
AUDIO_CHANNELS = 1           # Mono
AUDIO_RATE = 32000
OUTPUT_DIR = "/opt/data/camera/motion_snapshots"
MAX_SNAPSHOTS = 50
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Telegram
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '8588379897')

# ─── Helpers ───────────────────────────────────────────────────

def send_telegram(msg, video_path=None):
    """Send text + optional video to Telegram."""
    if not BOT_TOKEN:
        print("[TG] No token, skipping")
        return

    # Send text notification first
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
        if r.status_code != 200:
            print(f"[TG TXT ERR] {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"[TG TXT EXC] {e}")

    # Send video
    if video_path and os.path.exists(video_path):
        try:
            with open(video_path, 'rb') as f:
                r = requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo",
                    data={"chat_id": CHAT_ID, "supports_streaming": True},
                    files={"video": f},
                    timeout=60
                )
            if r.status_code == 200:
                print(f"[TG VID] Sent: {os.path.basename(video_path)}")
                os.remove(video_path)
            else:
                print(f"[TG VID ERR] {r.status_code} {r.text[:100]}")
                # Fallback: send as document
                with open(video_path, 'rb') as f:
                    r2 = requests.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                        data={"chat_id": CHAT_ID},
                        files={"document": f},
                        timeout=60
                    )
                if r2.status_code == 200:
                    os.remove(video_path)
                    print("[TG VID] Sent as document")
        except Exception as e:
            print(f"[TG VID EXC] {e}")


def record_audio(output_path, duration=10):
    """Record audio from C922 mic in a background thread."""
    try:
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "alsa",
            "-i", AUDIO_DEVICE,
            "-t", str(duration),
            "-ac", str(AUDIO_CHANNELS),
            "-ar", str(AUDIO_RATE),
            output_path
        ], capture_output=True, timeout=duration + 5)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
            return True
    except Exception as e:
        print(f"[AUDIO ERR] {e}")
    return False


def create_video_from_frames(frames_list, audio_path, output_path):
    """Save frames to temp dir, create video with ffmpeg, mux audio."""
    temp_dir = tempfile.mkdtemp(prefix="mv7_")
    try:
        total = len(frames_list)
        if total < 5:
            return False

        # Write frames as JPEGs
        for i, frame in enumerate(frames_list):
            cv2.imwrite(os.path.join(temp_dir, f"f_{i:04d}.jpg"), frame)

        # Create raw video from frames
        raw_video = os.path.join(temp_dir, "raw.mp4")
        subprocess.run([
            "ffmpeg", "-y",
            "-framerate", str(FPS),
            "-i", os.path.join(temp_dir, "f_%04d.jpg"),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "ultrafast",
            "-crf", "30",
            raw_video
        ], capture_output=True, timeout=30)

        if not os.path.exists(raw_video):
            return False

        # Mux audio if available
        if os.path.exists(audio_path) and os.path.getsize(audio_path) > 100:
            subprocess.run([
                "ffmpeg", "-y",
                "-i", raw_video,
                "-i", audio_path,
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "64k",
                "-shortest",
                output_path
            ], capture_output=True, timeout=30)
            os.remove(audio_path)
        else:
            shutil.copy2(raw_video, output_path)

        print(f"[VIDEO] {os.path.basename(output_path)} ({total} frames, {total/FPS:.1f}s)")
        return os.path.exists(output_path)
    except Exception as e:
        print(f"[VIDEO ERR] {e}")
        return False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ─── Main Loop ─────────────────────────────────────────────────

def main():
    print("[START] Motion Detection v7 - 10s video + audio + Telegram")
    print(f"[CONFIG] Threshold:{MOTION_PIXELS}px FPS:{FPS} Pre:{PRE_SECONDS}s Post:{POST_SECONDS}s")
    print(f"[TG] Chat:{CHAT_ID} Token:{'✓' if BOT_TOKEN else '✗'}")

    cap = cv2.VideoCapture(CAMERA_ID)
    if not cap.isOpened():
        print("[ERROR] Cannot open webcam")
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, FPS)

    # Read first frame
    ret, prev_frame = cap.read()
    if not ret:
        print("[ERROR] Cannot read initial frame")
        return
    prev_gray = cv2.cvtColor(prev_frame, cv2.COLOR_BGR2GRAY)
    prev_gray = cv2.GaussianBlur(prev_gray, (21, 21), 0)

    # Circular frame buffer
    frame_buffer = deque(maxlen=PRE_FRAMES)

    print("[READY] Monitoring...")

    last_alert = 0
    frame_count = 0
    motion_count = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            time.sleep(1)
            cap.release()
            cap = cv2.VideoCapture(CAMERA_ID)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
            cap.set(cv2.CAP_PROP_FPS, FPS)
            cap.read()
            continue

        frame_count += 1
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)

        # Keep frame in pre-buffer
        frame_buffer.append(frame.copy())

        # Frame differencing motion detection
        diff = cv2.absdiff(prev_gray, gray)
        _, thresh = cv2.threshold(diff, THRESHOLD_VAL, 255, cv2.THRESH_BINARY)
        thresh = cv2.dilate(thresh, None, iterations=2)
        changed_pixels = cv2.countNonZero(thresh)

        if changed_pixels > MOTION_PIXELS:
            now = time.time()
            if now - last_alert > COOLDOWN:
                last_alert = now
                motion_count += 1
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                print(f"\n[ALERT] 🚨 Motion #{motion_count}! ({changed_pixels}px) [{ts}]")

                # ── Build video from pre-buffer + new frames ──
                # Save snapshot
                snap_path = os.path.join(OUTPUT_DIR, f"motion_{ts}.jpg")
                cv2.imwrite(snap_path, frame)
                print(f"[SNAP] {os.path.basename(snap_path)}")

                # Start background audio recording
                audio_path = os.path.join(tempfile.gettempdir(), f"audio_{ts}.wav")
                audio_thread = threading.Thread(
                    target=record_audio, args=(audio_path, PRE_SECONDS + POST_SECONDS)
                )
                audio_thread.start()

                # Collect pre-buffer frames
                all_frames = list(frame_buffer)

                # Capture post-motion frames
                for i in range(POST_FRAMES):
                    ret, f = cap.read()
                    if not ret:
                        break
                    # Update prev_gray for continuing detection
                    g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
                    g = cv2.GaussianBlur(g, (21, 21), 0)
                    prev_gray = g
                    all_frames.append(f.copy())

                # Wait for audio recording to finish
                audio_thread.join(timeout=PRE_SECONDS + POST_SECONDS + 5)

                # Create video
                video_path = os.path.join(OUTPUT_DIR, f"motion_{ts}.mp4")
                video_ok = create_video_from_frames(all_frames, audio_path, video_path)

                # Send Telegram notification
                msg = (
                    f"🚨 *Hareket Algılandı!*\n"
                    f"├ Zaman: `{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}:{ts[13:15]}`\n"
                    f"├ Piksel: `{changed_pixels}px`\n"
                    f"└ Video: {len(all_frames)} kare ({len(all_frames)/FPS:.1f}s)"
                )
                if video_ok:
                    send_telegram(msg, video_path)
                else:
                    send_telegram(msg, None)

                # Rotate old snapshots
                snaps = sorted(
                    [os.path.join(OUTPUT_DIR, f) for f in os.listdir(OUTPUT_DIR)
                     if f.startswith('motion_')],
                    key=os.path.getmtime
                )
                while len(snaps) > MAX_SNAPSHOTS:
                    os.remove(snaps.pop(0))

        prev_gray = gray

        if frame_count % 500 == 0:
            print(f"[❤] Frames:{frame_count} Motions:{motion_count}")

        time.sleep(0.03)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[STOP]")
    except Exception as e:
        print(f"[FATAL] {e}")
        import traceback; traceback.print_exc()
