# DUT pack — Kaon CG-2200 (DOCSIS / EMTA)

Pack de referencia para certificar el **CG-2200** en banco A (LAN1–LAN4) con `tc-net-cert`.

## Identidad (LAB-002, ejemplo)

| Campo | Valor |
|--------|--------|
| `model` | CG-2200 |
| `family` | emta |
| `serial` | BS10007554006224 |
| `portal_host` | 192.168.1.1 |
| Hardware / SW | v1.0 / 1.4.0 (lectura portal) |

La serie es metadata de informe; **no** incluir contraseñas de admin en Git.

## Código del host

- Portal / scrape: `tc_net_cert/dut/broadcom.py`
- Perfil de umbrales DOCSIS: `tc_net_cert/reporting/reference.py` (`CG-2200`)
- Panel: módulo **EMTA DOCSIS** → `2 · Certificar` banco A

## Configuración local (operador)

1. Copie `profile.example.yaml` → `/var/lib/tc-net-cert/dut-packs/cg-2200.local.yaml` **o** guarde credenciales solo en el panel (banco A).
2. Portal IP: vacío = gateway DHCP del cable; para CG-2200 suele ser `192.168.1.1`.
3. Usuario típico: `admin` (clave del operador — **no commitear**).

## Sync de reglas (opcional)

Inserte filas en Supabase `dut_profiles` — ver `docs/supabase/dut_profiles.sql` (modelo `CG-2200`).
