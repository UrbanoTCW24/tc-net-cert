#!/usr/bin/env bash
# Copia solo dashboard.html al panel del sistema y reinicia el servicio (requiere sudo).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${ROOT}/package-overlay/tc_net_cert/web/templates/dashboard.html"
DEST="/opt/tc-net-cert/venv/lib/python3.14/site-packages/tc_net_cert/web/templates/dashboard.html"
[[ -f "${SRC}" ]] || { echo "Falta ${SRC}" >&2; exit 1; }
sudo cp "${SRC}" "${DEST}"
sudo systemctl restart tc-net-cert-web.service
echo "Listo. Recarga http://127.0.0.1:8080/ (Ctrl+F5)."
