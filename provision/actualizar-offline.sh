#!/usr/bin/env bash
# Actualiza el software desde la USB sin tocar config.yaml ni supabase.env.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WHL="$(ls -1 "${KIT_ROOT}"/tc_net_cert-*.whl 2>/dev/null | sort -V | tail -1)"

[[ -n "${WHL}" && -f "${WHL}" ]] || { echo "No hay tc_net_cert-*.whl en ${KIT_ROOT}" >&2; exit 1; }

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
