-- GENERADO, no editar a mano:
--   python -m database.models > database/schema.sql
-- Fuente de verdad: database/models.py (SCHEMA, INDEXES, MIGRATIONS).
-- `tests/unit/test_schema.py::test_schema_sql_al_dia` falla si se separan.

CREATE TABLE IF NOT EXISTS roles (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        system_prompt TEXT NOT NULL,
        allowed_tools TEXT NOT NULL,
        provider TEXT NOT NULL DEFAULT 'auto',
        model TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        role_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        tool_call_id TEXT,
        name TEXT
    );

CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket INTEGER NOT NULL UNIQUE,
        symbol TEXT NOT NULL,
        action TEXT NOT NULL,
        volume REAL,
        price_open REAL,
        price_close REAL,
        profit REAL,
        time_open REAL,
        time_close TEXT,
        commission REAL,
        swap REAL,
        exit_reason INTEGER,
        synced_at TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT,
        ticket TEXT,
        symbol TEXT NOT NULL DEFAULT 'EURUSD',
        action TEXT,
        poi_type TEXT,
        liquidity_swept TEXT,
        setup_json TEXT NOT NULL DEFAULT '{}',
        emotion TEXT,
        plan_compliance INTEGER,
        tags TEXT NOT NULL DEFAULT '[]',
        notes TEXT,
        entry_price REAL,
        sl_price REAL,
        tp_price REAL,
        time_open TEXT,
        time_close TEXT,
        timestamp TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS agent_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS setup_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL DEFAULT 'M15',
        direction TEXT NOT NULL,
        verdict TEXT NOT NULL,
        score REAL NOT NULL,
        breakdown_json TEXT NOT NULL DEFAULT '{}',
        entry REAL,
        sl REAL,
        target REAL,
        invalidate_level REAL,
        validated INTEGER,
        reject_reasons TEXT NOT NULL DEFAULT '[]',
        forced INTEGER NOT NULL DEFAULT 0,
        risk_state_json TEXT NOT NULL DEFAULT '{}',
        trade_result_json TEXT NOT NULL DEFAULT '{}',
        context_json TEXT NOT NULL DEFAULT '{}',
        snapshot_age_s REAL,
        source TEXT NOT NULL DEFAULT '',
        timestamp TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS trade_marks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket INTEGER NOT NULL UNIQUE,
        symbol TEXT NOT NULL DEFAULT '',
        direction TEXT NOT NULL DEFAULT '',
        setup_id INTEGER,
        volume REAL,
        price_open REAL,
        price_close REAL,
        sl REAL,
        tp REAL,
        profit REAL,
        r_multiple REAL,
        exit_reason INTEGER,
        exit_quality TEXT NOT NULL DEFAULT '',
        duration_s REAL,
        capture_path TEXT,
        outcome_json TEXT NOT NULL DEFAULT '{}',
        context_json TEXT NOT NULL DEFAULT '{}',
        source TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS setup_state (
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL DEFAULT 'M15',
        direction TEXT,
        score REAL,
        verdict TEXT,
        entry REAL,
        sl REAL,
        target REAL,
        invalidate_level REAL,
        status TEXT NOT NULL DEFAULT 'active',
        notified INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (symbol, timeframe)
    );

CREATE TABLE IF NOT EXISTS watcher_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS prop_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS chart_drawings (
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL DEFAULT 'M15',
        drawings_json TEXT NOT NULL DEFAULT '[]',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (symbol, timeframe)
    );

CREATE TABLE IF NOT EXISTS position_state (
        ticket INTEGER PRIMARY KEY,
        symbol TEXT NOT NULL DEFAULT '',
        direction TEXT NOT NULL DEFAULT '',
        price_open REAL,
        volume_original REAL,
        r_original REAL,
        partial_volume REAL,
        partial_deal INTEGER,
        partial_taken INTEGER NOT NULL DEFAULT 0,
        be_applied INTEGER NOT NULL DEFAULT 0,
        be_level REAL,
        runner_level REAL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

CREATE TABLE IF NOT EXISTS eurusd_spot_analysis_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_key TEXT NOT NULL UNIQUE,
        pair TEXT NOT NULL DEFAULT 'EURUSD',
        run_time TEXT NOT NULL CHECK(run_time IN ('07','11','14','18')),
        generated_at_utc TEXT NOT NULL,
        bias TEXT NOT NULL CHECK(bias IN ('alcista','bajista','neutral')),
        confidence INTEGER NOT NULL CHECK(confidence BETWEEN 0 AND 100),
        price_current REAL,
        change_pct_24h REAL,
        high_24h REAL,
        low_24h REAL,
        price_source TEXT,
        consensus_bull INTEGER DEFAULT 0,
        consensus_bear INTEGER DEFAULT 0,
        consensus_neutral INTEGER DEFAULT 0,
        sources_count INTEGER DEFAULT 0,
        md_report TEXT,
        payload_json TEXT NOT NULL,
        created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    );

CREATE TABLE IF NOT EXISTS eurusd_spot_analysis_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        name TEXT,
        domain TEXT,
        url TEXT NOT NULL,
        title TEXT,
        extracted_at_utc TEXT,
        sentiment TEXT CHECK(sentiment IN ('alcista','bajista','neutral','desconocido')),
        FOREIGN KEY(run_id) REFERENCES eurusd_spot_analysis_runs(id) ON DELETE CASCADE
    );

-- Índices
CREATE INDEX IF NOT EXISTS idx_setup_log_time ON setup_log(timestamp);

CREATE INDEX IF NOT EXISTS idx_setup_log_verdict ON setup_log(verdict);

CREATE INDEX IF NOT EXISTS idx_setup_log_age ON setup_log(snapshot_age_s);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);

CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(time_close);

CREATE INDEX IF NOT EXISTS idx_journal_time ON journal(timestamp);

CREATE INDEX IF NOT EXISTS idx_trade_marks_time ON trade_marks(created_at);

CREATE INDEX IF NOT EXISTS idx_trade_marks_symbol ON trade_marks(symbol);

CREATE INDEX IF NOT EXISTS idx_eurusd_spot_runs_gen ON eurusd_spot_analysis_runs(generated_at_utc);

CREATE INDEX IF NOT EXISTS idx_eurusd_spot_runs_rt ON eurusd_spot_analysis_runs(run_time);

CREATE INDEX IF NOT EXISTS idx_eurusd_spot_src_run ON eurusd_spot_analysis_sources(run_id);

-- Migraciones idempotentes (ALTER TABLE ... ADD COLUMN)
-- Se aplican en init_schema(); aquí solo como referencia.
-- setup_log      ADD COLUMN context_json   TEXT NOT NULL DEFAULT '{}'
-- setup_log      ADD COLUMN snapshot_age_s REAL
-- setup_log      ADD COLUMN source         TEXT NOT NULL DEFAULT ''
-- setup_log      ADD COLUMN forced         INTEGER NOT NULL DEFAULT 0
