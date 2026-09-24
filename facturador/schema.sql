-- Esquema SQLite del facturador (design.md §2.2).
-- Baseline del sistema de migraciones (versión 1).
-- No aplicar a mano: facturador.db.connect() lo corre vía
-- facturador.migrations (transaccional, una sola vez por DB).

-- Configuración global de la app (hoy: backups). Clave/valor para que
-- agregar un setting no requiera migración de esquema.
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Emisores (la app es dueña de su configuración): la entidad es el emisor,
-- LOCAL al perfil (ADR 0001) — la DB entera pertenece a un solo
-- ambiente. Un perfil puede tener varios emisores (una identidad fiscal:
-- el CUIT del certificado, sellado en settings.fiscal_cuit );
-- la app opera con el activo explícito (clave única active_emisor_id en
-- settings). La columna ambiente es el sello del perfil al crear, no una
-- elección del usuario.
CREATE TABLE emisores (
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

CREATE TABLE arca_params (
    kind        TEXT NOT NULL,
    code        TEXT NOT NULL,
    description TEXT,
    valid_from  TEXT,
    valid_to    TEXT,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (kind, code)
);

CREATE TABLE clients (
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

CREATE TABLE invoices (
    id                 TEXT PRIMARY KEY,
    emisor_id          TEXT REFERENCES emisores(id),
    client_id          TEXT REFERENCES clients(id),
    arca_id            INTEGER UNIQUE,
    cbte_tipo          INTEGER NOT NULL DEFAULT 19,
    punto_venta        INTEGER NOT NULL,
    cbte_nro           INTEGER,
    status             TEXT NOT NULL DEFAULT 'draft' CHECK (status IN
                       ('draft','submitting','authorized','rejected','unknown')),
    -- Procedencia: solo 'wsfex' es autoritativo para numeración /
    -- emisión. 'imported' es histórico de solo lectura y no cuenta
    -- en el chequeo de DB desactualizada frente a FEXGetLast_CMP.
    source             TEXT NOT NULL DEFAULT 'wsfex' CHECK (source IN
                       ('wsfex','imported')),
    fecha_cbte         TEXT NOT NULL,
    fecha_pago         TEXT NOT NULL,
    tipo_expo          INTEGER NOT NULL DEFAULT 2,
    permiso_existente  TEXT NOT NULL DEFAULT '',
    dst_cmp            INTEGER NOT NULL,
    -- Descripción de país/CUIT país/moneda resuelta al crear el borrador:
    -- el PDF no relee arca_params vivos.
    dst_cmp_ds         TEXT NOT NULL DEFAULT '',
    cliente            TEXT NOT NULL,
    cuit_pais_cliente  INTEGER NOT NULL,
    cuit_pais_cliente_ds TEXT NOT NULL DEFAULT '',
    domicilio_cliente  TEXT NOT NULL DEFAULT '',
    id_impositivo      TEXT NOT NULL DEFAULT '',
    moneda_id          TEXT NOT NULL DEFAULT 'DOL',
    moneda_ds          TEXT NOT NULL DEFAULT '',
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
    -- Identidad fiscal del perfil al crear: CUIT del certificado,
    -- inmutable; el PDF y la auditoría no releen el cert vivo.
    cuit_emisor        TEXT,
    -- Snapshot del emisor al crear el borrador: el PDF y la
    -- revisión usan estos campos, nunca la fila viva de emisores. emisor_id
    -- queda solo para trazabilidad.
    -- Intencional: vive en el baseline (migración v1). No hay DBs desplegadas
    -- que migrar — NO agregar una migración v2/ALTER solo por estas columnas.
    emisor_razon_social       TEXT NOT NULL DEFAULT '',
    emisor_domicilio          TEXT NOT NULL DEFAULT '',
    emisor_condicion_iva      TEXT NOT NULL DEFAULT '',
    emisor_iibb               TEXT NOT NULL DEFAULT '',
    emisor_inicio_actividades TEXT NOT NULL DEFAULT '',
    -- Contrato de render del PDF: versión del renderer que debe
    -- regenerar este comprobante. El registry de renderers despacha por esta versión.
    -- Intencional: vive en el baseline (migración v1). No hay DBs
    -- desplegadas que migrar — NO agregar una migración v2/ALTER solo
    -- por estas columnas de snapshot de render.
    pdf_render_version INTEGER NOT NULL DEFAULT 1,
    -- Auditoría inmutable (ADR 0001): sello del perfil al crear,
    -- validado contra el perfil corriente en cada acceso por id.
    environment        TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE invoice_items (
    id              TEXT PRIMARY KEY,
    invoice_id      TEXT NOT NULL REFERENCES invoices(id),
    pro_codigo      TEXT NOT NULL DEFAULT '0001',
    pro_ds          TEXT NOT NULL,
    pro_qty         TEXT NOT NULL DEFAULT '1',
    pro_umed        INTEGER NOT NULL DEFAULT 7,
    -- Descripción de U. Medida al crear el borrador; el PDF no
    -- relee arca_params ni el hardcode de constantes.
    pro_umed_ds     TEXT NOT NULL DEFAULT '',
    pro_precio_uni  TEXT NOT NULL,
    pro_total_item  TEXT NOT NULL
);

-- Huecos confirmados por ARCA durante rebuild/catch-up.
-- No cuentan para max_authorized_cbte_nro (solo source=wsfex authorized).
-- Intencional en el baseline (igual que los otros snapshots): no hay DBs desplegadas
-- que migrar — NO agregar una migración v2 solo por esta tabla.
CREATE TABLE registry_gaps (
    punto_venta INTEGER NOT NULL,
    cbte_tipo   INTEGER NOT NULL,
    cbte_nro    INTEGER NOT NULL,
    noted_at    TEXT NOT NULL,
    PRIMARY KEY (punto_venta, cbte_tipo, cbte_nro)
);
