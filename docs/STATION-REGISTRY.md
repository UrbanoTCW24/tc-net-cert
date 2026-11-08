# Identificador automático por PC (versionador)

Cada **PC de laboratorio** (no cada CPE/DUT) recibe un código estable al autenticarse con Supabase durante el despliegue OTA:

| Campo | Ejemplo |
|-------|---------|
| `station` en `config.yaml` | `LAB-002` |
| Validación de corrida en nube | `LAB-002-0042` |

## Cuándo se asigna

1. **Instalación en línea** — `install-station.sh` si existe `supabase.env` y hay red.
2. **Arranque del panel** — registro + sincronización de reglas + chequeo OTA (`cloud_bootstrap_background`).
3. **Manual** — `techcorp-lab-root register-station` o `doctor` (muestra estación si ya registró).

## Huella de la PC

Se usa hash de `/etc/machine-id` (o fallback hostname+MAC). La misma PC **reclama** su `LAB-NNN` aunque reinstale el wheel.

## HQ — SQL (una vez)

```text
docs/supabase/lab_stations.sql
```

Función: `register_lab_station(fingerprint, hostname, country, version, layout, requested_code)`.

- Si `station` local es `LAB-XX` → asigna siguiente `LAB-NNN`.
- Si ya fijó `TC_NET_CERT_STATION=LAB-007` o editó `config.yaml` → reclama ese código si está libre.
- Si vuelve la misma PC → devuelve el código existente.

## Operador

1. Copiar `supabase.env` (modo 600) antes o después del install.
2. Dejar `station: "LAB-XX"` en la plantilla; el versionador lo sustituye.
3. Caché local: `/var/lib/tc-net-cert/station_identity.json`.

## Consulta inventario

```sql
select station_code, hostname, country_code, app_version, last_seen_at
from public.lab_stations
order by last_seen_at desc;
```
