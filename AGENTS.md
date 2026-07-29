# AGENTS.md — Sistema de Facturación (invoice generator)

## Stack
- **Framework**: Flask (Jinja2 templates, no REST API framework)
- **Database**: MongoDB via PyMongo (Atlas URI in `.env`)
- **Python**: 3.14 (from `.venv`)
- **No tests, no linter, no typechecker, no CI**

## Project structure
```
app.py              ~2400 lines — routes, helpers, imports
database.py          MongoDB connection & collection globals
init_db.py           Creates DB: 10k tickets, config, indexes, factura counter
optimizar_db.py      Re-runs index creation + config upsert (safe re-run)
run_server.py        Entrypoint: flask dev server on 127.0.0.1:5000
templates/           9 Jinja2 templates (dashboard, vendedores, factura, etc.)
static/              Empty dir — all assets loaded from CDN
.agents/             Benchmark & debug scripts
```

## Developer commands
| Action | Command |
|---|---|
| Install deps | `pip install -r requirements.txt` |
| Initialize DB (destroys existing data) | `python init_db.py` |
| Create/recreate indexes & config | `python optimizar_db.py` |
| Run dev server | `python run_server.py` |
| Custom port | `PORT=8080 python run_server.py` |

## .env (required)
```
MONGO_URI=mongodb+srv://...
SECRET_KEY=...
```
Optional: `MONGO_DB`, `MONGO_TIMEOUT_MS`, `NOMBRE_RIFA`, `VALOR_BOLETA`, `COMISION_POR_BOLETA` (default 10000), `FLASK_HOST`, `FLASK_DEBUG`.

**`.gitignore` includes `.env`** — secrets are not tracked.

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
- **No blueprints** — all routes in a single `app.py`.

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
