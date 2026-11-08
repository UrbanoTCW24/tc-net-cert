# Instalar en otra PC (enlace + un comando)

PC **Ubuntu/Debian** con Internet, usuario con `sudo`. No hace falta USB si puede bajar desde GitHub.

## Opción A — una línea (recomendada)

Copie este enlace o ejecute en la PC nueva:

```bash
curl -fsSL https://raw.githubusercontent.com/UrbanoTCW24/tc-net-cert/main/scripts/install-online.sh | sudo bash
```

El script:

1. Lee la versión publicada en `release/fleet-manifest.json`.
2. Descarga el wheel y los scripts de instalación.
3. Instala en `/opt/tc-net-cert`, crea el servicio y abre el panel en `http://127.0.0.1:8080/`.

### Parámetros opcionales (misma línea, antes del pipe)

```bash
curl -fsSL https://raw.githubusercontent.com/UrbanoTCW24/tc-net-cert/main/scripts/install-online.sh | \
  sudo TC_NET_CERT_LAYOUT=4lan TC_NET_CERT_STATION=LAB-CR-01 bash
```

En algunas distros `sudo -E` no conserva el entorno; pase las variables **delante de** `bash` como arriba.

| Variable | Uso |
|----------|-----|
| `TC_NET_CERT_TAG=v1.2.0` | Fijar versión concreta |
| `TC_NET_CERT_LAYOUT=4lan\|8lan` | Plantilla de bancos |
| `TC_NET_CERT_STATION=…` | ID de estación (si no usa registro Supabase) |

## Opción B — kit desde GitHub (solo wheel en v1.2.0)

El archivo `tc-net-cert-install-*.tar.gz` aparece **desde el release posterior** a publicar `install-online.sh`. En **v1.2.0** solo está el `.whl`.

Desde el clon del repo:

```bash
cd ~/tc-net-cert-dev
bash scripts/assemble-install-kit.sh /tmp/tc-kit
sudo TC_NET_CERT_LAYOUT=4lan bash /tmp/tc-kit/provision/install-station.sh
```

En releases nuevos (ej. v1.2.1+):

```bash
curl -fsSL -o kit.tar.gz \
  https://github.com/UrbanoTCW24/tc-net-cert/releases/download/v1.2.1/tc-net-cert-install-1.2.1.tar.gz
sudo mkdir -p /tmp/tc-kit && sudo tar -xzf kit.tar.gz -C /tmp/tc-kit
sudo TC_NET_CERT_LAYOUT=4lan bash /tmp/tc-kit/provision/install-station.sh
```

## Después de instalar (todas las opciones)

1. Editar `/etc/tc-net-cert/config.yaml` — **MACs** de las NIC y operador.
2. Copiar `supabase.env` a `/root/.config/tc-net-cert/supabase.env` (chmod 600) para registro `LAB-NNN` y subida de corridas.
3. Portal: `profiles/portals.example.yaml` → `~/.config/tc-net-cert/portals.yaml` (claves del módem).
4. Verificar: `sudo /opt/tc-net-cert/venv/bin/python3 -m tc_net_cert.cli.app doctor`

## Sin Internet en la PC

Use el kit USB generado con `scripts/build-usb-kit.sh` (`dist_usb/`).

## Enlace corto para operadores

Texto listo para WhatsApp / correo:

> Instalar TECHCORP LAB:  
> `curl -fsSL https://raw.githubusercontent.com/UrbanoTCW24/tc-net-cert/main/scripts/install-online.sh | sudo bash`  
> Luego abrir http://127.0.0.1:8080/
