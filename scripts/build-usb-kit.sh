#!/usr/bin/env bash
# Compila el wheel y arma dist_usb/ listo para copiar a USB (TECHCORP-LAB-ubuntu).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${ROOT}/dist_usb"
BUILD_DIR="${ROOT}/dist"

log() { printf '[build-usb-kit] %s\n' "$*"; }

rm -rf "${OUT}"
mkdir -p "${OUT}/provision" "${BUILD_DIR}"

BUILD_VENV="${ROOT}/.build-venv"
if [[ ! -x "${BUILD_VENV}/bin/python3" ]]; then
    log "Creando venv de compilación en .build-venv"
    python3 -m venv "${BUILD_VENV}"
    "${BUILD_VENV}/bin/pip" install -q build
fi

log "Compilando wheel…"
(cd "${ROOT}" && "${BUILD_VENV}/bin/python" -m build --wheel --outdir "${BUILD_DIR}")

WHL="$(ls -1 "${BUILD_DIR}"/tc_net_cert-*.whl | sort -V | tail -1)"
cp -a "${WHL}" "${OUT}/"
SHA256="$(sha256sum "${WHL}" | awk '{print $1}')"
BASENAME="$(basename "${WHL}")"

VER="$(python3 - <<PY
import zipfile, re
from pathlib import Path
whl = Path("${WHL}")
with zipfile.ZipFile(whl) as zf:
    name = next(n for n in zf.namelist() if n.endswith("version.py"))
    text = zf.read(name).decode()
m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
print(m.group(1) if m else "0.0.0")
PY
)"

mkdir -p "${OUT}/scripts"
cp -a "${ROOT}/scripts/apply-update.sh" "${OUT}/scripts/apply-update.sh"

cp -a "${ROOT}/provision/install-station.sh" "${OUT}/provision/"
cp -a "${ROOT}/provision/actualizar-offline.sh" "${OUT}/provision/"
cp -a "${ROOT}/provision/config_4lan.yaml.example" "${OUT}/provision/"
cp -a "${ROOT}/provision/config_8lan.yaml.example" "${OUT}/provision/"
cp -a "${ROOT}/provision/tc-net-cert-web.service" "${OUT}/provision/"
chmod +x "${OUT}/provision/"*.sh "${OUT}/scripts/"*.sh

if [[ -f "${ROOT}/package-overlay/techcorp-lab-root" ]]; then
    cp -a "${ROOT}/package-overlay/techcorp-lab-root" "${OUT}/provision/techcorp-lab-root"
    chmod 755 "${OUT}/provision/techcorp-lab-root"
fi

mkdir -p "${OUT}/docs"
[[ -f "${ROOT}/docs/supabase/dut_profiles.sql" ]] && cp -a "${ROOT}/docs/supabase/dut_profiles.sql" "${OUT}/docs/"

cat >"${OUT}/version.json" <<EOF
{
  "name": "tc-net-cert",
  "version": "${VER}",
  "wheel": "${BASENAME}",
  "sha256": "${SHA256}",
  "channel": "lab",
  "built_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

cat >"${OUT}/update_offer.json" <<EOF
{
  "version": "${VER}",
  "url": "",
  "mandatory": false,
  "channel": "lab",
  "sha256": "${SHA256}",
  "wheel": "${BASENAME}",
  "note": "Rellene url tras subir el wheel con scripts/upload_release.py"
}
EOF

cat >"${OUT}/LEEME-USB.txt" <<'EOF'
TECHCORP LAB — Kit USB (Ubuntu)

PC NUEVA (con Internet opcional):
  sudo bash provision/install-station.sh

PC YA INSTALADA (solo software):
  sudo bash provision/actualizar-offline.sh

Después de instalar:
  1. Editar /etc/tc-net-cert/config.yaml (MACs, station, operator).
  2. Copiar supabase.env a /root/.config/tc-net-cert/supabase.env (chmod 0600).
  3. Abrir http://127.0.0.1:8080/ y usar «Sincronizar reglas».

Forzar plantilla 4 u 8 LAN:
  TC_NET_CERT_LAYOUT=4lan|8lan sudo bash provision/install-station.sh

SAFE MODE: mantener safe_mode: true — no mover la interfaz de gestión a namespaces.
EOF

log "Kit listo: ${OUT}"
log "Wheel: ${BASENAME} (${VER})"
log "Copie dist_usb/ a la USB (ej. /media/usb/TECHCORP-LAB-ubuntu/)"
