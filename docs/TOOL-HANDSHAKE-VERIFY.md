# Verificar TOOL-NNN sin certificar

No lanza pruebas físicas. Requiere panel activo y `supabase.env` (sin mostrar secretos).

## 1. SQL en Supabase (HQ, una vez)

Ejecutar `docs/supabase/lab_tools.sql`.

## 2. Desplegar overlay en LAB-02

```bash
sudo bash ~/tc-net-cert-dev/scripts/deploy-dual-bench.sh
sudo systemctl restart tc-net-cert-web.service
```

No modifica `/etc/tc-net-cert/config.yaml` ni `supabase.env` si no se fuerza plantilla.

## 3. Handshake por API local

Lea el token del panel en el HTML (variable `token` en la consola del navegador) o en:

```bash
sudo cat /run/tc-net-cert/panel-token-8080.txt
```

Obtenga el token real (no use el texto literal `TOKEN_DEL_PANEL`):

```bash
sudo cat /run/tc-net-cert/panel-token-8080.txt
```

No pegue el token en chats públicos.

### Estado en caché (GET)

```bash
curl -s http://127.0.0.1:8080/api/tool/identity | jq .
```

### Registrar / actualizar (POST)

```bash
curl -s -X POST http://127.0.0.1:8080/api/tool/identity \
  -H 'Content-Type: application/json' \
  -d '{"token":"TOKEN"}' | jq .
```

Esperado: `"ok": true`, `"tool_number": "TOOL-00N"`, `"tool_version": "…"`.

### Perfiles + herramienta (GET)

```bash
curl -s http://127.0.0.1:8080/api/profiles/status | jq '.tool_identity, .app_version'
```

## 4. Archivo local offline

```bash
sudo cat /var/lib/tc-net-cert/tool_identity.json | jq '{tool_number, tool_version, station_id, mac_host, source}'
```

## 5. Consulta en Supabase

Ejecute en el **SQL Editor de Supabase** (no en la terminal bash):

```sql
select tool_number, station_id, mac_host, installed_version, country_code, last_seen
from public.lab_tools
order by last_seen desc;
```

## CLI (sin token del panel — recomendado en LAB)

```bash
sudo /opt/tc-net-cert/venv/bin/tc-net-cert register-tool
```

Equivalente: `sudo /opt/tc-net-cert/venv/bin/python3 -m tc_net_cert.cli register-tool`

## CLI (script inline)

```bash
sudo /opt/tc-net-cert/venv/bin/python3 - <<'PY'
from pathlib import Path
from tc_net_cert.config.loader import default_config_path, load_config
from tc_net_cert.config.remote_sync import get_or_register_tool_identity, resolve_country_code
from tc_net_cert.storage.supabase_runs import config_path
from tc_net_cert.version import __version__

cfg, _ = load_config(default_config_path())
print(get_or_register_tool_identity(
    station_id=cfg.station,
    country_code=resolve_country_code(cfg.station),
    installed_version=__version__,
    mac_host=cfg.management_mac,
    env_path=config_path(),
))
PY
```
