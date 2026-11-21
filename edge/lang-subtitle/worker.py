import json
import time
from pathlib import Path
from faster_whisper import WhisperModel

PROCESSED = Path("/data/processed")
PROCESSED.mkdir(parents=True, exist_ok=True)

print("[lang-sub] Chargement modele faster-whisper tiny int8...", flush=True)
model = WhisperModel("tiny", device="cpu", compute_type="int8", cpu_threads=2, num_workers=1)
print("[lang-sub] Pret.", flush=True)


def fmt_ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def process_audio(split_file: Path):
    m = json.loads(split_file.read_text())
    seg_id = m["segment_id"]
    out_file = PROCESSED / f"{seg_id}.lang.json"
    if out_file.exists():
        return

    wav_path = m.get("wav_path", "")
    if not wav_path or not Path(wav_path).exists():
        out_file.write_text(json.dumps({"language": "", "has_speech": False, "subtitles_vtt": ""}))
        return

    segments, info = model.transcribe(
        wav_path,
        beam_size=2,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    segs = list(segments)
    has_speech = len(segs) > 0 and any(s.text.strip() for s in segs)

    vtt_lines = ["WEBVTT", ""]
    for s in segs:
        txt = s.text.strip()
        if txt:
            vtt_lines.append(f"{fmt_ts(s.start)} --> {fmt_ts(s.end)}")
            vtt_lines.append(txt)
            vtt_lines.append("")

    res = {
        "language": info.language if has_speech else "",
        "language_prob": round(float(info.language_probability), 2) if has_speech else 0.0,
        "has_speech": has_speech,
        "subtitles_vtt": "\n".join(vtt_lines) if has_speech else "",
    }
    out_file.write_text(json.dumps(res))
    print(f"[lang-sub] {seg_id} -> speech={has_speech} lang={res['language']}", flush=True)


while True:
    for sf in sorted(PROCESSED.glob("*.split.json")):
        try:
            process_audio(sf)
        except Exception as e:
            print(f"[lang-sub] Erreur {sf.name}: {e}", flush=True)
    time.sleep(1)
