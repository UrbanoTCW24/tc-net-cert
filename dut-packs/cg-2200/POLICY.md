# Política de certificación — CG-2200

## Alcance

- Throughput Ethernet **1 Gbps** (perfil `gigabit` / speed `1G`).
- Pares simultáneos: LAN1↔LAN3, LAN2↔LAN4 (banco A).
- Modo red: DHCP hacia el CPE (portal `192.168.1.1`).

## Portal DOCSIS

- Lectura de inventario vía portal Kaon (driver Broadcom en el host).
- DOCSIS DS/US y Wi‑Fi son **informativos** en el informe; el veredicto L4 depende de iperf y enlace.

## SAFE MODE

- No mover la NIC de administración (`eno1` / `management_mac`).
- Antes de certificar: **Recuperar namespaces** si hubo DHCP del módem en puertos de prueba.

## Exclusiones v1

- No se exige sync Supabase para medir en local.
- Identidad DUT puede quedar `SKIPPED` si el portal no responde; el throughput LAN puede continuar si preflight pasa.
