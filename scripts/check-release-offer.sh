#!/usr/bin/env bash
# Consulta el manifiesto OTA (GitHub / Supabase) y exporta si hay versión más nueva.
# Uso: source scripts/check-release-offer.sh && check_release_offer
set -euo pipefail

TC_NET_CERT_RELEASE_MANIFEST_URL="${TC_NET_CERT_RELEASE_MANIFEST_URL:-https://raw.githubusercontent.com/UrbanoTCW24/tc-net-cert/main/release/fleet-manifest.json}"
TC_NET_CERT_UPDATE_AVAILABLE=0
TC_NET_CERT_UPDATE_VERSION=""
TC_NET_CERT_UPDATE_URL=""
TC_NET_CERT_UPDATE_SHA256=""
TC_NET_CERT_UPDATE_MANDATORY=0

log_offer() { printf '[check-release] %s\n' "$*"; }

_installed_version() {
    local venv="${TC_NET_CERT_VENV:-/opt/tc-net-cert/venv}"
    local py="${venv}/bin/python3"
    if [[ -x "${py}" ]]; then
        "${py}" -c "from tc_net_cert.version import __version__; print(__version__)" 2>/dev/null || true
    fi
}

_wheel_version() {
    local whl="$1"
    [[ -f "${whl}" ]] || return 0
    python3 - <<'PY' "${whl}"
import re, sys, zipfile
from pathlib import Path
whl = Path(sys.argv[1])
with zipfile.ZipFile(whl) as zf:
    name = next(n for n in zf.namelist() if n.endswith("tc_net_cert/version.py"))
    text = zf.read(name).decode()
m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
print(m.group(1) if m else "")
PY
}

_version_gt() {
    local remote="$1" local="$2"
    [[ -n "${remote}" && -n "${local}" ]] || return 1
    python3 - <<'PY' "${remote}" "${local}"
import re, sys

def parts(v):
    token = (v or "0").strip().split("-")[0]
    nums = [int(x) for x in re.findall(r"\d+", token)]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])

r, l = parts(sys.argv[1]), parts(sys.argv[2])
sys.exit(0 if r > l else 1)
PY
}

check_release_offer() {
    TC_NET_CERT_UPDATE_AVAILABLE=0
    TC_NET_CERT_UPDATE_VERSION=""
    TC_NET_CERT_UPDATE_URL=""
    TC_NET_CERT_UPDATE_SHA256=""
    TC_NET_CERT_UPDATE_MANDATORY=0

    if [[ "${TC_NET_CERT_SKIP_UPDATE_CHECK:-0}" == "1" ]]; then
        log_offer "Comprobación OTA omitida (TC_NET_CERT_SKIP_UPDATE_CHECK=1)."
        return 0
    fi

    local local_ver="${TC_NET_CERT_LOCAL_VERSION:-}"
    if [[ -z "${local_ver}" ]]; then
        local_ver="$(_installed_version)"
    fi
    if [[ -z "${local_ver}" && -n "${TC_NET_CERT_KIT_WHL:-}" ]]; then
        local_ver="$(_wheel_version "${TC_NET_CERT_KIT_WHL}")"
    fi
    [[ -n "${local_ver}" ]] || local_ver="0.0.0"

    local tmp
    tmp="$(mktemp)"
    trap 'rm -f "${tmp}"' RETURN

    if ! curl -fsSL --connect-timeout 12 --max-time 25 \
        -o "${tmp}" "${TC_NET_CERT_RELEASE_MANIFEST_URL}" 2>/dev/null; then
        log_offer "Sin red o manifiesto no disponible (${TC_NET_CERT_RELEASE_MANIFEST_URL})."
        log_offer "Se continúa con el wheel local/USB (versión ${local_ver})."
        return 0
    fi

    if ! command -v jq >/dev/null 2>&1; then
        log_offer "jq no instalado; no se puede leer el manifiesto."
        return 0
    fi

    local remote_ver url sha mand
    remote_ver="$(jq -r '.version // ""' "${tmp}")"
    url="$(jq -r '.url // ""' "${tmp}")"
    sha="$(jq -r '.sha256 // ""' "${tmp}")"
    mand="$(jq -r '.mandatory // false' "${tmp}")"

  if [[ -z "${remote_ver}" || -z "${url}" ]]; then
        log_offer "Manifiesto incompleto; se usa wheel local."
        return 0
    fi

    if ! _version_gt "${remote_ver}" "${local_ver}"; then
        log_offer "Software al día: ${local_ver} (remoto ${remote_ver})."
        return 0
    fi

    TC_NET_CERT_UPDATE_AVAILABLE=1
    TC_NET_CERT_UPDATE_VERSION="${remote_ver}"
    TC_NET_CERT_UPDATE_URL="${url}"
    TC_NET_CERT_UPDATE_SHA256="${sha}"
    if [[ "${mand}" == "true" ]]; then
        TC_NET_CERT_UPDATE_MANDATORY=1
    fi
    log_offer "Hay mejora disponible: ${remote_ver} (usted tiene ${local_ver})."
    log_offer "URL: ${url}"
    return 0
}

# Si se ejecuta directamente (no source), imprimir variables.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    check_release_offer
    printf 'local_version=%s\nupdate_available=%s\nupdate_version=%s\nupdate_url=%s\n' \
        "${TC_NET_CERT_LOCAL_VERSION:-$(_installed_version)}" \
        "${TC_NET_CERT_UPDATE_AVAILABLE}" \
        "${TC_NET_CERT_UPDATE_VERSION}" \
        "${TC_NET_CERT_UPDATE_URL}"
fi
