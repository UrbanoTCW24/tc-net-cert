# Scraper de portal y perfiles de corrida (en Git)

Todo lo que **debe viajar con la versión** del wheel está en este repo. Los secretos y MACs **no**.

## Scraper (código)

| Pieza | Ruta |
|--------|------|
| Huawei ONT (GPON) | `package-overlay/tc_net_cert/dut/huawei.py` |
| Kaon / Broadcom EMTA (CG-2200) | `package-overlay/tc_net_cert/dut/broadcom.py` |
| Hitron (CGNV5) | `package-overlay/tc_net_cert/dut/hitron.py` |
| Perfiles guardados + fingerprint | `package-overlay/tc_net_cert/dut/portals.py` |
| Import Excel → portals | `package-overlay/tc_net_cert/dut/portals_spreadsheet.py` |
| Buscar banco A/B | `package-overlay/tc_net_cert/web/discover_benches.py` |
| API panel | `POST /api/scrape`, `POST /api/benches/discover` |

El scraper **no** vive en otro repositorio: al actualizar el wheel en flota, **todas** las estaciones reciben el mismo código de lectura.

## Perfiles de portal (credenciales)

| En Git | En cada lab |
|--------|-------------|
| `profiles/portals.example.yaml` | `~/.config/tc-net-cert/portals.yaml` |

```bash
mkdir -p ~/.config/tc-net-cert
cp ~/tc-net-cert-dev/profiles/portals.example.yaml ~/.config/tc-net-cert/portals.yaml
# Editar passwords y «Guardar perfil» en el panel
```

El panel también persiste perfiles al pulsar **Guardar perfil** en la tarjeta del banco.

## Perfiles de corrida (iperf / etapas)

| Archivo | Uso |
|---------|-----|
| `profiles/run-profiles.yaml` | Documentación SSOT: `dut_profiles` ont/emta, pares LAN, iperf |
| `provision/config_4lan.yaml.example` | Plantilla estación 4 LAN |
| `provision/config_8lan.yaml.example` | Plantilla dual banco |

Instalación real: `/etc/tc-net-cert/config.yaml` (no va a Git).

## Umbrales de aceptación (referencia + cloud)

| Capa | Ruta |
|------|------|
| Modelos ONT/EMTA | `profiles/acceptance-reference.yaml` |
| Runtime | `tc_net_cert/reporting/reference.py` |
| Sync cloud | `docs/supabase/dut_profiles.sql` + caché `/var/lib/tc-net-cert/profiles_cache.json` |

Botón panel: **Sincronizar reglas**.

## Ejemplo de informe de corrida

`fixtures/runs/TC-0002-emta-bancoA.sample.json` — estructura JSON esperada (CG-2200, banco A), sin claves.

Informes reales: `/var/lib/tc-net-cert/runs/` (solo en disco; opcional subir a Supabase con «Enviar no enviadas»).

## Despliegue en flota

Un release nuevo del wheel actualiza scraper + lógica de perfiles en **todos** los labs.  
Los YAML locales (`portals.yaml`, `config.yaml`) **no** se sobrescriben al aplicar OTA — ver [FLEET-ROLLOUT.md](FLEET-ROLLOUT.md).
