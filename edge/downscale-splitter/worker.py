import json
import os
import shutil
import subprocess
import time
from pathlib import Path

INBOX = Path("/data/inbox")
PROCESSED = Path("/data/processed")
DONE = Path("/data/done")
POLICY_FILE = Path("/data/policy/target_height")

DEFAULT_HEIGHT = os.getenv("TARGET_HEIGHT", "360")
SEG_SECONDS = os.getenv("SEG_SECONDS", "10")
PIPELINE_MODE = os.getenv("PIPELINE_MODE", "adaptive-hybrid")

for d in (INBOX, PROCESSED, DONE, POLICY_FILE.parent):
    d.mkdir(parents=True, exist_ok=True)


def get_target_height() -> str:
    if PIPELINE_MODE == "adaptive-hybrid" and POLICY_FILE.exists():
        val = POLICY_FILE.read_text().strip()
        if val in ("360", "480"):
            return val
    return "480" if PIPELINE_MODE == "adaptive-hybrid" else DEFAULT_HEIGHT


def process_video(video_path: Path):
    job_id = f"{video_path.stem}-{int(time.time())}"
    work_dir = Path(f"/tmp/work_{job_id}")
    work_dir.mkdir(parents=True, exist_ok=True)
    raw_total_bytes = video_path.stat().st_size

    if PIPELINE_MODE == "cloud-only":
        res_label = "raw"
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-c", "copy", "-f", "segment", "-segment_time", SEG_SECONDS, "-reset_timestamps", "1",
            str(work_dir / "seg_%03d.mp4"),
        ]
    else:
        height = get_target_height()
        res_label = f"{height}p"
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vf", f"scale=-2:{height}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "30", "-threads", "2",
            "-c:a", "aac", "-b:a", "64k",
            "-f", "segment", "-segment_time", SEG_SECONDS, "-reset_timestamps", "1",
            str(work_dir / "seg_%03d.mp4"),
        ]

    print(f"[splitter] {video_path.name} mode={PIPELINE_MODE} res={res_label}", flush=True)
    subprocess.run(cmd, check=True)

    segs = sorted(work_dir.glob("seg_*.mp4"))
    raw_per_seg = raw_total_bytes // max(1, len(segs))

    for idx, seg in enumerate(segs):
        seg_id = f"{job_id}-{idx:03d}"
        out_mp4 = PROCESSED / f"{seg_id}.mp4"
        out_wav = PROCESSED / f"{seg_id}.wav"
        frames_dir = PROCESSED / f"{seg_id}_frames"

        if PIPELINE_MODE != "cloud-only":
            frames_dir.mkdir(exist_ok=True)
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(seg), "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(out_wav)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(seg), "-vf", "fps=1", str(frames_dir / "f_%03d.jpg")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

        shutil.move(str(seg), str(out_mp4))
        manifest = {
            "segment_id": seg_id,
            "resolution": res_label,
            "event_ts_ms": int(time.time() * 1000),
            "raw_bytes": raw_per_seg,
            "video_path": str(out_mp4),
            "wav_path": str(out_wav) if out_wav.exists() else "",
            "frames_dir": str(frames_dir) if frames_dir.exists() else "",
        }
        # cloud-only saute l'IA locale ; edge-first et adaptive-hybrid passent par lang + animal
        suffix = "ready.json" if PIPELINE_MODE == "cloud-only" else "split.json"
        (PROCESSED / f"{seg_id}.{suffix}").write_text(json.dumps(manifest))

    shutil.rmtree(work_dir, ignore_errors=True)
    shutil.move(str(video_path), str(DONE / f"{job_id}_{video_path.name}"))


while True:
    for f in sorted(INBOX.glob("*.mp4")):
        try:
            process_video(f)
        except Exception as e:
            print(f"[splitter] Erreur sur {f.name}: {e}", flush=True)
    time.sleep(1)
