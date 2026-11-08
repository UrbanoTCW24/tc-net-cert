#!/usr/bin/env bash
# LAB-002: Internet/sync on eno1 (DC:4A:3E:76:B8:1D). Certification LANs use netns ns-lan*.
set -euo pipefail
CONFIG="${1:-/etc/tc-net-cert/config.yaml}"
MGMT_MAC="dc:4a:3e:76:b8:1d"
MGMT_IF="${MGMT_IF:-eno1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="${ROOT}/package-overlay:${PYTHONPATH:-}"
PYTHON="${PYTHON:-/opt/tc-net-cert/venv/bin/python3}"
if [[ ! -x "${PYTHON}" ]]; then
  PYTHON="$(command -v python3)"
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Ejecuta con sudo: sudo bash $0" >&2
  exit 1
fi

echo "==> Perfil: LAN administración ${MGMT_IF} (${MGMT_MAC}) sin namespace"
echo "==> Certificación: LAN1–LAN8 en netns (Remapear LAN A/B en el panel)"

"${PYTHON}" - "${CONFIG}" <<'PY'
import sys
from pathlib import Path

from tc_net_cert.config.loader import load_config, save_config
from tc_net_cert.core.models import PortConfig

path = Path(sys.argv[1])
config, _loaded = load_config(path)
config.management_mac = "dc:4a:3e:76:b8:1d"
layout = {
    "LAN1": "1a:4b:24:b4:22:56",
    "LAN2": "1a:4b:24:b4:22:59",
    "LAN3": "1a:4b:24:b4:22:57",
    "LAN4": "1a:4b:24:b4:22:58",
    "LAN5": "1a:4b:24:b4:2b:27",
    "LAN6": "1a:4b:24:b4:2b:24",
    "LAN7": "1a:4b:24:b4:2b:25",
    "LAN8": "1a:4b:24:b4:2b:26",
}
config.ports = {name: PortConfig(mac=mac) for name, mac in layout.items()}
config.simultaneous_pairs = [["LAN1", "LAN3"], ["LAN2", "LAN4"]]
save_config(config, path)
print(f"Actualizado {path} (management_mac=eno1; sin DC:4A:3E:76:B8:1D en ports)")
PY

echo "==> NetworkManager: DHCP Internet en ${MGMT_IF} (métrica baja)"
if ! nmcli -t -f NAME connection show 2>/dev/null | grep -qx 'lab-mgmt-eno1'; then
  nmcli connection add type ethernet ifname "$MGMT_IF" con-name lab-mgmt-eno1 \
    ipv4.method auto ipv4.route-metric 50 ipv6.method auto
else
  nmcli connection modify lab-mgmt-eno1 connection.interface-name "$MGMT_IF" \
    ipv4.method auto ipv4.route-metric 50
fi
nmcli device set "$MGMT_IF" managed yes 2>/dev/null || true
if nmcli connection up lab-mgmt-eno1 ifname "$MGMT_IF" 2>/dev/null; then
  echo "Conexión lab-mgmt-eno1 activa."
elif nmcli device connect "$MGMT_IF" 2>/dev/null; then
  echo "Dispositivo ${MGMT_IF} conectado."
else
  echo "Aviso: conecta cable de Internet a ${MGMT_IF} y ejecuta: nmcli connection up lab-mgmt-eno1"
fi

strip_default_from_nic() {
  local nic="$1"
  [[ -n "${nic}" ]] || return 0
  ip link show "${nic}" &>/dev/null || return 0
  local conn
  conn="$(nmcli -t -f NAME,DEVICE connection show 2>/dev/null | awk -F: -v d="${nic}" '$2==d{print $1; exit}')"
  if [[ -n "${conn}" ]]; then
    nmcli connection modify "${conn}" ipv4.never-default yes ipv4.route-metric 900 2>/dev/null || true
  fi
  ip -4 route del default dev "${nic}" 2>/dev/null || true
}

echo "==> Quitar ruta default de NICs de certificación (no deben llevar Internet)"
for nic in enp1s0f3 ens8f3; do
  strip_default_from_nic "${nic}"
done

echo "==> Remapear bancos A y B"
if [[ -x /usr/local/sbin/techcorp-lab-root ]]; then
  /usr/local/sbin/techcorp-lab-root ports-remap-bench A "${CONFIG}" || true
  /usr/local/sbin/techcorp-lab-root ports-remap-bench B "${CONFIG}" || true
else
  "${PYTHON}" -m tc_net_cert.config.station_ports --config "${CONFIG}" --bench A --yes || true
  "${PYTHON}" -m tc_net_cert.config.station_ports --config "${CONFIG}" --bench B --yes || true
fi

echo ""
echo "Listo. Comprueba:"
ip -4 route show default || echo "(sin ruta default; cable en ${MGMT_IF}?)"
ip netns list 2>/dev/null || true
echo "Panel: http://127.0.0.1:8080/ → Reconfigurar LAN A / B si hace falta."
