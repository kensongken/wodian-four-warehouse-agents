// @vitest-environment node

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { DatabaseSync, type SQLInputValue } from "node:sqlite";
import { describe, expect, it } from "vitest";

import type { CreateReplyFeedbackInput } from "@/domain/supervision/contracts";
import {
  D1SupervisionRepository,
  type D1DatabaseLike,
  type D1PreparedStatementLike,
  type D1RunResult,
} from "@/server/supervision/d1-repository";

const migrations = [
  "migrations/001_supervision.sql",
  "migrations/002_supervision_production_hardening.sql",
  "migrations/003_reply_feedback.sql",
  "migrations/004_reply_feedback_audit.sql",
].map((path) => readFileSync(resolve(process.cwd(), path), "utf8"));

class SqlitePreparedStatement implements D1PreparedStatementLike {
  private bindings: SQLInputValue[] = [];

  constructor(
    private readonly database: DatabaseSync,
    private readonly sql: string,
  ) {}

  bind(...values: unknown[]): D1PreparedStatementLike {
    this.bindings = values as SQLInputValue[];
    return this;
  }

  async first<T>(): Promise<T | null> {
    return (this.database.prepare(this.sql).get(...this.bindings) ?? null) as T | null;
  }

  async all<T>(): Promise<{ results: T[]; success: boolean }> {
    return {
      results: this.database.prepare(this.sql).all(...this.bindings) as T[],
      success: true,
    };
  }

  async run(): Promise<D1RunResult> {
    const result = this.database.prepare(this.sql).run(...this.bindings);
    return { success: true, meta: { changes: Number(result.changes) } };
  }
}

class SqliteD1 implements D1DatabaseLike {
  readonly database = new DatabaseSync(":memory:");

  constructor() {
    for (const migration of migrations) this.database.exec(migration);
    this.database.exec(`
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
      INSERT INTO customers (
        id, tenant_id, employee_user_id, customer_number, customer_alias,
        category, planned_follow_up_at, status, created_at
      ) VALUES (
        'customer-b', 'tenant-a', 'employee-a', 'C-002', '另一脱敏客户', '绿松石',
        '2026-08-31T00:00:00.000Z', 'pending', '2026-08-30T00:00:00.000Z'
      );
    `);
  }

  prepare(sql: string): D1PreparedStatementLike {
    return new SqlitePreparedStatement(this.database, sql);
  }

  async batch(
    statements: D1PreparedStatementLike[],
  ): Promise<Array<D1RunResult & { results: unknown[] }>> {
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const results: Array<D1RunResult & { results: unknown[] }> = [];
      for (const statement of statements) {
        const result = await statement.all<unknown>();
        results.push({ success: true, results: result.results });
      }
      this.database.exec("COMMIT");
      return results;
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }
}

class InterleavingPreparedStatement implements D1PreparedStatementLike {
  constructor(
    private readonly delegate: D1PreparedStatementLike,
    private readonly afterFirst: () => void,
  ) {}

  bind(...values: unknown[]): D1PreparedStatementLike {
    this.delegate.bind(...values);
    return this;
  }

  async first<T>(): Promise<T | null> {
    const result = await this.delegate.first<T>();
    this.afterFirst();
    return result;
  }

  all<T>(): Promise<{ results: T[]; success?: boolean }> {
    return this.delegate.all<T>();
  }

  run(): Promise<D1RunResult> {
    return this.delegate.run();
  }
}

class InterleavingSqliteD1 extends SqliteD1 {
  private armed = false;
  private concurrentUpdateFired = false;

  get didInterleave(): boolean {
    return this.concurrentUpdateFired;
  }

  armConcurrentUpdate(): void {
    this.armed = true;
  }

  override async batch(
    statements: D1PreparedStatementLike[],
  ): Promise<Array<D1RunResult & { results: unknown[] }>> {
    const results = await super.batch(statements);
    this.fireConcurrentUpdate();
    return results;
  }

  override prepare(sql: string): D1PreparedStatementLike {
    const delegate = super.prepare(sql);
    if (!/^\s*INSERT INTO reply_feedback/iu.test(sql)) return delegate;
    return new InterleavingPreparedStatement(delegate, () => this.fireConcurrentUpdate());
  }

  private fireConcurrentUpdate(): void {
    if (!this.armed) return;
    this.armed = false;
    this.concurrentUpdateFired = true;
    this.database.prepare(`
      UPDATE reply_feedback
      SET rating = 'needs_improvement',
          reason_codes_json = '["too_long"]',
          updated_at = '2026-08-30T01:59:00.000Z'
      WHERE tenant_id = 'tenant-a' AND card_instance_id = 'card-instance-a'
    `).run();
  }
}

const safeInput = {
  tenantId: "tenant-a",
  employeeUserId: "employee-a",
  customerId: "customer-a",
  decisionCardId: "card-a",
  cardInstanceId: "card-instance-a",
  cardDigest: `sha256_${"a".repeat(64)}`,
  agentVersion: "reply-agent.v2",
  rating: "helpful",
  reasonCodes: [],
  scenario: "product_selection",
  salesStage: "evaluate",
  evidenceIds: ["evidence-a"],
  privacyStatus: "safe",
  privacyReasonCodes: [],
  sanitizedFinalReply: "这款纹理清楚，我把已核实的信息整理给您。",
} as const satisfies CreateReplyFeedbackInput;

const quarantinedInput = {
  tenantId: safeInput.tenantId,
  employeeUserId: safeInput.employeeUserId,
  customerId: safeInput.customerId,
  decisionCardId: safeInput.decisionCardId,
  cardInstanceId: "card-instance-quarantined",
  cardDigest: safeInput.cardDigest,
  agentVersion: safeInput.agentVersion,
  rating: "needs_improvement",
  reasonCodes: ["too_long"],
  scenario: safeInput.scenario,
  salesStage: safeInput.salesStage,
  evidenceIds: safeInput.evidenceIds,
  privacyStatus: "quarantined",
  privacyReasonCodes: ["residual_phone"],
} as const satisfies CreateReplyFeedbackInput;

async function reproduceOldSplitUnchangedRead(
  d1: InterleavingSqliteD1,
  input: typeof safeInput,
): Promise<{
  outcome: "unchanged";
  row: Record<string, unknown>;
}> {
  const inserted = await d1.prepare(
    `INSERT INTO reply_feedback (
      id, tenant_id, employee_user_id, customer_id, decision_card_id,
      card_instance_id, card_digest, agent_version, rating, reason_codes_json,
      scenario, sales_stage, evidence_ids_json, privacy_status,
      privacy_reason_codes_json, sanitized_final_reply, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (tenant_id, employee_user_id, card_instance_id, agent_version)
    DO NOTHING
    RETURNING *`,
  ).bind(
    "feedback-old-split-candidate",
    input.tenantId,
    input.employeeUserId,
    input.customerId,
    input.decisionCardId,
    input.cardInstanceId,
    input.cardDigest,
    input.agentVersion,
    input.rating,
    JSON.stringify(input.reasonCodes),
    input.scenario,
    input.salesStage,
    JSON.stringify(input.evidenceIds),
    input.privacyStatus,
    JSON.stringify(input.privacyReasonCodes),
    input.sanitizedFinalReply,
    "2026-08-30T01:00:00.000Z",
    "2026-08-30T01:00:00.000Z",
  ).first<Record<string, unknown>>();
  if (inserted) throw new Error("controlled old split expected an existing record");

  const row = await d1.prepare(
    `SELECT * FROM reply_feedback
     WHERE tenant_id = ? AND employee_user_id = ?
       AND card_instance_id = ? AND agent_version = ?
     LIMIT 1`,
  ).bind(
    input.tenantId,
    input.employeeUserId,
    input.cardInstanceId,
    input.agentVersion,
  ).first<Record<string, unknown>>();
  if (!row) throw new Error("controlled old split returned no record");
  return { outcome: "unchanged", row };
}

function createRepository(): { d1: SqliteD1; repository: D1SupervisionRepository } {
  const timestamps = [
    "2026-08-30T01:00:00.000Z",
    "2026-08-30T01:01:00.000Z",
    "2026-08-30T01:02:00.000Z",
    "2026-08-30T01:03:00.000Z",
  ];
  let nextId = 0;
  const d1 = new SqliteD1();
  return {
    d1,
    repository: new D1SupervisionRepository(d1, {
      createId: () => `feedback-generated-${++nextId}`,
      now: () => timestamps.shift() ?? "2026-08-30T01:04:00.000Z",
    }),
  };
}

describe("D1 supervision reply feedback repository", () => {
  it("keeps feedback audit atomic and privacy-minimized", async () => {
    const { d1, repository } = createRepository();
    const secretReply = "仅用于证明审计不能保存这段回复";
    const created = await repository.saveReplyFeedback({
      ...quarantinedInput,
      privacyReasonCodes: ["residual_phone"],
    });
    await repository.replaceQuarantinedReplyFeedback({
      ...quarantinedInput,
      privacyStatus: "safe",
      privacyReasonCodes: [],
      sanitizedFinalReply: secretReply,
    });

    const audits = await repository.listAuditEvents({
      tenantId: "tenant-a",
      targetKind: "reply_feedback",
      targetId: created.record.id,
    });
    expect(audits.map(({ action }) => action)).toEqual([
      "reply_feedback.created",
      "reply_feedback.quarantine_released",
    ]);
    expect(audits.every(({ actorUserId }) => actorUserId === "employee-a")).toBe(true);
    expect(JSON.stringify(audits)).not.toContain(secretReply);

    await expect(repository.saveReplyFeedback({
      ...safeInput,
      cardInstanceId: quarantinedInput.cardInstanceId,
      customerId: "customer-b",
    })).rejects.toThrow("immutable snapshot mismatch");
    expect(d1.database.prepare(
      "SELECT COUNT(*) AS count FROM audit_events WHERE target_kind = 'reply_feedback'",
    ).get()).toEqual({ count: 2 });
  });

  it("creates, keeps exact retries unchanged, and updates one composite-key record", async () => {
    const { repository } = createRepository();

    const created = await repository.saveReplyFeedback(safeInput);
    const unchanged = await repository.saveReplyFeedback(safeInput);
    const updated = await repository.saveReplyFeedback({
      ...safeInput,
      rating: "needs_improvement",
      reasonCodes: ["too_long"],
    });

    expect(created.outcome).toBe("created");
    expect(unchanged).toEqual({ outcome: "unchanged", record: created.record });
    expect(updated).toMatchObject({
      outcome: "updated",
      record: {
        id: created.record.id,
        createdAt: created.record.createdAt,
        rating: "needs_improvement",
        reasonCodes: ["too_long"],
      },
    });
    expect(updated.record.updatedAt).not.toBe(created.record.updatedAt);
    expect(await repository.listReplyFeedbackForOwner({ tenantId: "tenant-b" })).toEqual([]);
    expect(await repository.listReplyFeedbackForOwner({
      tenantId: "tenant-a",
      employeeUserId: "employee-other",
    })).toEqual([]);
    expect(await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" })).toHaveLength(1);
  });

  it("binds reply text as data and never returns text for quarantined rows", async () => {
    const { d1, repository } = createRepository();
    const injection = "已脱敏'); DROP TABLE reply_feedback; --";
    const safe = await repository.saveReplyFeedback({
      ...safeInput,
      sanitizedFinalReply: injection,
    });
    const quarantined = await repository.saveReplyFeedback(quarantinedInput);

    expect(safe.record).toMatchObject({ sanitizedFinalReply: injection });
    expect(d1.database.prepare("SELECT count(*) AS count FROM reply_feedback").get())
      .toMatchObject({ count: 2 });
    expect(quarantined.record).not.toHaveProperty("sanitizedFinalReply");
  });

  it("upgrades only the exact immutable quarantined snapshot", async () => {
    const { repository } = createRepository();
    const quarantined = await repository.saveReplyFeedback(quarantinedInput);

    const rejected = await repository.replaceQuarantinedReplyFeedback({
      ...safeInput,
      cardInstanceId: quarantinedInput.cardInstanceId,
      cardDigest: `sha256_${"b".repeat(64)}`,
    });
    expect(rejected).toBeUndefined();

    const upgraded = await repository.replaceQuarantinedReplyFeedback({
      ...safeInput,
      cardInstanceId: quarantinedInput.cardInstanceId,
    });
    expect(upgraded).toMatchObject({
      outcome: "updated",
      record: {
        id: quarantined.record.id,
        createdAt: quarantined.record.createdAt,
        privacyStatus: "safe",
        sanitizedFinalReply: safeInput.sanitizedFinalReply,
      },
    });
  });

  it("parses every selected row through the durable record schema", async () => {
    const { d1, repository } = createRepository();
    d1.database.exec(`
      INSERT INTO reply_feedback (
        id, tenant_id, employee_user_id, customer_id, decision_card_id,
        card_instance_id, card_digest, agent_version, rating, reason_codes_json,
        scenario, sales_stage, evidence_ids_json, privacy_status,
        privacy_reason_codes_json, created_at, updated_at
      ) VALUES (
        'malformed-feedback', 'tenant-a', 'employee-a', 'customer-a', 'card-a',
        'malformed-instance',
        'sha256_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        'reply-agent.v2', 'helpful', '{}', 'product_selection', 'evaluate', '[]',
        'safe', '[]', '2026-08-30T01:00:00.000Z', '2026-08-30T01:00:00.000Z'
      )
    `);

    await expect(repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" }))
      .rejects.toThrow();
  });

  it.each([
    ["customer", { customerId: "customer-b" }],
    ["decision card", { decisionCardId: "card-b" }],
    ["digest", { cardDigest: `sha256_${"b".repeat(64)}` }],
    ["scenario", { scenario: "scan_service" as const }],
    ["sales stage", { salesStage: "advance" as const }],
    ["ordered evidence", { evidenceIds: ["evidence-b", "evidence-a"] }],
  ] as const)("rejects ordinary-save changes to immutable %s", async (_field, mismatch) => {
    const { repository } = createRepository();
    const original = {
      ...safeInput,
      evidenceIds: ["evidence-a", "evidence-b"],
    } as const satisfies CreateReplyFeedbackInput;
    const created = await repository.saveReplyFeedback(original);

    await expect(repository.saveReplyFeedback({
      ...original,
      ...mismatch,
      rating: "needs_improvement",
      reasonCodes: ["too_long"],
    })).rejects.toThrow(/immutable snapshot mismatch/u);

    expect(await repository.listReplyFeedbackForOwner({ tenantId: original.tenantId }))
      .toEqual([created.record]);
  });

  it("requires guarded replacement for an ordinary quarantined-to-safe save", async () => {
    const { repository } = createRepository();
    const quarantined = await repository.saveReplyFeedback(quarantinedInput);

    await expect(repository.saveReplyFeedback({
      ...safeInput,
      cardInstanceId: quarantinedInput.cardInstanceId,
    })).rejects.toThrow(/guarded replacement/u);

    expect(await repository.listReplyFeedbackForOwner({ tenantId: quarantinedInput.tenantId }))
      .toEqual([quarantined.record]);
  });

  it("returns an unchanged record from the same atomic snapshot as its outcome", async () => {
    const d1 = new InterleavingSqliteD1();
    let nextId = 0;
    const repository = new D1SupervisionRepository(d1, {
      createId: () => `feedback-concurrency-${++nextId}`,
      now: () => "2026-08-30T01:00:00.000Z",
    });
    const created = await repository.saveReplyFeedback(safeInput);
    d1.armConcurrentUpdate();

    const retry = await repository.saveReplyFeedback(safeInput);

    expect(d1.didInterleave).toBe(true);
    expect(d1.database.prepare(
      "SELECT rating FROM reply_feedback WHERE card_instance_id = 'card-instance-a'",
    ).get()).toMatchObject({ rating: "needs_improvement" });
    expect(retry).toEqual({ outcome: "unchanged", record: created.record });
  });

  it("reproduces the former split-read gap with the same concurrent update", async () => {
    const d1 = new InterleavingSqliteD1();
    const repository = new D1SupervisionRepository(d1, {
      createId: () => "feedback-old-split-existing",
      now: () => "2026-08-30T01:00:00.000Z",
    });
    const created = await repository.saveReplyFeedback(safeInput);
    d1.armConcurrentUpdate();

    const oldSplitResult = await reproduceOldSplitUnchangedRead(d1, safeInput);

    expect(d1.didInterleave).toBe(true);
    expect(oldSplitResult).toMatchObject({
      outcome: "unchanged",
      row: {
        id: created.record.id,
        rating: "needs_improvement",
        reason_codes_json: '["too_long"]',
      },
    });
    expect(oldSplitResult.row.rating).not.toBe(created.record.rating);
  });

  it.each([
    ["customer", { customerId: "customer-b" }],
    ["decision card", { decisionCardId: "card-b" }],
    ["digest", { cardDigest: `sha256_${"b".repeat(64)}` }],
    ["scenario", { scenario: "scan_service" as const }],
    ["sales stage", { salesStage: "advance" as const }],
    ["ordered evidence", { evidenceIds: ["evidence-b", "evidence-a"] }],
  ] as const)("rejects guarded replacement with mismatched immutable %s", async (
    _field,
    mismatch,
  ) => {
    const { repository } = createRepository();
    const quarantinedSnapshot = {
      ...quarantinedInput,
      evidenceIds: ["evidence-a", "evidence-b"],
    } as const satisfies CreateReplyFeedbackInput;
    const quarantined = await repository.saveReplyFeedback(quarantinedSnapshot);

    const replacement = await repository.replaceQuarantinedReplyFeedback({
      ...safeInput,
      cardInstanceId: quarantinedSnapshot.cardInstanceId,
      evidenceIds: quarantinedSnapshot.evidenceIds,
      ...mismatch,
    });

    expect(replacement).toBeUndefined();
    expect(await repository.listReplyFeedbackForOwner({ tenantId: quarantinedSnapshot.tenantId }))
      .toEqual([quarantined.record]);
  });
});
