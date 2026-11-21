import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

import requests
from prometheus_client import Counter, Gauge, start_http_server

PROCESSED = Path("/data/processed")
DB_PATH = Path("/data/buffer/buffer.db")
POLICY_FILE = Path("/data/policy/target_height")

CLOUD_API_URL = os.getenv("CLOUD_API_URL", "http://192.168.122.1:8000/api/segments")
EDGE_NODE_TOKEN = os.getenv("EDGE_NODE_TOKEN", "dev-edge-token-change-me")
NODE_ID = os.getenv("NODE_ID", "edge-vm-01")
MODE = os.getenv("PIPELINE_MODE", "adaptive-hybrid")
PACE_DELAY_S = float(os.getenv("PACE_DELAY_S", "0.4"))
SELECTIVE_TRANSFER = os.getenv("SELECTIVE_TRANSFER", "true").lower() == "true"

for p in (DB_PATH.parent, PROCESSED, POLICY_FILE.parent):
    p.mkdir(parents=True, exist_ok=True)

BACKLOG_GAUGE = Gauge("vidp_edge_buffer_backlog", "Segments en attente dans le buffer SQLite")
WAN_UP_GAUGE = Gauge("vidp_edge_wan_up", "1 si le Cloud est joignable, 0 sinon")
TARGET_RES_GAUGE = Gauge("vidp_edge_target_height", "Resolution cible active (360 ou 480)")
SENT_COUNTER = Counter("vidp_edge_sent_total", "Segments envoyes au Cloud")
ERR_COUNTER = Counter("vidp_edge_send_errors_total", "Echecs d'envoi reseau vers le Cloud")
BYTES_COUNTER = Counter("vidp_edge_bytes_sent_total", "Octets envoyes sur le WAN")
BYTES_SAVED_COUNTER = Counter("vidp_edge_wan_bytes_saved_total", "Octets WAN evites par Edge/Hybride")

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute(
    """CREATE TABLE IF NOT EXISTS queue (
        segment_id TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL,
        video_path TEXT,
        raw_bytes INTEGER DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'PENDING',
        created_at INTEGER NOT NULL
    )"""
)
try:
    conn.execute("ALTER TABLE queue ADD COLUMN raw_bytes INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass
conn.commit()

last_upload_mbps = 100.0
wan_is_up = 1


def update_adaptive_policy(backlog: int):
    if MODE != "adaptive-hybrid":
        return
    if backlog >= 1 or wan_is_up == 0 or last_upload_mbps < 6.0:
        POLICY_FILE.write_text("360")
        TARGET_RES_GAUGE.set(360)
    else:
        POLICY_FILE.write_text("480")
        TARGET_RES_GAUGE.set(480)


def enqueue_segment(seg_id: str, payload: dict, video_path: str, raw_bytes: int):
    conn.execute(
        "INSERT OR IGNORE INTO queue(segment_id, payload_json, video_path, raw_bytes, status, created_at) VALUES (?, ?, ?, ?, 'PENDING', ?)",
        (seg_id, json.dumps(payload), video_path, raw_bytes, int(time.time())),
    )
    conn.commit()


def ingest_ready_files():
    # 1. Cas cloud-only (.ready.json)
    for ready in sorted(PROCESSED.glob("*.ready.json")):
        m = json.loads(ready.read_text())
        seg_id = m["segment_id"]
        payload = {
            "segment_id": seg_id, "node_id": NODE_ID, "mode": MODE,
            "resolution": m["resolution"], "event_ts_ms": m["event_ts_ms"],
            "edge_done_ts_ms": int(time.time() * 1000),
            "language": "", "has_speech": False, "subtitles_vtt": "",
            "animals_detected": [], "has_relevant_event": True, "video_sent": True,
        }
        enqueue_segment(seg_id, payload, m["video_path"], m.get("raw_bytes", 0))
        ready.unlink(missing_ok=True)

    # 2. Cas edge-first / adaptive-hybrid (.split.json + .lang.json + .anim.json)
    for split_f in sorted(PROCESSED.glob("*.split.json")):
        m = json.loads(split_f.read_text())
        seg_id = m["segment_id"]
        lang_f = PROCESSED / f"{seg_id}.lang.json"
        anim_f = PROCESSED / f"{seg_id}.anim.json"

        age = time.time() - split_f.stat().st_mtime
        if not (lang_f.exists() and anim_f.exists()) and age < 45:
            continue

        lang_res = json.loads(lang_f.read_text()) if lang_f.exists() else {}
        anim_res = json.loads(anim_f.read_text()) if anim_f.exists() else {}

        animals = anim_res.get("animals_detected", [])
        has_speech = bool(lang_res.get("has_speech", False))
        relevant = has_speech or len(animals) > 0
        send_video = True if not SELECTIVE_TRANSFER else relevant

        payload = {
            "segment_id": seg_id,
            "node_id": NODE_ID,
            "mode": MODE,
            "resolution": m["resolution"],
            "event_ts_ms": m["event_ts_ms"],
            "edge_done_ts_ms": int(time.time() * 1000),
            "language": lang_res.get("language", ""),
            "has_speech": has_speech,
            "subtitles_vtt": lang_res.get("subtitles_vtt", ""),
            "animals_detected": animals,
            "has_relevant_event": relevant,
            "video_sent": send_video,
        }

        vpath = m["video_path"] if send_video else ""
        if not send_video and Path(m["video_path"]).exists():
            Path(m["video_path"]).unlink(missing_ok=True)

        enqueue_segment(seg_id, payload, vpath, m.get("raw_bytes", 0))

        # Nettoyage des fichiers intermediaires WAV / frames
        if m.get("wav_path"):
            Path(m["wav_path"]).unlink(missing_ok=True)
        if m.get("frames_dir"):
            shutil.rmtree(m["frames_dir"], ignore_errors=True)
        lang_f.unlink(missing_ok=True)
        anim_f.unlink(missing_ok=True)
        split_f.unlink(missing_ok=True)


def flush_queue():
    global last_upload_mbps, wan_is_up
    rows = conn.execute(
        "SELECT segment_id, payload_json, video_path, raw_bytes FROM queue WHERE status='PENDING' ORDER BY created_at ASC LIMIT 5"
    ).fetchall()
    backlog = conn.execute("SELECT COUNT(*) FROM queue WHERE status='PENDING'").fetchone()[0]
    BACKLOG_GAUGE.set(backlog)
    update_adaptive_policy(backlog)

    for seg_id, payload_json, video_path, raw_bytes in rows:
        try:
            t0 = time.monotonic()
            sent_bytes = len(payload_json.encode("utf-8"))
            if video_path and Path(video_path).exists():
                vpath = Path(video_path)
                sent_bytes += vpath.stat().st_size
                with open(vpath, "rb") as vf:
                    resp = requests.post(
                        CLOUD_API_URL,
                        headers={"X-Node-Token": EDGE_NODE_TOKEN},
                        data={"metadata": payload_json},
                        files={"video": (vpath.name, vf, "video/mp4")},
                        timeout=60,
                    )
                vpath.unlink(missing_ok=True)
            else:
                resp = requests.post(
                    CLOUD_API_URL,
                    headers={"X-Node-Token": EDGE_NODE_TOKEN},
                    data={"metadata": payload_json},
                    timeout=5,
                )

            resp.raise_for_status()
            dt = max(0.01, time.monotonic() - t0)
            if sent_bytes > 20000:  # Mesure debit reel uniquement sur vrai clip
                last_upload_mbps = (sent_bytes * 8) / (dt * 1_000_000)

            wan_is_up = 1
            WAN_UP_GAUGE.set(1)
            BYTES_COUNTER.inc(sent_bytes)
            if MODE != 'cloud-only' and raw_bytes > sent_bytes:
                BYTES_SAVED_COUNTER.inc(raw_bytes - sent_bytes)
            SENT_COUNTER.inc()
            conn.execute("UPDATE queue SET status='SENT' WHERE segment_id=?", (seg_id,))
            conn.commit()
            print(f"[sender] {seg_id} -> {resp.json().get('status')} ({sent_bytes} B, ~{last_upload_mbps:.1f} Mbit/s)", flush=True)
            time.sleep(PACE_DELAY_S)
        except Exception as e:
            wan_is_up = 0
            WAN_UP_GAUGE.set(0)
            ERR_COUNTER.inc()
            print(f"[sender] WAN indispo ({seg_id} reste en buffer): {e}", flush=True)
            break

    backlog = conn.execute("SELECT COUNT(*) FROM queue WHERE status='PENDING'").fetchone()[0]
    BACKLOG_GAUGE.set(backlog)
    update_adaptive_policy(backlog)


WAN_UP_GAUGE.set(1)
TARGET_RES_GAUGE.set(480 if MODE == "adaptive-hybrid" else 360)
start_http_server(9105)
print(f"[sender] Demarre mode={MODE} selective={SELECTIVE_TRANSFER}", flush=True)

while True:
    ingest_ready_files()
    flush_queue()
    time.sleep(1)
