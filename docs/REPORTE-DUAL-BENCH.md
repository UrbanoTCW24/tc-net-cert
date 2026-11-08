# Reporte: certificación dual 4+4 LAN (TECHCORP LAB / tc-net-cert)

**Fecha de referencia:** 2026-11-07  
**Versión objetivo:** 1.1.0

## Objetivo

- **Banco A** (LAN1–LAN4): certificar un equipo como hoy (iperf simultáneo, portal, orden TC por serial).
- **Banco B** (LAN5–LAN8): segundo equipo, **independiente** (lectura, orden TC, reinicio).
- **LAN nativa (onboard):** solo gestión e Internet → sync Supabase; **no** entra en certificación (`safe_mode`).
- **Wi‑Fi de la estación:** solo pruebas 2.4 / 5 GHz del módulo Wi‑Fi; no sustituye cables de prueba.

## Estado previo (v1.0.0)

| Área | Limitación |
|------|------------|
| Buscar / scrape | Un solo `found`; primer cable con portal OK |
| Identidad | Un serial en UI y `last-dut-identity.json` |
| Orden TC | Por serial al **certificar**; scrape sin `run_id` |
| Puertos | Instalador asignaba máximo **4** NIC |
| UI | Un bloque “equipo detectado” |

## Cambios v1.1.0 (esta implementación)

1. **Config `benches`** A/B con listas de LAN (default 1–4 y 5–8).
2. **`POST /api/benches/discover`**: lectura portal **por banco**, orden TC sugerida (`order_for_serial`).
3. **`POST /api/bench/reset`**: libera namespaces del banco (A o B) sin tocar el otro.
4. **`POST /api/ports/remap`**: reasigna MAC → LAN1…LAN8 (hasta 8 NIC de prueba).
5. **UI**: dos tarjetas de banco, Buscar todo / por banco, Certificar banco, Reiniciar banco, Remapear LAN.
6. **Certificación acotada**: `TC_NET_CERT_BENCH_PORTS` al correr solo mueve NIC del banco elegido.
7. **Remapeo / instalador**: hasta **8** interfaces Ethernet de prueba.

## Operación

1. Cablear 8 LAN (4+4); dejar **onboard** con ruta default (Internet).
2. **Remapear LAN** si cambió hardware.
3. **Buscar bancos** (cada uno con su lectura y orden TC prevista).
4. **Certificar banco A**, luego **B** (o reiniciar un banco si quedó namespace colgado).
5. Wi‑Fi: módulo aparte, tras leer SSID/claves del ONT del banco que corresponda.

## Despliegue en la estación

El código listo está en:

- `tc-net-cert-dev/package-overlay/tc_net_cert/` (Python + dashboard)
- `tc-net-cert-dev/package-overlay/techcorp-lab-root`
- `tc-net-cert-dev/scripts/deploy-dual-bench.sh`

En la PC del lab (con sudo):

```bash
bash /home/lab-002/tc-net-cert-dev/scripts/deploy-dual-bench.sh
```

Actualizar **sudoers** para permitir `bench-reset` y `ports-remap` (ver `docs/SUDOERS-BENCH.txt`). Reiniciar el proceso del panel en `:8080`.

## Uso en el panel (módulo ONT/EMTA)

1. **Remapear LAN** — detecta hasta 8 NIC de prueba; onboard queda protegida (Internet/BD).
2. **Buscar ambos bancos** — lectura portal independiente; orden TC sugerida por serial.
3. **Reiniciar banco A/B** — limpia namespaces de ese banco tras un corte.
4. **Certificar banco** — misma prueba simultaneous pero solo LAN1–4 o LAN5–8; orden TC propia por serial.

## Un solo radio Wi‑Fi (banco 1, luego banco 2)

| Paso | Acción |
|------|--------|
| 1 | **Buscar banco A** → guarda SSID/claves en `wifi-reading-bench-A.json` |
| 2 | **Wi‑Fi banco A** → el radio sale a la red del ONT A, mide **5 GHz** y luego **2.4** contra LAN2/LAN3, vuelve al lab |
| 3 | **Buscar banco B** → credenciales propias del ONT B |
| 4 | **Wi‑Fi banco B** → mismo radio, **5 GHz** y **2.4** contra LAN6/LAN7 |

No hay dos radios: las pruebas Wi‑Fi son **siempre secuenciales** (A completo, después B). La LAN onboard y el Wi‑Fi de gestión del lab no sustituyen esta secuencia.

## Pendiente (fases siguientes)

- Dos certificaciones en paralelo (dos procesos).
- Perfil DUT `ports: 8` vs dos corridas de 4.
- PDF dual en una sola vista.
