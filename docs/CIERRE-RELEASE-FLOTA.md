# Cierre de release (misma versión en toda la flota)

Antes de cerrar desarrollo del DUT / host y que **la próxima instalación** en cualquier PC instale **el mismo código**:

## 1. Subir versión (si hubo cambios de producto)

Editar solo un lugar canónico:

`package-overlay/tc_net_cert/version.py` → `__version__ = "X.Y.Z"`

## 2. Compilar artefacto inmutable

```bash
cd tc-net-cert-dev
# Si falló antes con "Permiso denegado" en dist_usb/:
# sudo rm -rf dist_usb
bash scripts/build-usb-kit.sh
```

Salida: `dist/tc_net_cert-X.Y.Z-py3-none-any.whl` + `dist_usb/`.

## 3. Sincronizar manifiesto de flota

```bash
bash scripts/sync-fleet-manifest.sh
```

Escribe `release/fleet-manifest.json` (versión + sha256 + URL del release GitHub).

## 4. Validar cierre

```bash
bash scripts/validate-fleet-release.sh --strict
```

En una estación ya instalada (opcional):

```bash
bash scripts/validate-fleet-release.sh --strict --with-installed
```

| Comprobación | Qué evita |
|--------------|-----------|
| `version.py` = `fleet-manifest` | OTA instala otra versión que la que crees publicar |
| sha256 wheel = manifest | `apply-update` rechaza o instala paquete corrupto |
| git limpio (`--strict`) | Cambios solo en tu PC que nunca llegaron a GitHub |
| vs GitHub `main` | Publicar una versión vieja cuando main ya avanzó |

## 5. Publicar (HQ)

1. Commit real (no escriba literal `…` en la terminal):

   ```bash
   git add package-overlay release scripts docs
   git commit -m "release: v1.3.0 — sync Supabase, TOOL, fleet manifest"
   git push origin main
   ```

2. Tag (si `v1.3.0` ya existe sin publicar: `git tag -d v1.3.0` y vuelva a crear, **o** suba patch `1.3.1` en `version.py`):

   ```bash
   git tag v1.3.0
   git push origin v1.3.0
   ```

3. GitHub **Release** adjuntando el `.whl` (mismo nombre que en el manifest).
4. Opcional: `scripts/upload_release.py` + `lab_releases` en Supabase.

## 6. En cada laboratorio (después)

- Panel → **Buscar actualización** → **Actualizar software**, o  
- `install-online.sh` en PC nueva (lee `fleet-manifest.json` de `main`).

**No sustituye** `config.yaml`, `supabase.env` ni MAC por PC.

## Desarrollo solo en LAB (sin release aún)

`sudo bash scripts/deploy-dual-bench.sh` — válido para una estación; **no** garantiza paridad en toda la flota hasta completar pasos 2–5.
