# Auditoría overlay — TOOL-NNN (LAB-02)

## `config/remote_sync.py`

- **`sync_profiles`**: lee `public.dut_profiles` vía pooler; escribe `/var/lib/tc-net-cert/profiles_cache.json`.
- **`sync_profiles_background`**: hilo al arranque (sustituido por `cloud_bootstrap_background` en `station_provision.py`).
- **`profile_sync_status`**: metadatos para el panel (conteo, país, fuente).
- **Nuevo**: `get_or_register_tool_identity()` delega en `tool_registry.py`.

## `config/station_provision.py`

- **`cloud_bootstrap_background`**: al iniciar el panel → estación LAB-NNN → **TOOL-NNN** → perfiles → OTA.
- No escribe `config.yaml` si la estación ya está definida (salvo placeholder).

## `storage/supabase_runs.py`

- **`save_run`**: upsert `ont_runs` + `lab_certification_runs`; inyecta `tool_number` / `tool_version` desde caché.
- **`listed_run_ids` / `sync_unsent_runs`**: reconcilian órdenes locales con nube.
- **`record_from_report`**: reconstruye fila desde `report.json` (reenvío manual).

## `web/server.py`

- Arranque: `cloud_bootstrap_background` (perfiles + herramienta + release).
- **`GET /api/profiles/status`**: incluye `tool_identity`.
- **`GET|POST /api/tool/identity`**: lectura / handshake sin certificar.
- Dashboard recibe `tool_number`, `tool_version` para el encabezado.

## `reporting/certificate.py`

- Sección **Trazabilidad**: fila «Herramienta certificadora» = `TOOL-NNN (versión)` desde `tool_identity.json`.

## Guardarraíles LAB-02

- No se tocan `config.yaml` ni `supabase.env` en este flujo de registro.
- SAFE MODE / `management_mac` solo se **leen** para `mac_host`; no mueven interfaces.
