ALTER TABLE product_strategy_receipts
ADD COLUMN strategy_input_json TEXT NOT NULL DEFAULT '{}';
