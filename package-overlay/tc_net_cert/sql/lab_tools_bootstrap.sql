create sequence if not exists public.lab_tool_number_seq start 1;

create table if not exists public.lab_tools (
    tool_number text primary key,
    station_id text not null default '',
    mac_host text not null,
    installed_version text not null default '',
    country_code text not null default 'GT',
    registered_at timestamptz not null default now(),
    last_seen timestamptz not null default now(),
    constraint lab_tools_mac_host_uq unique (mac_host)
);

create index if not exists lab_tools_station_idx on public.lab_tools (station_id, last_seen desc);

create or replace function public.register_or_update_tool(
    p_station_id text,
    p_mac_host text,
    p_installed_version text,
    p_country text default 'GT'
) returns text
language plpgsql
security definer
set search_path = public
as $$
declare
    existing text;
    next_n bigint;
    code text;
    mac_norm text;
begin
    mac_norm := lower(trim(coalesce(p_mac_host, '')));
    if mac_norm = '' then
        raise exception 'mac_host required';
    end if;

    select tool_number into existing
    from public.lab_tools
    where mac_host = mac_norm;

    if existing is not null then
        update public.lab_tools
        set last_seen = now(),
            station_id = coalesce(nullif(trim(p_station_id), ''), station_id),
            installed_version = coalesce(nullif(trim(p_installed_version), ''), installed_version),
            country_code = upper(coalesce(nullif(trim(p_country), ''), country_code))
        where tool_number = existing;
        return existing;
    end if;

    next_n := nextval('public.lab_tool_number_seq');
    code := format('TOOL-%s', lpad(next_n::text, 3, '0'));

    insert into public.lab_tools (
        tool_number, station_id, mac_host, installed_version, country_code
    ) values (
        code,
        coalesce(trim(p_station_id), ''),
        mac_norm,
        coalesce(trim(p_installed_version), ''),
        upper(coalesce(nullif(trim(p_country), ''), 'GT'))
    );

    return code;
end;
$$;

grant usage, select on sequence public.lab_tool_number_seq to postgres, service_role;
grant select, insert, update on public.lab_tools to postgres, service_role;
grant execute on function public.register_or_update_tool(text, text, text, text) to postgres, service_role;
