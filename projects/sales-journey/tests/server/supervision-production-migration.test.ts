// @vitest-environment node

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { describe, expect, it } from "vitest";

const INITIAL_MIGRATION = readFileSync(
  resolve(process.cwd(), "migrations/001_supervision.sql"),
  "utf8",
);
const PRODUCTION_HARDENING_MIGRATION = readFileSync(
  resolve(process.cwd(), "migrations/002_supervision_production_hardening.sql"),
  "utf8",
);

function seedBase(database: DatabaseSync): void {
  database.exec(`
    INSERT INTO users (id, tenant_id, email, role, created_at)
    VALUES ('employee-1', 'tenant-a', 'employee@example.test', 'employee',
            '2026-08-30T00:00:00.000Z');
    INSERT INTO customers (
      id, tenant_id, employee_user_id, customer_number, customer_alias,
      category, planned_follow_up_at, status, created_at
    ) VALUES (
      'customer-1', 'tenant-a', 'employee-1', 'C-001', '脱敏客户', '沉香',
      '2026-08-31T00:00:00.000Z', 'pending', '2026-08-30T00:00:00.000Z'
    );
    INSERT INTO vault_assets (
      id, tenant_id, customer_id, uploaded_by_user_id, kind, vault_key,
      uploaded_at, delete_after, created_at
    ) VALUES (
      'vault-1', 'tenant-a', 'customer-1', 'employee-1', 'chat_text',
      'private/synthetic/vault-1', '2026-08-30T00:00:00.000Z',
      '2026-09-29T00:00:00.000Z', '2026-08-30T00:00:00.000Z'
    );
  `);
}

function insertBoundFollowUp(database: DatabaseSync): void {
  database.exec(`
    INSERT INTO follow_ups (
      id, tenant_id, customer_id, employee_user_id,
      customer_message_vault_asset_id, customer_message_summary,
      planned_follow_up_at, result, next_action, decision_card_id,
      card_instance_id, reply_usage, created_at
    ) VALUES (
      'follow-up-1', 'tenant-a', 'customer-1', 'employee-1', 'vault-1',
      '品类：沉香；意图：了解。', '2026-08-31T00:00:00.000Z', 'pending',
      '稍后回访', 'card-1', 'card-instance-1', 'not_used',
      '2026-08-30T00:00:00.000Z'
    );
    INSERT INTO decision_usages (
      id, tenant_id, customer_id, follow_up_id, employee_user_id,
      decision_card_id, card_instance_id, card_digest, scenario, delivery_mode,
      provided_reply_ids_json, reply_usage, created_at
    ) VALUES (
      'usage-1', 'tenant-a', 'customer-1', 'follow-up-1', 'employee-1',
      'card-1', 'card-instance-1',
      'sha256_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      'identity_bead', 'copy_only', '["reply-1"]', 'not_used',
      '2026-08-30T00:00:00.000Z'
    );
  `);
}

function applyD1StyleMigration(database: DatabaseSync, sql: string): void {
  database.exec("BEGIN IMMEDIATE");
  try {
    database.exec(sql);
    database.exec("COMMIT");
  } catch (error) {
    database.exec("ROLLBACK");
    throw error;
  }
}

describe("002 supervision production hardening migration", () => {
  it("leaves transaction ownership to D1 and defers foreign keys for the migration", () => {
    expect(PRODUCTION_HARDENING_MIGRATION).toMatch(/PRAGMA defer_foreign_keys\s*=\s*ON/iu);
    expect(PRODUCTION_HARDENING_MIGRATION).not.toMatch(/PRAGMA foreign_keys\s*=\s*OFF/iu);
    expect(PRODUCTION_HARDENING_MIGRATION).not.toMatch(/^\s*BEGIN(?:\s+IMMEDIATE)?\s*;/imu);
    expect(PRODUCTION_HARDENING_MIGRATION).not.toMatch(/^\s*COMMIT\s*;/imu);
  });

  it("upgrades the frozen 001 schema and makes every persisted card binding mandatory", () => {
    const database = new DatabaseSync(":memory:");
    database.exec(INITIAL_MIGRATION);
    seedBase(database);
    insertBoundFollowUp(database);

    applyD1StyleMigration(database, PRODUCTION_HARDENING_MIGRATION);

    const followUpColumns = database.prepare("PRAGMA table_info(follow_ups)").all() as
      Array<{ name: string; notnull: number }>;
    const usageColumns = database.prepare("PRAGMA table_info(decision_usages)").all() as
      Array<{ name: string; notnull: number }>;
    expect(followUpColumns.find(({ name }) => name === "card_instance_id")?.notnull).toBe(1);
    expect(usageColumns.find(({ name }) => name === "card_instance_id")?.notnull).toBe(1);
    expect(usageColumns.find(({ name }) => name === "card_digest")?.notnull).toBe(1);
    expect(database.prepare("PRAGMA foreign_key_check").all()).toEqual([]);

    expect(() => database.exec(`
      INSERT INTO follow_ups (
        id, tenant_id, customer_id, employee_user_id,
        customer_message_vault_asset_id, customer_message_summary,
        planned_follow_up_at, result, next_action, decision_card_id,
        reply_usage, created_at
      ) VALUES (
        'follow-up-unbound', 'tenant-a', 'customer-1', 'employee-1', 'vault-1',
        '去身份化摘要', '2026-08-31T00:00:00.000Z', 'pending', '稍后回访',
        'card-1', 'not_used', '2026-08-30T00:00:00.000Z'
      );
    `)).toThrow(/NOT NULL constraint failed: follow_ups\.card_instance_id/u);
  });

  it("fails closed when a legacy row has no trustworthy card binding", () => {
    const database = new DatabaseSync(":memory:");
    database.exec(INITIAL_MIGRATION);
    seedBase(database);
    database.exec(`
      INSERT INTO follow_ups (
        id, tenant_id, customer_id, employee_user_id,
        customer_message_vault_asset_id, customer_message_summary,
        planned_follow_up_at, result, next_action, decision_card_id,
        reply_usage, created_at
      ) VALUES (
        'legacy-unbound', 'tenant-a', 'customer-1', 'employee-1', 'vault-1',
        '旧摘要', '2026-08-31T00:00:00.000Z', 'pending', '人工复核',
        'legacy-card', 'not_used', '2026-08-30T00:00:00.000Z'
      );
    `);

    expect(() => applyD1StyleMigration(database, PRODUCTION_HARDENING_MIGRATION))
      .toThrow(/NOT NULL constraint failed: follow_ups_v2\.card_instance_id/u);
    expect(database.prepare(
      "SELECT card_instance_id FROM follow_ups WHERE id = 'legacy-unbound'",
    ).get()).toMatchObject({ card_instance_id: null });
  });
});
