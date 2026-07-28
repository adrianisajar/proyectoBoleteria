from motores.constants import BOLETA_MIN, BOLETA_MAX, VENDEDOR_LOCAL
from motores.shared import (
    boletas, request, render_template, url_for, jsonify,
    get_config, require_collections, role_required,
    invalidate_dashboard_cache, estado_pipeline_expr,
    flash, redirect,
)
from pymongo import UpdateOne


def register_routes(app):

    @app.route("/compradores/rapido", methods=["GET", "POST"])
    @role_required("admin", "cajero")
    def compradores_rapido():
        require_collections()

        if request.method == "POST":
            data = request.get_json(force=True)
            rows = data.get("rows", [])

            boleta_ids = [r["boleta"] for r in rows if isinstance(r.get("boleta"), int)]
            existing = {d["_id"] for d in boletas.find({"_id": {"$in": boleta_ids}}, {"_id": 1})}

            ops = []
            not_found = []
            ids_con_nombre = []
            ids_sin_datos = []
            for row in rows:
                bid = row.get("boleta")
                if not isinstance(bid, int) or bid not in existing:
                    if isinstance(bid, int):
                        not_found.append(bid)
                    continue
                nombre = (row.get("nombre") or "").strip().upper()
                telefono = (row.get("telefono") or "").strip()
                direccion = (row.get("direccion") or "").strip().upper()
                if not nombre and not telefono and not direccion:
                    ids_sin_datos.append(bid)
                    continue
                ops.append(UpdateOne(
                    {"_id": bid},
                    {"$set": {
                        "cliente.nombre": nombre,
                        "cliente.telefono": telefono,
                        "cliente.direccion": direccion,
                    }},
                ))
                if nombre:
                    ids_con_nombre.append(bid)

            updated = 0
            if ops:
                result = boletas.bulk_write(ops, ordered=False)
                updated = result.modified_count

                if ids_con_nombre:
                    boletas.update_many(
                        {
                            "_id": {"$in": ids_con_nombre},
                            "vendedor_id": {"$in": ["", None, VENDEDOR_LOCAL]},
                            "total_abonado": 0,
                        },
                        {"$set": {"vendedor_id": VENDEDOR_LOCAL}},
                    )

                config = get_config()
                valor_boleta_local = int(config.get("valor_boleta", 10000) or 10000)
                boletas.update_many(
                    {"_id": {"$in": ids_con_nombre}},
                    [{"$set": {"estado": estado_pipeline_expr(valor_boleta_local)}}],
                )

                invalidate_dashboard_cache()

            return jsonify({
                "ok": True,
                "updated": updated,
                "total": len(rows),
                "not_found": not_found,
                "sin_datos": ids_sin_datos,
            })

        config = get_config()
        return render_template("compradores_rapido.html", valor_boleta=config.get("valor_boleta", 0))

    @app.route("/api/compradores/validar", methods=["POST"])
    @role_required("admin", "cajero")
    def api_compradores_validar():
        require_collections()
        data = request.get_json(force=True) or {}
        boletas_list = data.get("boletas", [])
        if not boletas_list:
            return jsonify({"ok": True, "resultados": {}})

        int_ids = []
        for b in boletas_list:
            try:
                int_ids.append(int(b))
            except (ValueError, TypeError):
                pass

        docs = {d["_id"]: d for d in boletas.find(
            {"_id": {"$in": int_ids}},
            {"_id": 1, "cliente": 1},
        )}

        resultados = {}
        for b in boletas_list:
            try:
                bid = int(b)
            except (ValueError, TypeError):
                resultados[str(b)] = {"existe": False}
                continue
            doc = docs.get(bid)
            if not doc:
                resultados[str(b)] = {"existe": False}
            else:
                cliente = doc.get("cliente") or {}
                tiene_cliente = bool(cliente.get("nombre", "").strip())
                resultados[str(b)] = {
                    "existe": True,
                    "tiene_cliente": tiene_cliente,
                    "cliente": {
                        "nombre": cliente.get("nombre", ""),
                        "telefono": cliente.get("telefono", ""),
                        "direccion": cliente.get("direccion", ""),
                    }
                }
        return jsonify({"ok": True, "resultados": resultados})
