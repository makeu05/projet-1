import json
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

PROCESSED = Path("/data/processed")
PROCESSED.mkdir(parents=True, exist_ok=True)

ANIMAL_CLASSES = {
    14: "bird", 15: "cat", 16: "dog", 17: "horse", 18: "sheep",
    19: "cow", 20: "elephant", 21: "bear", 22: "zebra", 23: "giraffe",
}
CONF_THRES = 0.50
MOTION_THRES = 2.0

print("[animal] Chargement YOLOv8n ONNX...", flush=True)
sess_opts = ort.SessionOptions()
sess_opts.intra_op_num_threads = 2
session = ort.InferenceSession("/app/yolov8n.onnx", sess_options=sess_opts, providers=["CPUExecutionProvider"])
input_name = session.get_inputs()[0].name
print("[animal] Pret.", flush=True)


def letterbox(img: np.ndarray, size: int = 640) -> np.ndarray:
    h, w = img.shape[:2]
    scale = min(size / w, size / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    resized = cv2.resize(img, (nw, nh))
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    top, left = (size - nh) // 2, (size - nw) // 2
    canvas[top:top + nh, left:left + nw] = resized
    return canvas


def detect_animals_in_image(img_bgr: np.ndarray):
    lb = letterbox(img_bgr, 640)
    img_rgb = cv2.cvtColor(lb, cv2.COLOR_BGR2RGB)
    blob = (img_rgb.astype(np.float32) / 255.0).transpose(2, 0, 1)[None, ...]
    preds = session.run(None, {input_name: blob})[0][0].T  # (8400, 84)

    animal_ids = list(ANIMAL_CLASSES.keys())
    animal_scores = preds[:, [4 + cid for cid in animal_ids]]
    best_idx = np.argmax(animal_scores, axis=1)
    best_conf = animal_scores[np.arange(len(preds)), best_idx]

    mask = best_conf >= CONF_THRES
    found = {}
    for idx_in_list, conf in zip(best_idx[mask], best_conf[mask]):
        label = ANIMAL_CLASSES[animal_ids[int(idx_in_list)]]
        c = round(float(conf), 2)
        if label not in found or c > found[label]:
            found[label] = c
    return found


def process_frames(split_file: Path):
    m = json.loads(split_file.read_text())
    seg_id = m["segment_id"]
    out_file = PROCESSED / f"{seg_id}.anim.json"
    if out_file.exists():
        return

    frames_dir = Path(m.get("frames_dir", ""))
    if not frames_dir.exists():
        out_file.write_text(json.dumps({"animals_detected": [], "frames_scanned": 0, "frames_skipped": 0}))
        return

    best_animals = {}
    prev_gray = None
    scanned = 0
    skipped = 0

    for fpath in sorted(frames_dir.glob("*.jpg")):
        img = cv2.imread(str(fpath))
        if img is None:
            continue
        gray = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (160, 90))
        if prev_gray is not None and np.mean(cv2.absdiff(prev_gray, gray)) < MOTION_THRES:
            skipped += 1
            continue
        prev_gray = gray
        scanned += 1
        for label, conf in detect_animals_in_image(img).items():
            if label not in best_animals or conf > best_animals[label]:
                best_animals[label] = conf

    animals_list = [{"label": k, "confidence": v} for k, v in best_animals.items()]
    out_file.write_text(json.dumps({"animals_detected": animals_list, "frames_scanned": scanned, "frames_skipped": skipped}))
    print(f"[animal] {seg_id} -> {animals_list} (scan={scanned}, skip={skipped})", flush=True)


while True:
    for sf in sorted(PROCESSED.glob("*.split.json")):
        try:
            process_frames(sf)
        except Exception as e:
            print(f"[animal] Erreur {sf.name}: {e}", flush=True)
    time.sleep(1)
