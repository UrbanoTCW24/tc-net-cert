-- Release OTA del panel tc-net-cert (wheel en Storage).
-- Tras subir el .whl, inserte o actualice la fila del canal lab.

create table if not exists public.lab_releases (
    channel text not null default 'lab',
    version text not null,
    whl_url text not null,
    sha256 text,
    mandatory boolean not null default false,
    is_active boolean not null default true,
    notes text,
    updated_at timestamptz not null default now(),
    primary key (channel, version)
);

create index if not exists lab_releases_active_idx
    on public.lab_releases (channel, is_active, updated_at desc);

comment on table public.lab_releases is 'Oferta OTA para estaciones; el panel compara version vs local.';

-- Ejemplo (sustituya whl_url por la URL pública de Storage tras upload_release.py):
-- insert into public.lab_releases (channel, version, whl_url, sha256, mandatory, notes)
-- values (
--   'lab', '1.1.0',
--   'https://<ref>.supabase.co/storage/v1/object/public/lab-releases/releases/tc_net_cert-1.1.0-py3-none-any.whl',
--   '<sha256 del wheel>',
--   false,
--   'Release inicial empaquetado'
-- )
-- on conflict (channel, version) do update set
--   whl_url = excluded.whl_url,
--   sha256 = excluded.sha256,
--   mandatory = excluded.mandatory,
--   is_active = true,
--   updated_at = now();
