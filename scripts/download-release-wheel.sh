#!/usr/bin/env bash
# Descarga el wheel del manifiesto y verifica SHA256.
set -euo pipefail

URL="${1:-}"
SHA="${2:-}"
DEST="${3:-}"

[[ -n "${URL}" && -n "${DEST}" ]] || {
    echo "Uso: download-release-wheel.sh <url> <sha256|empty> <dest.whl>" >&2
    exit 2
}

mkdir -p "$(dirname "${DEST}")"
if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "${DEST}" "${URL}"
else
    wget -q -O "${DEST}" "${URL}"
fi

if [[ -n "${SHA}" ]]; then
    got="$(sha256sum "${DEST}" | awk '{print $1}')"
    if [[ "${got}" != "${SHA}" ]]; then
        echo "SHA256 no coincide (esperado ${SHA}, obtuvo ${got})" >&2
        rm -f "${DEST}"
        exit 1
    fi
fi

printf '%s\n' "${DEST}"
