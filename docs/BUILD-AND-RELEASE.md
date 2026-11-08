# Build wheel, kit USB y publicación

Versionado con Git/GitHub (tags, CI, releases): ver [VERSIONING-AND-GITHUB.md](VERSIONING-AND-GITHUB.md).

## Requisitos (máquina de build)

- Python ≥ 3.12
- `python3 -m pip install build`

## Actualizar LAB-02 (venv en /opt)

El venv de producción es de **root**. No uses `pip` como usuario `lab-002`:

```bash
cd ~/tc-net-cert-dev
sudo bash dist_usb/provision/actualizar-offline.sh
# o:
sudo /opt/tc-net-cert/scripts/apply-update.sh ~/tc-net-cert-dev/dist_usb/tc_net_cert-1.1.0-py3-none-any.whl
```

## Compilar wheel

```bash
cd /home/lab-002/tc-net-cert-dev
python3 -m build --wheel
```

Artefacto: `dist/tc_net_cert-<versión>-py3-none-any.whl`  
Versión SemVer: `package-overlay/tc_net_cert/version.py` → `__version__`.

## Kit USB

```bash
bash scripts/build-usb-kit.sh
```

Salida: `dist_usb/` (copiar a `/media/.../TECHCORP-LAB-ubuntu/`).

| En la USB | Uso |
|-----------|-----|
| `tc_net_cert-*.whl` | Paquete inmutable |
| `provision/install-station.sh` | PC nueva |
| `provision/actualizar-offline.sh` | Actualizar sin tocar config/env |
| `scripts/apply-update.sh` | OTA local o HTTPS |
| `version.json` / `update_offer.json` | Metadatos de versión |

## Publicar en Supabase Storage (HQ)

El archivo `supabase.env` de **publicación** (no el de cada lab) debe incluir, además de Postgres:

```ini
project_url=https://<ref>.supabase.co
service_role_key=<solo HQ>
storage_bucket=lab-releases
```

```bash
python3 scripts/upload_release.py \
  --whl dist/tc_net_cert-1.1.0-py3-none-any.whl \
  --env ~/.config/tc-net-cert/supabase.env \
  --public \
  --out dist_usb/update_offer.json
```

Copie `update_offer.json` a `/var/lib/tc-net-cert/update_offer.json` en cada estación **o** distribúyalo vía canal interno.

## Qué nunca va en el wheel

- `/etc/tc-net-cert/config.yaml` (MACs)
- `supabase.env`, `portals.yaml` con credenciales
- Certificados ni claves Wi‑Fi
