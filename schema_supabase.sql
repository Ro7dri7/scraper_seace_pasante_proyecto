-- LicitApp schema — pegar en Supabase → SQL Editor → Run
-- Si las tablas ya existen pero sin columnas, este script las completa.

create table if not exists public.convocatorias (
  nomenclatura_norm text primary key
);

alter table public.convocatorias add column if not exists nomenclatura text;
alter table public.convocatorias add column if not exists entidad text;
alter table public.convocatorias add column if not exists fecha_publicacion timestamptz;
alter table public.convocatorias add column if not exists objeto text;
alter table public.convocatorias add column if not exists descripcion text;
alter table public.convocatorias add column if not exists monto text;
alter table public.convocatorias add column if not exists moneda text;
alter table public.convocatorias add column if not exists fuente text;
alter table public.convocatorias add column if not exists ocid text;
alter table public.convocatorias add column if not exists ficha_url text;
alter table public.convocatorias add column if not exists url_bases text;
alter table public.convocatorias add column if not exists file_code text;
alter table public.convocatorias add column if not exists nid_convocatoria text;
alter table public.convocatorias add column if not exists nid_proceso text;
alter table public.convocatorias add column if not exists updated_at timestamptz default now();

-- PROD6 (Compras Menores <= 8 UIT, API REST)
alter table public.convocatorias add column if not exists id_contrato text;
alter table public.convocatorias add column if not exists estado text;
alter table public.convocatorias add column if not exists fecha_fin_cotizacion timestamptz;

-- Estrategia on-demand: el análisis IA arranca bloqueado y solo se dispara
-- cuando el usuario desbloquea la licitación en el frontend.
-- Los scrapers NO escriben esta columna: el DEFAULT la llena en el INSERT y
-- el upsert posterior no pisa el estado de desbloqueo.
alter table public.convocatorias
  add column if not exists requiere_iso text default 'Bloqueado';
alter table public.convocatorias
  alter column requiere_iso set default 'Bloqueado';
update public.convocatorias set requiere_iso = 'Bloqueado' where requiere_iso is null;

-- Si la PK no es nomenclatura_norm, hay que recrear. Caso típico: tabla stub vacía.
do $$
begin
  if not exists (
    select 1 from information_schema.columns
    where table_schema='public' and table_name='convocatorias' and column_name='nomenclatura_norm'
  ) then
    drop table if exists public.documentos_proceso cascade;
    drop table if exists public.proveedores cascade;
    drop table if exists public.convocatorias cascade;
    create table public.convocatorias (
      nomenclatura_norm text primary key,
      nomenclatura text,
      entidad text,
      fecha_publicacion timestamptz,
      objeto text,
      descripcion text,
      monto text,
      moneda text,
      fuente text,
      ocid text,
      ficha_url text,
      url_bases text,
      file_code text,
      nid_convocatoria text,
      nid_proceso text,
      id_contrato text,
      estado text,
      fecha_fin_cotizacion timestamptz,
      requiere_iso text default 'Bloqueado',
      updated_at timestamptz default now()
    );
  end if;
end $$;

-- Documentos: solo metadatos + URL dinámica de Alfresco. El binario NO se
-- descarga en el scraping; se resuelve on-demand al desbloquear la ficha.
create table if not exists public.documentos_proceso (
  file_id text primary key,
  nomenclatura_norm text not null references public.convocatorias(nomenclatura_norm) on delete cascade,
  categoria text,
  documento text,
  nombre_archivo text,
  file_code text,
  url_descarga text,
  updated_at timestamptz default now()
);

alter table public.documentos_proceso add column if not exists file_code text;
-- Legado del modo "descarga masiva": ya nadie escribe estas columnas.
alter table public.documentos_proceso drop column if exists ruta_local;
alter table public.documentos_proceso drop column if exists bytes;

-- Ítems de compras menores (uitContratoItemProjectionList de PROD6)
create table if not exists public.items_proceso (
  nomenclatura_norm text not null references public.convocatorias(nomenclatura_norm) on delete cascade,
  secuencia int not null,
  id_contrato_item bigint,
  codigo_cubso text,
  nombre_cubso text,
  descripcion_item text,
  cantidad numeric,
  unidad_medida text,
  distrito text,
  moneda text,
  precio_total numeric,
  updated_at timestamptz default now(),
  primary key (nomenclatura_norm, secuencia)
);

create table if not exists public.proveedores (
  ruc text not null,
  nomenclatura_norm text not null references public.convocatorias(nomenclatura_norm) on delete cascade,
  razon_social text,
  condicion text,
  telefono text,
  email text,
  updated_at timestamptz default now(),
  primary key (ruc, nomenclatura_norm)
);

-- PROD6: la ficha pública vive en el buscador, no en /compras-menores/detalle.
update public.convocatorias
set ficha_url = 'https://prod6.seace.gob.pe/buscador-publico/contrataciones/' || id_contrato
where fuente = 'PROD6'
  and id_contrato is not null
  and id_contrato <> ''
  and (
    ficha_url is null
    or ficha_url like '%/compras-menores/detalle/%'
  );

-- OECE: tipo de procedimiento + categoría (vienen del CSV UES).
alter table public.convocatorias add column if not exists tipo_procedimiento text;
alter table public.convocatorias add column if not exists categoria text;
alter table public.convocatorias add column if not exists fecha_inicio_consultas timestamptz;
alter table public.convocatorias add column if not exists fecha_fin_consultas timestamptz;
alter table public.convocatorias add column if not exists fecha_inicio_cotizacion timestamptz;

create table if not exists public.cronograma_proceso (
  nomenclatura_norm text not null references public.convocatorias(nomenclatura_norm) on delete cascade,
  id_etapa int not null,
  nombre_etapa text,
  fecha_inicio timestamptz,
  fecha_fin timestamptz,
  updated_at timestamptz default now(),
  primary key (nomenclatura_norm, id_etapa)
);

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

create or replace view public.convocatorias_con_antiguedad as
select
  c.*,
  greatest(
    0,
    (current_date - (c.fecha_publicacion at time zone 'America/Lima')::date)
  )::integer as dias_transcurridos
from public.convocatorias c;

create index if not exists idx_conv_fecha on public.convocatorias (fecha_publicacion desc);
create index if not exists idx_conv_fuente on public.convocatorias (fuente);
create index if not exists idx_conv_requiere_iso on public.convocatorias (requiere_iso);

-- Embudo de ahorro: procesos terminales no se vuelven a consultar en SEACE.
alter table public.convocatorias
  add column if not exists bloqueada boolean default false;
create index if not exists idx_conv_bloqueada
  on public.convocatorias (bloqueada)
  where bloqueada is true;
create index if not exists idx_prov_ruc on public.proveedores (ruc);
create index if not exists idx_items_cubso on public.items_proceso (codigo_cubso);

-- Exponer a PostgREST (API)
notify pgrst, 'reload schema';
