// @vitest-environment node

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { describe, expect, it } from "vitest";

const INITIAL_MIGRATION = readFileSync(
  resolve(process.cwd(), "migrations/001_supervision.sql"),
  "utf8",
);
const HARDENING_MIGRATION = readFileSync(
  resolve(process.cwd(), "migrations/002_supervision_production_hardening.sql"),
  "utf8",
);
const FEEDBACK_MIGRATION = readFileSync(
  resolve(process.cwd(), "migrations/003_reply_feedback.sql"),
  "utf8",
);
const DRIZZLE_FEEDBACK_MIGRATION = readFileSync(
  resolve(process.cwd(), "drizzle/0003_reply_feedback.sql"),
  "utf8",
);
const FEEDBACK_AUDIT_MIGRATION = readFileSync(
  resolve(process.cwd(), "migrations/004_reply_feedback_audit.sql"),
  "utf8",
);
const DRIZZLE_FEEDBACK_AUDIT_MIGRATION = readFileSync(
  resolve(process.cwd(), "drizzle/0004_reply_feedback_audit.sql"),
  "utf8",
);

function migratedDatabase(): DatabaseSync {
  const database = new DatabaseSync(":memory:");
  database.exec(INITIAL_MIGRATION);
  database.exec(`
    INSERT INTO users (id, tenant_id, email, role, created_at)
    VALUES ('employee-a', 'tenant-a', 'employee@example.test', 'employee',
            '2026-08-30T00:00:00.000Z');
    INSERT INTO customers (
      id, tenant_id, employee_user_id, customer_number, customer_alias,
      category, planned_follow_up_at, status, created_at
    ) VALUES (
      'customer-a', 'tenant-a', 'employee-a', 'C-001', '脱敏客户', '绿松石',
      '2026-08-31T00:00:00.000Z', 'pending', '2026-08-30T00:00:00.000Z'
    );
  `);
  database.exec(HARDENING_MIGRATION);
  database.exec(FEEDBACK_MIGRATION);
  database.exec(FEEDBACK_AUDIT_MIGRATION);
  return database;
}

const INSERT_COLUMNS = `
  id, tenant_id, employee_user_id, customer_id, decision_card_id,
  card_instance_id, card_digest, agent_version, rating, reason_codes_json,
  scenario, sales_stage, evidence_ids_json, privacy_status,
  privacy_reason_codes_json, sanitized_final_reply, created_at, updated_at
`;

const SAFE_VALUES = `
  'feedback-safe', 'tenant-a', 'employee-a', 'customer-a', 'card-a',
  'card-instance-a',
  'sha256_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
  'reply-agent.v2', 'helpful', '[]', 'product_selection', 'evaluate',
  '["evidence-a"]', 'safe', '[]', '已脱敏回复',
  '2026-08-30T01:00:00.000Z', '2026-08-30T01:00:00.000Z'
`;

describe("003 reply feedback migration", () => {
  it("keeps the migrations and drizzle copies byte-equivalent", () => {
    expect(DRIZZLE_FEEDBACK_MIGRATION).toBe(FEEDBACK_MIGRATION);
    expect(DRIZZLE_FEEDBACK_AUDIT_MIGRATION).toBe(FEEDBACK_AUDIT_MIGRATION);
  });

  it("creates the tenant indexes and accepts a privacy-safe row", () => {
    const database = migratedDatabase();
    database.exec(`INSERT INTO reply_feedback (${INSERT_COLUMNS}) VALUES (${SAFE_VALUES})`);

    expect(database.prepare("SELECT id FROM reply_feedback").all()).toEqual([
      { id: "feedback-safe" },
    ]);
    expect(database.prepare("PRAGMA foreign_key_check").all()).toEqual([]);
    const indexes = database.prepare("PRAGMA index_list(reply_feedback)").all() as
      Array<{ name: string }>;
    expect(indexes.map(({ name }) => name)).toEqual(expect.arrayContaining([
      "idx_reply_feedback_tenant_employee",
      "idx_reply_feedback_tenant_scenario_stage_version",
      "idx_reply_feedback_tenant_privacy_status",
    ]));
  });

  it("atomically audits state transitions without copying reply text", () => {
    const database = migratedDatabase();
    database.exec(`INSERT INTO reply_feedback (${INSERT_COLUMNS}) VALUES (${SAFE_VALUES})`);
    database.exec(`
      UPDATE reply_feedback
      SET rating = 'needs_improvement',
          reason_codes_json = '["other"]',
          privacy_status = 'quarantined',
          privacy_reason_codes_json = '["residual_phone"]',
          sanitized_final_reply = NULL,
          updated_at = '2026-08-30T01:01:00.000Z'
      WHERE id = 'feedback-safe'
    `);

    const audits = database.prepare(`
      SELECT action, actor_user_id, target_id, details_json
      FROM audit_events WHERE target_kind = 'reply_feedback'
      ORDER BY occurred_at
    `).all() as Array<Record<string, unknown>>;
    expect(audits).toHaveLength(2);
    expect(audits.map(({ action }) => action)).toEqual([
      "reply_feedback.created",
      "reply_feedback.updated",
    ]);
    expect(audits.every(({ actor_user_id, target_id }) => (
      actor_user_id === "employee-a" && target_id === "feedback-safe"
    ))).toBe(true);
    expect(JSON.stringify(audits)).not.toContain("已脱敏回复");
  });

  it("does not create a success audit when the feedback row is rejected", () => {
    const database = migratedDatabase();
    expect(() => database.exec(`
      INSERT INTO reply_feedback (${INSERT_COLUMNS}) VALUES (
        'feedback-rejected', 'tenant-a', 'employee-a', 'customer-a', 'card-a',
        'card-instance-rejected',
        'sha256_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        'reply-agent.v2', 'needs_improvement', '["other"]',
        'product_selection', 'evaluate', '["evidence-a"]', 'quarantined',
        '["residual_phone"]', '不应落库的原文',
        '2026-08-30T01:00:00.000Z', '2026-08-30T01:00:00.000Z'
      )
    `)).toThrow();
    expect(database.prepare("SELECT COUNT(*) AS count FROM audit_events").get())
      .toEqual({ count: 0 });
  });

  it("rejects quarantined reply text at the database layer", () => {
    const database = migratedDatabase();

    expect(() => database.exec(`
      INSERT INTO reply_feedback (${INSERT_COLUMNS}) VALUES (
        'feedback-quarantined', 'tenant-a', 'employee-a', 'customer-a', 'card-a',
        'card-instance-q',
        'sha256_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        'reply-agent.v2', 'needs_improvement', '["too_long"]',
        'product_selection', 'evaluate', '["evidence-a"]', 'quarantined',
        '["residual_phone"]', '不应落库的原文',
        '2026-08-30T01:00:00.000Z', '2026-08-30T01:00:00.000Z'
      )
    `)).toThrow(/CHECK constraint failed/u);
  });
});
