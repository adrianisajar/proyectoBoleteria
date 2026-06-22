# AGENTS.md — Boletería (raffle ticket system)

## Stack
- **Framework**: Flask (Jinja2 templates, no REST API framework)
- **Database**: MongoDB via PyMongo (Atlas URI in `.env`)
- **Python**: 3.14 (from `.venv`)
- **No tests, no linter, no typechecker, no CI**

## Project structure
```
app.py              ~2300 lines — routes, helpers, XLSX generation, imports
database.py          MongoDB connection & collection globals
init_db.py           Creates DB: 10k tickets (0000-9999), config, admin user, indexes
optimizar_db.py      Re-runs index creation + config/admin upsert (safe re-run)
run_server.py        Entrypoint: flask dev server on 127.0.0.1:5000
templates/           11 Jinja2 templates (dashboard, caja, vendedores, etc.)
static/              Empty dir — all assets loaded from CDN (Bootstrap 5.3, Bootstrap Icons)
.agents/             Empty directory
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

**No `.gitignore`** — `.env` with secrets is tracked. Be careful not to commit secrets.

## Architecture notes
- **No auth system** — removed. All routes are accessible without login. `current_user()` always returns admin.
- **Ticket numbers** are `int` in range 0000–9999 used as `_id` in MongoDB. Displayed zero-padded.
- **Three ticket states**: `disponible`, `abonado`, `pagada`.
- **Default vendedor**: `"LOCAL"` when no seller is assigned.
- **Commission**: flat fee per ticket (`comision_por_boleta`, default 10,000 COP). Configurable per vendor and as a global default. Tier-based logic (10k/15k/20k depending on volume) is pending implementation.
- **Config cached** in memory with 8-second TTL (`CONFIG_CACHE_SECONDS`).
- **Config doc** stored at `_id: "rifa"` in `configuracion` collection.
- **XLSX export** is handcrafted (raw XML in a zip) — no openpyxl dependency.
- **Modelo Rifa import** now only updates tickets already assigned to a vendor (non-LOCAL `vendedor_id`). LOCAL tickets are preserved unchanged.
- **Audit log** in `auditoria` collection (logged for all write operations).
- **No blueprints** — all routes in a single `app.py`.

## Special behaviors
- `invalidate_config_cache()` must be called after config writes.
- `sync_ticket_statuses(valor_boleta)` recalculates `estado` based on `total_abonado` after config changes.
- Duplicate `_id` writes will fail — `init_db.py` uses `delete_many({})` first.

## Routes overview
| Route | Purpose |
|---|---|
| `/dashboard` | Dashboard with stats + ranking |
| `/consultas` | Ticket search with filters + pagination |
| `/abono_masivo` | Batch payment registration |
| `/caja` | Quick POS-style payment + customer entry |
| `/vendedores` | CRUD + assign/remove/replace ticket blocks |
| `/reportes/exportar` | CSV/XLSX export (7 report types) |
| `/reportes/modelo-rifa.xlsx` | Full modelo rifa export |
| `/configuracion` | Config edit + "Nueva Rifa" reset + XLSX import |
| `/auditoria` | Audit log viewer |
| `/api/boletas/<id>` | JSON ticket lookup |
| `/api/clientes` | Autocomplete (min 2 chars) |
