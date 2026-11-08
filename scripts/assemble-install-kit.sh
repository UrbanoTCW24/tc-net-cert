#!/usr/bin/env bash
# Arma un kit instalable sin compilar wheel (descarga release de GitHub o usa wheel local).
# Uso:
#   bash scripts/assemble-install-kit.sh /tmp/tc-kit
#   sudo TC_NET_CERT_LAYOUT=4lan bash /tmp/tc-kit/provision/install-station.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-${HOME}/tc-kit}"
REPO="${TC_NET_CERT_REPO:-UrbanoTCW24/tc-net-cert}"
TAG="${TC_NET_CERT_TAG:-v1.2.0}"
VERSION="${TAG#v}"
WHL_NAME="tc_net_cert-${VERSION}-py3-none-any.whl"
WHL_URL="https://github.com/${REPO}/releases/download/${TAG}/${WHL_NAME}"

log() { printf '[assemble-kit] %s\n' "$*"; }

if [[ -e "${DEST}" && ! -w "${DEST}" ]]; then
    log "Sin permiso en ${DEST}; use: sudo rm -rf ${DEST}  o  bash $0 ~/tc-kit"
    exit 1
fi
mkdir -p "${DEST}"
rm -rf "${DEST:?}/"*

PREFER_REMOTE="${TC_NET_CERT_PREFER_RELEASE:-1}"
LOCAL_WHL="$(ls -1 "${ROOT}"/dist/tc_net_cert-*.whl "${ROOT}"/dist_usb/tc_net_cert-*.whl "${ROOT}"/tc_net_cert-*.whl 2>/dev/null | sort -V | tail -1 || true)"
if [[ "${PREFER_REMOTE}" == "1" ]]; then
    log "Descargando ${WHL_URL}"
    curl -fsSL -o "${DEST}/${WHL_NAME}" "${WHL_URL}"
elif [[ -n "${LOCAL_WHL}" && -f "${LOCAL_WHL}" ]]; then
    log "Wheel local: ${LOCAL_WHL}"
    cp -a "${LOCAL_WHL}" "${DEST}/${WHL_NAME}"
else
    log "Descargando ${WHL_URL}"
    curl -fsSL -o "${DEST}/${WHL_NAME}" "${WHL_URL}"
fi

cp -a "${ROOT}/provision" "${ROOT}/scripts" "${DEST}/"
if [[ -f "${ROOT}/package-overlay/techcorp-lab-root" ]]; then
    cp -a "${ROOT}/package-overlay/techcorp-lab-root" "${DEST}/provision/techcorp-lab-root"
    chmod 755 "${DEST}/provision/techcorp-lab-root"
fi
chmod +x "${DEST}/provision/"*.sh "${DEST}/scripts/"*.sh 2>/dev/null || true

log "Kit listo en ${DEST}"
log "Instalar: sudo TC_NET_CERT_LAYOUT=4lan bash ${DEST}/provision/install-station.sh"
