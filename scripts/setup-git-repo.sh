#!/usr/bin/env bash
# Primer uso: instalar git, luego este script en ~/tc-net-cert-dev
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

if ! command -v git >/dev/null 2>&1; then
  echo "Falta git. Ejecute primero:" >&2
  echo "  sudo apt update && sudo apt install -y git" >&2
  exit 2
fi

if [[ ! -d .git ]]; then
  git init -b main
fi

git add -A
if git diff --cached --quiet; then
  echo "Nada nuevo que commitear."
else
  git commit -m "tc-net-cert: sync LAB-002 tree ($(grep '__version__' package-overlay/tc_net_cert/version.py | sed -E 's/.*"([^"]+)".*/\1/'))"
fi

REMOTE="$(git remote get-url origin 2>/dev/null || true)"
if [[ -z "${REMOTE}" ]]; then
  echo ""
  echo "Siguiente (reemplace TU_ORG):"
  echo "  git remote add origin git@github.com:TU_ORG/tc-net-cert.git"
  echo "  git push -u origin main"
  VER="$(grep '__version__' package-overlay/tc_net_cert/version.py | sed -E 's/.*"([^"]+)".*/\1/')"
  echo "  git tag v${VER} && git push origin v${VER}"
else
  echo "remote origin: ${REMOTE}"
  echo "  git push -u origin main"
  VER="$(grep '__version__' package-overlay/tc_net_cert/version.py | sed -E 's/.*"([^"]+)".*/\1/')"
  echo "  git tag v${VER}  # si aún no existe"
  echo "  git push origin main --tags"
fi
