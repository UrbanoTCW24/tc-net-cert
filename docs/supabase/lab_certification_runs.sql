-- Corridas de laboratorio (ONT + EMTA) con número de validación = orden TC-000N.
-- Ejecutar en el mismo proyecto Supabase que ont_runs (SQL editor).

create table if not exists public.lab_certification_runs (
    validation_number text primary key,
    run_id text not null,
    station text not null default '',
    operator text not null default '',
    country_code text not null default 'GT',
    bench_scope text not null default '',
    profile text not null default '',
    speed_profile text not null default '',
    dut_profile text not null default '',
    global_verdict text not null default '',
    brand text not null default '',
    model text not null default '',
    device_type text not null default '',
    serial text not null default '',
    mac text not null default '',
    software_version text not null default '',
    hardware_version text not null default '',
    started_at timestamptz,
    finished_at timestamptz,
    throughput jsonb not null default '[]'::jsonb,
    report_json jsonb,
    recorded_at timestamptz not null default now()
);

create index if not exists lab_cert_runs_serial_idx on public.lab_certification_runs (serial);
create index if not exists lab_cert_runs_station_idx on public.lab_certification_runs (station, recorded_at desc);
create index if not exists lab_cert_runs_verdict_idx on public.lab_certification_runs (global_verdict);

comment on table public.lab_certification_runs is
  'Certificación tc-net-cert. validation_number = orden TC-0001 (número de validación).';
comment on column public.lab_certification_runs.validation_number is
  'Global único: {station}-NNNN (ej. LAB-002-0042). run_id conserva TC-0042 local.';

-- Compatibilidad: ont_runs sigue recibiendo la misma corrida (panel histórico).
