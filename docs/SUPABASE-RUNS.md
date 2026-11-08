# Corridas en Supabase y número de validación

## Dos números (local vs nube)

| Uso | Formato | Ejemplo |
|-----|---------|---------|
| Carpeta local / PDF / panel | `TC-NNNN` | `TC-0042` |
| **Número de validación en Supabase** | `{station}-NNNN` | `LAB-002-0042` |

La estación sale de `station:` en `/etc/tc-net-cert/config.yaml`. Suele asignarse sola al instalar con `supabase.env` (ver `docs/STATION-REGISTRY.md`).

Override manual en install:

```bash
TC_NET_CERT_STATION=LAB-002 sudo bash provision/install-station.sh
```

## Campos en la base

| Campo local | Campo Supabase |
|-------------|----------------|
| `report.json` → `validation_number` | `lab_certification_runs.validation_number` (PK) |
| `report.json` → `run_id` | `lab_certification_runs.run_id` |
| — | `ont_runs.run_id` = `validation_number` (clave global) |

Filas antiguas sin `validation_number` en JSON se calculan al reenviar: `LAB-002` + `TC-0042` → `LAB-002-0042`.

## Flujo automático

1. Termina la corrida → `report.json` en disco.
2. Con `supabase.env` configurado, inserta en `lab_certification_runs` + `ont_runs`.
3. Consola: `SUPABASE OK LAB-002-0042`.
4. Sin red: **Enviar no enviadas** en historial.

## Preparar Supabase (una vez HQ)

1. `docs/supabase/lab_certification_runs.sql`
2. Credenciales en `~/.config/tc-net-cert/supabase.env` (modo 600).

## Consulta

```sql
select validation_number, run_id, station, model, serial, global_verdict, recorded_at
from public.lab_certification_runs
where station = 'LAB-002'
order by recorded_at desc;
```
