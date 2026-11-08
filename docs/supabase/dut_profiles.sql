-- TECHCORP LAB — reglas centralizadas por modelo (Supabase / Postgres)
-- Ejecutar en el proyecto Supabase del laboratorio (SQL editor).

create table if not exists public.dut_profiles (
    model_id text not null,
    country_code text not null default 'GT',
    min_throughput_mbps double precision not null default 900.0,
    allowed_gpon_states text[] not null default array['01', '05'],
    default_gateway inet not null default '192.168.1.1',
    optical_rx_low double precision not null default -27.0,
    optical_rx_high double precision not null default -8.0,
    notes text,
    updated_at timestamptz not null default now(),
    primary key (model_id, country_code)
);

create index if not exists dut_profiles_country_idx on public.dut_profiles (country_code);

comment on table public.dut_profiles is 'Umbrales y reglas DUT sincronizados a estaciones tc-net-cert (caché offline).';
comment on column public.dut_profiles.allowed_gpon_states is 'Códigos ONT aceptados; 01 válido en banco sin OLT.';

-- Semilla inicial — Huawei ONT evaluados en GT (ajuste country_code por país)
insert into public.dut_profiles (
    model_id, country_code, min_throughput_mbps, allowed_gpon_states,
    default_gateway, optical_rx_low, optical_rx_high, notes
) values
    (
        'HG8145X6-10', 'GT', 900.0, array['01', '05'],
        '192.168.1.1', -27.0, -8.0,
        'GPON — laboratorio GT; estado 01 informativo sin OLT'
    ),
    (
        'HG8245W5-6T', 'GT', 900.0, array['01', '05'],
        '192.168.1.1', -27.0, -8.0,
        'GPON — laboratorio GT'
    ),
    (
        'HG8145X6-10', 'DO', 900.0, array['01', '05'],
        '192.168.1.1', -27.0, -8.0,
        'Réplica DO'
    ),
    (
        'HG8245W5-6T', 'DO', 900.0, array['01', '05'],
        '192.168.1.1', -27.0, -8.0,
        'Réplica DO'
    )
on conflict (model_id, country_code) do update set
    min_throughput_mbps = excluded.min_throughput_mbps,
    allowed_gpon_states = excluded.allowed_gpon_states,
    default_gateway = excluded.default_gateway,
    optical_rx_low = excluded.optical_rx_low,
    optical_rx_high = excluded.optical_rx_high,
    notes = excluded.notes,
    updated_at = now();

-- Fallback global (opcional): filas con country_code = '*' aplican si no hay fila específica del país.
