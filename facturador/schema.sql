-- Esquema SQLite del facturador (design.md §2.2).
-- Aplicado idempotente en cada conexión (CREATE TABLE IF NOT EXISTS).

-- Configuración global de la app (hoy: backups). Clave/valor para que
-- agregar un setting no requiera migración de esquema.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Emisores (la app es dueña de su configuración): la entidad es el emisor,
-- y cada uno declara con qué ambiente interactúa y qué puntos de venta
-- tiene habilitados (JSON). Un mismo ambiente puede tener varios emisores;
-- la app opera con el activo explícito (active_emisor_id_<ambiente>).
CREATE TABLE IF NOT EXISTS emisores (
    id                 TEXT PRIMARY KEY,
    razon_social       TEXT NOT NULL DEFAULT '',
    domicilio          TEXT NOT NULL DEFAULT '',
    iibb               TEXT NOT NULL DEFAULT '',
    inicio_actividades TEXT NOT NULL DEFAULT '',
    condicion_iva      TEXT NOT NULL DEFAULT '',
    ambiente           TEXT NOT NULL CHECK (ambiente IN ('homo', 'prod')),
    puntos_venta       TEXT NOT NULL DEFAULT '[1]',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS arca_params (
    kind        TEXT NOT NULL,
    code        TEXT NOT NULL,
    description TEXT,
    valid_from  TEXT,
    valid_to    TEXT,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (kind, code)
);

CREATE TABLE IF NOT EXISTS clients (
    id                  TEXT PRIMARY KEY,
    razon_social        TEXT NOT NULL,
    domicilio           TEXT NOT NULL DEFAULT '',
    pais_dst            INTEGER NOT NULL,
    cuit_pais           INTEGER NOT NULL,
    id_impositivo       TEXT NOT NULL DEFAULT '',
    moneda_default      TEXT NOT NULL DEFAULT 'DOL',
    incoterms_default   TEXT NOT NULL DEFAULT '',
    idioma_default      INTEGER NOT NULL DEFAULT 1,
    forma_pago_default  TEXT NOT NULL DEFAULT 'WIRE TRANSFER',
    descripcion_default TEXT NOT NULL DEFAULT '',
    is_default          INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id                 TEXT PRIMARY KEY,
    emisor_id          TEXT REFERENCES emisores(id),
    client_id          TEXT REFERENCES clients(id),
    arca_id            INTEGER UNIQUE,
    cbte_tipo          INTEGER NOT NULL DEFAULT 19,
    punto_venta        INTEGER NOT NULL,
    cbte_nro           INTEGER,
    status             TEXT NOT NULL DEFAULT 'draft' CHECK (status IN
                       ('draft','submitting','authorized','rejected','unknown')),
    fecha_cbte         TEXT NOT NULL,
    fecha_pago         TEXT NOT NULL,
    tipo_expo          INTEGER NOT NULL DEFAULT 2,
    permiso_existente  TEXT NOT NULL DEFAULT '',
    dst_cmp            INTEGER NOT NULL,
    cliente            TEXT NOT NULL,
    cuit_pais_cliente  INTEGER NOT NULL,
    domicilio_cliente  TEXT NOT NULL DEFAULT '',
    id_impositivo      TEXT NOT NULL DEFAULT '',
    moneda_id          TEXT NOT NULL DEFAULT 'DOL',
    moneda_ctz         TEXT NOT NULL,
    incoterms          TEXT NOT NULL DEFAULT '',
    incoterms_ds       TEXT NOT NULL DEFAULT '',
    forma_pago         TEXT NOT NULL DEFAULT '',
    idioma_cbte        INTEGER NOT NULL DEFAULT 1,
    imp_total          TEXT NOT NULL,
    obs                TEXT NOT NULL DEFAULT '',
    cae                TEXT,
    cae_fch_vto        TEXT,
    raw_request        TEXT,
    raw_response       TEXT,
    last_error         TEXT,
    environment        TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_items (
    id              TEXT PRIMARY KEY,
    invoice_id      TEXT NOT NULL REFERENCES invoices(id),
    pro_codigo      TEXT NOT NULL DEFAULT '0001',
    pro_ds          TEXT NOT NULL,
    pro_qty         TEXT NOT NULL DEFAULT '1',
    pro_umed        INTEGER NOT NULL DEFAULT 7,
    pro_precio_uni  TEXT NOT NULL,
    pro_total_item  TEXT NOT NULL
);
