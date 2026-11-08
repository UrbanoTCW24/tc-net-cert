#!/usr/bin/env bash
# Remapea LAN1–4 (A) o LAN5–8 (B). Requiere sudo sobre /etc/tc-net-cert/config.yaml
set -euo pipefail
BENCH="${1:-}"
[[ "${BENCH}" == "A" || "${BENCH}" == "B" ]] || {
  echo "Uso: sudo bash remap-bench.sh A|B" >&2
  exit 1
}
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/package-overlay:${PYTHONPATH:-}"
CONFIG="${TC_NET_CERT_CONFIG:-/etc/tc-net-cert/config.yaml}"
PYTHON="${PYTHON:-/opt/tc-net-cert/venv/bin/python3}"
if [[ -x /usr/local/sbin/techcorp-lab-root ]] && /usr/local/sbin/techcorp-lab-root ports-remap-bench "${BENCH}" "${CONFIG}" 2>/dev/null; then
  echo "Listo (techcorp-lab-root)."
  exit 0
fi
exec "${PYTHON}" -m tc_net_cert.config.station_ports --config "${CONFIG}" --bench "${BENCH}" --yes
