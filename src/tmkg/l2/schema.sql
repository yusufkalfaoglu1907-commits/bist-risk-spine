-- L2 quant store schema (DuckDB dialect). BUILD_PLAN.md M0.
--
-- Every table is BITEMPORAL: bar/period dates = when true in the world;
-- knowledge_date = when WE learned it (publication/declaration/ex-date).
-- All reads in signal code go through tmkg.pit.PITAccess (knowledge_date <= as_of).
-- These columns are not optional — they are the immunity spec (CLAUDE.md §5).

-- Daily bars as sourced (back-adjusted per W7). One row per (symbol, date, knowledge_date).
CREATE TABLE IF NOT EXISTS prices (
    symbol          VARCHAR NOT NULL,
    bar_date        DATE    NOT NULL,
    open            DOUBLE,
    high            DOUBLE,
    low             DOUBLE,
    close           DOUBLE,
    volume_try      DOUBLE,                 -- turnover in TRY (Matriks 'volume')
    quantity        DOUBLE,                 -- shares (Matriks 'quantity')
    adjusted        BOOLEAN,                -- vendor flag; NB false != raw (golden samples)
    is_limit_lock   BOOLEAN DEFAULT FALSE,  -- M1: +/-10% band censoring
    is_stale        BOOLEAN DEFAULT FALSE,  -- M1: non-trading / carried-forward
    knowledge_date  DATE    NOT NULL,
    source          VARCHAR NOT NULL,
    PRIMARY KEY (symbol, bar_date, knowledge_date)
);

-- Clean total-return series (USD-primary + real-TRY cross-check). M1.
CREATE TABLE IF NOT EXISTS total_returns (
    symbol          VARCHAR NOT NULL,
    bar_date        DATE    NOT NULL,
    ret_usd         DOUBLE,                 -- USD-primary total return (primary base)
    ret_real_try    DOUBLE,                 -- CPI-real-TRY cross-check
    ret_nominal_try DOUBLE,                 -- reference only
    limit_lock_adj  BOOLEAN DEFAULT FALSE,  -- return is cumulative-across-lock-window
    knowledge_date  DATE    NOT NULL,
    PRIMARY KEY (symbol, bar_date, knowledge_date)
);

-- Macro / market factor series. M2. (USDTRY, EURTRY, XU100, BRENT, GAS, CDS, VIX, EEM, GOLD, ...)
CREATE TABLE IF NOT EXISTS factors (
    factor          VARCHAR NOT NULL,
    bar_date        DATE    NOT NULL,
    value           DOUBLE,
    ret             DOUBLE,
    knowledge_date  DATE    NOT NULL,
    source          VARCHAR NOT NULL,
    PRIMARY KEY (factor, bar_date, knowledge_date)
);

-- Foreign / non-resident flow factor — the BIST comovement driver. M2.
CREATE TABLE IF NOT EXISTS foreign_flow (
    symbol          VARCHAR,                -- NULL = market aggregate
    period          VARCHAR NOT NULL,       -- YYYYMM (monthly) or YYYY-MM-DD (daily)
    net_flow_try    DOUBLE,
    grain           VARCHAR NOT NULL,       -- 'monthly' | 'daily'
    knowledge_date  DATE    NOT NULL,
    source          VARCHAR NOT NULL,
    PRIMARY KEY (symbol, period, grain, knowledge_date)
);

-- Rolling, regime-tagged factor betas, fit per universe_class. M2.
CREATE TABLE IF NOT EXISTS betas (
    symbol          VARCHAR NOT NULL,
    factor          VARCHAR NOT NULL,
    bar_date        DATE    NOT NULL,       -- end of the rolling window
    beta            DOUBLE,
    method          VARCHAR,                -- 'ols' | 'dimson' | 'scholes_williams'
    "window"        INTEGER,                -- WINDOW is a DuckDB reserved keyword -> quoted
    regime          VARCHAR,
    universe_class  VARCHAR,
    knowledge_date  DATE    NOT NULL,
    PRIMARY KEY (symbol, factor, bar_date, knowledge_date)
);

-- Residual returns after the explicit neutralization ladder. M2.
-- ladder: market -> FX -> rates/CDS -> energy -> sector -> foreign-flow -> holding -> residual
CREATE TABLE IF NOT EXISTS residuals (
    symbol          VARCHAR NOT NULL,
    bar_date        DATE    NOT NULL,
    residual        DOUBLE,
    strip_order     VARCHAR,
    universe_class  VARCHAR,
    knowledge_date  DATE    NOT NULL,
    PRIMARY KEY (symbol, bar_date, knowledge_date)
);

-- Filtered residual-correlation snapshots (glasso / PMFG / MST survivors only). M3.
-- NEVER the dense 500x500 matrix — only edges past the statistical + economic filter.
CREATE TABLE IF NOT EXISTS residual_corr (
    symbol_a        VARCHAR NOT NULL,
    symbol_b        VARCHAR NOT NULL,
    window_end      DATE    NOT NULL,
    "window"        INTEGER,                -- WINDOW is a DuckDB reserved keyword -> quoted
    value           DOUBLE,
    p_value         DOUBLE,
    sign            INTEGER,
    method          VARCHAR,                -- 'glasso' | 'pmfg' | 'mst'
    fdr_passed      BOOLEAN,
    knowledge_date  DATE    NOT NULL,
    PRIMARY KEY (symbol_a, symbol_b, window_end, knowledge_date)
);

-- accounting_regime state per (symbol, period). M1. The regime SELECTS the comparable basis.
CREATE TABLE IF NOT EXISTS accounting_regime (
    symbol          VARCHAR NOT NULL,
    period          VARCHAR NOT NULL,       -- YYYYMM
    regime          VARCHAR NOT NULL,       -- nominal_pre2023 | ias29_2023_2024 | suspended_2025_2027
    knowledge_date  DATE    NOT NULL,       -- = declaration date
    PRIMARY KEY (symbol, period, knowledge_date)
);

-- Survivorship-correct universe membership (the W2 wall). Time-varying: a name
-- listed 2015-2021 then delisted keeps its row with valid_to set — dead names are
-- RETAINED, never dropped (CLAUDE.md §5). PITAccess.universe(as_of) returns names
-- whose [valid_from, valid_to] window contains as_of and knowledge_date <= as_of.
CREATE TABLE IF NOT EXISTS universe_membership (
    symbol          VARCHAR NOT NULL,
    universe        VARCHAR NOT NULL,       -- 'listed' | 'bist_30' | 'bist_100' | ...
    universe_class  VARCHAR,                -- operating | gyo_reit | holding | investment_trust | etf
    valid_from      DATE    NOT NULL,
    valid_to        DATE,                   -- NULL = still a member (open interval)
    knowledge_date  DATE    NOT NULL,
    source          VARCHAR NOT NULL,
    PRIMARY KEY (symbol, universe, valid_from, knowledge_date)
);

-- Per-name/per-date short availability. design §3 / M4 venue-feasible book.
CREATE TABLE IF NOT EXISTS short_eligible (
    symbol          VARCHAR NOT NULL,
    bar_date        DATE    NOT NULL,
    short_eligible  BOOLEAN NOT NULL,
    knowledge_date  DATE    NOT NULL,
    source          VARCHAR,
    PRIMARY KEY (symbol, bar_date, knowledge_date)
);

-- Signal registry — every candidate alpha logged with its honesty stats. M4.
CREATE TABLE IF NOT EXISTS signal_registry (
    signal_id       VARCHAR NOT NULL,
    hypothesis      VARCHAR,
    feature_family  VARCHAR,
    train_start     DATE,
    train_end       DATE,
    test_start      DATE,
    test_end        DATE,
    n_trials        INTEGER,
    cost_model      VARCHAR,
    purge_embargo   VARCHAR,
    deflated_sharpe DOUBLE,                 -- promotion gates on DSR > 0, not raw Sharpe
    pbo             DOUBLE,                 -- probability of backtest overfitting
    beat_baselines  BOOLEAN,                -- cleared the naive-baseline ladder
    book            VARCHAR,                -- research | venue_feasible | stress
    promoted        BOOLEAN DEFAULT FALSE,
    knowledge_date  DATE    NOT NULL,
    PRIMARY KEY (signal_id, knowledge_date)
);
