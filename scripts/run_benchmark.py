import csv
import json
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

VM_HOST = "belva@192.168.122.32"
DATASET_DIR = Path.home() / "vidp-edge/dataset"
OUT_DIR = Path.home() / "vidp-edge/monitoring"
API_URL = "http://localhost:8000/api/segments"
PROM_URL = "http://localhost:9090/api/v1/query"
RAPL_URL = "http://localhost:9106/metrics"
EDGE_METRICS_URL = "http://192.168.122.32:9105/metrics"


def prom_query(expr: str) -> float:
    url = f"{PROM_URL}?query={urllib.parse.quote(expr)}"
    try:
        data = json.loads(urllib.request.urlopen(url, timeout=3).read().decode())
        res = data["data"]["result"]
        return float(res[0]["value"][1]) if res else 0.0
    except Exception:
        return 0.0


def get_rapl_pkg_j() -> float:
    txt = urllib.request.urlopen(RAPL_URL, timeout=3).read().decode()
    for line in txt.splitlines():
        if line.startswith('vidp_rapl_energy_joules_total{domain="intel-rapl:0_package-0"}'):
            return float(line.split()[-1])
    return 0.0


def get_edge_counters() -> tuple[int, float, float]:
    txt = urllib.request.urlopen(EDGE_METRICS_URL, timeout=3).read().decode()
    backlog, sent_b, saved_b = 0, 0.0, 0.0
    for line in txt.splitlines():
        if line.startswith("vidp_edge_buffer_backlog "):
            backlog = int(float(line.split()[1]))
        elif line.startswith("vidp_edge_bytes_sent_total "):
            sent_b = float(line.split()[1])
        elif line.startswith("vidp_edge_wan_bytes_saved_total "):
            saved_b = float(line.split()[1])
    return backlog, sent_b, saved_b


def set_edge_mode(mode: str):
    subprocess.run(["ssh", VM_HOST, f"echo 'PIPELINE_MODE={mode}' > /opt/vidp-edge/.env"], check=True)
    subprocess.run(
        ["ssh", VM_HOST, "cd /opt/vidp-edge && docker compose up -d downscale-splitter buffer-sender"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)


def reset_state():
    subprocess.run(["ssh", VM_HOST, "sudo /opt/vidp-edge/tc_limit.sh reset 192.168.122.1 8000"], stderr=subprocess.DEVNULL)
    subprocess.run(["ssh", VM_HOST, "rm -rf /opt/vidp-edge/data/inbox/* /opt/vidp-edge/data/processed/* /opt/vidp-edge/data/done/* /opt/vidp-edge/data/policy/*"], check=True)
    urllib.request.urlopen(urllib.request.Request(API_URL, method="DELETE"), timeout=5)


def pick_suite() -> list[tuple[str, Path]]:
    suite = []
    for cat in ("animals_speech", "animals_only", "speech_only", "calm_empty"):
        vids = sorted((DATASET_DIR / cat).glob("*.mp4"))
        if vids:
            suite.append((cat, vids[0]))
    return suite


def percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(round((len(sorted_vals) - 1) * p)))
    return round(sorted_vals[idx], 1)


def measure_idle_watts(seconds: int = 10) -> float:
    print(f"[bench] Mesure puissance idle RAPL sur {seconds}s...")
    e0 = get_rapl_pkg_j()
    t0 = time.monotonic()
    time.sleep(seconds)
    e1 = get_rapl_pkg_j()
    return max(0.0, (e1 - e0) / (time.monotonic() - t0))


def run_scenario(name: str, mode: str, suite: list[tuple[str, Path]], idle_w: float, wan_limit: int = 0, cut_sec: int = 0, stagger_sec: int = 0):
    print(f"\n=== SCENARIO : {name} (mode={mode}, tc={wan_limit}M, cut={cut_sec}s, stagger={stagger_sec}s) ===")
    reset_state()
    set_edge_mode(mode)

    if wan_limit > 0:
        subprocess.run(["ssh", VM_HOST, f"sudo /opt/vidp-edge/tc_limit.sh {wan_limit} 192.168.122.1 8000"], check=True)

    _, b_sent0, b_saved0 = get_edge_counters()
    e0 = get_rapl_pkg_j()
    t_start = time.monotonic()

    cut_proc = None
    cut_end_t = None
    if cut_sec > 0:
        cut_proc = subprocess.Popen(["ssh", VM_HOST, f"sudo /opt/vidp-edge/tc_cut_2min.sh 192.168.122.1 8000 {cut_sec}"])
        cut_end_t = time.monotonic() + cut_sec
        time.sleep(2)

    for idx, (cat, vpath) in enumerate(suite):
        rname = f"{name}__{cat}__{vpath.name}"
        subprocess.run(["scp", "-q", str(vpath), f"{VM_HOST}:/tmp/{rname}"], check=True)
        subprocess.run(["ssh", VM_HOST, f"mv /tmp/{rname} /opt/vidp-edge/data/inbox/"], check=True)
        if stagger_sec > 0 and idx < len(suite) - 1:
            time.sleep(stagger_sec)

    max_backlog = 0
    peak_wss = 0.0
    cpu_samples = []
    recovery_s = 0.0
    drained_after_cut = False

    while True:
        pending = int(subprocess.check_output(
            ["ssh", VM_HOST, "find /opt/vidp-edge/data/inbox /opt/vidp-edge/data/processed -maxdepth 1 -type f | wc -l"],
            text=True,
        ).strip())
        backlog, _, _ = get_edge_counters()
        max_backlog = max(max_backlog, backlog)
        peak_wss = max(peak_wss, prom_query('sum(container_memory_working_set_bytes{job="edge-cadvisor",name=~"edge-.*"})') / (1024 * 1024))
        cpu_samples.append(prom_query('sum(rate(container_cpu_usage_seconds_total{job="edge-cadvisor",name=~"edge-.*"}[15s]))'))

        cut_running = cut_proc is not None and cut_proc.poll() is None
        if cut_sec > 0 and not cut_running and not drained_after_cut and pending == 0 and backlog == 0:
            recovery_s = max(0.0, time.monotonic() - cut_end_t)
            drained_after_cut = True

        if not cut_running and pending == 0 and backlog == 0:
            break
        time.sleep(2)

    time.sleep(2)
    dt = time.monotonic() - t_start
    e1 = get_rapl_pkg_j()
    _, b_sent1, b_saved1 = get_edge_counters()

    if wan_limit > 0:
        subprocess.run(["ssh", VM_HOST, "sudo /opt/vidp-edge/tc_limit.sh reset 192.168.122.1 8000"], check=True)

    items = json.loads(urllib.request.urlopen(API_URL, timeout=10).read().decode())["items"]
    lats = sorted([float(i.get("e2e_latency_ms", 0)) for i in items])
    res_set = sorted({str(i.get("resolution", "-")) for i in items})

    sent_mb = max(0.0, (b_sent1 - b_sent0) / (1024 * 1024))
    saved_mb = max(0.0, (b_saved1 - b_saved0) / (1024 * 1024))
    total_raw_mb = sent_mb + saved_mb
    avoided_pct = (saved_mb / total_raw_mb * 100.0) if total_raw_mb > 0 else 0.0

    raw_j = max(0.0, e1 - e0)
    avg_watts = raw_j / max(1.0, dt)
    net_j = max(0.0, raw_j - idle_w * dt)
    j_per_seg = (net_j / len(items)) if items else 0.0
    avg_cpu = sum(cpu_samples) / max(1, len(cpu_samples))

    summary = {
        "scenario": name,
        "mode": mode,
        "resolutions": "+".join(res_set),
        "segments": len(items),
        "p50_ms": percentile(lats, 0.50),
        "p95_ms": percentile(lats, 0.95),
        "p99_ms": percentile(lats, 0.99),
        "wan_sent_mb": round(sent_mb, 2),
        "wan_saved_mb": round(saved_mb, 2),
        "wan_avoided_pct": round(avoided_pct, 1),
        "max_backlog": max_backlog,
        "recovery_s": round(recovery_s, 1),
        "peak_wss_mib": round(peak_wss, 1),
        "avg_cpu_cores": round(avg_cpu, 3),
        "raw_energy_j": round(raw_j, 1),
        "avg_watts": round(avg_watts, 2),
        "net_energy_j": round(net_j, 1),
        "j_per_segment": round(j_per_seg, 1),
        "duration_s": round(dt, 1),
    }
    print(f"[result] {summary}")
    return summary, items


suite = pick_suite()
print("[bench] Suite retenue :", [(c, p.name) for c, p in suite])
idle_w = measure_idle_watts(10)
print(f"[bench] Puissance idle mesuree : {idle_w:.2f} W")

scenarios = [
    ("01_cloud_only", "cloud-only", 0, 0, 0),
    ("02_cloud_only_5mbit", "cloud-only", 5, 0, 0),
    ("03_edge_first", "edge-first", 0, 0, 0),
    ("04_adaptive_hybrid", "adaptive-hybrid", 0, 0, 0),
    ("05_adaptive_5mbit", "adaptive-hybrid", 5, 0, 10),
    ("06_wan_cut_2min", "adaptive-hybrid", 0, 120, 0),
]

summaries = []
raw_rows = []
try:
    for sname, mode, wlim, csec, stag in scenarios:
        summ, items = run_scenario(sname, mode, suite, idle_w, wan_limit=wlim, cut_sec=csec, stagger_sec=stag)
        summaries.append(summ)
        for it in items:
            raw_rows.append({
                "scenario": sname,
                "segment_id": it.get("segment_id"),
                "mode": it.get("mode"),
                "resolution": it.get("resolution"),
                "e2e_latency_ms": it.get("e2e_latency_ms"),
                "language": it.get("language"),
                "animals": ",".join(a.get("label", "") for a in it.get("animals_detected", [])),
                "video_sent": it.get("video_sent"),
            })
finally:
    subprocess.run(["ssh", VM_HOST, "sudo /opt/vidp-edge/tc_limit.sh reset 192.168.122.1 8000"], stderr=subprocess.DEVNULL)
    set_edge_mode("adaptive-hybrid")

with open(OUT_DIR / "benchmark_summary.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(summaries[0].keys()))
    w.writeheader()
    w.writerows(summaries)

with open(OUT_DIR / "benchmark_raw_segments.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(raw_rows[0].keys()))
    w.writeheader()
    w.writerows(raw_rows)

print("\n=== BENCHMARK TERMINE : monitoring/benchmark_summary.csv ===")
for s in summaries:
    print(s)
