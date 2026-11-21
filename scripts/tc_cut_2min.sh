#!/usr/bin/env bash
set -euo pipefail

CLOUD_IP="${1:-192.168.122.1}"
CLOUD_PORT="${2:-8000}"
DURATION="${3:-120}"

cleanup() {
  sudo iptables -D DOCKER-USER -d "$CLOUD_IP" -p tcp --dport "$CLOUD_PORT" -j DROP 2>/dev/null || true
  sudo iptables -D OUTPUT -d "$CLOUD_IP" -p tcp --dport "$CLOUD_PORT" -j DROP 2>/dev/null || true
  echo "[WAN] Connexion vers ${CLOUD_IP}:${CLOUD_PORT} retablie !"
}
trap cleanup EXIT

sudo iptables -I DOCKER-USER 1 -d "$CLOUD_IP" -p tcp --dport "$CLOUD_PORT" -j DROP
sudo iptables -I OUTPUT 1 -d "$CLOUD_IP" -p tcp --dport "$CLOUD_PORT" -j DROP

echo "[WAN] COUPURE ACTIVE vers ${CLOUD_IP}:${CLOUD_PORT} pendant ${DURATION}s (SSH/Grafana preserves)..."
sleep "$DURATION"
