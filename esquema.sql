--
-- PostgreSQL database dump
--

\restrict Af16mKQKKVN1Dpto2oewaA7Pg3frUCtCf7XJ8FndnzjBwxJk114OVun3bsuUWa6

-- Dumped from database version 15.19 (Ubuntu 15.19-1.pgdg24.04+2)
-- Dumped by pg_dump version 18.6 (Ubuntu 18.6-1.pgdg24.04+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: convocatorias; Type: TABLE; Schema: public; Owner: lici
--

CREATE TABLE public.convocatorias (
    nomenclatura_norm text NOT NULL,
    nomenclatura text,
    entidad text,
    fecha_publicacion timestamp with time zone,
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
    updated_at timestamp with time zone DEFAULT now(),
    id_contrato text,
    estado text,
    fecha_fin_cotizacion timestamp with time zone,
    requiere_iso text DEFAULT 'Bloqueado'::text
);


ALTER TABLE public.convocatorias OWNER TO lici;

--
-- Name: crm_negocios; Type: TABLE; Schema: public; Owner: lici
--

CREATE TABLE public.crm_negocios (
    id integer NOT NULL,
    comercial_id uuid,
    ruc_empresa text NOT NULL,
    nomenclatura_norm text,
    origen_lead text NOT NULL,
    etapa_negocio text DEFAULT 'Nuevo'::text,
    producto_id integer,
    monto_cotizado numeric(10,2) DEFAULT 0.00,
    motivo_rechazo text,
    notas_seguimiento text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.crm_negocios OWNER TO lici;

--
-- Name: crm_negocios_id_seq; Type: SEQUENCE; Schema: public; Owner: lici
--

CREATE SEQUENCE public.crm_negocios_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.crm_negocios_id_seq OWNER TO lici;

--
-- Name: crm_negocios_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: lici
--

ALTER SEQUENCE public.crm_negocios_id_seq OWNED BY public.crm_negocios.id;


--
-- Name: cubso_ciiu_match; Type: TABLE; Schema: public; Owner: lici
--

CREATE TABLE public.cubso_ciiu_match (
    cubso text NOT NULL,
    unspsc text,
    descripcion text,
    tipo text,
    ciiu_fab text,
    ciiu_fab_desc text,
    ciiu_com text,
    ciiu_com_desc text,
    razonamiento text
);


ALTER TABLE public.cubso_ciiu_match OWNER TO lici;

--
-- Name: cubso_items; Type: TABLE; Schema: public; Owner: lici
--

CREATE TABLE public.cubso_items (
    id bigint NOT NULL,
    codigo text NOT NULL,
    titulo text NOT NULL,
    tipo text NOT NULL,
    id_cubso text,
    match_ciiu_codigo text,
    match_ciiu_descripcion text,
    razonamiento text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cubso_items_tipo_check CHECK ((tipo = ANY (ARRAY['BIENES'::text, 'SERVICIOS'::text, 'OBRAS'::text])))
);


ALTER TABLE public.cubso_items OWNER TO lici;

--
-- Name: cubso_items_id_seq; Type: SEQUENCE; Schema: public; Owner: lici
--

CREATE SEQUENCE public.cubso_items_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.cubso_items_id_seq OWNER TO lici;

--
-- Name: cubso_items_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: lici
--

ALTER SEQUENCE public.cubso_items_id_seq OWNED BY public.cubso_items.id;


--
-- Name: documentos_proceso; Type: TABLE; Schema: public; Owner: lici
--

CREATE TABLE public.documentos_proceso (
    file_id text NOT NULL,
    nomenclatura_norm text NOT NULL,
    categoria text,
    documento text,
    nombre_archivo text,
    url_descarga text,
    updated_at timestamp with time zone DEFAULT now(),
    file_code text
);


ALTER TABLE public.documentos_proceso OWNER TO lici;

--
-- Name: items_proceso; Type: TABLE; Schema: public; Owner: lici
--

CREATE TABLE public.items_proceso (
    nomenclatura_norm text NOT NULL,
    secuencia integer NOT NULL,
    id_contrato_item bigint,
    codigo_cubso text,
    nombre_cubso text,
    descripcion_item text,
    cantidad numeric,
    unidad_medida text,
    distrito text,
    moneda text,
    precio_total numeric,
    updated_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.items_proceso OWNER TO lici;

--
-- Name: notificaciones; Type: TABLE; Schema: public; Owner: lici
--

CREATE TABLE public.notificaciones (
    id integer NOT NULL,
    perfil_id uuid,
    tipo text,
    titulo text NOT NULL,
    mensaje text NOT NULL,
    leido boolean DEFAULT false,
    enlace_accion text,
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.notificaciones OWNER TO lici;

--
-- Name: notificaciones_id_seq; Type: SEQUENCE; Schema: public; Owner: lici
--

CREATE SEQUENCE public.notificaciones_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.notificaciones_id_seq OWNER TO lici;

--
-- Name: notificaciones_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: lici
--

ALTER SEQUENCE public.notificaciones_id_seq OWNED BY public.notificaciones.id;


--
-- Name: perfiles; Type: TABLE; Schema: public; Owner: lici
--

CREATE TABLE public.perfiles (
    id uuid NOT NULL,
    rol text DEFAULT 'USUARIO'::text,
    ruc_empresa text,
    nombre_completo text NOT NULL,
    celular text,
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.perfiles OWNER TO lici;

--
-- Name: pipeline_usuarios; Type: TABLE; Schema: public; Owner: lici
--

CREATE TABLE public.pipeline_usuarios (
    id integer NOT NULL,
    perfil_id uuid,
    nomenclatura_norm text,
    etapa_licitacion text DEFAULT 'Sin estado'::text,
    notas_privadas text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.pipeline_usuarios OWNER TO lici;

--
-- Name: pipeline_usuarios_id_seq; Type: SEQUENCE; Schema: public; Owner: lici
--

CREATE SEQUENCE public.pipeline_usuarios_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.pipeline_usuarios_id_seq OWNER TO lici;

--
-- Name: pipeline_usuarios_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: lici
--

ALTER SEQUENCE public.pipeline_usuarios_id_seq OWNED BY public.pipeline_usuarios.id;


--
-- Name: postulaciones; Type: TABLE; Schema: public; Owner: lici
--

CREATE TABLE public.postulaciones (
    id integer NOT NULL,
    convocatoria_id integer,
    proveedor_ruc character varying(11),
    condicion character varying(50) NOT NULL,
    monto_adjudicado numeric(15,2) DEFAULT 0.00,
    consorcio_nombre text,
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.postulaciones OWNER TO lici;

--
-- Name: postulaciones_id_seq; Type: SEQUENCE; Schema: public; Owner: lici
--

CREATE SEQUENCE public.postulaciones_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.postulaciones_id_seq OWNER TO lici;

--
-- Name: postulaciones_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: lici
--

ALTER SEQUENCE public.postulaciones_id_seq OWNED BY public.postulaciones.id;


--
-- Name: productos_iso; Type: TABLE; Schema: public; Owner: lici
--

CREATE TABLE public.productos_iso (
    id integer NOT NULL,
    codigo text NOT NULL,
    nombre text NOT NULL,
    descripcion text,
    precio_referencial numeric(10,2) DEFAULT 0.00
);


ALTER TABLE public.productos_iso OWNER TO lici;

--
-- Name: productos_iso_id_seq; Type: SEQUENCE; Schema: public; Owner: lici
--

CREATE SEQUENCE public.productos_iso_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.productos_iso_id_seq OWNER TO lici;

--
-- Name: productos_iso_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: lici
--

ALTER SEQUENCE public.productos_iso_id_seq OWNED BY public.productos_iso.id;


--
-- Name: proveedores; Type: TABLE; Schema: public; Owner: lici
--

CREATE TABLE public.proveedores (
    ruc text NOT NULL,
    nomenclatura_norm text NOT NULL,
    razon_social text,
    condicion text,
    telefono text,
    email text,
    updated_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.proveedores OWNER TO lici;

--
-- Name: relacion_insumos; Type: TABLE; Schema: public; Owner: lici
--

CREATE TABLE public.relacion_insumos (
    id integer NOT NULL,
    convocatoria_id integer,
    descripcion_item text NOT NULL,
    unidad_medida character varying(50),
    cantidad numeric(15,2),
    precio_unitario numeric(15,2),
    origen_extraccion character varying(50) DEFAULT 'OCR_Bases'::character varying,
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE public.relacion_insumos OWNER TO lici;

--
-- Name: relacion_insumos_id_seq; Type: SEQUENCE; Schema: public; Owner: lici
--

CREATE SEQUENCE public.relacion_insumos_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.relacion_insumos_id_seq OWNER TO lici;

--
-- Name: relacion_insumos_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: lici
--

ALTER SEQUENCE public.relacion_insumos_id_seq OWNED BY public.relacion_insumos.id;


--
-- Name: crm_negocios id; Type: DEFAULT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.crm_negocios ALTER COLUMN id SET DEFAULT nextval('public.crm_negocios_id_seq'::regclass);


--
-- Name: cubso_items id; Type: DEFAULT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.cubso_items ALTER COLUMN id SET DEFAULT nextval('public.cubso_items_id_seq'::regclass);


--
-- Name: notificaciones id; Type: DEFAULT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.notificaciones ALTER COLUMN id SET DEFAULT nextval('public.notificaciones_id_seq'::regclass);


--
-- Name: pipeline_usuarios id; Type: DEFAULT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.pipeline_usuarios ALTER COLUMN id SET DEFAULT nextval('public.pipeline_usuarios_id_seq'::regclass);


--
-- Name: postulaciones id; Type: DEFAULT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.postulaciones ALTER COLUMN id SET DEFAULT nextval('public.postulaciones_id_seq'::regclass);


--
-- Name: productos_iso id; Type: DEFAULT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.productos_iso ALTER COLUMN id SET DEFAULT nextval('public.productos_iso_id_seq'::regclass);


--
-- Name: relacion_insumos id; Type: DEFAULT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.relacion_insumos ALTER COLUMN id SET DEFAULT nextval('public.relacion_insumos_id_seq'::regclass);


--
-- Name: convocatorias convocatorias_pkey; Type: CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.convocatorias
    ADD CONSTRAINT convocatorias_pkey PRIMARY KEY (nomenclatura_norm);


--
-- Name: crm_negocios crm_negocios_pkey; Type: CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.crm_negocios
    ADD CONSTRAINT crm_negocios_pkey PRIMARY KEY (id);


--
-- Name: cubso_ciiu_match cubso_ciiu_match_pkey; Type: CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.cubso_ciiu_match
    ADD CONSTRAINT cubso_ciiu_match_pkey PRIMARY KEY (cubso);


--
-- Name: cubso_items cubso_items_pkey; Type: CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.cubso_items
    ADD CONSTRAINT cubso_items_pkey PRIMARY KEY (id);


--
-- Name: documentos_proceso documentos_proceso_pkey; Type: CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.documentos_proceso
    ADD CONSTRAINT documentos_proceso_pkey PRIMARY KEY (file_id);


--
-- Name: items_proceso items_proceso_pkey; Type: CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.items_proceso
    ADD CONSTRAINT items_proceso_pkey PRIMARY KEY (nomenclatura_norm, secuencia);


--
-- Name: notificaciones notificaciones_pkey; Type: CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.notificaciones
    ADD CONSTRAINT notificaciones_pkey PRIMARY KEY (id);


--
-- Name: perfiles perfiles_pkey; Type: CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.perfiles
    ADD CONSTRAINT perfiles_pkey PRIMARY KEY (id);


--
-- Name: pipeline_usuarios pipeline_usuarios_perfil_id_nomenclatura_norm_key; Type: CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.pipeline_usuarios
    ADD CONSTRAINT pipeline_usuarios_perfil_id_nomenclatura_norm_key UNIQUE (perfil_id, nomenclatura_norm);


--
-- Name: pipeline_usuarios pipeline_usuarios_pkey; Type: CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.pipeline_usuarios
    ADD CONSTRAINT pipeline_usuarios_pkey PRIMARY KEY (id);


--
-- Name: postulaciones postulaciones_convocatoria_id_proveedor_ruc_key; Type: CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.postulaciones
    ADD CONSTRAINT postulaciones_convocatoria_id_proveedor_ruc_key UNIQUE (convocatoria_id, proveedor_ruc);


--
-- Name: postulaciones postulaciones_pkey; Type: CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.postulaciones
    ADD CONSTRAINT postulaciones_pkey PRIMARY KEY (id);


--
-- Name: productos_iso productos_iso_codigo_key; Type: CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.productos_iso
    ADD CONSTRAINT productos_iso_codigo_key UNIQUE (codigo);


--
-- Name: productos_iso productos_iso_pkey; Type: CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.productos_iso
    ADD CONSTRAINT productos_iso_pkey PRIMARY KEY (id);


--
-- Name: proveedores proveedores_pkey; Type: CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.proveedores
    ADD CONSTRAINT proveedores_pkey PRIMARY KEY (ruc, nomenclatura_norm);


--
-- Name: relacion_insumos relacion_insumos_pkey; Type: CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.relacion_insumos
    ADD CONSTRAINT relacion_insumos_pkey PRIMARY KEY (id);


--
-- Name: idx_conv_fecha; Type: INDEX; Schema: public; Owner: lici
--

CREATE INDEX idx_conv_fecha ON public.convocatorias USING btree (fecha_publicacion DESC);


--
-- Name: idx_conv_fuente; Type: INDEX; Schema: public; Owner: lici
--

CREATE INDEX idx_conv_fuente ON public.convocatorias USING btree (fuente);


--
-- Name: idx_conv_requiere_iso; Type: INDEX; Schema: public; Owner: lici
--

CREATE INDEX idx_conv_requiere_iso ON public.convocatorias USING btree (requiere_iso);


--
-- Name: idx_crm_comercial; Type: INDEX; Schema: public; Owner: lici
--

CREATE INDEX idx_crm_comercial ON public.crm_negocios USING btree (comercial_id);


--
-- Name: idx_crm_etapa; Type: INDEX; Schema: public; Owner: lici
--

CREATE INDEX idx_crm_etapa ON public.crm_negocios USING btree (etapa_negocio);


--
-- Name: idx_cubso_ciiu; Type: INDEX; Schema: public; Owner: lici
--

CREATE INDEX idx_cubso_ciiu ON public.cubso_items USING btree (match_ciiu_codigo);


--
-- Name: idx_cubso_ciiu_match_ciiu_com; Type: INDEX; Schema: public; Owner: lici
--

CREATE INDEX idx_cubso_ciiu_match_ciiu_com ON public.cubso_ciiu_match USING btree (ciiu_com);


--
-- Name: idx_cubso_ciiu_match_ciiu_fab; Type: INDEX; Schema: public; Owner: lici
--

CREATE INDEX idx_cubso_ciiu_match_ciiu_fab ON public.cubso_ciiu_match USING btree (ciiu_fab);


--
-- Name: idx_cubso_ciiu_match_tipo; Type: INDEX; Schema: public; Owner: lici
--

CREATE INDEX idx_cubso_ciiu_match_tipo ON public.cubso_ciiu_match USING btree (tipo);


--
-- Name: idx_cubso_ciiu_match_unspsc; Type: INDEX; Schema: public; Owner: lici
--

CREATE INDEX idx_cubso_ciiu_match_unspsc ON public.cubso_ciiu_match USING btree (unspsc);


--
-- Name: idx_cubso_tipo; Type: INDEX; Schema: public; Owner: lici
--

CREATE INDEX idx_cubso_tipo ON public.cubso_items USING btree (tipo);


--
-- Name: idx_items_cubso; Type: INDEX; Schema: public; Owner: lici
--

CREATE INDEX idx_items_cubso ON public.items_proceso USING btree (codigo_cubso);


--
-- Name: idx_perfiles_ruc; Type: INDEX; Schema: public; Owner: lici
--

CREATE INDEX idx_perfiles_ruc ON public.perfiles USING btree (ruc_empresa);


--
-- Name: idx_postulaciones_condicion; Type: INDEX; Schema: public; Owner: lici
--

CREATE INDEX idx_postulaciones_condicion ON public.postulaciones USING btree (condicion);


--
-- Name: idx_prov_ruc; Type: INDEX; Schema: public; Owner: lici
--

CREATE INDEX idx_prov_ruc ON public.proveedores USING btree (ruc);


--
-- Name: uq_cubso_codigo_tipo; Type: INDEX; Schema: public; Owner: lici
--

CREATE UNIQUE INDEX uq_cubso_codigo_tipo ON public.cubso_items USING btree (codigo, tipo);


--
-- Name: crm_negocios crm_negocios_comercial_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.crm_negocios
    ADD CONSTRAINT crm_negocios_comercial_id_fkey FOREIGN KEY (comercial_id) REFERENCES public.perfiles(id);


--
-- Name: crm_negocios crm_negocios_nomenclatura_norm_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.crm_negocios
    ADD CONSTRAINT crm_negocios_nomenclatura_norm_fkey FOREIGN KEY (nomenclatura_norm) REFERENCES public.convocatorias(nomenclatura_norm);


--
-- Name: crm_negocios crm_negocios_producto_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.crm_negocios
    ADD CONSTRAINT crm_negocios_producto_id_fkey FOREIGN KEY (producto_id) REFERENCES public.productos_iso(id);


--
-- Name: documentos_proceso documentos_proceso_nomenclatura_norm_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.documentos_proceso
    ADD CONSTRAINT documentos_proceso_nomenclatura_norm_fkey FOREIGN KEY (nomenclatura_norm) REFERENCES public.convocatorias(nomenclatura_norm) ON DELETE CASCADE;


--
-- Name: items_proceso items_proceso_nomenclatura_norm_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.items_proceso
    ADD CONSTRAINT items_proceso_nomenclatura_norm_fkey FOREIGN KEY (nomenclatura_norm) REFERENCES public.convocatorias(nomenclatura_norm) ON DELETE CASCADE;


--
-- Name: notificaciones notificaciones_perfil_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.notificaciones
    ADD CONSTRAINT notificaciones_perfil_id_fkey FOREIGN KEY (perfil_id) REFERENCES public.perfiles(id) ON DELETE CASCADE;


--
-- Name: pipeline_usuarios pipeline_usuarios_nomenclatura_norm_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.pipeline_usuarios
    ADD CONSTRAINT pipeline_usuarios_nomenclatura_norm_fkey FOREIGN KEY (nomenclatura_norm) REFERENCES public.convocatorias(nomenclatura_norm) ON DELETE CASCADE;


--
-- Name: pipeline_usuarios pipeline_usuarios_perfil_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.pipeline_usuarios
    ADD CONSTRAINT pipeline_usuarios_perfil_id_fkey FOREIGN KEY (perfil_id) REFERENCES public.perfiles(id) ON DELETE CASCADE;


--
-- Name: proveedores proveedores_nomenclatura_norm_fkey; Type: FK CONSTRAINT; Schema: public; Owner: lici
--

ALTER TABLE ONLY public.proveedores
    ADD CONSTRAINT proveedores_nomenclatura_norm_fkey FOREIGN KEY (nomenclatura_norm) REFERENCES public.convocatorias(nomenclatura_norm) ON DELETE CASCADE;


--
-- Name: cubso_ciiu_match Lectura pública del catálogo CUBSO-CIIU; Type: POLICY; Schema: public; Owner: lici
--

CREATE POLICY "Lectura pública del catálogo CUBSO-CIIU" ON public.cubso_ciiu_match FOR SELECT USING (true);


--
-- Name: convocatorias; Type: ROW SECURITY; Schema: public; Owner: lici
--

ALTER TABLE public.convocatorias ENABLE ROW LEVEL SECURITY;

--
-- Name: crm_negocios; Type: ROW SECURITY; Schema: public; Owner: lici
--

ALTER TABLE public.crm_negocios ENABLE ROW LEVEL SECURITY;

--
-- Name: cubso_ciiu_match; Type: ROW SECURITY; Schema: public; Owner: lici
--

ALTER TABLE public.cubso_ciiu_match ENABLE ROW LEVEL SECURITY;

--
-- Name: documentos_proceso; Type: ROW SECURITY; Schema: public; Owner: lici
--

ALTER TABLE public.documentos_proceso ENABLE ROW LEVEL SECURITY;

--
-- Name: items_proceso; Type: ROW SECURITY; Schema: public; Owner: lici
--

ALTER TABLE public.items_proceso ENABLE ROW LEVEL SECURITY;

--
-- Name: notificaciones; Type: ROW SECURITY; Schema: public; Owner: lici
--

ALTER TABLE public.notificaciones ENABLE ROW LEVEL SECURITY;

--
-- Name: perfiles; Type: ROW SECURITY; Schema: public; Owner: lici
--

ALTER TABLE public.perfiles ENABLE ROW LEVEL SECURITY;

--
-- Name: pipeline_usuarios; Type: ROW SECURITY; Schema: public; Owner: lici
--

ALTER TABLE public.pipeline_usuarios ENABLE ROW LEVEL SECURITY;

--
-- Name: postulaciones; Type: ROW SECURITY; Schema: public; Owner: lici
--

ALTER TABLE public.postulaciones ENABLE ROW LEVEL SECURITY;

--
-- Name: productos_iso; Type: ROW SECURITY; Schema: public; Owner: lici
--

ALTER TABLE public.productos_iso ENABLE ROW LEVEL SECURITY;

--
-- Name: proveedores; Type: ROW SECURITY; Schema: public; Owner: lici
--

ALTER TABLE public.proveedores ENABLE ROW LEVEL SECURITY;

--
-- Name: relacion_insumos; Type: ROW SECURITY; Schema: public; Owner: lici
--

ALTER TABLE public.relacion_insumos ENABLE ROW LEVEL SECURITY;

--
-- Name: SCHEMA public; Type: ACL; Schema: -; Owner: pg_database_owner
--

GRANT ALL ON SCHEMA public TO lici;
GRANT USAGE ON SCHEMA public TO postgres;


--
-- Name: DEFAULT PRIVILEGES FOR SEQUENCES; Type: DEFAULT ACL; Schema: public; Owner: postgres
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO lici;


--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: public; Owner: postgres
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT,INSERT,REFERENCES,DELETE,TRIGGER,TRUNCATE,UPDATE ON TABLES TO lici;


--
-- PostgreSQL database dump complete
--

\unrestrict Af16mKQKKVN1Dpto2oewaA7Pg3frUCtCf7XJ8FndnzjBwxJk114OVun3bsuUWa6

