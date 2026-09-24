# AGENTS.md — Sistema de Facturación (invoice generator)

## Stack
- **Framework**: Flask (Jinja2 templates, no REST API framework)
- **Database**: MongoDB via PyMongo (Atlas URI in `.env`)
- **Python**: 3.14 (from `.venv`)
- **Tests**: pytest (200+ functional tests, real MongoDB, DB de test única por proceso `boleteria_test_<pid>_<ts>`).
- **Lint/format**: Ruff (`pyproject.toml`). No typechecker.
- **CI**: GitHub Actions (`.github/workflows/test.yml`) — lint + pytest on push/PR. Requires secrets `MONGO_URI` y `SECRET_KEY` en el repo.
- **Servidor**: waitress (WSGI de producción) vía `run_server.py`; opcional reverse proxy HTTPS con nginx/Caddy (ver "Despliegue").

## Project structure
```
app.py              ~40 lines — Flask app factory, delegate to motores/ (requires SECRET_KEY)
database.py          MongoDB connection & collection globals
init_db.py           Creates DB: 10k tickets, config, indexes, factura counter
optimizar_db.py      Validates required indexes (creates missing ones) + config upsert
run_server.py        Entrypoint: waitress server (browser abierto salvo OPEN_BROWSER=0)
templates/           23 Jinja2 templates (dashboard, vendedores, facturas, error, etc.)
static/js/app.js     Extracted JS (theme toggle, keyboard shortcuts, formatting)
Dockerfile           Imagen para contenedor (app + waitress)
docker-compose.yml   App + nginx como reverse proxy con TLS
.env.example         Plantilla de variables de entorno para producción
deploy/              nginx.conf + Caddyfile (reverse proxy HTTPS)
scripts/
  backup.py          Respaldo automático ZIP de todas las colecciones (+ rotación)
  integridad.py      Verifica/repara el ledger (total_abonado == neto, estados, referencias)

motores/
  shared.py         101 — central re-export hub
  auth.py            62 — current_user, has_role, role_required, _es_solicitud_api
  csrf.py            28 — CSRF token generation + verification (per-request)
  cache.py           23 — TTL caches + invalidators
  ticket_service.py  85 — estado helpers, sync_ticket_statuses, movimiento_neto_expr
  config_service.py 103 — get_config, require_collections
  payment_service.py 285 — payment validation, invoice detail, abono ops, rollback
  fechas.py           6 — now_local (timezone)
  constants.py      138 — enums, defaults, XLSX namespaces
  validacion.py      90 — parsers (int, money, boleta, ticket_number_query)
  validacion_factura.py 206 — factura form validation helpers
  modelos.py         14 — crear_boleta_base
  excel_export.py    86 — XLSX generation helpers
  excel_import.py    30 — XLSX parsing helpers
  flask_integration.py 64 — template filters, before_request, context processor
  vendor_service.py 197 — vendor CRUD, snapshot, commission calc, next_vendedor_id
  dashboard_service.py 237 — dashboard stats & cache
  excel_service.py  241 — XLSX import/export (modelo_rifa, vendor assignments)
  rifa_lifecycle.py  75 — crear_nueva_rifa, crear_indices_boletas
  consulta_service.py 186 — build_consulta_context, build_page_url
  facturacion_common.py 40 — shared validation helpers (transfer, dedup, existence)
  egresos.py        264 — egreso invoice routes (comprobante interno, admin + caja)
  egreso_service.py  83 — egreso ledger ops (registrar/rollback) on historial_movimientos
  traslado_service.py 110 — traslado de saldo ops (movimientos entrada/salida + comprobante)
  traslados.py      176 — traslado routes (cambio de número, admin + caja)
  health.py          51 — /health endpoint (colecciones, factura_counter, índices requeridos)
  errores.py         52 — custom 404/500 handlers: HTML pages + JSON for /api/* (error.html)
  boletas.py        519 — ticket routes (consultas, guardar/limpiar cliente, APIs)
  pagos.py          426 — vendor panel routes
  rifas.py          192 — config & rifa lifecycle routes
  facturacion.py    181 — invoice list & detail routes (ver_factura)
  facturacion_cliente.py 235 — customer invoice creation routes
  facturacion_vendedor.py 330 — vendor invoice creation routes (dynamic rows + preview modal)
  reportes.py       208 — dashboard & export routes
  compradores.py    152 — buyer quick-entry routes
  usuarios.py       209 — user CRUD, authenticate, ensure_initial_admin
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
| Respaldo automático (ZIP + rotación) | `python scripts/backup.py [--dest DIR] [--keep N]` |
| Verificar integridad ledger | `python scripts/integridad.py` |
| Reparar movimientos pago faltantes | `python scripts/integridad.py reparar` |

## Tests (pytest)
- Suite: `tests/` — 200+ tests against real MongoDB, isolated DB `boleteria_test_<pid>_<ts>` (única por proceso: permite CI y corridas locales concurrentes sin pisarse datos; se elimina al terminar la sesión).
- `conftest.py` sets `MONGO_DB` to esa DB única **before** importing `app` (env var wins over `load_dotenv`), seeds 500 tickets + config + active rifa once per session, and resets collections before each test.
- `_warm_up()` (con retry ×3) ejecuta un count/find sobre cada colección tras la siembra para mitigar la primera petición fría contra Atlas.
- Never point tests at the production DB; the suite drops/resets everything in `MONGO_DB`.
- Coverage: validation parsers, ticket state machine, commission tiers, vendor CRUD + assign/remove/delete rules, customer invoices (full/partial/multiple/rejected cases), vendor invoices (incl. rollback on overpayment), invoice annulment, payment dedup, egreso invoices, traslados de saldo, health/API endpoints, HTML/JSON 404 errors.

## .env (required)
```
MONGO_URI=mongodb+srv://...
SECRET_KEY=...
```
Optional: `MONGO_DB`, `MONGO_TIMEOUT_MS`, `SERVER_SELECTION_TIMEOUT_MS` (alias), `MIN_POOL_SIZE` (default 0), 
`MAX_POOL_SIZE` (default 100), `MONGO_TLS_INSECURE` (default `false`; ponla en `true` solo si tu cluster Atlas requiere TLS sin verificación de CA), 
`NOMBRE_RIFA`, `VALOR_BOLETA`, `COMISION_POR_BOLETA` (default 10000), `FLASK_HOST`, `FLASK_DEBUG`,
`SESSION_COOKIE_SECURE` (default `0`; ponla en `1` si sirves por HTTPS), `SESSION_COOKIE_SAMESITE` (default `Lax`),
`SESSION_COOKIE_DAYS` (default `7`; días que dura la cookie de sesión, se refresca con cada petición autenticada),
`SESSION_IDLE_TIMEOUT_SECONDS` (default `1800`; inactividad máxima antes de cerrar sesión),
`TRUST_PROXY_HEADERS` (default `0`; ponla en `1` solo detrás de un reverse proxy para confiar en `X-Forwarded-*`),
`OPEN_BROWSER` (default `1`; `0` desactiva abrir el navegador al iniciar), 
`BACKUP_DIR` (default `respaldos`) y `BACKUP_KEEP` (default `30`) para `scripts/backup.py`,
`MAX_CONTENT_LENGTH_MB` (default 16).

**`.gitignore` includes `.env`** — secrets are not tracked.

**`app.py` fails to start with a clear error if `SECRET_KEY` is missing** — no hardcoded fallback.

## Despliegue en producción (HTTPS)
- La app escucha con **waitress** en `FLASK_HOST`/`PORT` (default `127.0.0.1:5000`). Para acceso externo coloca un **reverse proxy HTTPS** delante.
- **Opciones**:
  - **Caddy** (más simple, HTTPS automático): `deploy/Caddyfile` → `caddy run --config deploy/Caddyfile`. Requiere dominio real (o `--tls internal` para LAN).
  - **Docker + nginx** (TLS terminado): `docker-compose.yml` levanta `app` (imagen `Dockerfile`) + `nginx` con `deploy/nginx.conf`. Pon tus certificados en `deploy/certs/server.{crt,key}` (o los de certbot).
  - **nginx local**: copia `deploy/nginx.conf` y ajusta `server_name` y las rutas de certificado.
- Detrás de cualquier proxy debes activar en `.env`: `TRUST_PROXY_HEADERS=1` y `SESSION_COOKIE_SECURE=1` (la cookie solo viaja por HTTPS).
- `TRUST_PROXY_HEADERS=1` activa `ProxyFix` en `app.py` para que `url_for`/redirects generen enlaces `https://` correctos. No la actives si no hay proxy.
- `ADMIN_INICIAL_PASSWORD` por defecto es `admin`: cámbiala en `.env` antes de exponer el sistema.
- Usuarios y contaseñas: la app genera un admin inicial automáticamente si no existen usuarios (`ensure_initial_admin`).

## Ejecutables para la PC servidor (PyInstaller)
- **PyInstaller spec**: `boleteria.spec` genera en `dist/` el ejecutable `boleteria.exe` desde `.venv`. **Embebe** `templates/` y `static/`. Build: `python -m PyInstaller --noconfirm --clean boleteria.spec`. Los exes de backup/integridad (`BoleteriaBackup.exe`, `BoleteriaIntegridad.exe`) se construyen con specs separados si existen.
- **El `.env` NO se empaqueta**: en modo frozen (`sys.frozen`) `app.py`/`database.py` lo leen desde el directorio del `.exe`. Las credenciales quedan fuera del binario y se cambian sin recompilar.
- `dist/`, `build/`, `*.exe` están en `.gitignore`; `boleteria.spec` está versionado (`!boleteria.spec`).
- Kit de despliegue (copia a la PC servidor, misma carpeta): el exe + un `.env` con `MONGO_URI`, `SECRET_KEY`, `FLASK_HOST=0.0.0.0`, `PORT`, `ADMIN_INICIAL_PASSWORD`, `SESSION_COOKIE_SECURE`, `TRUST_PROXY_HEADERS`, `BACKUP_DIR`/`BACKUP_KEEP`.
- Para LAN interna no hace falta proxy: `SESSION_COOKIE_SECURE=0` y `TRUST_PROXY_HEADERS=0`.

## Architecture notes
- **Auth system**: session login with two roles (`admin`, `cajero`). `role_required(...)` guards every route (403 for HTML, JSON error for `/api/*`); menu visibility follows the same rules via `can(...)`. Dashboard, Configuración, Gestión de usuarios, Vendedores y respaldos son exclusivos del admin. Caja opera: consultas, compradores, facturas cliente/vendedor, egresos y traslados.
- **Primary feature**: generate printable invoices (facturas) from ticket sales and seller payments.
- **Payment cap rule**: No individual payment may exceed the ticket's `valor_boleta`. The accumulated `total_abonado` CAN exceed the ticket value without restriction.
- **Pagada terminal state with excedente**: A `pagada` ticket accepts additional payments via `confirmar_pagadas` flag (set on form + backend). When `confirmar_pagadas=1`, payments register as excedente without changing `estado` (stays `pagada`). Frontend warns and requires explicit confirmation before submitting; backend returns `requiere_confirmacion_pagadas` to prompt the UI confirmation dialog if flag is missing.
- **`confirmar_pagadas` flow**: Applied across `facturacion_cliente.py`, `facturacion_vendedor.py`, `compradores.py` (`_procesar_rows_rapido`), `payment_service.py` (`build_abono_preview`, `registrar_abono_lote`), `validacion_factura.py` (`_validar_boletas_en_db`). Frontend shows warning modal and badge in preview when pagada tickets detected.
- **Vendedor ID**: 100% interno y auto-asignado por el sistema (`VEND_0001`, `VEND_0002`, ...) vía `configuracion.vendedor_counter` (`next_vendedor_id`). Es persistente entre rifas y se asigna tanto en la creación manual (campo id vacío) como en la importación Excel para vendedores nuevos; el campo id no es editable por el usuario.
- **Unified ledger**: each ticket stores `historial_movimientos` with typed entries (`pago`, `egreso`, `traslado_entrada`, `traslado_salida`). Legacy entries without `tipo` count as `pago`.
- **Invoice detail**: built from `historial_movimientos` of the selected tickets (pago movements only).
- **Ticket numbers** are `int` in range 0000–9999 used as `_id` in MongoDB. Displayed zero-padded.
- **Five ticket states**: `disponible`, `asignada` (vendor assigned, no buyer yet), `separada` (buyer data saved without payments, any vendor — reserved, not for sale), `abonando` (partial payment), `pagada`.
- **Default vendedor**: `"LOCAL"` when no seller is assigned.
- **Commission**: flat fee per ticket (`comision_por_boleta`, default 10,000 COP). Configurable per vendor as tier-based via config page.
- **Config cached** in memory with 30-second TTL (`CONFIG_CACHE_SECONDS`).
- **Config doc** stored at `_id: "rifa"` in `configuracion` collection.
- **No blueprints** — each `motores/*.py` module registers routes directly via `register_routes(app)`.

## Special behaviors
- `invalidate_config_cache()` must be called after config writes.
- `sync_ticket_statuses(valor_boleta)` recalculates `estado` based on `total_abonado` after config changes.
- Duplicate `_id` writes will fail — `init_db.py` uses `delete_many({})` first.

## Ledger & data integrity
- `historial_movimientos` is the single source of truth per ticket; `total_abonado` must equal `movimiento_neto_expr()` (income `pago` + `traslado_entrada` minus `traslado_salida`; egresos excluded). `estado` derives from `total_abonado` only.
- A factura's `detalle` is built from the matching movements (`pago` for cliente/vendedor, `egreso` for egreso). `valor_total == sum(detalle)`.
- `rollback_pagos_por_factura(factura_id, valor_boleta)` removes only `pago` movements with that `factura_id` and recomputes `total_abonado`/`estado`. `rollback_egresos_por_factura(factura_id)` removes only `egreso` movements and never touches totals. `revertir_traslado(traslado_id)` removes both traslado movements and recomputes.
- Known historical incident: legacy payments recorded in `historial_pagos` before the unified-ledger migration can be missing from `historial_movimientos` even though `total_abonado` was kept. Verify with `python scripts/integridad.py` after any restore/import; repair with `python scripts/integridad.py reparar`, which reinserts the `pago` movement from the factura's `detalle` without changing `total_abonado`/`estado`.

## Routes overview
| Route | Module | Purpose |
|---|---|---|
| `/dashboard` | reportes | Dashboard with invoice + ticket stats (admin only) |
| `/consultas` | boletas | Ticket search with filters + pagination |

| `/vendedores` | pagos | CRUD + assign/remove ticket blocks + invoice |
| `/facturas` | facturacion | List of all invoices |
| `/facturas/vendedor` | facturacion | Vendor invoices list |
| `/facturas/cliente` | facturacion | Customer invoices list |
| `/facturas/<id>` | facturacion | Printable invoice view |
| `/facturas/egreso` | egresos | List of egreso comprobantes (admin + caja) |
| `/facturas/egreso/nueva` | egresos | Create egreso invoice (admin + caja) |
| `/traslados` | traslados | List of traslados de saldo (admin + caja) |
| `/compradores/reservas` | compradores | Reservas fijas: números fijos del local con comprador (admin); sobreviven a nueva rifa como separadas |
| `/traslados/nuevo` | traslados | Create traslado (admin + caja) |
| `/traslados/<id>` | traslados | Traslado comprobante (admin + caja) |
| `/facturas/nueva/cliente` | facturacion_cliente | Create customer invoice |
| `/facturas/nueva/vendedor` | facturacion_vendedor | Create seller invoice — dynamic table: enter tickets + amounts (different per ticket), registers payments + generates invoice |
| `/api/validar-factura` | facturacion | Real-time invoice validation (no writes) |
| `/configuracion` | rifas | Config edit |
| `/health` | health | Liveness: db connected, factura_counter, config doc, required indexes |
| `/api/boletas/<id>` | boletas | JSON ticket lookup |
| `/api/clientes` | boletas | Autocomplete (min 2 chars) |

## Invoices (facturas)
- Collection: `facturas` in MongoDB
- Auto-increment ID via `configuracion.factura_counter`
- Three types: `cliente` (customer purchase), `vendedor` (seller payment summary) and `egreso` (internal outflow comprobante, e.g. vendor commission)
- Template `factura.html`: print-friendly with `window.print()` support
- Accessible via `/facturas/<id>` and listed at `/facturas`
- **Vendor invoice creation** (`/facturas/nueva/vendedor`): dynamic form where user adds rows with ticket number(s) comma-separated + payment amount (different per ticket) + method + reference (hidden unless "transferencia"); shows a **preview modal** (grouped by amount) before confirming; each payment registered to the ticket with `factura_id` in `historial_movimientos`
- **Customer invoice template** (`factura_cliente.html`): "RECIBO DE PAGO / ABONO" layout; shows boleta info (price, state), movement type (ABONO/PAGO TOTAL/SEPARACIÓN), participation status per adicional, payment history table; supports multiple boletas per invoice; `boletas_info` passed from `ver_factura` route with `calcular_premios_adicionales`
