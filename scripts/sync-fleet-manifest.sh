#!/usr/bin/env bash
# Actualiza release/fleet-manifest.json desde el wheel en dist/ (misma versión que version.py).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GITHUB_REPO="${GITHUB_REPO:-UrbanoTCW24/tc-net-cert}"
MANIFEST="${ROOT}/release/fleet-manifest.json"

VER="$(python3 - <<'PY' "${ROOT}/package-overlay/tc_net_cert/version.py"
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
print(m.group(1) if m else "")
PY
)"
[[ -n "${VER}" ]] || { echo "No __version__" >&2; exit 1; }

WHL="${ROOT}/dist/tc_net_cert-${VER}-py3-none-any.whl"
[[ -f "${WHL}" ]] || WHL="$(ls -1 "${ROOT}/dist"/tc_net_cert-"${VER}"-py3-none-any.whl 2>/dev/null | head -1 || true)"
[[ -f "${WHL}" ]] || { echo "Falta wheel; ejecute: bash scripts/build-usb-kit.sh" >&2; exit 1; }

BASENAME="$(basename "${WHL}")"
SHA256="$(sha256sum "${WHL}" | awk '{print $1}')"
TAG="v${VER}"
URL="https://github.com/${GITHUB_REPO}/releases/download/${TAG}/${BASENAME}"

python3 - <<'PY' "${MANIFEST}" "${VER}" "${URL}" "${SHA256}" "${BASENAME}"
import json, sys
path, ver, url, sha, wheel = sys.argv[1:6]
data = {
    "version": ver,
    "url": url,
    "mandatory": False,
    "channel": "lab",
    "sha256": sha,
    "wheel": wheel,
    "note": "Manifiesto único para todas las estaciones. Actualícelo en cada tag vX.Y.Z.",
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(data, handle, indent=2, ensure_ascii=False)
    handle.write("\n")
print(f"Actualizado {path} → {ver} sha256={sha[:12]}…")
PY
