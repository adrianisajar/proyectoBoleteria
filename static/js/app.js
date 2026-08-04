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

    document.querySelectorAll("[data-money-input]").forEach(function(input) {
        var formatMoney = function() {
            var digits = input.value.replace(/\D/g, "");
            input.value = digits ? Number(digits).toLocaleString("es-CO") : "";
        };
        input.addEventListener("input", formatMoney);
        input.addEventListener("blur", formatMoney);
        formatMoney();
    });

    document.addEventListener("keydown", function(e) {
        if (e.target.matches("input, textarea, select, [contenteditable]")) return;
        if (e.key === "?" || (e.key === "h" && !e.ctrlKey && !e.metaKey)) {
            e.preventDefault();
            var help = document.getElementById("shortcutsHelp");
            if (help) {
                help.style.display = help.style.display === "none" ? "block" : "none";
            } else {
                var d = document.createElement("div");
                d.id = "shortcutsHelp";
                d.style.cssText = "position:fixed;bottom:20px;right:20px;z-index:9999;background:var(--bs-body-bg);border:1px solid var(--bs-border-color);color:var(--bs-body-color);border-radius:8px;padding:16px;box-shadow:0 4px 12px rgba(0,0,0,.35);max-width:320px;font-size:13px;";
                d.innerHTML = "<div class='fw-bold mb-2' style='font-size:14px;'>Atajos de teclado</div>" +
                    "<div class='d-flex justify-content-between mb-1'><kbd>g</kbd> <kbd>c</kbd> <span>Consultas</span></div>" +
                    "<div class='d-flex justify-content-between mb-1'><kbd>g</kbd> <kbd>v</kbd> <span>Vendedores</span></div>" +
                    "<div class='d-flex justify-content-between mb-1'><kbd>g</kbd> <kbd>f</kbd> <span>Facturas</span></div>" +
                    "<div class='d-flex justify-content-between mb-1'><kbd>g</kbd> <kbd>d</kbd> <span>Dashboard</span></div>" +
                    "<div class='d-flex justify-content-between mb-1'><kbd>g</kbd> <kbd>n</kbd> <span>Factura cliente</span></div>" +
                    "<div class='d-flex justify-content-between mb-1'><kbd>g</kbd> <kbd>m</kbd> <span>Factura vendedor</span></div>" +
                    "<div class='d-flex justify-content-between mb-1'><kbd>g</kbd> <kbd>x</kbd> <span>Configuraci\u00f3n</span></div>" +
                    "<div class='d-flex justify-content-between mb-1'><kbd>/</kbd> <span>Buscar boleta</span></div>" +
                    "<div class='d-flex justify-content-between mb-1'><kbd>?</kbd> <span>Ayuda</span></div>" +
                    "<button class='btn btn-sm btn-outline-secondary mt-2' onclick='this.parentElement.style.display=\"none\"'>Cerrar</button>";
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
            var map = {c:"consultas", v:"vendedores_panel", f:"facturas_list", d:"dashboard", n:"nueva_factura_cliente", m:"nueva_factura_vendedor", x:"configuracion"};
            var ep = map[e.key];
            if (ep) { e.preventDefault(); window.location.href = "/" + (ep === "dashboard" ? "" : ep.replace(/_/g, "/")); }
        }
        if (e.key === "/" && !e.ctrlKey && !e.metaKey) {
            e.preventDefault();
            var search = document.getElementById("searchInput") || document.querySelector("[name='numero'], [name='buscar_numero']");
            if (search) search.focus();
        }
    });
})();
