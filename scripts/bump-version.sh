#!/usr/bin/env bash
# Sincroniza SemVer en version.py antes de commit + tag vX.Y.Z
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Uso: $0 X.Y.Z" >&2
  exit 2
fi

NEW="$1"
if [[ ! "${NEW}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Versión inválida (use SemVer X.Y.Z)" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FILE="${ROOT}/package-overlay/tc_net_cert/version.py"

OLD="$(grep -E '^__version__' "${FILE}" | sed -E 's/.*"([^"]+)".*/\1/')"
sed -i "s/^__version__ = \".*\"/__version__ = \"${NEW}\"/" "${FILE}"
echo "tc-net-cert: ${OLD} → ${NEW}"
echo "Siguiente: git commit, git tag v${NEW}, git push origin main --tags"
