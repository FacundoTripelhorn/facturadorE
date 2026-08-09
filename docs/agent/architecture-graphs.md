# Architecture graphs (agents)

Curated Mermaid diagrams for **orientation and architectural intent**. They are
**navigation aids only** — not a substitute for reading the code.

**Authoritative sources:** implementation under `facturador/`, tests under
`tests/`, [`docs/design.md`](../design.md), and
[`docs/adr/0001-perfiles-de-ambiente-aislados.md`](../adr/0001-perfiles-de-ambiente-aislados.md)
(environment/profile contract). When a diagram disagrees with those, trust the
code, design doc, and ADR.

For file-level navigation, see [`repo-map.md`](repo-map.md). For commands and
constraints, see [`AGENTS.md`](../../AGENTS.md). For verification scopes by task,
see [`verification-matrix.md`](verification-matrix.md).

---

## Runtime architecture

One FacturadorE launcher, two hidden profiles (Homologación / Producción), one
immutable environment per backend process. Single-process FastAPI: JSON API +
Jinja/HTMX UI, SQLite, outbound SOAP to ARCA. No workers, queues, or external
database. Changing environment restarts the backend (no hot switching).

```mermaid
flowchart TB
  USER["Usuario"] --> CHOICE

  subgraph launcher["Launcher FacturadorE"]
    CHOICE{"Elegir ambiente<br/>Homologación / Producción"}
    SUP["ProcessSupervisor<br/>start · stop · readiness · lock"]
    CHOICE --> SUP
  end

  SUP -->|"arranca UN backend<br/>con el perfil elegido"| PROC

  subgraph proc["Backend process (uvicorn) — ambiente fijo"]
    direction TB
    API["api/<br/>JSON routes"]
    WEB["web/<br/>Jinja + HTMX + badge"]
    SVC["service.py<br/>InvoiceService"]
    REPO["repo/<br/>SQLite queries"]
    PDF["pdf/<br/>Playwright + QR"]
    ARCA["arca/<br/>WsaaClient + WsfexClient + WscdcClient"]
  end

  subgraph profiles["Perfiles internos ocultos (app-data del SO)"]
    SEL["Perfil ELEGIDO<br/>SQLite · cert.crt/key<br/>PDFs · TA · params<br/>logs · backups"]
    OTHER["Perfil del OTRO ambiente<br/>(intacto)"]
  end

  subgraph external["External (outbound HTTPS only)"]
    WSAA["ARCA WSAA<br/>LoginCms"]
    WSFEX["ARCA WSFEXv1<br/>FEXAuthorize, params, …"]
    WSCDC["ARCA WSCDC<br/>ComprobanteConstatar (FAC-84)"]
    S3["S3 backup<br/>(CLI, off critical path)"]
  end

  PROC -->|"lee/escribe SOLO su perfil"| SEL
  PROC -.-x|"nunca"| OTHER
  API --> SVC
  WEB --> SVC
  SVC --> REPO
  SVC --> ARCA
  SVC --> PDF
  ARCA --> WSAA
  ARCA --> WSFEX
  ARCA --> WSCDC
  subgraph backup_cli["Backup CLI (off critical path)"]
    BAK["backup.py / restore.py"]
  end
  BAK -.-> S3
  BAK -.-> SEL
```

Cambio de ambiente (siempre por reinicio):

```mermaid
sequenceDiagram
  actor U as Usuario
  participant L as Launcher
  participant BH as Backend (perfil homo)
  participant BP as Backend (perfil prod)

  U->>BH: Cambiar ambiente
  BH->>L: pedido de reinicio (archivo en perfil)
  L->>U: chooser (backend aún vivo)
  U->>L: confirma Producción
  L->>BH: stop limpio
  BH-->>L: proceso terminado
  L->>BP: start con perfil producción
  BP-->>L: ready (GET /health)
  L->>U: reabrir app (badge: Producción)
```

---

## Intentional boundaries

Layers are separated so domain rules and ARCA integration stay testable and
isolated from HTTP concerns.

```mermaid
flowchart LR
  subgraph presentation["Presentation"]
    API["api/*<br/>JSON + error mapping"]
    WEB["web/*<br/>HTML partials"]
  end

  subgraph domain["Domain"]
    SVC["service.py<br/>validation, state machine,<br/>numeración, idempotencia"]
    MAP["mappers.py"]
    SCH["schemas.py"]
  end

  subgraph persistence["Persistence"]
    REPO["repo/*"]
    DB[("SQLite (perfil)")]
  end

  subgraph integration["ARCA integration"]
    WSAA["arca/wsaa.py<br/>TA cache"]
    WSFEX["arca/wsfex.py<br/>SOAP client"]
  end

  subgraph output["Output"]
    PDF["pdf/*"]
  end

  subgraph config["Config (immutable at boot)"]
    PROF["profile.py<br/>EnvironmentProfile + ProfilePaths"]
    CFG["config.py<br/>load_config(profile)"]
    SET["settings.py<br/>emisor, PV, backups"]
    LAUNCH["launcher/<br/>chooser + supervisor"]
  end

  LAUNCH --> PROF
  PROF --> CFG
  API --> SVC
  WEB --> SVC
  SVC --> MAP
  SVC --> SCH
  SVC --> REPO
  SVC --> WSFEX
  SVC --> SET
  REPO --> DB
  WSFEX --> WSAA
  SVC --> PDF
  CFG --> WSAA
  CFG --> WSFEX
```

**Boundary rules:**

| Layer | Responsibility | Must not |
|---|---|---|
| `api/` / `web/` | HTTP, routing, thin handlers | Domain validation, SOAP, SQL |
| `service.py` | Business rules, authorize flow | Raw XML, template rendering |
| `repo/` | SQLite CRUD | ARCA calls, HTTP |
| `arca/` | WSAA/WSFEX SOAP | Invoice state machine |
| `pdf/` | HTML → PDF, QR payload | ARCA or DB writes |
| `launcher/` | Choose env, supervise one profile | Mutate ARCA clients in a live backend |
| `profile.py` | Resolve isolated roots / paths | Expose profile paths in normal UI |

---

## Forbidden dependency directions

These directions are **intentionally blocked**. Adding them is an architecture
regression unless `docs/design.md` is updated first.

```mermaid
flowchart TB
  subgraph allowed["Allowed (top → bottom)"]
    direction TB
    PRES["api/ · web/"]
    DOM["service.py"]
    PER["repo/ · pdf/"]
    INT["arca/"]
    EXT["ARCA SOAP"]
    PRES --> DOM
    DOM --> PER
    DOM --> INT
    INT --> EXT
  end

  subgraph forbidden["Forbidden (never add)"]
    direction LR
    F1["arca/ → repo/"]
    F2["arca/ → service.py"]
    F3["repo/ → arca/"]
    F4["repo/ → api/ · web/"]
    F5["pdf/ → arca/"]
    F6["external network → app<br/>(reverse proxy, tunnel, 0.0.0.0 bind)"]
    F7["secrets → repo / CI / logs"]
    F8["local counter alone<br/>(skip FEXGetLast_CMP)"]
    F9["hot-switch ARCA_ENV / certs / DB<br/>inside a live backend"]
  end

  style F1 fill:#fee,stroke:#c00
  style F2 fill:#fee,stroke:#c00
  style F3 fill:#fee,stroke:#c00
  style F4 fill:#fee,stroke:#c00
  style F5 fill:#fee,stroke:#c00
  style F6 fill:#fee,stroke:#c00
  style F7 fill:#fee,stroke:#c00
  style F8 fill:#fee,stroke:#c00
  style F9 fill:#fee,stroke:#c00
```

**Quick checks before merging:**

- `arca/` imports only `config`, `constants`, and sibling `wsaa`/`wsfex` — no `repo` or `service`.
- `repo/` has no `httpx`, no `arca` imports.
- `pdf/` receives already-authorized data; it does not call WSFEX.
- Uvicorn binds `127.0.0.1` on the host; Docker publishes `127.0.0.1:PORT` only.
- Environment is fixed at boot from `EnvironmentProfile`; no in-process ARCA/env swap.

---

## Invoice authorization sequence

Happy path and recovery branches for `POST /invoices/:id/authorize` (rules in
`docs/design.md` §2.3). All authorize calls are serialized (`_AUTHORIZE_LOCK`).

```mermaid
sequenceDiagram
  actor U as User / API client
  participant H as api/ or web/
  participant S as InvoiceService
  participant R as repo/
  participant W as WsfexClient
  participant A as ARCA WSFEX

  U->>H: POST …/authorize
  H->>S: authorize(invoice_id)
  Note over S: _AUTHORIZE_LOCK acquired

  S->>R: get_invoice
  alt already authorized
    S-->>H: same row (idempotent)
  else rejected
    S-->>H: 409 Conflict
  else draft / unknown / submitting
    S->>R: try_transition_to_submitting
    alt first time (raw_request is NULL)
      S->>W: FEXGetLast_CMP, FEXGetLast_ID
      S->>S: check DB vs ARCA numbering
      S->>R: persist arca_id, cbte_nro, raw_request
      Note over S,R: request written BEFORE SOAP call
      S->>W: FEXAuthorize
    else retry (raw_request exists)
      S->>W: FEXGetCMP (reconcile)
      alt found on ARCA
        S->>R: update authorized + CAE
      else not found
        S->>W: FEXAuthorize (reproceso, same Id)
      end
    end
    W->>A: SOAP FEXAuthorize
  end

  alt success
    A-->>W: CAE + vencimiento
    W-->>S: AuthResult
    S->>R: status=authorized, CAE, raw_response
    S->>S: render PDF to profile pdfs/
    S-->>H: authorized invoice
  else ARCA business error
    W-->>S: WsfexError
    S->>R: status=rejected
    S-->>H: rejected invoice
  else timeout / no response
    W-->>S: httpx error
    S->>R: status=unknown
    S-->>H: unknown (reconcile later)
  end

  H-->>U: response / redirect
```

---

## WSAA / WSFEX auth flow

Every WSFEX call needs a valid Ticket de Acceso (TA). WSAA is called only when
the cached TA is missing or near expiry.

```mermaid
sequenceDiagram
  participant S as InvoiceService / WsfexClient
  participant W as WsfexClient
  participant WA as WsaaClient
  participant DISK as TA cache (perfil)
  participant WSAA as ARCA WSAA
  participant WSFEX as ARCA WSFEX

  S->>W: authorize / get_last_cmp / get_params / …
  W->>WA: get_ticket()

  alt valid cached TA
    WA->>DISK: read cache
    DISK-->>WA: token + sign + expiration
    WA-->>W: Ticket
  else cache miss or expired
    WA->>WA: build TRA (service=wsfex)
    WA->>WA: CMS sign with cert + key
    WA->>WSAA: LoginCms (SOAP)
    WSAA-->>WA: token + sign
    WA->>WA: validate service & expiration
    WA->>DISK: persist cache
    WA-->>W: Ticket
  end

  W->>W: build SOAP body + Auth{Token, Sign, Cuit}
  W->>WSFEX: FEX* method (HTTPS)
  WSFEX-->>W: SOAP response
  W-->>S: parsed result / WsfexError
```

**Notes:**

- The boot `EnvironmentProfile` derives both WSAA and WSFEX URLs and cert paths
  atomically — never mix environments; never hot-switch mid-process.
- Token, sign, and CMS payloads are redacted in logs (credentials).
- Clock skew breaks WSAA; NTP is required on the host.

---

## Localhost security model

The app is a **local desktop tool**, not a network service. The only outbound
connections are to ARCA (and optional S3 backup via CLI).

```mermaid
flowchart TB
  subgraph internet["Internet"]
    ARCA["ARCA WSAA / WSFEX"]
    S3["S3 (backup CLI only)"]
  end

  subgraph host["User machine"]
    subgraph blocked["Blocked inbound"]
      NET["LAN / WAN clients"]
    end

    subgraph allowed_local["Allowed: localhost only"]
      BROWSER["Browser<br/>127.0.0.1:PORT"]
      subgraph bind["Server bind"]
        NATIVE["python -m facturador<br/>host = 127.0.0.1"]
        DOCKER["Docker container<br/>process 0.0.0.0:8399"]
        PUBLISH["docker-compose publish<br/>127.0.0.1:PORT → container"]
      end
    end

    subgraph secrets["Perfil activo (nunca en el repo)"]
      KEY["secrets/cert.key mode 400/600"]
      CRT["secrets/cert.crt"]
      DB[("SQLite + PDFs + TA")]
    end
  end

  NET -.->|"❌ no reverse proxy,<br/>tunnel, or 0.0.0.0 on host"| BROWSER
  BROWSER --> NATIVE
  BROWSER --> PUBLISH
  PUBLISH --> DOCKER
  NATIVE --> DB
  DOCKER --> DB
  KEY --> NATIVE
  KEY --> DOCKER
  NATIVE -->|"outbound HTTPS"| ARCA
  DOCKER -->|"outbound HTTPS"| ARCA
  secrets -.->|"age-encrypted .tar.age"| S3
```

**Do not add without a design change:**

- Uvicorn `0.0.0.0` on the bare host
- Reverse proxies, Tailscale/WireGuard exposure, or auth layers to reach the app
  from another machine
- Cert/key pairs in the repo, CI, or logs
- In-process hot switching of `ARCA_ENV`, certificates, or database
- Permissive CORS / `Access-Control-Allow-Origin: *`
- Accepting non-loopback `Host` or cross-origin `Origin`/`Referer` on
  state-changing requests (see `facturador/api/localhost_policy.py`, FAC-41)

**Allowed loopback forms** (ports = listen ∪ optional Docker public port):

| Header | Allowed values |
|--------|----------------|
| `Host` | `127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>` |
| `Origin` (unsafe methods) | `http://127.0.0.1:<port>`, `http://localhost:<port>`, `http://[::1]:<port>` |

- Native / launcher: one port (`FACTURADOR_PORT`).
- Docker: container listens on 8399; host publish may differ
  (`127.0.0.1:${FACTURADOR_PORT}:8399`). Compose sets `FACTURADOR_PUBLIC_PORT`
  to the host-side port so both internal healthchecks and the browser Host
  are accepted.

Unexpected `Host` → 400. Unexpected `Origin`/`Referer` on POST/PUT/PATCH/DELETE → 403.
Missing Origin/Referer on unsafe methods remains allowed for non-browser
clients (API / launcher health). Browser form POSTs under `/ui/` additionally
require a CSRF double-submit token (FAC-42, `facturador/api/csrf.py`): cookie
`facturador_csrf` + form field `csrf_token` (or `X-CSRF-Token` header).
Missing/invalid CSRF → 403. Token material is never logged or echoed.

**Future multi-machine use** (documented in `docs/design.md` §2.5): restore
encrypted backup on a secondary machine; still localhost-only on that machine.
Never run two instances emitting in parallel.

---

## Related reading

- [`repo-map.md`](repo-map.md) — where code lives by task type
- [`docs/design.md`](../design.md) — authoritative architecture and domain rules
- [`docs/adr/0001-perfiles-de-ambiente-aislados.md`](../adr/0001-perfiles-de-ambiente-aislados.md)
  — authoritative environment/profile contract (launcher-selected isolated profiles)
- [`AGENTS.md`](../../AGENTS.md) — agent contract, commands, security rules
