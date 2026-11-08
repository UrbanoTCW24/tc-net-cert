#!/usr/bin/env bash
# Crea UrbanoTCW24/tc-net-cert en GitHub (requiere gh auth login) y empuja main + tags.
set -euo pipefail

OWNER="${GITHUB_OWNER:-UrbanoTCW24}"
NAME="${GITHUB_REPO:-tc-net-cert}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if ! command -v gh >/dev/null 2>&1; then
  echo "Instale GitHub CLI: sudo apt install -y gh && gh auth login" >&2
  exit 2
fi

cd "${ROOT}"
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "No hay repositorio git en ${ROOT}" >&2
  exit 2
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "Ejecute: gh auth login" >&2
  exit 2
fi

if ! gh repo view "${OWNER}/${NAME}" >/dev/null 2>&1; then
  gh repo create "${OWNER}/${NAME}" --private --source=. --remote=origin --description "TECHCORP LAB tc-net-cert (piloto CG-2200)"
else
  git remote set-url origin "git@github.com:${OWNER}/${NAME}.git"
fi

VER="$(grep '__version__' package-overlay/tc_net_cert/version.py | sed -E 's/.*"([^"]+)".*/\1/')"
git push -u origin main
git tag -f "v${VER}" 2>/dev/null || true
git push origin "v${VER}" --force-with-lease 2>/dev/null || git push origin "v${VER}"
echo "Repo: https://github.com/${OWNER}/${NAME}"
