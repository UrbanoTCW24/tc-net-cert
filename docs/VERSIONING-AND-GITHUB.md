# Versiones, GitHub y mantenimiento del lab

Objetivo: **código y releases versionados en GitHub**; cada estación (LAB-002, etc.) sigue instalando **wheels** sin tocar `config.yaml` ni secretos locales.

## Piezas

| Pieza | Rol |
|--------|-----|
| `package-overlay/tc_net_cert/version.py` | **Única** fuente SemVer del paquete (`__version__`) |
| Tag Git `v1.2.0` | Dispara `.github/workflows/release.yml` |
| GitHub Release | Adjunta `.whl` + `update_offer.json` + `version.json` |
| Supabase (opcional) | Canal OTA ya usado por el panel («Buscar actualización») |
| USB `dist_usb/` | Mismo wheel sin Internet |

## Flujo de mantenimiento (equipo)

1. Cambios en `~/tc-net-cert-dev` (overlay → commit en Git).
2. Subir versión: `bash scripts/bump-version.sh 1.2.0`
3. Commit + push a `main`.
4. Tag alineado al wheel: `git tag v1.2.0 && git push origin v1.2.0`
5. GitHub Actions compila, publica Release y (si hay secrets) sube a Supabase.

El workflow **falla** si el tag no coincide con `version.py` (evita releases inconsistentes).

## Conectar el repo (una vez)

Hoy `tc-net-cert-dev` **no tiene** `.git`. En la máquina de desarrollo:

```bash
cd ~/tc-net-cert-dev
git init -b main
git add -A
git commit -m "Initial import tc-net-cert dev tree"
# Crear repo vacío en GitHub (org TECHCORP) y:
git remote add origin git@github.com:ORG/tc-net-cert.git
git push -u origin main
git tag v1.1.0
git push origin v1.1.0
```

Instalar `gh` en la estación de build (opcional): `sudo apt install gh` → `gh auth login`.

### Secrets opcionales en GitHub (Settings → Secrets)

| Secret | Uso |
|--------|-----|
| `SUPABASE_PROJECT_URL` | Publicación automática del wheel en Storage + `lab_releases` |
| `SUPABASE_SERVICE_ROLE_KEY` | Solo HQ; nunca en labs |

Sin secrets, el release en GitHub sigue siendo válido; HQ sube con `scripts/upload_release.py` a mano.

## Cómo se actualiza cada laboratorio

### A — Panel OTA (recomendado con Internet)

Igual que hoy: `lab_releases` / Storage o `update_offer.json` en `/var/lib/tc-net-cert/`.
Tras un release, HQ puede copiar el `update_offer.json` del GitHub Release o dejar que CI llene Supabase.

### B — Manifest HTTPS (sin tocar Supabase en cada tag)

En el `supabase.env` **de la estación** (solo lectura / URL pública), se puede apuntar:

```ini
release_manifest_url=https://github.com/ORG/tc-net-cert/releases/download/v1.2.0/update_offer.json
```

El motor `release_sync` ya consulta `release_manifest_url` si está definido.

### C — USB / sudo local

```bash
sudo bash dist_usb/provision/actualizar-offline.sh
```

## Qué va y qué no va a GitHub

**Sí:** `package-overlay/`, `scripts/`, `docs/`, workflows, plantillas `provision/`.

**No** (`.gitignore`): `supabase.env`, `dist/`, venv, credenciales de portal, `/etc/tc-net-cert/config.yaml` de cada PC.

## Relación con deploy en LAB-002

Desarrollo diario en la estación:

```bash
sudo bash ~/tc-net-cert-dev/scripts/deploy-dual-bench.sh
```

Eso rsync el overlay a `/opt` **sin** esperar un tag. Los **tags** son para **reproducir** y **distribuir** versiones a otros labs y al OTA.

## Checklist de release

- [ ] `CHANGELOG` o notas en el GitHub Release
- [ ] `version.py` = tag `vX.Y.Z`
- [ ] CI verde en `main`
- [ ] Probar wheel en un lab piloto (`actualizar-offline.sh`)
- [ ] Flag `mandatory` solo si el negocio lo exige
