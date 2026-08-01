# AGENTS.md — Sistema de Facturación (invoice generator)

## Stack
- **Framework**: Flask (Jinja2 templates, no REST API framework)
- **Database**: MongoDB via PyMongo (Atlas URI in `.env`)
- **Python**: 3.14 (from `.venv`)
- **Tests**: pytest (58 functional tests, real MongoDB, DB de test única por proceso `boleteria_test_<pid>_<ts>`).
- **Lint/format**: Ruff (`pyproject.toml`). No typechecker.
- **CI**: GitHub Actions (`.github/workflows/test.yml`) — lint + pytest on push/PR. Requires secrets `MONGO_URI` y `SECRET_KEY` en el repo.

## Project structure
```
app.py              ~40 lines — Flask app factory, delegate to motores/ (requires SECRET_KEY)
database.py          MongoDB connection & collection globals
init_db.py           Creates DB: 10k tickets, config, indexes, factura counter
optimizar_db.py      Validates required indexes (creates missing ones) + config upsert
run_server.py        Entrypoint: flask dev server on 127.0.0.1:5000
templates/           10 Jinja2 templates (dashboard, vendedores, factura, error, etc.)
static/js/app.js     Extracted JS (theme toggle, keyboard shortcuts, formatting)

motores/
  shared.py          74 lines — central re-export hub
  auth.py             20 — current_user, has_role, role_required
  cache.py            26 — TTL caches + invalidators
  ticket_service.py   48 — estado helpers, sync_ticket_statuses
  config_service.py  110 — get_config, require_collections
  payment_service.py 272 — payment validation, invoice detail, abono ops
  fechas.py            — now_local (timezone)
  constants.py         — enums, defaults, XLSX namespaces
  validacion.py        — parsers (int, money, boleta, ticket_number_query)
  modelos.py           — crear_boleta_base
  excel_export.py      — XLSX generation helpers
  excel_import.py      — XLSX parsing helpers
  flask_integration.py 41 — template filters, before_request, context processor
  vendor_service.py  142 — vendor CRUD, snapshot, commission calc
  dashboard_service.py 182 — dashboard stats & cache
  excel_service.py    269 — XLSX import/export (modelo_rifa, vendor assignments)
  rifa_lifecycle.py   76 — crear_nueva_rifa, crear_indices_boletas
  consulta_service.py 165 — build_consulta_context, build_page_url
  facturacion_common.py 55 — shared validation helpers (transfer, dedup, existence)
  health.py           14 — /health endpoint (colecciones, factura_counter, índices requeridos)
  errores.py            — custom 404/500 handlers: HTML pages + JSON for /api/* (error.html)
  boletas.py           — ticket routes (consultas, cliente/BD redirects)
  pagos.py             — vendor panel routes
  rifas.py             — config & rifa lifecycle routes
  facturacion.py       — invoice list & detail routes
  facturacion_cliente.py  — customer invoice creation routes (~25 less due to dedup)
  facturacion_vendedor.py — vendor invoice creation routes (~25 less due to dedup)
  reportes.py          — dashboard & export routes
  compradores.py       — buyer quick-entry routes
tests/                pytest suite (conftest seeds/resets test DB per test)
.github/workflows/    CI: Ruff lint/format + pytest (secrets MONGO_URI, SECRET_KEY)
.agents/             Benchmark & debug scripts
```

## Developer commands
| Action | Command |
|---|---|
| Install deps | `pip install -r requirements.txt` |
| Install dev deps (incl. pytest) | `pip install -r requirements-dev.txt` |
| Lint (Ruff) | `python -m ruff check motores tests *.py` |
| Format check (Ruff) | `python -m ruff format --check motores tests *.py` |
| Run tests (uses DB única por proceso `boleteria_test_<pid>_<ts>`) | `pytest` |
| Run single test file | `pytest tests/test_vendedores.py` |
| Initialize DB (destroys existing data) | `python init_db.py` |
| Validate/create indexes & config | `python optimizar_db.py` |
| Run dev server | `python run_server.py` |
| Custom port | `PORT=8080 python run_server.py` |

## Tests (pytest)
- Suite: `tests/` — 58 tests against real MongoDB, isolated DB `boleteria_test_<pid>_<ts>` (única por proceso: permite CI y corridas locales concurrentes sin pisarse datos; se elimina al terminar la sesión).
- `conftest.py` sets `MONGO_DB` to esa DB única **before** importing `app` (env var wins over `load_dotenv`), seeds 500 tickets + config + active rifa once per session, and resets collections before each test.
- `_warm_up()` (con retry ×3) ejecuta un count/find sobre cada colección tras la siembra para mitigar la primera petición fría contra Atlas.
- Never point tests at the production DB; the suite drops/resets everything in `MONGO_DB`.
- Coverage: validation parsers, ticket state machine, commission tiers, vendor CRUD + assign/remove/delete rules, customer invoices (full/partial/multiple/rejected cases), vendor invoices (incl. rollback on overpayment), invoice annulment, payment dedup, health/API endpoints, HTML/JSON 404 errors.

## .env (required)
```
MONGO_URI=mongodb+srv://...
SECRET_KEY=...
```
Optional: `MONGO_DB`, `MONGO_TIMEOUT_MS`, `SERVER_SELECTION_TIMEOUT_MS` (alias), `MIN_POOL_SIZE` (default 0), 
`MAX_POOL_SIZE` (default 100), `MONGO_TLS_INSECURE` (default `false`; ponla en `true` solo si tu cluster Atlas requiere TLS sin verificación de CA), 
`NOMBRE_RIFA`, `VALOR_BOLETA`, `COMISION_POR_BOLETA` (default 10000), `FLASK_HOST`, `FLASK_DEBUG`,
`SESSION_COOKIE_SECURE` (default `0`; ponla en `1` si sirves por HTTPS), `SESSION_COOKIE_SAMESITE` (default `Lax`),
`MAX_CONTENT_LENGTH_MB` (default 16).

**`.gitignore` includes `.env`** — secrets are not tracked.

**`app.py` fails to start with a clear error if `SECRET_KEY` is missing** — no hardcoded fallback.

## Architecture notes
- **No auth system** — all routes accessible without login. `current_user()` always returns admin.
- **Primary feature**: generate printable invoices (facturas) from ticket sales and seller payments.
- **Invoice types**: `cliente` (customer data: name, address, phone) and `vendedor` (seller payment summary).
- **Factura ID**: auto-incrementing integer from `configuracion.factura_counter` (displayed zero-padded 5 digits).
- **Invoice detail**: built from `historial_pagos` of the selected tickets.
- **Ticket numbers** are `int` in range 0000–9999 used as `_id` in MongoDB. Displayed zero-padded.
- **Five ticket states**: `disponible`, `asignada` (vendor assigned), `separada` (client info saved), `abonando` (partial payment), `pagada`.
- **Default vendedor**: `"LOCAL"` when no seller is assigned.
- **Commission**: flat fee per ticket (`comision_por_boleta`, default 10,000 COP). Configurable per vendor as tier-based via config page.
- **Config cached** in memory with 30-second TTL (`CONFIG_CACHE_SECONDS`).
- **Config doc** stored at `_id: "rifa"` in `configuracion` collection.
- **No blueprints** — each `motores/*.py` module registers routes directly via `register_routes(app)`.

## Special behaviors
- `invalidate_config_cache()` must be called after config writes.
- `sync_ticket_statuses(valor_boleta)` recalculates `estado` based on `total_abonado` after config changes.
- Duplicate `_id` writes will fail — `init_db.py` uses `delete_many({})` first.

## Routes overview
| Route | Module | Purpose |
|---|---|---|
| `/dashboard` | reportes | Dashboard with invoice + ticket stats |
| `/consultas` | boletas | Ticket search with filters + pagination |

| `/vendedores` | pagos | CRUD + assign/remove ticket blocks + invoice |
| `/facturas` | facturacion | List of all invoices |
| `/facturas/vendedor` | facturacion | Vendor invoices list |
| `/facturas/cliente` | facturacion | Customer invoices list |
| `/facturas/<id>` | facturacion | Printable invoice view |
| `/facturas/nueva/cliente` | facturacion_cliente | Create customer invoice |
| `/facturas/nueva/vendedor` | facturacion_vendedor | Create seller invoice — dynamic table: enter tickets + amounts (different per ticket), registers payments + generates invoice |
| `/api/generar-factura` | facturacion | Create invoice (cliente or vendedor) via API |
| `/configuracion` | rifas | Config edit |
| `/health` | health | Liveness: db connected, factura_counter, config doc, required indexes |
| `/api/boletas/<id>` | boletas | JSON ticket lookup |
| `/api/clientes` | boletas | Autocomplete (min 2 chars) |

## Invoices (facturas)
- Collection: `facturas` in MongoDB
- Auto-increment ID via `configuracion.factura_counter`
- Two types: `cliente` (customer purchase) and `vendedor` (seller payment summary)
- Template `factura.html`: print-friendly with `window.print()` support
- Accessible via `/facturas/<id>` and listed at `/facturas`
- **Vendor invoice creation** (`/facturas/nueva/vendedor`): dynamic form where user adds rows with ticket number(s) comma-separated + payment amount (different per ticket) + method + reference (hidden unless "transferencia"); shows a **preview modal** (grouped by amount) before confirming; each payment registered to the ticket with `factura_id` in `historial_pagos`
- **Vendor invoice template** (`factura_vendedor.html`): "COMPROBANTE DE RECAUDO" layout; detalle **grouped by amount** (Jinja2 `groupby` filter); boleta numbers displayed in CSS grid per group; observations, signature lines; commissions summary; `page-break-inside: avoid` per group for clean printing
- **Customer invoice template** (`factura_cliente.html`): "RECIBO DE PAGO / ABONO" layout; shows boleta info (price, state), movement type (ABONO/PAGO TOTAL/SEPARACIÓN), participation status per adicional, payment history table; supports multiple boletas per invoice; `boletas_info` passed from `ver_factura` route with `calcular_premios_adicionales`
