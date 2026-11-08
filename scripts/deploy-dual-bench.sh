#!/usr/bin/env bash
# Despliega tc-net-cert v1.1.0 (bancos dual) en la estación local.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OVERLAY="${ROOT}/package-overlay/tc_net_cert"
DEST="/opt/tc-net-cert/venv/lib/python3.14/site-packages/tc_net_cert"
[[ -d "${OVERLAY}" ]] || { echo "Falta overlay en ${OVERLAY}" >&2; exit 1; }
sudo rsync -a --no-owner --no-group --exclude '__pycache__' "${OVERLAY}/" "${DEST}/"
sudo install -d /opt/tc-net-cert/scripts
sudo install -m 755 "${ROOT}/scripts/apply-update.sh" /opt/tc-net-cert/scripts/apply-update.sh
sudo cp "${ROOT}/package-overlay/techcorp-lab-root" /usr/local/sbin/techcorp-lab-root
sudo chmod 755 /usr/local/sbin/techcorp-lab-root
echo "Desplegado. Versión:"
/opt/tc-net-cert/venv/bin/python3 -c "from tc_net_cert.version import __version__; print(__version__)"
if systemctl is-active --quiet tc-net-cert-web.service 2>/dev/null; then
  sudo systemctl restart tc-net-cert-web.service
  echo "Servicio tc-net-cert-web reiniciado → http://127.0.0.1:8080/"
else
  echo "Reinicia el panel manualmente y actualiza sudoers según docs/SUDOERS-BENCH.txt"
fi
