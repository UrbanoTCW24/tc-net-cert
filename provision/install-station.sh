#!/usr/bin/env bash
# Instalación desatendida de TECHCORP LAB en una PC nueva (ejecutar con root).
# No incluye supabase.env ni MACs reales — el operador los configura después.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PREFIX="${TC_NET_CERT_PREFIX:-/opt/tc-net-cert}"
VENV="${PREFIX}/venv"
LAYOUT="${TC_NET_CERT_LAYOUT:-auto}"
FORCE_CONFIG="${TC_NET_CERT_FORCE_CONFIG:-0}"

log() { printf '[install-station] %s\n' "$*"; }
die() { log "ERROR: $*"; exit 1; }

[[ "${EUID}" -eq 0 ]] || die "Ejecuta como root: sudo bash install-station.sh"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
    iperf3 ethtool iproute2 python3 python3-venv python3-pip jq curl ca-certificates rsync

mkdir -p "${PREFIX}/scripts" "${PREFIX}/bin" /etc/tc-net-cert /var/lib/tc-net-cert
install -m 755 "${KIT_ROOT}/scripts/apply-update.sh" "${PREFIX}/scripts/apply-update.sh"
install -m 755 "${KIT_ROOT}/scripts/check-release-offer.sh" "${PREFIX}/scripts/check-release-offer.sh"
install -m 755 "${KIT_ROOT}/scripts/download-release-wheel.sh" "${PREFIX}/scripts/download-release-wheel.sh"

WHL="$(ls -1 "${KIT_ROOT}"/tc_net_cert-*.whl 2>/dev/null | sort -V | tail -1)"
[[ -n "${WHL}" && -f "${WHL}" ]] || die "No se encontró tc_net_cert-*.whl en ${KIT_ROOT}"

# Comprobar mejora publicada (GitHub fleet-manifest) antes de instalar el wheel del kit.
# TC_NET_CERT_AUTO_UPDATE=1 → descarga el wheel remoto si es más nuevo que el USB.
# TC_NET_CERT_SKIP_UPDATE_CHECK=1 → omite la consulta (offline puro).
# shellcheck source=/dev/null
source "${KIT_ROOT}/scripts/check-release-offer.sh"
export TC_NET_CERT_KIT_WHL="${WHL}"
export TC_NET_CERT_VENV="${VENV}"
check_release_offer || true
if [[ "${TC_NET_CERT_UPDATE_AVAILABLE:-0}" == "1" ]]; then
    if [[ "${TC_NET_CERT_UPDATE_MANDATORY:-0}" == "1" ]]; then
        log "Actualización obligatoria en catálogo: ${TC_NET_CERT_UPDATE_VERSION}"
        TC_NET_CERT_AUTO_UPDATE="${TC_NET_CERT_AUTO_UPDATE:-1}"
    fi
    if [[ "${TC_NET_CERT_AUTO_UPDATE:-1}" == "1" && -n "${TC_NET_CERT_UPDATE_URL}" ]]; then
        REMOTE_WHL="${TMPDIR:-/tmp}/tc_net_cert-${TC_NET_CERT_UPDATE_VERSION}.whl"
        log "Descargando mejora ${TC_NET_CERT_UPDATE_VERSION}…"
        if bash "${KIT_ROOT}/scripts/download-release-wheel.sh" \
            "${TC_NET_CERT_UPDATE_URL}" \
            "${TC_NET_CERT_UPDATE_SHA256}" \
            "${REMOTE_WHL}"; then
            WHL="${REMOTE_WHL}"
            log "Se instalará wheel remoto: $(basename "${WHL}")"
        else
            log "No se pudo verificar el wheel remoto; se usa el del kit USB."
        fi
    else
        log "Para aplicar la mejora: TC_NET_CERT_AUTO_UPDATE=1 sudo bash install-station.sh"
    fi
fi

if [[ ! -x "${VENV}/bin/python3" ]]; then
    log "Creando venv en ${VENV}"
    python3 -m venv "${VENV}"
fi
"${VENV}/bin/pip" install --upgrade pip wheel
log "Instalando ${WHL}"
"${VENV}/bin/pip" install --upgrade "${WHL}"

if [[ -f "${KIT_ROOT}/provision/techcorp-lab-root" ]]; then
    install -m 755 "${KIT_ROOT}/provision/techcorp-lab-root" /usr/local/sbin/techcorp-lab-root
elif [[ -f "${KIT_ROOT}/package-overlay/techcorp-lab-root" ]]; then
    install -m 755 "${KIT_ROOT}/package-overlay/techcorp-lab-root" /usr/local/sbin/techcorp-lab-root
fi

install -m 644 "${KIT_ROOT}/provision/tc-net-cert-web.service" /etc/systemd/system/tc-net-cert-web.service
systemctl daemon-reload
systemctl enable tc-net-cert-web.service

apply_station_id() {
    local cfg="$1"
    local id="${TC_NET_CERT_STATION:-}"
    [[ -f "${cfg}" ]] || return 0
    [[ -n "${id}" ]] || return 0
    if grep -q '^station:' "${cfg}"; then
        sed -i "s/^station:.*/station: \"${id}\"/" "${cfg}"
    else
        printf '\nstation: "%s"\n' "${id}" >>"${cfg}"
    fi
    log "Identificador de estación en config: ${id} (nube → ${id}-0001, ${id}-0002, …)"
}

detect_layout() {
    if [[ "${LAYOUT}" == "4lan" || "${LAYOUT}" == "8lan" ]]; then
        echo "${LAYOUT}"
        return
    fi
    # Heurística: NICs Ethernet (excluye loopback, Wi‑Fi, puentes virtuales).
    local count
    count="$(
        ip -o link show 2>/dev/null \
            | awk -F': ' '{print $2}' \
            | grep -Ev '^(lo|wl|wlan|docker|br-|veth|virbr)' \
            | wc -l \
            | tr -d ' '
    )"
    if [[ "${count}" -ge 8 ]]; then
        echo "8lan"
    else
        echo "4lan"
    fi
}

chosen="$(detect_layout)"
log "Disposición detectada: ${chosen}"

CONFIG_DST="/etc/tc-net-cert/config.yaml"
if [[ -f "${CONFIG_DST}" && "${FORCE_CONFIG}" != "1" ]]; then
    log "Se conserva ${CONFIG_DST} existente (usa TC_NET_CERT_FORCE_CONFIG=1 para plantilla nueva)."
else
    case "${chosen}" in
        8lan) src="${KIT_ROOT}/provision/config_8lan.yaml.example" ;;
        *) src="${KIT_ROOT}/provision/config_4lan.yaml.example" ;;
    esac
    install -m 640 "${src}" "${CONFIG_DST}"
    log "Plantilla instalada en ${CONFIG_DST} — edite MACs, station y operator antes de certificar."
fi
apply_station_id "${CONFIG_DST}"

ROOT_CFG="/root/.config/tc-net-cert"
mkdir -p "${ROOT_CFG}"
chmod 700 "${ROOT_CFG}"
touch "${ROOT_CFG}/portals.yaml"
chmod 600 "${ROOT_CFG}/portals.yaml" 2>/dev/null || true

if [[ -f "${ROOT_CFG}/supabase.env" ]]; then
    log "Registro de estación en versionador (Supabase)…"
    if TC_NET_CERT_SUPABASE="${ROOT_CFG}/supabase.env" "${VENV}/bin/python3" - <<'PY'; then
from tc_net_cert.config.loader import default_config_path
from tc_net_cert.config.station_provision import provision_station

result = provision_station(config_path=default_config_path())
if result.get("ok"):
    print(result.get("station_code", ""))
else:
    raise SystemExit(result.get("error") or "provision failed")
PY
        assigned="$("${VENV}/bin/python3" -c "import json; print(json.load(open('/var/lib/tc-net-cert/station_identity.json')).get('station_code',''))" 2>/dev/null || true)"
        log "Identificador de estación: ${assigned:-(ver station_identity.json)}"
    else
        log "Registro en línea diferido (sin red o ejecute docs/supabase/lab_stations.sql)."
    fi
fi

if [[ ! -f "${ROOT_CFG}/supabase.env" ]]; then
    log "Coloque supabase.env en ${ROOT_CFG}/supabase.env (chmod 0600) — NO va en la USB."
    cat >"${ROOT_CFG}/supabase.env.placeholder" <<'EOF'
# Renombrar a supabase.env con las claves de ESTA estación (HQ / USB privada).
# Este archivo no contiene secretos y puede borrarse tras instalar.
EOF
    chmod 644 "${ROOT_CFG}/supabase.env.placeholder"
fi

systemctl restart tc-net-cert-web.service || systemctl start tc-net-cert-web.service

VER="$("${VENV}/bin/python3" -c "from tc_net_cert.version import __version__; print(__version__)" 2>/dev/null || echo unknown)"
log "Instalación lista — versión ${VER}"
log "Panel: http://127.0.0.1:8080/ (solo localhost)"
