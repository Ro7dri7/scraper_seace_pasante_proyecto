-- Embudo de ahorro: no reconsultar en SEACE procesos ya cerrados por OECE.
-- Pegar en Supabase → SQL Editor → Run.

alter table public.convocatorias
  add column if not exists bloqueada boolean default false;

update public.convocatorias
set bloqueada = true
where bloqueada is not true
  and estado ~* '(finaliz|culminad|otorgad|adjudicad|cancelad|anulad|desiert)';

create index if not exists idx_conv_bloqueada
  on public.convocatorias (bloqueada)
  where bloqueada is true;

comment on column public.convocatorias.bloqueada is
  'true = proceso terminal (Finalizada/Otorgada/Cancelada). PROD2/PROD6 no lo vuelven a consultar.';

notify pgrst, 'reload schema';
