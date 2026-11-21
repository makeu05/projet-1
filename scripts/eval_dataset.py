import json
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

DATASET_DIR = Path.home() / "vidp-edge/dataset"
VM_HOST = "belva@192.168.122.32"
API_URL = "http://localhost:8000/api/segments"
EDGE_METRICS_URL = "http://192.168.122.32:9105/metrics"
MAX_PER_CAT = int(sys.argv[1]) if len(sys.argv) > 1 else 2

# 1. Vider l'historique Cloud pour un test propre
req = urllib.request.Request(API_URL, method="DELETE")
urllib.request.urlopen(req, timeout=5)
print("[eval] Historique Cloud vide.")

# 2. Envoyer jusqu'a MAX_PER_CAT videos par categorie avec prefixe de categorie
sent_count = 0
for cat_dir in sorted(DATASET_DIR.iterdir()):
    if not cat_dir.is_dir():
        continue
    videos = sorted(cat_dir.glob("*.mp4"))[:MAX_PER_CAT]
    for v in videos:
        remote_name = f"{cat_dir.name}__{v.name}"
        print(f"[eval] Envoi {cat_dir.name}/{v.name} -> {remote_name}")
        subprocess.run(["scp", "-q", str(v), f"{VM_HOST}:/tmp/{remote_name}"], check=True)
        subprocess.run(["ssh", VM_HOST, f"mv /tmp/{remote_name} /opt/vidp-edge/data/inbox/"], check=True)
        sent_count += 1

if sent_count == 0:
    print("[eval] Aucune video trouvee dans dataset/ !")
    sys.exit(1)

# 3. Attendre que inbox, processed et le backlog SQLite soient a 0
print(f"[eval] {sent_count} video(s) envoyee(s). Attente de fin de traitement...")
while True:
    out = subprocess.check_output(
        ["ssh", VM_HOST, "find /opt/vidp-edge/data/inbox /opt/vidp-edge/data/processed -maxdepth 1 -type f | wc -l"],
        text=True,
    ).strip()
    pending_files = int(out)
    metrics_txt = urllib.request.urlopen(EDGE_METRICS_URL, timeout=5).read().decode()
    backlog = 0.0
    for line in metrics_txt.splitlines():
        if line.startswith("vidp_edge_buffer_backlog "):
            backlog = float(line.split()[1])
    if pending_files == 0 and backlog == 0:
        break
    print(f"   ... en cours (fichiers={pending_files}, backlog={int(backlog)})")
    time.sleep(5)

time.sleep(2)
items = json.loads(urllib.request.urlopen(API_URL, timeout=10).read().decode())["items"]

# 4. Bilan par categorie
stats = defaultdict(lambda: {"segs": 0, "with_animal": 0, "with_speech": 0, "video_sent": 0, "langs": set(), "animals": set(), "latencies": []})
for s in items:
    cat = s["segment_id"].split("__")[0] if "__" in s["segment_id"] else "unknown"
    st = stats[cat]
    st["segs"] += 1
    if s.get("animals_detected"):
        st["with_animal"] += 1
        for a in s["animals_detected"]:
            st["animals"].add(a["label"])
    if s.get("has_speech"):
        st["with_speech"] += 1
        if s.get("language"):
            st["langs"].add(s["language"])
    if s.get("video_sent"):
        st["video_sent"] += 1
    st["latencies"].append(s.get("e2e_latency_ms", 0))

print("\n=== BILAN EVALUATION DATASET ===")
for cat, st in sorted(stats.items()):
    lats = sorted(st["latencies"])
    p50 = lats[len(lats) // 2] if lats else 0
    p99 = lats[min(len(lats) - 1, int(len(lats) * 0.99))] if lats else 0
    print(
        f"- {cat:15s} | segs={st['segs']:2d} | animaux={st['with_animal']:2d} {sorted(st['animals'])} "
        f"| parole={st['with_speech']:2d} {sorted(st['langs'])} "
        f"| videos_envoyees={st['video_sent']:2d}/{st['segs']:2d} | p50={p50}ms p99={p99}ms"
    )
