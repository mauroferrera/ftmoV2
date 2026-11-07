import sqlite3
import json
import os
import html

def build_viewer():
    db_path = "database/trading.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 1. Fetch setup_log (latest 400 rows)
    cursor.execute("""
        SELECT id, timestamp, symbol, timeframe, direction, score, verdict, validated, 
               reject_reasons, entry, sl, target, trade_result_json, breakdown_json, source
        FROM setup_log 
        ORDER BY id DESC 
        LIMIT 400
    """)
    setup_logs = [dict(r) for r in cursor.fetchall()]

    # 2. Fetch setup_state (all rows)
    cursor.execute("""
        SELECT symbol, timeframe, direction, score, verdict, entry, sl, target, 
               invalidate_level, status, notified, created_at, updated_at
        FROM setup_state 
        ORDER BY score DESC
    """)
    setup_states = [dict(r) for r in cursor.fetchall()]

    # 3. Fetch trade_marks
    cursor.execute("""
        SELECT id, ticket, symbol, direction, volume, price_open, price_close, sl, tp, 
               profit, r_multiple, exit_reason, exit_quality, source, created_at
        FROM trade_marks 
        ORDER BY id DESC
    """)
    trade_marks = [dict(r) for r in cursor.fetchall()]

    # 4. Fetch prop_state
    cursor.execute("SELECT key, value FROM prop_state")
    prop_states = [dict(r) for r in cursor.fetchall()]

    # 5. Total counts
    cursor.execute("SELECT COUNT(*) FROM setup_log")
    total_setup_log = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM setup_log WHERE trade_result_json LIKE '%10009%' OR trade_result_json LIKE '%done%' OR trade_result_json LIKE '%ejecutad%'")
    executed_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM setup_log WHERE validated = 1")
    passed_gate_count = cursor.fetchone()[0]

    html_content = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Visualizador de Base de Datos - trading.db</title>
    <style>
        :root {{
            --bg-main: #0d1117;
            --bg-card: #161b22;
            --bg-hover: #1f2937;
            --border: #30363d;
            --text-primary: #f0f6fc;
            --text-secondary: #8b949e;
            --accent: #58a6ff;
            --green: #238636;
            --green-text: #3fb950;
            --red: #da3633;
            --red-text: #f85149;
            --yellow: #d29922;
            --purple: #a371f7;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: var(--bg-main);
            color: var(--text-primary);
            padding: 24px;
            line-height: 1.5;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-bottom: 20px;
            border-bottom: 1px solid var(--border);
            margin-bottom: 24px;
        }}
        .title h1 {{ font-size: 24px; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 10px; }}
        .title p {{ color: var(--text-secondary); font-size: 14px; margin-top: 4px; }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }}
        .stat-card {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 16px;
        }}
        .stat-label {{ font-size: 12px; color: var(--text-secondary); text-transform: uppercase; font-weight: 600; letter-spacing: 0.5px; }}
        .stat-val {{ font-size: 26px; font-weight: 700; color: var(--text-primary); margin-top: 4px; }}
        
        .tabs {{
            display: flex;
            gap: 8px;
            border-bottom: 1px solid var(--border);
            margin-bottom: 20px;
        }}
        .tab-btn {{
            background: transparent;
            border: none;
            color: var(--text-secondary);
            font-size: 14px;
            font-weight: 600;
            padding: 10px 18px;
            cursor: pointer;
            border-bottom: 2px solid transparent;
            transition: all 0.2s;
        }}
        .tab-btn:hover {{ color: var(--text-primary); }}
        .tab-btn.active {{
            color: var(--accent);
            border-bottom-color: var(--accent);
        }}
        
        .controls {{
            display: flex;
            gap: 12px;
            margin-bottom: 16px;
            flex-wrap: wrap;
            align-items: center;
        }}
        .search-box {{
            flex: 1;
            min-width: 250px;
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 6px;
            color: #fff;
            padding: 8px 12px;
            font-size: 14px;
            outline: none;
        }}
        .search-box:focus {{ border-color: var(--accent); }}
        .select-filter {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 6px;
            color: #fff;
            padding: 8px 12px;
            font-size: 14px;
            outline: none;
        }}

        .table-container {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 8px;
            overflow-x: auto;
            max-height: 70vh;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
            text-align: left;
        }}
        th {{
            background: #21262d;
            color: var(--text-secondary);
            font-weight: 600;
            padding: 12px 14px;
            position: sticky;
            top: 0;
            z-index: 10;
            border-bottom: 1px solid var(--border);
            white-space: nowrap;
        }}
        td {{
            padding: 10px 14px;
            border-bottom: 1px solid #21262d;
            white-space: nowrap;
        }}
        tr:hover td {{ background: var(--bg-hover); }}

        .badge {{
            display: inline-block;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: 600;
        }}
        .badge-buy {{ background: rgba(63, 185, 80, 0.15); color: var(--green-text); border: 1px solid rgba(63, 185, 80, 0.3); }}
        .badge-sell {{ background: rgba(248, 81, 73, 0.15); color: var(--red-text); border: 1px solid rgba(248, 81, 73, 0.3); }}
        .badge-passed {{ background: rgba(63, 185, 80, 0.2); color: var(--green-text); }}
        .badge-rejected {{ background: rgba(248, 81, 73, 0.15); color: var(--red-text); }}
        .badge-info {{ background: rgba(88, 166, 255, 0.15); color: var(--accent); }}
        .badge-warning {{ background: rgba(210, 153, 34, 0.2); color: var(--yellow); }}

        .score-pill {{
            font-weight: 700;
            padding: 2px 6px;
            border-radius: 4px;
        }}
        .score-high {{ background: #238636; color: #fff; }}
        .score-mid {{ background: #9e6a03; color: #fff; }}
        .score-low {{ background: #484f58; color: #8b949e; }}

        .tab-content {{ display: none; }}
        .tab-content.active {{ display: block; }}

        .json-cell {{
            max-width: 280px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            color: #8b949e;
            font-family: monospace;
            cursor: pointer;
        }}
        .json-cell:hover {{ color: var(--accent); text-decoration: underline; }}
        
        /* Modal for JSON */
        .modal {{
            display: none;
            position: fixed;
            top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0, 0, 0, 0.7);
            z-index: 100;
            align-items: center;
            justify-content: center;
        }}
        .modal-content {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 8px;
            width: 80%;
            max-width: 700px;
            max-height: 80vh;
            display: flex;
            flex-direction: column;
            padding: 20px;
        }}
        .modal-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }}
        .modal-close {{
            background: transparent;
            border: none;
            color: #fff;
            font-size: 20px;
            cursor: pointer;
        }}
        .modal-body pre {{
            background: #0d1117;
            padding: 16px;
            border-radius: 6px;
            overflow: auto;
            color: #3fb950;
            font-size: 13px;
            max-height: 60vh;
        }}
    </style>
</head>
<body>

    <div class="header">
        <div class="title">
            <h1>🗄️ Visualizador de Base de Datos SQLite (trading.db)</h1>
            <p>Monitoreo completo de análisis SMC, rechazos de filtros FTMO, setups en vivo y órdenes de MetaTrader 5</p>
        </div>
        <div>
            <span style="font-size: 12px; color: var(--text-secondary); background: #21262d; padding: 6px 12px; border-radius: 20px; border: 1px solid var(--border);">
                📁 database/trading.db ({round(os.path.getsize(db_path)/(1024*1024), 1)} MB)
            </span>
        </div>
    </div>

    <!-- STATS -->
    <div class="stats-grid">
        <div class="stat-card">
            <div class="stat-label">Total Evaluaciones Bot</div>
            <div class="stat-val">{total_setup_log:,}</div>
            <div style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">En setup_log</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Setups Que Pasaron Gate</div>
            <div class="stat-val" style="color: var(--green-text);">{passed_gate_count:,}</div>
            <div style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">Score >= 60 y R:R >= 2.0</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Pares en Monitoreo</div>
            <div class="stat-val" style="color: var(--accent);">{len(setup_states)}</div>
            <div style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">setup_state (12 pares)</div>
        </div>
        <div class="stat-card">
            <div class="stat-label">Trades Ejecutados</div>
            <div class="stat-val" style="color: var(--yellow);">{len(trade_marks)}</div>
            <div style="font-size: 12px; color: var(--text-secondary); margin-top: 4px;">trade_marks</div>
        </div>
    </div>

    <!-- TABS -->
    <div class="tabs">
        <button class="tab-btn active" onclick="switchTab('tab-setuplog')">📊 Historial de Decisiones (setup_log - Últimos 400)</button>
        <button class="tab-btn" onclick="switchTab('tab-setupstate')">🎯 Estado Actual de los 12 Pares (setup_state)</button>
        <button class="tab-btn" onclick="switchTab('tab-trades')">💼 Historial de Trades (trade_marks)</button>
        <button class="tab-btn" onclick="switchTab('tab-prop')">🛡️ Reglas y Drawdown FTMO (prop_state)</button>
    </div>

    <!-- TAB 1: SETUP LOG -->
    <div id="tab-setuplog" class="tab-content active">
        <div class="controls">
            <input type="text" id="logSearch" class="search-box" placeholder="🔍 Buscar por símbolo, motivo de rechazo, dirección, fecha..." onkeyup="filterLogTable()">
            <select id="logSymFilter" class="select-filter" onchange="filterLogTable()">
                <option value="">Todos los Símbolos</option>
                <option value="EURUSD">EURUSD</option>
                <option value="GBPUSD">GBPUSD</option>
                <option value="AUDUSD">AUDUSD</option>
                <option value="USDJPY">USDJPY</option>
                <option value="AUDJPY">AUDJPY</option>
                <option value="XAUUSD">XAUUSD</option>
                <option value="AUDCAD">AUDCAD</option>
                <option value="USDCHF">USDCHF</option>
                <option value="EURJPY">EURJPY</option>
                <option value="GBPJPY">GBPJPY</option>
                <option value="NZDUSD">NZDUSD</option>
                <option value="USDCAD">USDCAD</option>
            </select>
            <select id="logGateFilter" class="select-filter" onchange="filterLogTable()">
                <option value="">Todos los Estados</option>
                <option value="APROBADO">Solo Aprobados (Gate OK)</option>
                <option value="RECHAZADO">Solo Rechazados</option>
                <option value="MT5_EJECUTADO">Órdenes a MT5</option>
            </select>
            <span id="logRowCount" style="font-size: 13px; color: var(--text-secondary); margin-left: auto;"></span>
        </div>

        <div class="table-container">
            <table id="tableLog">
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Fecha / Hora</th>
                        <th>Símbolo</th>
                        <th>TF</th>
                        <th>Dirección</th>
                        <th>Score</th>
                        <th>Gate FTMO</th>
                        <th>Motivo Rechazo / Estado</th>
                        <th>Entrada</th>
                        <th>SL</th>
                        <th>TP</th>
                        <th>Respuesta MT5 / Broker</th>
                        <th>Detalles SMC</th>
                    </tr>
                </thead>
                <tbody>
"""

    for row in setup_logs:
        rid = row['id']
        ts = row['timestamp'] or ''
        sym = row['symbol'] or ''
        tf = row['timeframe'] or ''
        dir_val = (row['direction'] or '').upper()
        dir_badge = f'<span class="badge badge-buy">BUY</span>' if 'BUY' in dir_val else (f'<span class="badge badge-sell">SELL</span>' if 'SELL' in dir_val else dir_val)
        
        score = row['score'] or 0
        score_cls = 'score-high' if score >= 75 else ('score-mid' if score >= 60 else 'score-low')
        score_pill = f'<span class="score-pill {score_cls}">{score}</span>'

        validated = row['validated']
        if validated == 1:
            gate_badge = '<span class="badge badge-passed">✓ PASÓ</span>'
            gate_status_attr = "APROBADO"
        else:
            gate_badge = '<span class="badge badge-rejected">✗ BLOQUEO</span>'
            gate_status_attr = "RECHAZADO"

        reasons = html.escape(str(row['reject_reasons'] or ''))
        if not reasons and validated == 1:
            reasons = '<span style="color: var(--green-text);">Cumple R:R >= 2.0 y Filtros FTMO</span>'

        entry = row['entry'] or ''
        sl = row['sl'] or ''
        tp = row['target'] or ''

        trade_res = row['trade_result_json'] or ''
        res_display = ''
        if trade_res:
            res_str = str(trade_res)
            if '10027' in res_str:
                res_display = '<span class="badge badge-warning" title="10027: Algo Trading deshabilitado en MT5">⚠️ 10027 MT5 Algo OFF</span>'
            elif '10009' in res_str or 'done' in res_str:
                res_display = '<span class="badge badge-passed">✅ MT5 EJECUTADO</span>'
                gate_status_attr = "MT5_EJECUTADO"
            else:
                res_display = f'<span class="badge badge-info" title="{html.escape(res_str)}">MT5 Intento</span>'
        else:
            res_display = '<span style="color: var(--text-secondary);">-</span>'

        breakdown = row['breakdown_json'] or ''
        breakdown_btn = ''
        if breakdown:
            clean_json = html.escape(str(breakdown)).replace('"', '&quot;')
            breakdown_btn = f'<span class="json-cell" onclick="showModal(\'Detalles SMC - {sym} #{rid}\', `{clean_json}`)">Ver Factores SMC</span>'

        html_content += f"""                    <tr data-sym="{sym}" data-gate="{gate_status_attr}">
                        <td style="color: var(--text-secondary);">#{rid}</td>
                        <td style="color: var(--text-secondary); font-size: 12px;">{ts}</td>
                        <td style="font-weight: 700;">{sym}</td>
                        <td>{tf}</td>
                        <td>{dir_badge}</td>
                        <td>{score_pill}</td>
                        <td>{gate_badge}</td>
                        <td style="max-width: 320px; overflow: hidden; text-overflow: ellipsis;" title="{reasons}">{reasons}</td>
                        <td>{entry}</td>
                        <td style="color: var(--red-text);">{sl}</td>
                        <td style="color: var(--green-text);">{tp}</td>
                        <td>{res_display}</td>
                        <td>{breakdown_btn}</td>
                    </tr>
"""

    html_content += """                </tbody>
            </table>
        </div>
    </div>

    <!-- TAB 2: SETUP STATE -->
    <div id="tab-setupstate" class="tab-content">
        <div style="margin-bottom: 16px; color: var(--text-secondary); font-size: 14px;">
            Estado vivo de los 12 pares monitoreados en tiempo real por el escáner:
        </div>
        <div class="table-container">
            <table>
                <thead>
                    <tr>
                        <th>Símbolo</th>
                        <th>Timeframe</th>
                        <th>Dirección</th>
                        <th>Score</th>
                        <th>Veredicto</th>
                        <th>Estado Actual</th>
                        <th>Entrada</th>
                        <th>Stop Loss</th>
                        <th>Target (TP)</th>
                        <th>Nivel Invalidación</th>
                        <th>Notificado</th>
                        <th>Última Actualización</th>
                    </tr>
                </thead>
                <tbody>
"""

    for row in setup_states:
        sym = row['symbol']
        tf = row['timeframe']
        dir_val = (row['direction'] or '').upper()
        dir_badge = f'<span class="badge badge-buy">BUY</span>' if 'BUY' in dir_val else (f'<span class="badge badge-sell">SELL</span>' if 'SELL' in dir_val else dir_val)
        score = row['score'] or 0
        score_cls = 'score-high' if score >= 75 else ('score-mid' if score >= 60 else 'score-low')
        verdict = row['verdict'] or ''
        status = row['status'] or ''
        status_badge = f'<span class="badge badge-info">{status}</span>' if status == 'ACTIVE' else f'<span class="badge badge-warning">{status}</span>'
        
        html_content += f"""                    <tr>
                        <td style="font-weight: 700; font-size: 14px;">{sym}</td>
                        <td>{tf}</td>
                        <td>{dir_badge}</td>
                        <td><span class="score-pill {score_cls}">{score}</span></td>
                        <td>{verdict}</td>
                        <td>{status_badge}</td>
                        <td>{row['entry']}</td>
                        <td style="color: var(--red-text);">{row['sl']}</td>
                        <td style="color: var(--green-text);">{row['target']}</td>
                        <td>{row['invalidate_level']}</td>
                        <td>{row['notified']}</td>
                        <td style="color: var(--text-secondary); font-size: 12px;">{row['updated_at']}</td>
                    </tr>
"""

    html_content += """                </tbody>
            </table>
        </div>
    </div>

    <!-- TAB 3: TRADES -->
    <div id="tab-trades" class="tab-content">
        <div style="margin-bottom: 16px; color: var(--text-secondary); font-size: 14px;">
            Operaciones auditadas en MT5 registradas en trade_marks:
        </div>
        <div class="table-container">
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Ticket MT5</th>
                        <th>Símbolo</th>
                        <th>Dirección</th>
                        <th>Lotes</th>
                        <th>Precio Entrada</th>
                        <th>Precio Cierre</th>
                        <th>SL</th>
                        <th>TP</th>
                        <th>Beneficio ($)</th>
                        <th>R Múltiplo</th>
                        <th>Motivo Salida</th>
                        <th>Fecha Creación</th>
                    </tr>
                </thead>
                <tbody>
"""

    for row in trade_marks:
        sym = row['symbol']
        dir_val = (row['direction'] or '').upper()
        dir_badge = f'<span class="badge badge-buy">BUY</span>' if 'BUY' in dir_val else f'<span class="badge badge-sell">SELL</span>'
        profit = row['profit'] or 0
        profit_color = 'var(--green-text)' if profit >= 0 else 'var(--red-text)'
        
        html_content += f"""                    <tr>
                        <td>#{row['id']}</td>
                        <td style="font-weight: 700;">{row['ticket']}</td>
                        <td style="font-weight: 700;">{sym}</td>
                        <td>{dir_badge}</td>
                        <td>{row['volume']}</td>
                        <td>{row['price_open']}</td>
                        <td>{row['price_close'] or '-'}</td>
                        <td style="color: var(--red-text);">{row['sl']}</td>
                        <td style="color: var(--green-text);">{row['tp']}</td>
                        <td style="font-weight: 700; color: {profit_color};">${profit:.2f}</td>
                        <td>{row['r_multiple'] or '-'}</td>
                        <td>{row['exit_reason'] or '-'}</td>
                        <td style="color: var(--text-secondary); font-size: 12px;">{row['created_at']}</td>
                    </tr>
"""

    if not trade_marks:
        html_content += '<tr><td colspan="13" style="text-align: center; color: var(--text-secondary); padding: 20px;">No hay trades cerrados en trade_marks aún.</td></tr>'

    html_content += """                </tbody>
            </table>
        </div>
    </div>

    <!-- TAB 4: PROP STATE -->
    <div id="tab-prop" class="tab-content">
        <div style="margin-bottom: 16px; color: var(--text-secondary); font-size: 14px;">
            Variables de control de riesgo FTMO y estado de la cuenta:
        </div>
        <div class="table-container" style="max-width: 700px;">
            <table>
                <thead>
                    <tr>
                        <th>Parámetro (Key)</th>
                        <th>Valor Actual (Value)</th>
                    </tr>
                </thead>
                <tbody>
"""

    for row in prop_states:
        html_content += f"""                    <tr>
                        <td style="font-weight: 600; color: var(--accent);">{row['key']}</td>
                        <td style="font-family: monospace;">{row['value']}</td>
                    </tr>
"""

    html_content += """                </tbody>
            </table>
        </div>
    </div>

    <!-- MODAL -->
    <div id="jsonModal" class="modal" onclick="closeModal(event)">
        <div class="modal-content" onclick="event.stopPropagation()">
            <div class="modal-header">
                <h3 id="modalTitle" style="font-size: 16px;">Detalle</h3>
                <button class="modal-close" onclick="closeModal()">&times;</button>
            </div>
            <div class="modal-body">
                <pre id="modalJson"></pre>
            </div>
        </div>
    </div>

    <script>
        function switchTab(tabId) {
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
            document.getElementById(tabId).classList.add('active');
            event.target.classList.add('active');
        }

        function filterLogTable() {
            const search = document.getElementById('logSearch').value.toLowerCase();
            const sym = document.getElementById('logSymFilter').value;
            const gate = document.getElementById('logGateFilter').value;
            const rows = document.querySelectorAll('#tableLog tbody tr');
            let visibleCount = 0;

            rows.forEach(r => {
                const text = r.innerText.toLowerCase();
                const rSym = r.getAttribute('data-sym');
                const rGate = r.getAttribute('data-gate');

                const matchSearch = !search || text.includes(search);
                const matchSym = !sym || rSym === sym;
                const matchGate = !gate || rGate === gate;

                if (matchSearch && matchSym && matchGate) {
                    r.style.display = '';
                    visibleCount++;
                } else {
                    r.style.display = 'none';
                }
            });

            document.getElementById('logRowCount').innerText = `Mostrando ${visibleCount} de ${rows.length} registros`;
        }

        function showModal(title, jsonStr) {
            try {
                const parsed = JSON.parse(jsonStr);
                document.getElementById('modalJson').innerText = JSON.stringify(parsed, null, 2);
            } catch (e) {
                document.getElementById('modalJson').innerText = jsonStr;
            }
            document.getElementById('modalTitle').innerText = title;
            document.getElementById('jsonModal').style.display = 'flex';
        }

        function closeModal(e) {
            document.getElementById('jsonModal').style.display = 'none';
        }

        filterLogTable();
    </script>
</body>
</html>
"""

    out_file = "visor_base_datos.html"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"Viewer generated at {os.path.abspath(out_file)}")

    # Also save one in the parent directory for quick access
    root_out_file = "../visor_base_datos.html"
    with open(root_out_file, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"Viewer copied to root at {os.path.abspath(root_out_file)}")

if __name__ == "__main__":
    build_viewer()

