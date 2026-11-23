CREATE TABLE reply_feedback (
  id TEXT PRIMARY KEY NOT NULL,
  tenant_id TEXT NOT NULL,
  employee_user_id TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  decision_card_id TEXT NOT NULL,
  card_instance_id TEXT NOT NULL,
  card_digest TEXT NOT NULL,
  agent_version TEXT NOT NULL,
  rating TEXT NOT NULL CHECK (rating IN ('helpful', 'needs_improvement')),
  reason_codes_json TEXT NOT NULL CHECK (json_valid(reason_codes_json)),
  scenario TEXT NOT NULL,
  sales_stage TEXT NOT NULL,
  evidence_ids_json TEXT NOT NULL CHECK (json_valid(evidence_ids_json)),
  privacy_status TEXT NOT NULL CHECK (privacy_status IN ('safe', 'quarantined')),
  privacy_reason_codes_json TEXT NOT NULL CHECK (json_valid(privacy_reason_codes_json)),
  sanitized_final_reply TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id, employee_user_id) REFERENCES users(tenant_id, id),
  FOREIGN KEY (tenant_id, customer_id) REFERENCES customers(tenant_id, id),
  UNIQUE (tenant_id, employee_user_id, card_instance_id, agent_version),
  CHECK (privacy_status = 'safe' OR sanitized_final_reply IS NULL)
);

CREATE INDEX idx_reply_feedback_tenant_employee
  ON reply_feedback (tenant_id, employee_user_id);

CREATE INDEX idx_reply_feedback_tenant_scenario_stage_version
  ON reply_feedback (tenant_id, scenario, sales_stage, agent_version);

CREATE INDEX idx_reply_feedback_tenant_privacy_status
  ON reply_feedback (tenant_id, privacy_status);
