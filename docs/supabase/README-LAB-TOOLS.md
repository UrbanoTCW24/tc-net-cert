# Desplegar `lab_tools` en Supabase (HQ)

La estación muestra:

`Ejecute docs/supabase/lab_tools.sql en el SQL Editor de Supabase.`

cuando **no existe** la tabla o la función en el proyecto cloud.

## Pasos

1. Abrir [Supabase](https://supabase.com/dashboard) → proyecto del laboratorio → **SQL Editor**.
2. **New query** → pegar **`lab_tools_paso1_minimo.sql`** (o todo `lab_tools.sql`) → **Run**.
3. **Solo después** abrir otra query de prueba:

```sql
select count(*) as filas from public.lab_tools;
```

Si ejecuta `select … from lab_tools` **antes** del paso 2, verá `42P01: relation does not exist` — es normal.

4. Opcional: ejecutar el resto de `lab_tools.sql` (columnas en `ont_runs` / `lab_certification_runs`).

## Verificar desde LAB (sin secretos)

```bash
sudo /opt/tc-net-cert/venv/bin/python3 <<'PY'
from tc_net_cert.storage.supabase_runs import config_path, _settings, _settings_error
import psycopg

p = config_path()
if _settings_error(p):
    print("supabase:", _settings_error(p))
    raise SystemExit(1)
for target in _settings(p):
    with psycopg.connect(**target, connect_timeout=8) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select to_regclass('public.lab_tools'), "
                "to_regprocedure('public.register_or_update_tool(text,text,text,text)')"
            )
            t, f = cur.fetchone()
            print("lab_tools:", t)
            print("register_or_update_tool:", f)
    break
PY
```

Ambos deben ser distintos de `None`.

## Registrar TOOL en LAB-02

```bash
sudo /opt/tc-net-cert/venv/bin/tc-net-cert register-tool
```

Consulta en SQL Editor:

```sql
select tool_number, station_id, mac_host, installed_version, last_seen
from public.lab_tools
order by last_seen desc;
```
