# Validar datos subidos a Supabase (LAB-02 / TOOL-001)

Sin lanzar certificación nueva. Solo lectura.

## 1. Herramienta certificadora

**SQL Editor (Supabase):**

```sql
select tool_number, station_id, mac_host, installed_version, country_code, last_seen
from public.lab_tools
where station_id = 'LAB-002'
order by last_seen desc;
```

**LAB (local):**

```bash
curl -s http://127.0.0.1:8080/api/tool/identity | jq .
sudo cat /var/lib/tc-net-cert/tool_identity.json | jq .
```

Debe coincidir: `TOOL-001`, estación `LAB-002`, versión `1.3.0`, misma `mac_host`.

---

## 2. Esquema de corridas

```sql
select column_name
from information_schema.columns
where table_schema = 'public'
  and table_name = 'ont_runs'
  and column_name in ('tool_number', 'tool_version', 'run_id', 'station');
```

**Upsert (obligatorio para `tc-net-cert sync`):** si `save_run` devuelve Postgres `42P10`, ejecutar en Supabase
`docs/supabase/ont_runs_run_id_unique.sql` (índice único **parcial** en `run_id` formato `LAB-…-NNNN`; el legacy `TC-*` duplicado se deja igual).

---

## 3. Corridas en `ont_runs`

Todas las de esta estación / herramienta:

```sql
select
  run_id,
  station,
  operator,
  serial,
  model,
  verdict,
  tool_number,
  tool_version,
  recorded_at
from public.ont_runs
where station = 'LAB-002'
   or tool_number = 'TOOL-001'
order by recorded_at desc nulls last
limit 20;
```

**Comprobar:**

| Campo | Esperado |
|-------|----------|
| `station` | `LAB-002` |
| `tool_number` | `TOOL-001` |
| `tool_version` | Versión del panel (ej. `1.3.0`) |
| `run_id` | `LAB-002-NNNN` si usa validación por estación, o `TC-NNNN` legado |

Conteo rápido:

```sql
select count(*) as total,
       count(*) filter (where tool_number = 'TOOL-001') as con_tool
from public.ont_runs
where station = 'LAB-002';
```

---

## 4. Tabla unificada (si existe)

```sql
select validation_number, run_id, station, serial, global_verdict,
       tool_number, tool_version, recorded_at
from public.lab_certification_runs
where station = 'LAB-002' or tool_number = 'TOOL-001'
order by recorded_at desc
limit 20;
```

Si la tabla no existe → solo validar `ont_runs`.

---

## 5. Pendientes solo en disco (LAB)

```bash
sudo /opt/tc-net-cert/venv/bin/tc-net-cert sync
```

(o panel → **Historial** → **Enviar no enviadas**)

Vuelve a ejecutar el `select` de `ont_runs` y confirma que aparecen órdenes que antes faltaban.

---

## 6. Perfiles DUT (reglas)

```bash
curl -s http://127.0.0.1:8080/api/profiles/status | jq '{count, country_code, source, tool_identity}'
```

En Supabase:

```sql
select model_id, country_code, min_throughput_mbps, updated_at
from public.dut_profiles
where country_code in ('GT', '*')
order by model_id
limit 30;
```

---

## 7. Checklist de aceptación HQ

- [ ] `lab_tools` tiene **TOOL-001** para LAB-002  
- [ ] `ont_runs` incluye columnas `tool_number` / `tool_version`  
- [ ] Cada corrida enviada lleva `TOOL-001` y versión coherente  
- [ ] `run_id` / `validation_number` no duplica otras estaciones  
- [ ] LOCAL: historial marca **Enviada** tras sync  
