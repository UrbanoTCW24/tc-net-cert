# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/). Versión en `package-overlay/tc_net_cert/version.py`.

## [Unreleased]

### Added
- Instaladores consultan manifiesto OTA al inicio (`check-release-offer.sh`); descarga automática si hay versión mayor.
- `profiles/`: `portals.example.yaml`, `run-profiles.yaml`, `acceptance-reference.yaml`.
- `fixtures/runs/TC-0002-emta-bancoA.sample.json` (contrato de informe).
- `docs/SCRAPER-AND-PROFILES.md`; perfiles incluidos en kit USB.

## [1.2.0] - 2026-11-08

### Added
- Pack DUT **CG-2200** (`dut-packs/cg-2200/`).
- Manifiesto de flota `release/fleet-manifest.json`.
- Docs: `FLEET-ROLLOUT.md`, ampliación GitHub CI/Release.
- SAFE MODE: rutas default por métrica; limpieza de default en NICs de lab antes del preflight.
- UI: mensajes claros si preflight aborta al instante.

### Fixed
- Certificación banco A bloqueada por `management-protection` en LAN1 cuando el CPE instalaba default route en puertos de prueba.

## [1.1.0] - anterior

- Dual banco A/B, sync perfiles, OTA Supabase, kit USB.

[1.2.0]: https://github.com/UrbanoTCW24/tc-net-cert/releases/tag/v1.2.0
