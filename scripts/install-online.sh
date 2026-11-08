#!/usr/bin/env bash
# Instalación en PC nueva desde Internet (sin USB).
# Uso recomendado:
#   curl -fsSL https://raw.githubusercontent.com/UrbanoTCW24/tc-net-cert/main/scripts/install-online.sh | sudo bash
#
# Variables opcionales:
#   TC_NET_CERT_REPO=UrbanoTCW24/tc-net-cert
#   TC_NET_CERT_TAG=v1.2.0          (si vacío, lee release/fleet-manifest.json en main)
#   TC_NET_CERT_LAYOUT=4lan|8lan|auto
#   TC_NET_CERT_STATION=LAB-003
#   TC_NET_CERT_INSTALL_TAR=<url>   (kit .tar.gz de GitHub Releases; omite clonar fuentes)
set -euo pipefail

REPO="${TC_NET_CERT_REPO:-UrbanoTCW24/tc-net-cert}"
RAW_BASE="https://raw.githubusercontent.com/${REPO}/main"
TAG="${TC_NET_CERT_TAG:-}"
INSTALL_TAR="${TC_NET_CERT_INSTALL_TAR:-}"

log() { printf '[install-online] %s\n' "$*"; }
die() { log "ERROR: $*"; exit 1; }

[[ "${EUID}" -eq 0 ]] || die "Ejecute con root: curl … | sudo bash"

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "Falta el comando: $1"
}

need_cmd curl
need_cmd tar
need_cmd python3

WORKDIR="$(mktemp -d)"
cleanup() { rm -rf "${WORKDIR}"; }
trap cleanup EXIT

fetch_manifest_version() {
    local manifest="${WORKDIR}/fleet-manifest.json"
    curl -fsSL "${RAW_BASE}/release/fleet-manifest.json" -o "${manifest}"
    python3 - <<'PY' "${manifest}"
import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
ver = str(data.get("version") or "").strip()
url = str(data.get("url") or "").strip()
sha = str(data.get("sha256") or "").strip()
if not ver:
    raise SystemExit("manifest sin version")
print(ver)
print(url)
print(sha)
PY
}

layout_kit() {
    local kit_root="$1"
    [[ -f "${kit_root}/provision/install-station.sh" ]] || die "Kit incompleto (falta provision/install-station.sh)."
    chmod +x "${kit_root}/provision/"*.sh 2>/dev/null || true
    chmod +x "${kit_root}/scripts/"*.sh 2>/dev/null || true
}

if [[ -n "${INSTALL_TAR}" ]]; then
    log "Descargando kit ${INSTALL_TAR}…"
    curl -fsSL "${INSTALL_TAR}" -o "${WORKDIR}/kit.tar.gz"
    mkdir -p "${WORKDIR}/kit"
    tar -xzf "${WORKDIR}/kit.tar.gz" -C "${WORKDIR}/kit"
    KIT_ROOT="${WORKDIR}/kit"
    layout_kit "${KIT_ROOT}"
else
    if [[ -z "${TAG}" ]]; then
        mapfile -t META < <(fetch_manifest_version)
        VERSION="${META[0]}"
        WHEEL_URL="${META[1]:-}"
        WHEEL_SHA="${META[2]:-}"
        TAG="v${VERSION}"
        log "Versión OTA: ${VERSION} (${TAG})"
    else
        VERSION="${TAG#v}"
        WHEEL_URL=""
        WHEEL_SHA=""
        log "Versión fijada: ${TAG}"
    fi

    ARCHIVE="https://github.com/${REPO}/archive/refs/tags/${TAG}.tar.gz"
    log "Descargando fuentes ${ARCHIVE}…"
    curl -fsSL "${ARCHIVE}" -o "${WORKDIR}/src.tar.gz"
    tar -xzf "${WORKDIR}/src.tar.gz" -C "${WORKDIR}"
    SRC_DIR="$(find "${WORKDIR}" -maxdepth 1 -type d -name 'tc-net-cert-*' | head -1)"
    [[ -n "${SRC_DIR}" ]] || die "No se encontró carpeta extraída del tarball."
    KIT_ROOT="${SRC_DIR}"

    mkdir -p "${KIT_ROOT}/scripts"
    for script in apply-update.sh check-release-offer.sh download-release-wheel.sh; do
        if [[ ! -f "${KIT_ROOT}/scripts/${script}" ]]; then
            curl -fsSL "${RAW_BASE}/scripts/${script}" -o "${KIT_ROOT}/scripts/${script}"
        fi
    done

    if [[ -z "${WHEEL_URL}" ]]; then
        WHEEL_URL="https://github.com/${REPO}/releases/download/${TAG}/tc_net_cert-${VERSION}-py3-none-any.whl"
    fi

    WHL_NAME="tc_net_cert-${VERSION}-py3-none-any.whl"
    WHL_PATH="${KIT_ROOT}/${WHL_NAME}"
    log "Descargando wheel…"
    if [[ -x "${KIT_ROOT}/scripts/download-release-wheel.sh" ]]; then
        bash "${KIT_ROOT}/scripts/download-release-wheel.sh" "${WHEEL_URL}" "${WHEEL_SHA}" "${WHL_PATH}"
    else
        curl -fsSL "${WHEEL_URL}" -o "${WHL_PATH}"
    fi
    layout_kit "${KIT_ROOT}"
fi

log "Iniciando instalación en /opt/tc-net-cert …"
export TC_NET_CERT_AUTO_UPDATE="${TC_NET_CERT_AUTO_UPDATE:-0}"
export TC_NET_CERT_SKIP_UPDATE_CHECK="${TC_NET_CERT_SKIP_UPDATE_CHECK:-1}"
bash "${KIT_ROOT}/provision/install-station.sh"

log "Listo. Panel: http://127.0.0.1:8080/"
log "Siguiente: MACs en /etc/tc-net-cert/config.yaml y supabase.env en /root/.config/tc-net-cert/"
