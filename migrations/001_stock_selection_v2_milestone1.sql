-- TXF Pro Viewer stock-selection V2 Milestone 1
-- Runtime applies this migration idempotently through stock_selection_schema.py.

CREATE TABLE IF NOT EXISTS security_master (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    market TEXT,
    security_type TEXT NOT NULL DEFAULT 'common_stock',
    industry TEXT,
    listing_date TEXT,
    delisting_date TEXT,
    is_etf INTEGER NOT NULL DEFAULT 0,
    is_leveraged INTEGER NOT NULL DEFAULT 0,
    is_inverse INTEGER NOT NULL DEFAULT 0,
    is_etn INTEGER NOT NULL DEFAULT 0,
    is_warrant INTEGER NOT NULL DEFAULT 0,
    is_preferred INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'stock_names_migration',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_security_master_type ON security_master(security_type);
CREATE INDEX IF NOT EXISTS idx_security_master_market ON security_master(market);

-- Existing databases are upgraded conditionally by stock_selection_schema.py:
-- ALTER TABLE institutional_trading ADD COLUMN foreign_buy_shares INTEGER;
-- ALTER TABLE institutional_trading ADD COLUMN investment_buy_shares INTEGER;
-- ALTER TABLE institutional_trading ADD COLUMN dealer_buy_shares INTEGER;
-- Capital Flow V2.1 Milestone 1 additionally applies:
-- ALTER TABLE institutional_trading ADD COLUMN foreign_net INTEGER;       -- shares
-- ALTER TABLE institutional_trading ADD COLUMN trust_net INTEGER;         -- shares
-- ALTER TABLE institutional_trading ADD COLUMN dealer_prop_net INTEGER;   -- shares
-- ALTER TABLE institutional_trading ADD COLUMN dealer_hedge_net INTEGER;  -- shares
-- ALTER TABLE institutional_trading ADD COLUMN dealer_unknown_net INTEGER;-- shares
-- ALTER TABLE institutional_trading ADD COLUMN flow_detail_level TEXT;
-- ALTER TABLE institutional_trading ADD COLUMN flow_data_source TEXT;
