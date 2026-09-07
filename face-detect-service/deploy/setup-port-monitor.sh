#!/usr/bin/env bash
# Add counter-only rules for the monitoring page. These rules RETURN and do
# not change the existing firewall policy. Use the legacy backend explicitly:
# this Debian host has both nft and legacy iptables tables.
set -euo pipefail

IPT=/usr/sbin/iptables-legacy
IN_CHAIN=FACE_MON_IN
OUT_CHAIN=FACE_MON_OUT
PORTS="8080 8081 8090 8082"

ensure_chain() {
  local chain="$1"
  "$IPT" -nL "$chain" >/dev/null 2>&1 || "$IPT" -N "$chain"
}

ensure_jump() {
  local parent="$1"
  local chain="$2"
  "$IPT" -C "$parent" -j "$chain" >/dev/null 2>&1 || "$IPT" -A "$parent" -j "$chain"
}

ensure_chain "$IN_CHAIN"
ensure_chain "$OUT_CHAIN"
ensure_jump INPUT "$IN_CHAIN"
ensure_jump OUTPUT "$OUT_CHAIN"

for port in $PORTS; do
  "$IPT" -C "$IN_CHAIN" -p tcp --dport "$port" -j RETURN >/dev/null 2>&1 || \
    "$IPT" -A "$IN_CHAIN" -p tcp --dport "$port" -j RETURN
  "$IPT" -C "$OUT_CHAIN" -p tcp --sport "$port" -j RETURN >/dev/null 2>&1 || \
    "$IPT" -A "$OUT_CHAIN" -p tcp --sport "$port" -j RETURN
done

echo "Port monitoring rules ready: $PORTS"
