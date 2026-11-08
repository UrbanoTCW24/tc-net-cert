# Despliegue en flota — todas las estaciones

Un **release** (`v1.2.0` en GitHub) es el mismo software para **todos** los labs instalados. Lo que **cambia en todas** al actualizar es el **wheel en `/opt/tc-net-cert`**. Lo que **no cambia** es la config de cada PC.

## Qué se actualiza en bloque (mismo wheel)

| Componente | Ruta típica | Al aplicar OTA |
|------------|-------------|----------------|
| Código Python + panel | `/opt/tc-net-cert/venv/.../tc_net_cert/` | Sí |
| Servicio web | `tc-net-cert-web.service` (reinicio) | Sí |
| Versión en panel | Header `vX.Y.Z` | Sí |
| Scripts en `/opt/tc-net-cert/scripts/` | si vienen en el paquete/USB | Sí |

## Qué NO toca el despliegue (por estación)

| Dato | Dónde |
|------|--------|
| MACs LAN1–LAN8 | `/etc/tc-net-cert/config.yaml` |
| `management_mac`, bancos A/B | mismo |
| Credenciales portal / Supabase | `supabase.env`, panel, `portals.yaml` local |
| Historial de corridas | `/var/lib/tc-net-cert/runs/` |
| DUT piloto (CG-2200, etc.) | lectura en panel; pack en Git es solo referencia |

Regla: **`deploy-dual-bench.sh` en LAB-002** = desarrollo rápido (rsync overlay). **OTA / wheel** = misma versión reproducible para **toda la flota**.

## Tres formas de llevar la versión a todos los equipos

### 1) Manifiesto único en GitHub (recomendado si usas el repo público)

Archivo canónico: [`release/fleet-manifest.json`](../release/fleet-manifest.json) en `main`.

En **cada estación**, en `~/.config/tc-net-cert/supabase.env` o el env que use el panel:

```ini
release_manifest_url=https://raw.githubusercontent.com/UrbanoTCW24/tc-net-cert/main/release/fleet-manifest.json
```

Operador en el panel: **Buscar actualización** → **Actualizar software** (descarga el `.whl`, verifica SHA256, `apply-update.sh`).

Tras publicar **v1.3.0**:

1. Tag `v1.3.0` → GitHub Release con wheel.
2. Actualice `release/fleet-manifest.json` en `main` (versión, url, sha256).
3. `git push origin main` — **todas** las PCs que apunten a esa URL verán la nueva versión en la siguiente búsqueda.

### 2) Supabase `lab_releases` (flota + BI)

HQ ejecuta una vez por release:

```bash
python3 scripts/upload_release.py \
  --whl dist/tc_net_cert-1.3.0-py3-none-any.whl \
  --env /ruta/hq/supabase.env --public
```

Todas las estaciones con Postgres/Storage configurado leen la fila activa del canal `lab`. Ver `docs/supabase/lab_releases.sql`.

### 3) USB / visita física

Copie `dist_usb/` del release a cada lab → `sudo bash provision/actualizar-offline.sh`.

**Desde v1.2+**, ambos instaladores consultan al iniciar si hay mejora publicada:

| Script | Comportamiento |
|--------|----------------|
| `provision/install-station.sh` | Lee `release/fleet-manifest.json` en GitHub (URL configurable) |
| `provision/actualizar-offline.sh` | Igual; si hay versión mayor, descarga el `.whl` (por defecto `AUTO_UPDATE=1`) |

Variables:

```bash
# Manifiesto (una URL para toda la flota)
export TC_NET_CERT_RELEASE_MANIFEST_URL="https://raw.githubusercontent.com/UrbanoTCW24/tc-net-cert/main/release/fleet-manifest.json"
# Offline puro en USB sin Internet
export TC_NET_CERT_SKIP_UPDATE_CHECK=1
# Solo avisar, no descargar
export TC_NET_CERT_AUTO_UPDATE=0
```

Sin Internet el instalador sigue con el wheel de la USB.

## Flujo de trabajo del mantenedor

```text
Código en main → bump-version → commit → tag vX.Y.Z → push tag
        ↓
GitHub Actions: wheel + update_offer.json
        ↓
Actualizar release/fleet-manifest.json (+ opcional Supabase)
        ↓
Cada lab: Buscar actualización (o USB)
```

### LAB-002 hoy (ya en 1.2.0)

- Desarrollo: `sudo bash ~/tc-net-cert-dev/scripts/deploy-dual-bench.sh`
- Alinear con flota: panel OTA o wheel del [Release v1.2.0](https://github.com/UrbanoTCW24/tc-net-cert/releases/tag/v1.2.0)

## `mandatory`

Si en el manifiesto o en `lab_releases` pone `"mandatory": true`, el panel puede exigir actualizar antes de certificar (según política UI). Use solo para parches de seguridad o ruptura de contrato.

## Relación con otros repos

`jayurbano24/itadr2v2` es **ITAD** (HTML/Vercel), **no** contiene `tc-net-cert`. El software de laboratorio vive solo en **`UrbanoTCW24/tc-net-cert`**.
