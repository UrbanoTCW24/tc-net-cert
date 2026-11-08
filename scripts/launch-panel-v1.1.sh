#!/usr/bin/env bash
# Panel TECHCORP LAB v1.1 (dual banco) sin tocar /opt — usa PYTHONPATH.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/package-overlay:${PYTHONPATH:-}"
export PREFIX="${PREFIX:-/opt/tc-net-cert}"
PYTHON="${PREFIX}/venv/bin/python3"
[[ -x "${PYTHON}" ]] || PYTHON="$(command -v python3)"
CONFIG="${TC_NET_CERT_CONFIG:-/etc/tc-net-cert/config.yaml}"
if [[ ! -f "${CONFIG}" && -f "${HOME}/.config/tc-net-cert/config.yaml" ]]; then
  CONFIG="${HOME}/.config/tc-net-cert/config.yaml"
fi
PORT="${TC_NET_CERT_PORT:-8080}"
if ss -tln 2>/dev/null | grep -q ":${PORT} "; then
  echo "Puerto ${PORT} en uso (panel anterior). Para v1.1: detén el servicio viejo o usa TC_NET_CERT_PORT=8081" >&2
  PORT=8081
fi
echo "TECHCORP LAB v1.1 → http://127.0.0.1:${PORT}/" >&2
exec "${PYTHON}" -m tc_net_cert web --host 127.0.0.1 --port "${PORT}" 2>&1 | tee -a "${HOME}/.local/state/tc-net-cert/panel-v1.1.log"
