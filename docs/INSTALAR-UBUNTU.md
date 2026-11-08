# Instalar TECHCORP LAB en otra PC (Ubuntu)

Guía para operadores. PC limpia o ya usada, con **sudo** e **Internet**.

---

## Requisitos

| Ítem | Detalle |
|------|---------|
| SO | Ubuntu 22.04 / 24.04 / 26.04 (Debian similar) |
| Red | Internet para descargar el wheel (~300 KB) |
| Usuario | Puede usar `sudo` |
| Puerto panel | `http://127.0.0.1:8080/` (solo en la PC; no expone a la LAN por defecto) |
| Después | Editar MACs en config; opcional `supabase.env` para nube |

---

## Método A — Kit desde GitHub (recomendado hoy, release v1.2.0)

No hace falta clonar el repo. En la **PC nueva**:

```bash
sudo apt-get update
sudo apt-get install -y curl ca-certificates

mkdir -p ~/tc-kit
cd ~/tc-kit

curl -fsSL -o tc_net_cert-1.2.0-py3-none-any.whl \
  https://github.com/UrbanoTCW24/tc-net-cert/releases/download/v1.2.0/tc_net_cert-1.2.0-py3-none-any.whl

curl -fsSL -o repo.tar.gz \
  https://github.com/UrbanoTCW24/tc-net-cert/archive/refs/tags/v1.2.0.tar.gz

tar -xzf repo.tar.gz
cp -a tc-net-cert-1.2.0/provision tc-net-cert-1.2.0/scripts .
cp tc_net_cert-1.2.0-py3-none-any.whl .

chmod +x provision/*.sh scripts/*.sh

sudo TC_NET_CERT_LAYOUT=4lan bash provision/install-station.sh
```

- **`TC_NET_CERT_LAYOUT=4lan`** → banco A (4 LAN). Para 8 LAN: `8lan`. Sin variable: detección automática.
- Al final debe decir: `Instalación lista` y `Panel: http://127.0.0.1:8080/`.

### Comprobar

```bash
/opt/tc-net-cert/venv/bin/python3 -c "from tc_net_cert.version import __version__; print(__version__)"
systemctl is-active tc-net-cert-web.service
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8080/
```

Esperado: `1.2.0`, `active`, `HTTP 200`.

---

## Método B — Si ya tienes el repo en la PC (como LAB-002)

```bash
cd ~/tc-net-cert-dev
bash scripts/assemble-install-kit.sh ~/tc-kit
sudo TC_NET_CERT_LAYOUT=4lan bash ~/tc-kit/provision/install-station.sh
```

O directo (wheel en `dist/`):

```bash
cd ~/tc-net-cert-dev
sudo TC_NET_CERT_LAYOUT=4lan bash provision/install-station.sh
```

> **No uses `/tmp/tc-kit` con sudo y luego scripts como usuario** — queda sin permiso de escritura. Usa `~/tc-kit`.

---

## Método C — Una línea (cuando esté publicado en GitHub `main`)

Tras `git push` del script `install-online.sh`:

```bash
curl -fsSL https://raw.githubusercontent.com/UrbanoTCW24/tc-net-cert/main/scripts/install-online.sh | \
  sudo TC_NET_CERT_LAYOUT=4lan bash
```

Si da **404**, el script aún no está en GitHub → use el **Método A**.

### Releases futuros (v1.2.1+)

Podrá existir un solo archivo:

```bash
curl -fsSL -o kit.tar.gz \
  https://github.com/UrbanoTCW24/tc-net-cert/releases/download/v1.2.1/tc-net-cert-install-1.2.1.tar.gz
mkdir -p ~/tc-kit && tar -xzf kit.tar.gz -C ~/tc-kit
sudo TC_NET_CERT_LAYOUT=4lan bash ~/tc-kit/provision/install-station.sh
```

En **v1.2.0** ese `.tar.gz` **no existe** (solo el `.whl`).

---

## Después de instalar (todas las PCs)

### 1. Configuración de red del banco

```bash
sudo nano /etc/tc-net-cert/config.yaml
```

- `station:` identificador (ej. `LAB-003` o dejar que Supabase asigne en versión futura).
- `operator:` nombre del operador.
- `ports:` MAC reales de **LAN1–LAN4** (u 8 en banco dual).

Ver MACs:

```bash
ip -o link show | grep -v lo
```

### 2. Portal del módem (contraseñas)

```bash
mkdir -p ~/.config/tc-net-cert
cp /ruta/al/repo/profiles/portals.example.yaml ~/.config/tc-net-cert/portals.yaml
chmod 600 ~/.config/tc-net-cert/portals.yaml
```

Editar hosts/usuarios/claves por modelo (CG-2200, ONT, etc.).

### 3. Supabase (corridas en nube + LAB-NNN automático)

Solo HQ entrega el archivo (no va en USB pública):

```bash
sudo install -m 600 supabase.env /root/.config/tc-net-cert/supabase.env
```

Requisito en proyecto Supabase: SQL de `docs/supabase/` (`lab_stations.sql`, `lab_certification_runs.sql`).

Registro manual (versión con overlay / futura):

```bash
sudo /opt/tc-net-cert/venv/bin/python3 -m tc_net_cert.cli.app register-station
```

### 4. Verificación

```bash
sudo /opt/tc-net-cert/venv/bin/python3 -m tc_net_cert.cli.app doctor
```

Abrir navegador en la misma PC: **http://127.0.0.1:8080/**

---

## Errores frecuentes

| Síntoma | Causa | Solución |
|---------|--------|----------|
| `curl: 404` en `install-online.sh` | Script no subido a GitHub | Método A |
| `curl: 404` en `tc-net-cert-install-*.tar.gz` | Solo en releases nuevos | Método A (wheel + tarball fuentes) |
| `Permiso denegado` en `/tmp/tc-kit` | Carpeta creada con `sudo` | `sudo rm -rf /tmp/tc-kit`; usar `~/tc-kit` |
| Se corta tras `apt` sin instalar wheel | Wheel no en raíz del kit | Método A o `assemble-install-kit.sh` |
| `sudo -E` ignorado | Política de sudo | `sudo VAR=valor bash script.sh` |
| Panel en 1.1.0 tras instalar | Wheel viejo en `dist/` | `assemble-install-kit` (baja 1.2.0) |

---

## Mensaje corto para otra sede (copiar/pegar)

```text
TECHCORP LAB — Ubuntu

1) curl wheel + repo tag v1.2.0 (comandos en docs/INSTALAR-UBUNTU.md método A)
2) sudo TC_NET_CERT_LAYOUT=4lan bash provision/install-station.sh
3) Editar /etc/tc-net-cert/config.yaml (MACs)
4) http://127.0.0.1:8080/
```

Repositorio: https://github.com/UrbanoTCW24/tc-net-cert  
Release: https://github.com/UrbanoTCW24/tc-net-cert/releases/tag/v1.2.0
