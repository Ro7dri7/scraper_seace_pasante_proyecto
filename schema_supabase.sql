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
      updated_at timestamptz default now()
    );
  end if;
end $$;

create table if not exists public.documentos_proceso (
  file_id text primary key,
  nomenclatura_norm text not null references public.convocatorias(nomenclatura_norm) on delete cascade,
  categoria text,
  documento text,
  nombre_archivo text,
  url_descarga text,
  ruta_local text,
  bytes bigint,
  updated_at timestamptz default now()
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

create index if not exists idx_conv_fecha on public.convocatorias (fecha_publicacion desc);
create index if not exists idx_conv_fuente on public.convocatorias (fuente);
create index if not exists idx_prov_ruc on public.proveedores (ruc);

-- Exponer a PostgREST (API)
notify pgrst, 'reload schema';
