-- LicitApp — migración CRM / scrapers (idempotente)
-- Pegar en Supabase → SQL Editor → Run
-- Añade procedimiento OECE, cronograma PROD6, documentos PROD6 y ficha SUNAT/RNP.

-- ---------------------------------------------------------------------------
-- 1) convocatorias: procedimiento OECE + fechas clave del cronograma PROD6
-- ---------------------------------------------------------------------------
alter table public.convocatorias add column if not exists tipo_procedimiento text;
alter table public.convocatorias add column if not exists categoria text;
alter table public.convocatorias add column if not exists fecha_inicio_consultas timestamptz;
alter table public.convocatorias add column if not exists fecha_fin_consultas timestamptz;
alter table public.convocatorias add column if not exists fecha_inicio_cotizacion timestamptz;
alter table public.convocatorias add column if not exists fecha_integracion timestamptz;
alter table public.convocatorias add column if not exists fecha_presentacion timestamptz;
-- fecha_fin_cotizacion ya existía; se rellena desde la etapa 2 de PROD6.

-- Días transcurridos: NO se guarda. Es un valor vivo; se calcula en la vista.
create or replace view public.convocatorias_con_antiguedad as
select
  c.*,
  greatest(
    0,
    (current_date - (c.fecha_publicacion at time zone 'America/Lima')::date)
  )::integer as dias_transcurridos
from public.convocatorias c;

comment on view public.convocatorias_con_antiguedad is
  'Misma fila que convocatorias + dias_transcurridos calculado al vuelo (America/Lima).';

-- ---------------------------------------------------------------------------
-- 2) cronograma_proceso: una fila por etapa (Consultas, Cotización, …)
--    Mejor que aplastar todo en columnas: el portal puede añadir etapas.
--    Las 4 fechas de arriba son el denormalizado para filtros del CRM.
-- ---------------------------------------------------------------------------
create table if not exists public.cronograma_proceso (
  nomenclatura_norm text not null
    references public.convocatorias(nomenclatura_norm) on delete cascade,
  id_etapa int not null,
  nombre_etapa text,
  fecha_inicio timestamptz,
  fecha_fin timestamptz,
  updated_at timestamptz default now(),
  primary key (nomenclatura_norm, id_etapa)
);

create index if not exists idx_cronograma_fin
  on public.cronograma_proceso (fecha_fin);

-- ---------------------------------------------------------------------------
-- 3) proveedores: ficha comercial SUNAT / RNP (crítica para crm_negocios)
-- ---------------------------------------------------------------------------
alter table public.proveedores add column if not exists departamento text;
alter table public.proveedores add column if not exists provincia text;
alter table public.proveedores add column if not exists distrito text;
alter table public.proveedores add column if not exists estado_sunat text;
alter table public.proveedores add column if not exists condicion_domicilio text;
alter table public.proveedores add column if not exists habilitado_rnp text;
alter table public.proveedores add column if not exists apto_contratar text;
alter table public.proveedores add column if not exists ficha_rnp_url text;
alter table public.proveedores add column if not exists telefono_rnp text;
alter table public.proveedores add column if not exists email_rnp text;

-- Compatibilidad: si el CRM ya lee telefono/email, se siguen llenando.
-- telefono_rnp / email_rnp son alias explícitos de la misma fuente.

create index if not exists idx_prov_estado_sunat on public.proveedores (estado_sunat);
create index if not exists idx_prov_habilitado on public.proveedores (habilitado_rnp);
create index if not exists idx_conv_tipo_proc on public.convocatorias (tipo_procedimiento);

notify pgrst, 'reload schema';
