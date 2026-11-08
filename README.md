# tc-net-cert

Estación **TECHCORP LAB** — certificación Ethernet (iperf3, bancos A/B, portal ONT/EMTA).

- Versión del paquete: `package-overlay/tc_net_cert/version.py`
- Panel local: `http://127.0.0.1:8080/`
- Build / OTA: [docs/BUILD-AND-RELEASE.md](docs/BUILD-AND-RELEASE.md)
- GitHub / tags: [docs/VERSIONING-AND-GITHUB.md](docs/VERSIONING-AND-GITHUB.md)

## DUT piloto en LAB-002

| Campo | Valor (ejemplo banco A) |
|--------|-------------------------|
| Modelo | **CG-2200** (Kaon, DOCSIS / EMTA) |
| Serie | `BS10007554006224` |
| Portal | `192.168.1.1` |
| Módulo panel | **EMTA DOCSIS** |

Perfil versionado (sin contraseñas): [dut-packs/cg-2200/](dut-packs/cg-2200/).

## Repositorio en GitHub

Propietario: **UrbanoTCW24** → `UrbanoTCW24/tc-net-cert`

```bash
cd ~/tc-net-cert-dev
git add -A && git commit -m "docs: README + DUT pack CG-2200"
git push -u origin main
git push origin v1.2.0
```

No subir: `config.yaml` de la PC, `portals.yaml` con claves, `supabase.env`.
