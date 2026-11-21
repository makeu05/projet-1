#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-5}"
CLOUD_IP="${2:-192.168.122.1}"
CLOUD_PORT="${3:-8000}"

IFACE=$(ip route get "$CLOUD_IP" | awk '{for(i=1;i<=NF;i++) if($i=="dev") {print $(i+1); exit}}')

if [[ "$ACTION" == "reset" ]]; then
  sudo tc qdisc del dev "$IFACE" root 2>/dev/null || true
  echo "[tc] Bridage supprime sur $IFACE"
  exit 0
fi

RATE_MBIT="$ACTION"
sudo tc qdisc del dev "$IFACE" root 2>/dev/null || true
sudo tc qdisc add dev "$IFACE" root handle 1: htb default 20
sudo tc class add dev "$IFACE" parent 1: classid 1:10 htb rate "${RATE_MBIT}mbit" ceil "${RATE_MBIT}mbit"
sudo tc class add dev "$IFACE" parent 1: classid 1:20 htb rate 1000mbit
sudo tc qdisc add dev "$IFACE" parent 1:10 handle 10: netem delay 100ms 15ms loss 1%
sudo tc filter add dev "$IFACE" protocol ip parent 1:0 prio 1 u32 \
  match ip protocol 6 0xff \
  match ip dst "${CLOUD_IP}/32" \
  match ip dport "${CLOUD_PORT}" 0xffff \
  flowid 1:10

echo "[tc] $IFACE bride a ${RATE_MBIT} Mbit/s (+100ms delai, 1% perte) vers ${CLOUD_IP}:${CLOUD_PORT}"
