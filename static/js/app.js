(function() {
    'use strict';

    var root = document.documentElement;
    var button = document.getElementById("themeToggle");
    var icon = document.getElementById("themeIcon");

    window.getCsrfToken = function() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute("content") : "";
    };

    window.fetchWithCsrf = function(url, options) {
        options = options || {};
        options.headers = Object.assign({}, options.headers, { "X-CSRF-Token": window.getCsrfToken() });
        return fetch(url, options);
    };

    var refreshIcon = function() {
        var theme = root.getAttribute("data-bs-theme");
        icon.className = theme === "dark" ? "bi bi-sun" : "bi bi-moon-stars";
    };

    if (button && icon) {
        button.addEventListener("click", function() {
            var nextTheme = root.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
            root.setAttribute("data-bs-theme", nextTheme);
            localStorage.setItem("boleteria-theme", nextTheme);
            refreshIcon();
        });
        refreshIcon();
    }

    document.querySelectorAll("[data-autodismiss]").forEach(function(alertNode) {
        window.setTimeout(function() {
            bootstrap.Alert.getOrCreateInstance(alertNode).close();
        }, 4800);
    });

    var analyzeTickets = function(value) {
        var parts = value.trim().split(/[\s,;]+/).filter(Boolean);
        var counts = new Map();
        var valid = 0;
        var invalid = 0;
        var outOfRange = 0;

        parts.forEach(function(part) {
            if (!/^\d+$/.test(part)) {
                invalid += 1;
                return;
            }
            var number = Number(part);
            if (number < 0 || number > 9999) {
                outOfRange += 1;
                return;
            }
            valid += 1;
            counts.set(number, (counts.get(number) || 0) + 1);
        });

        var unique = counts.size;
        var duplicates = Array.from(counts.values()).reduce(function(total, count) { return total + Math.max(count - 1, 0); }, 0);
        return { valid: valid, unique: unique, duplicates: duplicates, invalid: invalid, outOfRange: outOfRange };
    };

    document.querySelectorAll("[data-ticket-input]").forEach(function(input) {
        var counter = document.querySelector(input.dataset.ticketCounter);
        var helper = document.querySelector(input.dataset.ticketHelper);
        if (!counter) return;

        var autoComma = function() {
            var sel = input.selectionStart;
            var before = input.value;
            var cleaned = before.replace(/[,\s;]+/g, ",");
            var parts = cleaned.split(",").map(function(p) {
                if (/^\d{4,}$/.test(p)) return p.replace(/(\d{4})(?=\d)/g, "$1,");
                return p;
            });
            var after = parts.join(",");
            if (after !== before) {
                input.value = after;
                var shift = after.length - before.length;
                input.selectionStart = input.selectionEnd = Math.min(sel + shift, after.length);
            }
        };

        var refreshTicketStats = function() {
            var stats = analyzeTickets(input.value);
            counter.textContent = stats.unique + " unica" + (stats.unique === 1 ? "" : "s");
            counter.className = "badge border " + (stats.invalid || stats.outOfRange ? "text-bg-warning" : "text-bg-light");

            if (helper) {
                var notes = [];
                if (stats.duplicates) notes.push(stats.duplicates + " duplicada" + (stats.duplicates === 1 ? "" : "s"));
                if (stats.invalid) notes.push(stats.invalid + " no numerica" + (stats.invalid === 1 ? "" : "s"));
                if (stats.outOfRange) notes.push(stats.outOfRange + " fuera de rango");
                helper.textContent = notes.length ? notes.join(" \u00b7 ") : stats.valid + " entrada" + (stats.valid === 1 ? "" : "s") + " valida" + (stats.valid === 1 ? "" : "s");
                helper.classList.toggle("text-warning", Boolean(stats.invalid || stats.outOfRange || stats.duplicates));
                helper.classList.toggle("text-secondary", !(stats.invalid || stats.outOfRange || stats.duplicates));
            }
        };

        input.addEventListener("input", function() { autoComma(); refreshTicketStats(); });
        refreshTicketStats();
    });

    var formatMoneyField = function(input) {
        var digits = input.value.replace(/\D/g, "");
        input.value = digits ? Number(digits).toLocaleString("es-CO") : "";
    };

    var SANITIZERS = {
        numbers: function(v) { return v.replace(/[^0-9]/g, ""); },
        name: function(v) { return v.replace(/[^A-Za-z\u00c1\u00c9\u00cd\u00d3\u00da\u00dc\u00d1\u00e1\u00e9\u00ed\u00f3\u00fa\u00fc\u00f1 .'-]/g, ""); },
        address: function(v) { return v.replace(/[^A-Za-z\u00c1\u00c9\u00cd\u00d3\u00da\u00dc\u00d1\u00e1\u00e9\u00ed\u00f3\u00fa\u00fc\u00f10-9 .,#\/-]/g, ""); },
        titulo: function(v) { return v.replace(/[^A-Za-z\u00c1\u00c9\u00cd\u00d3\u00da\u00dc\u00d1\u00e1\u00e9\u00ed\u00f3\u00fa\u00fc\u00f10-9 .,'&\-()]/g, ""); },
        referencia: function(v) { return v.replace(/[^A-Za-z0-9_\-.\/]/g, ""); },
        tickets: function(v) { return v.replace(/[^0-9,\s;]/g, ""); }
    };

    var inputValidationMode = function(t) {
        if (t.dataset.moneyInput !== undefined) return "money";
        var mode = t.dataset.validation;
        return mode && SANITIZERS[mode] ? mode : null;
    };

    var applySanitized = function(input, mode) {
        var old = input.value;
        var cleaned = SANITIZERS[mode](old);
        if (input.maxLength > 0 && cleaned.length > input.maxLength) cleaned = cleaned.slice(0, input.maxLength);
        if (cleaned === old) return;
        var pos = input.selectionStart != null ? input.selectionStart : old.length;
        var keptBefore = SANITIZERS[mode](old.slice(0, pos)).length;
        input.value = cleaned;
        var np = Math.min(keptBefore, cleaned.length);
        input.selectionStart = input.selectionEnd = np;
    };

    document.addEventListener("input", function(e) {
        var t = e.target;
        if (!t || !t.matches("input, textarea")) return;
        var mode = inputValidationMode(t);
        if (!mode) return;
        if (mode === "money") { formatMoneyField(t); return; }
        applySanitized(t, mode);
    });

    document.addEventListener("blur", function(e) {
        var t = e.target;
        if (!t || !t.matches("input")) return;
        if (t.dataset.moneyInput !== undefined) formatMoneyField(t);
    }, true);

    document.addEventListener("paste", function(e) {
        var t = e.target;
        if (!t || !t.matches("input, textarea")) return;
        var mode = inputValidationMode(t);
        if (!mode || mode === "money") return;
        if (t.closest && t.closest("#tbodyCompradores")) return;
        e.preventDefault();
        var cb = e.clipboardData || window.clipboardData;
        var text = cb && cb.getData ? (cb.getData("text") || "") : "";
        var cleaned = SANITIZERS[mode](text);
        if (t.maxLength > 0) cleaned = cleaned.slice(0, t.maxLength);
        var start = t.selectionStart || 0;
        var end = t.selectionEnd != null ? t.selectionEnd : start;
        t.value = t.value.slice(0, start) + cleaned + t.value.slice(end);
        var np = start + cleaned.length;
        t.selectionStart = t.selectionEnd = np;
        t.dispatchEvent(new Event("input", { bubbles: true }));
    });

    document.querySelectorAll("[data-money-input]").forEach(formatMoneyField);
    document.querySelectorAll("[data-validation]").forEach(function(input) {
        var mode = inputValidationMode(input);
        if (mode && mode !== "money") applySanitized(input, mode);
    });

    document.addEventListener("keydown", function(e) {
        if (e.target.matches("input, textarea, select, [contenteditable]")) return;
        if (e.key === "?" || (e.key === "h" && !e.ctrlKey && !e.metaKey)) {
            e.preventDefault();
            var isAdmin = window.CURRENT_USER_ROL === "admin";
            var help = document.getElementById("shortcutsHelp");
            if (help) {
                help.style.display = help.style.display === "none" ? "block" : "none";
            } else {
                var d = document.createElement("div");
                d.id = "shortcutsHelp";
                d.style.cssText = "position:fixed;bottom:20px;right:20px;z-index:9999;background:var(--bs-body-bg);border:1px solid var(--bs-border-color);color:var(--bs-body-color);border-radius:8px;padding:16px;box-shadow:0 4px 12px rgba(0,0,0,.35);max-width:320px;font-size:13px;";
                var html = "<div class='fw-bold mb-2' style='font-size:14px;'>Atajos de teclado</div>" +
                    "<div class='d-flex justify-content-between mb-1'><kbd>g</kbd> <kbd>c</kbd> <span>Consultas</span></div>" +
                    (isAdmin ? "<div class='d-flex justify-content-between mb-1'><kbd>g</kbd> <kbd>v</kbd> <span>Vendedores</span></div>" : "") +
                    "<div class='d-flex justify-content-between mb-1'><kbd>g</kbd> <kbd>f</kbd> <span>Facturas</span></div>" +
                    "<div class='d-flex justify-content-between mb-1'><kbd>g</kbd> <kbd>d</kbd> <span>Dashboard</span></div>" +
                    "<div class='d-flex justify-content-between mb-1'><kbd>g</kbd> <kbd>n</kbd> <span>Factura cliente</span></div>" +
                    "<div class='d-flex justify-content-between mb-1'><kbd>g</kbd> <kbd>m</kbd> <span>Factura vendedor</span></div>" +
                    (isAdmin ? "<div class='d-flex justify-content-between mb-1'><kbd>g</kbd> <kbd>x</kbd> <span>Configuraci\u00f3n</span></div>" : "") +
                    "<div class='d-flex justify-content-between mb-1'><kbd>/</kbd> <span>Buscar boleta</span></div>" +
                    "<div class='d-flex justify-content-between mb-1'><kbd>?</kbd> <span>Ayuda</span></div>" +
                    "<button class='btn btn-sm btn-outline-secondary mt-2' onclick='this.parentElement.style.display=\"none\"'>Cerrar</button>";
                d.innerHTML = html;
                document.body.appendChild(d);
            }
        }
        if (e.key === "Escape") {
            var modals = document.querySelectorAll(".modal.show");
            modals.forEach(function(m) { var b = bootstrap.Modal.getInstance(m); if(b) b.hide(); });
        }
    });

    var _g = {};
    document.addEventListener("keydown", function(e) {
        if (e.target.matches("input, textarea, select, [contenteditable]")) return;
        if (e.key === "g" && !e.ctrlKey && !e.metaKey) {
            _g.pressed = true;
            _g.timer = setTimeout(function() { _g.pressed = false; }, 1000);
            return;
        }
        if (_g.pressed && e.key) {
            _g.pressed = false;
            if (_g.timer) clearTimeout(_g.timer);
            var isAdmin = window.CURRENT_USER_ROL === "admin";
            var map = {
                c: "/consultas",
                f: "/facturas",
                n: "/facturas/nueva/cliente",
                m: "/facturas/nueva/vendedor"
            };
            if (isAdmin) {
                map.d = "/dashboard";
                map.v = "/vendedores";
                map.x = "/configuracion";
            }
            var url = map[e.key];
            if (url) { e.preventDefault(); window.location.href = url; }
        }
        if (e.key === "/" && !e.ctrlKey && !e.metaKey) {
            e.preventDefault();
            var search = document.getElementById("searchInput") || document.querySelector("[name='numero'], [name='buscar_numero']");
            if (search) search.focus();
        }
    });

    var pendingForm = null;
    var modalClave = document.getElementById("modalConfirmarClave");
    var formClave = document.getElementById("formConfirmarClave");
    var inputClave = document.getElementById("confirmarClaveInput");
    var textoClave = document.getElementById("confirmarClaveTexto");

    if (modalClave && formClave && inputClave) {
        document.addEventListener("click", function(e) {
            var btn = e.target.closest("[data-confirmar-clave]");
            if (!btn) return;
            e.preventDefault();
            pendingForm = btn.closest("form");
            if (textoClave) {
                textoClave.textContent = btn.dataset.confirmarTexto || "Confirme esta acci\u00f3n con su contrase\u00f1a de administrador.";
            }
            inputClave.value = "";
            bootstrap.Modal.getOrCreateInstance(modalClave).show();
        });

        formClave.addEventListener("submit", function(e) {
            e.preventDefault();
            var clave = inputClave.value;
            if (!pendingForm || !clave) return;
            var hidden = pendingForm.querySelector('input[name="clave_admin"]');
            if (!hidden) {
                hidden = document.createElement("input");
                hidden.type = "hidden";
                hidden.name = "clave_admin";
                pendingForm.appendChild(hidden);
            }
            hidden.value = clave;
            bootstrap.Modal.getOrCreateInstance(modalClave).hide();
            if (typeof pendingForm.requestSubmit === "function") {
                pendingForm.requestSubmit();
            } else {
                pendingForm.submit();
            }
        });
    }
window.initSortableTables = function(containerSelector) {
    var container = document.querySelector(containerSelector || 'body');
    if (!container) return;
    var ths = container.querySelectorAll('th.sortable');
    ths.forEach(function(th) {
        th.style.cursor = 'pointer';
        var col = th.getAttribute('data-sort');
        if (!col) return;
        var params = new URLSearchParams(window.location.search);
        var currentSort = params.get('sort_by') || '_id';
        var currentDir = params.get('sort_dir') || 'asc';
        // Limpiar iconos previos
        th.querySelectorAll('.sort-icon').forEach(function(e) { e.remove(); });
        if (col === currentSort) {
            var icon = document.createElement('i');
            icon.className = 'bi bi-caret-' + (currentDir === 'asc' ? 'up' : 'down') + '-fill ms-1 sort-icon';
            th.appendChild(icon);
        }
        th.addEventListener('click', function() {
            var params2 = new URLSearchParams(window.location.search);
            if (col === currentSort) {
                params2.set('sort_dir', currentDir === 'asc' ? 'desc' : 'asc');
            } else {
                params2.set('sort_by', col);
                params2.set('sort_dir', 'asc');
            }
            params2.delete('page');
            window.location.search = params2.toString();
        });
    });
};

window.sortTableFrontend = function(tableId, colIndex, isNumeric) {
    var table = document.getElementById(tableId);
    if (!table) return;
    var tbody = table.querySelector("tbody");
    if (!tbody) return;
    var rows = Array.from(tbody.querySelectorAll("tr"));
    var dir = table.getAttribute("data-sort-dir") || "asc";
    table.setAttribute("data-sort-dir", dir === "asc" ? "desc" : "asc");
    rows.sort(function(a, b) {
        var va = a.querySelectorAll("td")[colIndex] ? a.querySelectorAll("td")[colIndex].textContent.trim() : "";
        var vb = b.querySelectorAll("td")[colIndex] ? b.querySelectorAll("td")[colIndex].textContent.trim() : "";
        if (isNumeric) {
            var na = parseFloat(va.replace(/[^0-9.-]/g, "")) || 0;
            var nb = parseFloat(vb.replace(/[^0-9.-]/g, "")) || 0;
            return dir === "asc" ? na - nb : nb - na;
        }
        return dir === "asc" ? va.localeCompare(vb) : vb.localeCompare(va);
    });
    rows.forEach(function(row) { tbody.appendChild(row); });
};

if (typeof window.ordenar === 'undefined') {
    window.ordenar = function(col) {
        var params = new URLSearchParams(window.location.search);
        var currentSort = params.get('sort_by') || '_id';
        var currentDir = params.get('sort_dir') || 'asc';
        if (col === currentSort) {
            params.set('sort_dir', currentDir === 'asc' ? 'desc' : 'asc');
        } else {
            params.set('sort_by', col);
            params.set('sort_dir', 'asc');
        }
        params.delete('page');
        window.location.search = params.toString();
    };
}
})();
