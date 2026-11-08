#!/usr/bin/env bash
# Abre TECHCORP LAB en el puerto que tenga UI dual banco (cards-bank-a).
set -euo pipefail
pick_port() {
  /opt/tc-net-cert/venv/bin/python3 - <<'PY'
import urllib.request

for port in (8080, 8081, 8082):
    try:
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2).read().decode()
    except Exception:
        continue
    if "cards-bank-a" in html and "cards-bank-b" in html:
        print(port)
        raise SystemExit(0)
print(8080)
PY
}
PORT="$(pick_port)"
URL="http://127.0.0.1:${PORT}/"
if command -v xdg-open >/dev/null 2>&1; then
  exec xdg-open "${URL}"
fi
if command -v gio >/dev/null 2>&1; then
  exec gio open "${URL}"
fi
echo "Abre en el navegador: ${URL}" >&2
