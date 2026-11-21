import time
from pathlib import Path
from prometheus_client import Counter, start_http_server

POWERCAP = Path("/powercap")
ENERGY_COUNTER = Counter("vidp_rapl_energy_joules_total", "Energie physique Intel RAPL (Joules)", ["domain"])

domains = {}
for energy_f in sorted(POWERCAP.rglob("energy_uj")):
    p = energy_f.parent
    if "mmio" in str(p):
        continue
    name_f = p / "name"
    max_f = p / "max_energy_range_uj"
    if name_f.exists():
        label = f"{p.name}_{name_f.read_text().strip()}"
        max_uj = int(max_f.read_text().strip()) if max_f.exists() else 2**32
        domains[label] = {
            "energy_f": energy_f,
            "max_uj": max_uj,
            "prev": int(energy_f.read_text().strip()),
        }

print(f"[rapl] Domaines detectes : {list(domains.keys())}", flush=True)
start_http_server(9106)

while True:
    time.sleep(1)
    for label, d in domains.items():
        try:
            curr = int(d["energy_f"].read_text().strip())
            delta = curr - d["prev"]
            if delta < 0:
                delta += d["max_uj"]
            d["prev"] = curr
            ENERGY_COUNTER.labels(domain=label).inc(delta / 1_000_000.0)
        except Exception as e:
            print(f"[rapl] Erreur {label}: {e}", flush=True)
