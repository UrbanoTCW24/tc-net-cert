#!/usr/bin/env bash
# Validación de cierre antes de publicar: misma versión en código, wheel y fleet-manifest.
# Objetivo: la próxima instalación OTA en todas las estaciones instale el mismo artefacto.
#
# Uso:
#   bash scripts/validate-fleet-release.sh           # comprobar
#   bash scripts/validate-fleet-release.sh --strict  # falla si hay cambios sin commitear
#
# Tras build:
#   bash scripts/sync-fleet-manifest.sh
#   bash scripts/validate-fleet-release.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STRICT=0
CHECK_INSTALLED=0
CHECK_GITHUB="${CHECK_GITHUB:-1}"
GITHUB_REPO="${GITHUB_REPO:-UrbanoTCW24/tc-net-cert}"
MANIFEST="${ROOT}/release/fleet-manifest.json"
MANIFEST_URL="https://raw.githubusercontent.com/${GITHUB_REPO}/main/release/fleet-manifest.json"

for arg in "$@"; do
  case "${arg}" in
    --strict) STRICT=1 ;;
    --with-installed) CHECK_INSTALLED=1 ;;
    -h|--help)
      sed -n '1,12p' "$0"
      exit 0
      ;;
  esac
done

log() { printf '[validate-release] %s\n' "$*"; }
fail() { log "FAIL: $*"; exit 1; }
warn() { log "WARN: $*"; }
pass() { log "PASS: $*"; }

code_version() {
  python3 - <<'PY' "${ROOT}/package-overlay/tc_net_cert/version.py"
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
print(m.group(1) if m else "")
PY
}

wheel_for_version() {
  local ver="$1"
  local candidate
  for candidate in \
    "${ROOT}/dist/tc_net_cert-${ver}-py3-none-any.whl" \
    "${ROOT}/dist_usb/tc_net_cert-${ver}-py3-none-any.whl"; do
    if [[ -f "${candidate}" ]]; then
      echo "${candidate}"
      return 0
    fi
  done
  ls -1 "${ROOT}/dist"/tc_net_cert-"${ver}"-py3-none-any.whl 2>/dev/null | head -1 || true
}

wheel_version() {
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

version_gt() {
  python3 - <<'PY' "$1" "$2"
import re, sys

def parts(v):
    token = (v or "0").strip().split("-")[0]
    nums = [int(x) for x in re.findall(r"\d+", token)]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])

sys.exit(0 if parts(sys.argv[1]) > parts(sys.argv[2]) else 1)
PY
}

VER="$(code_version)"
[[ -n "${VER}" ]] || fail "No se leyó __version__ en package-overlay/tc_net_cert/version.py"
pass "Versión en código: ${VER}"

if [[ -d "${ROOT}/.git" ]]; then
  dirty="$(git -C "${ROOT}" status --porcelain -- package-overlay release scripts docs/supabase 2>/dev/null || true)"
  if [[ -n "${dirty}" ]]; then
    if [[ "${STRICT}" -eq 1 ]]; then
      fail "Hay cambios sin commit en código de producto (use git add/commit o sin --strict)."
    fi
    warn "Hay cambios locales sin commit (la flota no los verá hasta push + release)."
    printf '%s\n' "${dirty}" | sed 's/^/  /'
  else
    pass "Árbol git limpio en package-overlay / release / scripts / docs/supabase"
  fi
  unpushed="$(git -C "${ROOT}" rev-list --count "@{u}..HEAD" 2>/dev/null || echo "")"
  if [[ -n "${unpushed}" && "${unpushed}" != "0" ]]; then
    warn "${unpushed} commit(s) locales sin push a origin."
  elif git -C "${ROOT}" rev-parse '@{u}' >/dev/null 2>&1; then
    pass "Branch al día con origin (sin commits pendientes de push)."
  fi
else
  warn "No es un clon git; no se puede validar commit/push."
fi

[[ -f "${MANIFEST}" ]] || fail "Falta ${MANIFEST}"
MAN_VER="$(python3 -c "import json;print(json.load(open('${MANIFEST}'))['version'])")"
if [[ "${MAN_VER}" != "${VER}" ]]; then
  fail "fleet-manifest version=${MAN_VER} ≠ código ${VER}. Ejecute: bash scripts/sync-fleet-manifest.sh tras build."
fi
pass "fleet-manifest.json version=${MAN_VER}"

if command -v jq >/dev/null 2>&1; then
  SHA_MAN="$(jq -r '.sha256 // ""' "${MANIFEST}")"
  URL_MAN="$(jq -r '.url // ""' "${MANIFEST}")"
  [[ -n "${SHA_MAN}" ]] || warn "fleet-manifest sin sha256."
  [[ "${URL_MAN}" == *"${VER}"* ]] || warn "URL del manifiesto no parece incluir v${VER} (revise tras tag GitHub)."
fi

WHL="$(wheel_for_version "${VER}")"
if [[ -z "${WHL}" || ! -f "${WHL}" ]]; then
  warn "No hay wheel dist/tc_net_cert-${VER}-py3-none-any.whl — ejecute: bash scripts/build-usb-kit.sh"
else
  WHL_VER="$(wheel_version "${WHL}")"
  [[ "${WHL_VER}" == "${VER}" ]] || fail "Wheel ${WHL} declara ${WHL_VER}, código ${VER}"
  pass "Wheel coherente: $(basename "${WHL}")"
  SHA_WHL="$(sha256sum "${WHL}" | awk '{print $1}')"
  if [[ -n "${SHA_MAN:-}" && "${SHA_MAN}" != "${SHA_WHL}" ]]; then
    fail "sha256 del wheel (${SHA_WHL}) ≠ fleet-manifest (${SHA_MAN}). Ejecute sync-fleet-manifest.sh."
  fi
  [[ -n "${SHA_MAN:-}" ]] && pass "sha256 wheel = manifiesto"
fi

if [[ "${CHECK_GITHUB}" == "1" ]] && command -v jq >/dev/null 2>&1; then
  tmp="$(mktemp)"
  remote_ver=""
  # raw.../main/... suele quedar en CDN con manifest viejo; el SHA de origin/main es fiable.
  if [[ -d "${ROOT}/.git" ]] && git -C "${ROOT}" rev-parse origin/main >/dev/null 2>&1; then
    git -C "${ROOT}" fetch -q origin main 2>/dev/null || true
    ref="$(git -C "${ROOT}" rev-parse origin/main)"
    sha_url="https://raw.githubusercontent.com/${GITHUB_REPO}/${ref}/release/fleet-manifest.json"
    if remote_ver="$(git -C "${ROOT}" show "origin/main:release/fleet-manifest.json" 2>/dev/null | jq -r '.version // ""')"; then
      [[ -n "${remote_ver}" ]] && pass "origin/main@${ref:0:7} declara ${remote_ver} (git, no CDN)"
    fi
    if command -v curl >/dev/null 2>&1; then
      if curl -fsSL --connect-timeout 8 --max-time 20 -o "${tmp}" "${sha_url}" 2>/dev/null; then
        curl_ver="$(jq -r '.version // ""' "${tmp}")"
        if [[ -n "${curl_ver}" && "${curl_ver}" != "${remote_ver}" ]]; then
          warn "CDN SHA=${curl_ver} vs git ${remote_ver} (use install-online con tag o espere CDN main)."
        elif [[ -n "${curl_ver}" ]]; then
          remote_ver="${curl_ver}"
        fi
      fi
    fi
  elif command -v curl >/dev/null 2>&1; then
    if curl -fsSL --connect-timeout 8 --max-time 20 -o "${tmp}" "${MANIFEST_URL}" 2>/dev/null; then
      remote_ver="$(jq -r '.version // ""' "${tmp}")"
    fi
  fi
  if [[ -n "${remote_ver}" ]]; then
    if version_gt "${remote_ver}" "${VER}"; then
      fail "GitHub main publica ${remote_ver} y su código es ${VER} (main más nuevo que local)."
    elif [[ "${remote_ver}" != "${VER}" ]]; then
      warn "GitHub main=${remote_ver}, local=${VER} — publicar tag v${VER} + manifest."
    else
      pass "GitHub main alineado con ${VER}."
    fi
  else
    warn "No se pudo leer manifiesto remoto."
  fi
  rm -f "${tmp}"
fi

if [[ "${CHECK_INSTALLED}" -eq 1 ]]; then
  INSTALLED=""
  if [[ -x /opt/tc-net-cert/venv/bin/python3 ]]; then
    INSTALLED="$(/opt/tc-net-cert/venv/bin/python3 -c 'from tc_net_cert.version import __version__; print(__version__)' 2>/dev/null || true)"
  fi
  if [[ -n "${INSTALLED}" ]]; then
    if [[ "${INSTALLED}" != "${VER}" ]]; then
      warn "Esta PC tiene /opt=${INSTALLED}, repo=${VER} (deploy-dual-bench o OTA cuando publique)."
    else
      pass "/opt instalado = ${VER}"
    fi
  fi
fi

log ""
log "Listo para flota si: wheel construido, manifest sincronizado, git push, GitHub Release v${VER}, (opcional) upload_release.py."
exit 0
