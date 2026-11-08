#!/usr/bin/env bash
# 1) Arranque inmediato con overlay (sin sudo).
# 2) Si tienes sudo, despliega también en /opt y actualiza techcorp-lab-root.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
chmod +x "${ROOT}/scripts/launch-panel-v1.1.sh" "${ROOT}/scripts/deploy-dual-bench.sh"
mkdir -p "${HOME}/.local/state/tc-net-cert"
DESKTOP="${HOME}/Escritorio/TECHCORP LAB v1.1.desktop"
cat > "${DESKTOP}" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=TECHCORP LAB v1.1 (dual banco)
Comment=Consola local con bancos A/B — 127.0.0.1:8080
Exec=bash ${ROOT}/scripts/launch-panel-v1.1.sh
Icon=${HOME}/.local/share/icons/techcorp-lab-256.png
Terminal=true
Categories=Network;
EOF
chmod +x "${DESKTOP}"
echo "Creado: ${DESKTOP}"
echo ""
echo "=== Modo A (sin sudo) ==="
echo "  bash ${ROOT}/scripts/launch-panel-v1.1.sh"
echo "  Abre http://127.0.0.1:8080/"
echo ""
if sudo -n true 2>/dev/null; then
  echo "=== Modo B (sudo sin contraseña): desplegando en /opt ==="
  bash "${ROOT}/scripts/deploy-dual-bench.sh"
else
  echo "=== Modo B (requiere contraseña en terminal) ==="
  echo "  bash ${ROOT}/scripts/deploy-dual-bench.sh"
  echo "Luego actualiza sudoers: ${ROOT}/docs/SUDOERS-BENCH.txt"
fi
