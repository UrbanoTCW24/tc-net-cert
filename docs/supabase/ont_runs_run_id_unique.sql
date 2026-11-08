-- ont_runs: upsert del panel usa ON CONFLICT (run_id).
-- Legacy: muchos labs compartieron el mismo run_id (TC-0023 × 14). No uses UNIQUE global en run_id.
-- Nuevo: run_id en nube = validation_number (ej. LAB-002-0001) — único por estación+orden.
-- Ejecutar en Supabase SQL Editor.

-- Duplicados legacy (información)
select run_id, count(*) as n
from public.ont_runs
group by run_id
having count(*) > 1
order by n desc
limit 30;

-- Duplicados solo en formato nuevo (debe estar vacío antes del índice)
select run_id, count(*) as n
from public.ont_runs
where run_id ~ '^LAB-[0-9]{3}-[0-9]{4}$'
group by run_id
having count(*) > 1;

-- Índice único PARCIAL: validation_number tc-net-cert (LAB-002-0001). No aplica a TC-* ni LAB-005-TC-0636.
drop index if exists public.ont_runs_run_id_uidx;
create unique index if not exists ont_runs_run_id_lab_validation_uidx
    on public.ont_runs (run_id)
    where run_id ~ '^LAB-[0-9]{3}-[0-9]{4}$';

comment on index public.ont_runs_run_id_lab_validation_uidx is
  'Upsert tc-net-cert: requiere ON CONFLICT (run_id) WHERE (run_id ~ ^LAB-[0-9]{3}-[0-9]{4}$) en save_run.';
