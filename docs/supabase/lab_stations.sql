-- Registro de estaciones (cada PC de laboratorio). El versionador asigna LAB-NNN al autenticarse.
-- Ejecutar en el mismo proyecto Supabase que lab_releases.

create sequence if not exists public.lab_station_number_seq start 1;

create table if not exists public.lab_stations (
    station_code text primary key,
    install_fingerprint text not null,
    hostname text not null default '',
    country_code text not null default 'GT',
    app_version text not null default '',
    bench_layout text not null default '',
    registered_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    constraint lab_stations_fingerprint_uq unique (install_fingerprint)
);

create index if not exists lab_stations_country_idx on public.lab_stations (country_code, last_seen_at desc);

comment on table public.lab_stations is
  'Inventario de PCs tc-net-cert; station_code = LAB-002 usado en corridas (validation_number).';

create or replace function public.register_lab_station(
    p_fingerprint text,
    p_hostname text default '',
    p_country text default 'GT',
    p_app_version text default '',
    p_layout text default '',
    p_requested_code text default null
) returns text
language plpgsql
security definer
set search_path = public
as $$
declare
    existing text;
    requested text;
    next_n bigint;
    code text;
begin
    if coalesce(trim(p_fingerprint), '') = '' then
        raise exception 'fingerprint required';
    end if;

    select station_code into existing
    from public.lab_stations
    where install_fingerprint = trim(p_fingerprint);

    if existing is not null then
        update public.lab_stations
        set last_seen_at = now(),
            hostname = coalesce(nullif(trim(p_hostname), ''), hostname),
            country_code = upper(coalesce(nullif(trim(p_country), ''), country_code)),
            app_version = coalesce(nullif(trim(p_app_version), ''), app_version),
            bench_layout = coalesce(nullif(trim(p_layout), ''), bench_layout)
        where station_code = existing;
        return existing;
    end if;

    requested := upper(trim(coalesce(p_requested_code, '')));
    if requested <> '' and requested not in ('LAB-XX', 'LAB-UNKNOWN') then
        perform 1 from public.lab_stations where station_code = requested;
        if not found then
            insert into public.lab_stations (
                station_code, install_fingerprint, hostname, country_code, app_version, bench_layout
            ) values (
                requested, trim(p_fingerprint), coalesce(p_hostname, ''), upper(coalesce(nullif(p_country, ''), 'GT')),
                coalesce(p_app_version, ''), coalesce(p_layout, '')
            );
            return requested;
        end if;
    end if;

    next_n := nextval('public.lab_station_number_seq');
    code := format('LAB-%s', lpad(next_n::text, 3, '0'));

    insert into public.lab_stations (
        station_code, install_fingerprint, hostname, country_code, app_version, bench_layout
    ) values (
        code, trim(p_fingerprint), coalesce(p_hostname, ''), upper(coalesce(nullif(p_country, ''), 'GT')),
        coalesce(p_app_version, ''), coalesce(p_layout, '')
    );

    return code;
end;
$$;

comment on function public.register_lab_station is
  'Idempotente por fingerprint; reclama p_requested_code si libre; si no, asigna LAB-NNN secuencial.';
