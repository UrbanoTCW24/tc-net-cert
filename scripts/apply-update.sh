#!/usr/bin/env bash
# Instala un .whl de tc-net-cert sin tocar config local ni credenciales Supabase.
set -euo pipefail

WHL_SOURCE="${1:-}"
VENV="${TC_NET_CERT_VENV:-/opt/tc-net-cert/venv}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE_DEST="${INSTALL_ROOT}/web/templates"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

log() { printf '[apply-update] %s\n' "$*"; }

die() { log "ERROR: $*"; exit 1; }

if [[ "${EUID}" -ne 0 ]] && [[ "${TC_NET_CERT_VENV:-/opt/tc-net-cert/venv}" == /opt/* ]]; then
    exec sudo -E bash "$0" "$@"
fi

[[ -n "${WHL_SOURCE}" ]] || die "Uso: apply-update.sh <ruta-local.whl|https-url>"

PYTHON="${VENV}/bin/python3"
PIP="${VENV}/bin/pip"
[[ -x "${PIP}" ]] || die "No existe pip en ${VENV}"

WHL_FILE="${TMP_DIR}/update.whl"
if [[ -f "${WHL_SOURCE}" ]]; then
    WHL_FILE="${WHL_SOURCE}"
    log "Instalando wheel local: $(basename "${WHL_FILE}")"
else
    case "${WHL_SOURCE}" in
      https://*.supabase.co/*|https://github.com/*|https://objects.githubusercontent.com/*|https://raw.githubusercontent.com/*) ;;
      *) die "URL no permitida (solo HTTPS Supabase Storage o GitHub Releases)." ;;
    esac
    log "Descargando paquete…"
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL -o "${WHL_FILE}" "${WHL_SOURCE}"
    elif command -v wget >/dev/null 2>&1; then
      wget -q -O "${WHL_FILE}" "${WHL_SOURCE}"
    else
      die "Instala curl o wget."
    fi
fi

if [[ -f "${WHL_SOURCE}" ]]; then
    log "Instalando wheel local (con dependencias nuevas si las hay)…"
    "${PIP}" install --upgrade "${WHL_FILE}"
else
    log "Instalando wheel remoto (sin resolver dependencias)…"
    "${PIP}" install --no-deps --upgrade "${WHL_FILE}"
fi

SITE_PACKAGES="$("${PYTHON}" -c "import site; print(site.getsitepackages()[0])")"
PKG_TEMPLATES="${SITE_PACKAGES}/tc_net_cert/web/templates"
if [[ -d "${PKG_TEMPLATES}" ]]; then
  mkdir -p "${TEMPLATE_DEST}"
  log "Actualizando plantillas HTML en ${TEMPLATE_DEST}"
  rsync -a --delete "${PKG_TEMPLATES}/" "${TEMPLATE_DEST}/"
fi

# Nunca modificar:
#   /etc/tc-net-cert/config.yaml
#   /root/.config/tc-net-cert/supabase.env
#   portals locales en ~/.config o /root/.config

if systemctl is-active --quiet tc-net-cert-web.service 2>/dev/null; then
  log "Reiniciando tc-net-cert-web.service"
  systemctl restart tc-net-cert-web.service
else
  log "Servicio tc-net-cert-web no activo; reinicie el panel manualmente."
fi

log "Actualización aplicada."
