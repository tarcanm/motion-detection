# USB Webcam Motion Detection

Continuous motion detection using **frame differencing** with **10-second video + audio** capture and **Telegram notifications**.

## Features

- **Frame-diff detection** — reliable, low false-alarm rate
- **10-second video** with audio (C922 microphone) — captures via ffmpeg
- **Snapshot** on motion
- **Telegram notification** — snapshot + video sent automatically
- **Sensitivity control** — SIGUSR1 to toggle (normal ↔ sensitive)
- **Config file** — `/opt/data/.motion_config.json`

## Files

| File | Purpose |
|---|---|
| `motion_detector.py` | **STABLE** — frame-diff + video/audio + Telegram |
| `webcam_motion.py` | Legacy frame-diff (snapshot only, no audio/video) |
| `motion_config.example.json` | Config file reference |

## Requirements

- Python 3 with OpenCV (`cv2`)
- ffmpeg
- USB webcam (C922 Pro Stream recommended)
- ALSA audio device (`hw:1,0` for C922 mic)
- Telegram bot token (`TELEGRAM_BOT_TOKEN` in `/opt/data/.env`)

## Usage

```bash
# Start
python3 motion_detector.py

# Toggle sensitivity
kill -SIGUSR1 $(cat /tmp/motion_detector.pid)

# Stop
kill $(cat /tmp/motion_detector.pid)
```

## Configuration

Edit `/opt/data/.motion_config.json`:

```json
{
  "enabled": true,
  "sensitivity": 5,
  "min_interval_sec": 10,
  "resolution_width": 640,
  "resolution_height": 480,
  "alert_cooldown_sec": 15
}
```

Sensitivity: 1-10 (1=low, 10=high). Default 5 = ~3000px threshold.
