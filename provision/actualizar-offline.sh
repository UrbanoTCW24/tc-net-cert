#!/usr/bin/env bash
# Actualiza el software desde la USB sin tocar config.yaml ni supabase.env.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WHL="$(ls -1 "${KIT_ROOT}"/tc_net_cert-*.whl 2>/dev/null | sort -V | tail -1)"

[[ -n "${WHL}" && -f "${WHL}" ]] || { echo "No hay tc_net_cert-*.whl en ${KIT_ROOT}" >&2; exit 1; }

# Buscar mejora en GitHub antes de aplicar el wheel de la USB.
if [[ -f "${KIT_ROOT}/scripts/check-release-offer.sh" ]]; then
    # shellcheck source=/dev/null
    source "${KIT_ROOT}/scripts/check-release-offer.sh"
    export TC_NET_CERT_KIT_WHL="${WHL}"
    check_release_offer || true
    if [[ "${TC_NET_CERT_UPDATE_AVAILABLE:-0}" == "1" ]]; then
        if [[ "${TC_NET_CERT_UPDATE_MANDATORY:-0}" == "1" ]]; then
            TC_NET_CERT_AUTO_UPDATE="${TC_NET_CERT_AUTO_UPDATE:-1}"
        fi
        if [[ "${TC_NET_CERT_AUTO_UPDATE:-1}" == "1" && -n "${TC_NET_CERT_UPDATE_URL}" ]]; then
            REMOTE_WHL="${TMPDIR:-/tmp}/tc_net_cert-${TC_NET_CERT_UPDATE_VERSION}.whl"
            echo "[actualizar-offline] Descargando ${TC_NET_CERT_UPDATE_VERSION}…"
            if bash "${KIT_ROOT}/scripts/download-release-wheel.sh" \
                "${TC_NET_CERT_UPDATE_URL}" \
                "${TC_NET_CERT_UPDATE_SHA256}" \
                "${REMOTE_WHL}"; then
                WHL="${REMOTE_WHL}"
            fi
        else
            echo "[actualizar-offline] Mejora ${TC_NET_CERT_UPDATE_VERSION} disponible; use TC_NET_CERT_AUTO_UPDATE=1 o el panel."
        fi
    fi
fi

if systemctl is-active --quiet tc-net-cert-web.service 2>/dev/null; then
    if curl -sf http://127.0.0.1:8080/api/status >/dev/null 2>&1; then
        running="$(curl -sf http://127.0.0.1:8080/api/status | jq -r '.running // false' 2>/dev/null || echo false)"
        if [[ "${running}" == "true" ]]; then
            echo "Hay una medición en curso. Deténgala antes de actualizar." >&2
            exit 1
        fi
    fi
fi

exec sudo "${KIT_ROOT}/scripts/apply-update.sh" "${WHL}"
