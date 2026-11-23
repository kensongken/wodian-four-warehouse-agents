import { describe, expect, it } from "vitest";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { DatabaseSync, type SQLInputValue } from "node:sqlite";

import {
  D1SupervisionRepository,
  type D1DatabaseLike,
  type D1PreparedStatementLike,
} from "@/server/supervision/d1-repository";
import { ownerCustomerMatches } from "@/domain/supervision/customer-query";

type QueryResult = Record<string, unknown> | readonly Record<string, unknown>[] | null;
type RunResult = Readonly<{ success: boolean; meta?: { changes?: number } }>;

class RecordingD1 implements D1DatabaseLike {
  readonly calls: Array<{ sql: string; bindings: readonly unknown[] }> = [];
  readonly batches: D1PreparedStatementLike[][] = [];
  runCount = 0;

  constructor(
    private readonly responses: QueryResult[] = [],
    private readonly runResults: RunResult[] = [],
  ) {}

  prepare(sql: string) {
    const response = this.responses.shift() ?? null;
    const call = { sql, bindings: [] as unknown[] };
    this.calls.push(call);

    const statement = {
      bind: (...bindings: unknown[]) => {
        call.bindings = bindings;
        return statement;
      },
      first: async <T>() => (Array.isArray(response) ? response[0] ?? null : response) as T | null,
      all: async <T>() => ({
        results: (Array.isArray(response) ? response : response ? [response] : []) as T[],
        success: true,
      }),
      run: async () => {
        this.runCount += 1;
        return this.runResults.shift() ?? { success: true, meta: { changes: 1 } };
      },
    };

    return statement;
  }

  async batch(statements: D1PreparedStatementLike[]): Promise<RunResult[]> {
    this.batches.push(statements);
    return statements.map(() => ({ success: true, meta: { changes: 1 } }));
  }
}

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

  async run(): Promise<RunResult> {
    const result = this.database.prepare(this.sql).run(...this.bindings);
    return { success: true, meta: { changes: Number(result.changes) } };
  }
}

class SqliteD1 implements D1DatabaseLike {
  readonly database = new DatabaseSync(":memory:");

  prepare(sql: string): D1PreparedStatementLike {
    return new SqlitePreparedStatement(this.database, sql);
  }

  async batch(statements: D1PreparedStatementLike[]): Promise<RunResult[]> {
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const results: RunResult[] = [];
      for (const statement of statements) results.push(await statement.run());
      this.database.exec("COMMIT");
      return results;
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }
}

const customerRow = {
  id: "customer-a",
  tenant_id: "tenant-a",
  employee_user_id: "employee-a",
  customer_number: "C-001",
  customer_alias: "林女士",
  category: "沉香",
  planned_follow_up_at: "2026-08-31T06:30:00.000Z",
  status: "pending",
  version: 1,
  created_at: "2026-08-30T06:30:00.000Z",
};

const followUpRow = {
  id: "f-1",
  tenant_id: "tenant-a",
  customer_id: "customer-a",
  employee_user_id: "employee-a",
  customer_message_vault_asset_id: "vault-follow-up-a",
  customer_message_summary: "客户希望确认到店时间。",
  planned_follow_up_at: "2026-08-31T06:30:00.000Z",
  actual_follow_up_at: null,
  result: "pending",
  next_action: "明天电话确认。",
  decision_card_id: "card-001",
  reply_usage: "not_used",
  chosen_reply_id: null,
  final_edited_text: null,
  version: 1,
  created_at: "2026-08-30T06:30:00.000Z",
};

const vaultRow = {
  id: "vault-a",
  tenant_id: "tenant-a",
  customer_id: "customer-a",
  uploaded_by_user_id: "employee-a",
  kind: "chat_text",
  vault_key: "tenant-a/vault-a",
  uploaded_at: "2026-08-01T00:00:00.000Z",
  delete_after: "2026-08-31T00:00:00.000Z",
  deleted_at: null,
  deleted_by_user_id: null,
  version: 1,
  created_at: "2026-08-01T00:00:00.000Z",
};

const alertRow = {
  id: "alert-existing",
  tenant_id: "tenant-a",
  customer_id: null,
  employee_user_id: null,
  detected_by_user_id: null,
  kind: "retention_failed",
  status: "open",
  resolved_at: null,
  resolved_by_user_id: null,
  resolution_note: "第二次检测",
  version: 2,
  created_at: "2026-08-30T00:00:00.000Z",
};

describe("D1 supervision repository", () => {
  it("applies the complete D1 migration with vault-referenced follow-ups", () => {
    const migration = readFileSync(
      resolve(process.cwd(), "migrations/001_supervision.sql"),
      "utf8",
    );

    expect(() => execFileSync("sqlite3", [":memory:"], {
      input: migration,
      stdio: "pipe",
    })).not.toThrow();
  });

  it("returns tenant-scoped safe dashboard rows with exact drill-down IDs", async () => {
    const d1 = new RecordingD1([[
      {
        ...followUpRow,
        customer_number: "C-001",
        customer_alias: "林女士",
        category: "沉香",
        response_started_at: customerRow.created_at,
        customer_fingerprint: `fp_${"a".repeat(64)}`,
        employee_label: "employee-a",
        safety_status: "reviewRequired",
        safety_rule_ids_json: JSON.stringify(["PRICE_OR_PAYMENT_HUMAN_REVIEW"]),
      },
    ], []]);
    const repo = new D1SupervisionRepository(d1);

    const dashboard = await repo.getOwnerDashboard({
      tenantId: "tenant-a",
      now: "2026-09-01T00:00:00.000Z",
      employeeUserId: "employee-a",
    });

    expect(dashboard.records).toEqual([
      expect.objectContaining({ id: "f-1", customerNumber: "C-001" }),
    ]);
    expect(dashboard.totals.drillDownRecordIds.overdue).toEqual(["f-1"]);
    expect(dashboard.riskSignals).toEqual(expect.arrayContaining([
      expect.objectContaining({ kind: "sales_safety", recordIds: ["f-1"] }),
    ]));
    expect(d1.calls[0]?.sql).toContain("c.tenant_id = f.tenant_id");
    expect(d1.calls[0]?.bindings).toEqual(["tenant-a", "employee-a"]);
  });

  it("does not turn a safe card's copy-only rule into a sales-safety signal", async () => {
    const d1 = new RecordingD1([[
      {
        ...followUpRow,
        customer_number: "C-001",
        customer_alias: "林女士",
        category: "沉香",
        response_started_at: customerRow.created_at,
        customer_fingerprint: null,
        employee_label: "employee-a",
        safety_status: "safe",
        safety_rule_ids_json: JSON.stringify(["COPY_ONLY_DELIVERY"]),
      },
    ], []]);
    const repo = new D1SupervisionRepository(d1);

    const dashboard = await repo.getOwnerDashboard({
      tenantId: "tenant-a",
      now: "2026-08-30T07:00:00.000Z",
    });

    expect(dashboard.riskSignals.some((signal) => signal.kind === "sales_safety")).toBe(false);
  });

  it("unions multiple review-required usages independently of joined-row order", async () => {
    const joinedRow = (rule: string, safetyStatus = "reviewRequired") => ({
      ...followUpRow,
      customer_number: "C-001",
      customer_alias: "林女士",
      category: "沉香",
      response_started_at: customerRow.created_at,
      customer_fingerprint: null,
      employee_label: "employee-a",
      safety_status: safetyStatus,
      safety_rule_ids_json: JSON.stringify([rule]),
    });
    const rows = [
      joinedRow("PRICE_OR_PAYMENT_HUMAN_REVIEW"),
      joinedRow("DEFECT_OR_DISPUTE_HUMAN_REVIEW"),
      joinedRow("COPY_ONLY_DELIVERY", "safe"),
    ];
    const dashboards = await Promise.all([
      new D1SupervisionRepository(new RecordingD1([rows, [], []])).getOwnerDashboard({
        tenantId: "tenant-a",
        now: "2026-08-30T07:00:00.000Z",
      }),
      new D1SupervisionRepository(new RecordingD1([[...rows].reverse(), [], []])).getOwnerDashboard({
        tenantId: "tenant-a",
        now: "2026-08-30T07:00:00.000Z",
      }),
    ]);

    const labels = dashboards.map((dashboard) => dashboard.riskSignals
      .find((signal) => signal.kind === "sales_safety")?.labels);
    expect(labels).toEqual([
      ["DEFECT_OR_DISPUTE_HUMAN_REVIEW", "PRICE_OR_PAYMENT_HUMAN_REVIEW"],
      ["DEFECT_OR_DISPUTE_HUMAN_REVIEW", "PRICE_OR_PAYMENT_HUMAN_REVIEW"],
    ]);
  });

  it("keeps generic review-required signals when joined rule IDs are null or empty", async () => {
    const joinedRow = (rules: string | null) => ({
      ...followUpRow,
      customer_number: "C-001",
      customer_alias: "林女士",
      category: "沉香",
      response_started_at: customerRow.created_at,
      customer_fingerprint: null,
      employee_label: "employee-a",
      safety_status: "reviewRequired",
      safety_rule_ids_json: rules,
    });
    const dashboards = await Promise.all([
      new D1SupervisionRepository(new RecordingD1([[joinedRow(null)], [], []]))
        .getOwnerDashboard({ tenantId: "tenant-a", now: "2026-08-30T07:00:00.000Z" }),
      new D1SupervisionRepository(new RecordingD1([[joinedRow("[]")], [], []]))
        .getOwnerDashboard({ tenantId: "tenant-a", now: "2026-08-30T07:00:00.000Z" }),
    ]);

    expect(dashboards.map((dashboard) => dashboard.riskSignals
      .find((signal) => signal.kind === "sales_safety")?.labels)).toEqual([
      ["review_required"],
      ["review_required"],
    ]);
  });

  it("queries tenant fingerprints independently from dashboard filters", async () => {
    const fingerprint = `fp_${"b".repeat(64)}`;
    const d1 = new RecordingD1([[], [
      {
        id: "customer-duplicate-a",
        tenant_id: "tenant-a",
        employee_user_id: "employee-a",
        customer_fingerprint: fingerprint,
      },
      {
        id: "customer-duplicate-b",
        tenant_id: "tenant-a",
        employee_user_id: "employee-b",
        customer_fingerprint: fingerprint,
      },
    ], []]);
    const dashboard = await new D1SupervisionRepository(d1).getOwnerDashboard({
      tenantId: "tenant-a",
      now: "2026-08-30T07:00:00.000Z",
      employeeUserId: "employee-no-match",
      from: "2026-09-01T00:00:00.000Z",
      result: "won",
    });

    expect(dashboard.records).toEqual([]);
    expect(dashboard.riskSignals).toEqual([
      expect.objectContaining({
        kind: "duplicate_customer",
        recordIds: ["customer-duplicate-a", "customer-duplicate-b"],
      }),
    ]);
    expect(d1.calls[1]?.sql).toContain("FROM customers");
    expect(d1.calls[1]?.bindings).toEqual(["tenant-a"]);
  });

  it("attaches duplicate-customer labels by customer ID while other risks stay follow-up scoped", async () => {
    const fingerprint = `fp_${"d".repeat(64)}`;
    const joinedRow = (
      row: typeof followUpRow,
      customerNumber: string,
      customerAlias: string,
    ) => ({
      ...row,
      customer_number: customerNumber,
      customer_alias: customerAlias,
      category: "沉香",
      response_started_at: customerRow.created_at,
      customer_fingerprint: fingerprint,
      employee_label: row.employee_user_id,
      safety_status: null,
      safety_rule_ids_json: null,
    });
    const secondFollowUp = {
      ...followUpRow,
      id: "f-2",
      customer_id: "customer-b",
      employee_user_id: "employee-b",
      customer_message_vault_asset_id: "vault-follow-up-b",
      decision_card_id: "card-002",
    };
    const d1 = new RecordingD1([[
      joinedRow(followUpRow, "C-001", "林女士"),
      joinedRow(secondFollowUp, "C-002", "陈女士"),
    ], [
      {
        id: "customer-a",
        tenant_id: "tenant-a",
        employee_user_id: "employee-a",
        customer_fingerprint: fingerprint,
      },
      {
        id: "customer-b",
        tenant_id: "tenant-a",
        employee_user_id: "employee-b",
        customer_fingerprint: fingerprint,
      },
    ], []]);

    const dashboard = await new D1SupervisionRepository(d1).getOwnerDashboard({
      tenantId: "tenant-a",
      now: "2026-09-01T00:00:00.000Z",
    });

    expect(dashboard.riskSignals.find((signal) => signal.kind === "duplicate_customer"))
      .toMatchObject({ recordIds: ["customer-a", "customer-b"] });
    expect(dashboard.riskSignals
      .filter((signal) => signal.kind === "overdue_follow_up")
      .flatMap((signal) => signal.recordIds)
      .sort()).toEqual(["f-1", "f-2"]);
    expect(dashboard.records).toEqual([
      expect.objectContaining({
        id: "f-1",
        riskLabels: ["duplicate_customer", "overdue_follow_up"],
      }),
      expect.objectContaining({
        id: "f-2",
        riskLabels: ["duplicate_customer", "overdue_follow_up"],
      }),
    ]);
  });

  it("normalizes offset dashboard filters before binding D1", async () => {
    const d1 = new RecordingD1([[], [], []]);

    await new D1SupervisionRepository(d1).getOwnerDashboard({
      tenantId: "tenant-a",
      now: "2026-08-30T07:00:00.000Z",
      from: "2026-08-30T01:00:00+08:00",
      to: "2026-08-29T18:00:00.1Z",
    });

    expect(d1.calls[0]?.bindings).toEqual([
      "tenant-a",
      "2026-08-29T17:00:00.000Z",
      "2026-08-29T18:00:00.100Z",
    ]);
  });

  it("keeps customerQuery out of SQLite while preserving tenant and other SQL bounds", async () => {
    const d1 = new RecordingD1([[], [], []]);
    await new D1SupervisionRepository(d1).getOwnerDashboard({
      tenantId: "tenant-a", now: "2026-08-30T07:00:00.000Z", customerQuery: "C-%_01",
    });
    expect(d1.calls[0]?.sql).not.toContain("LIKE");
    expect(d1.calls[0]?.bindings).toEqual(["tenant-a"]);
  });

  it("applies shared Unicode case and whitespace semantics to rows fetched from real SQLite", () => {
    const database = new DatabaseSync(":memory:");
    database.exec("CREATE TABLE customers(tenant_id TEXT, customer_alias TEXT, customer_number TEXT); INSERT INTO customers VALUES('tenant-a','Élodie','C-018'),('tenant-a','林女士','C-019'),('tenant-b','Élodie','C-999');");
    const rows = database.prepare("SELECT customer_alias, customer_number FROM customers WHERE tenant_id = ?").all("tenant-a") as Array<{customer_alias:string;customer_number:string}>;
    expect(rows.filter((row) => ownerCustomerMatches(row.customer_alias, row.customer_number, "\télodie\u2003C-" )).map((row) => row.customer_number)).toEqual(["C-018"]);
  });

  it("rejects malformed joined customer fields instead of stringifying them", async () => {
    const d1 = new RecordingD1([[
      {
        ...followUpRow,
        customer_number: "C-001",
        customer_alias: null,
        category: "沉香",
        response_started_at: customerRow.created_at,
        customer_fingerprint: null,
        employee_label: "employee-a",
        safety_rule_ids_json: "[]",
      },
    ], []]);
    const repo = new D1SupervisionRepository(d1);

    await expect(repo.getOwnerDashboard({
      tenantId: "tenant-a",
      now: "2026-09-01T00:00:00.000Z",
    })).rejects.toThrow();
  });

  it("persists structured export filters and row count in the audit ledger", async () => {
    const d1 = new RecordingD1();
    const repo = new D1SupervisionRepository(d1, {
      now: () => "2026-08-30T12:00:00.000Z",
      createId: () => "audit-export-1",
    });

    await repo.appendAudit({
      tenantId: "tenant-a",
      actorUserId: "owner-a",
      action: "owner.exported",
      targetKind: "owner_export",
      targetId: "export-1",
      occurredAt: "2026-08-30T12:00:00.000Z",
      details: { filters: { employeeUserId: "employee-a" }, rowCount: 1 },
    });

    expect(d1.calls[0]?.sql).toContain("details_json");
    expect(d1.calls[0]?.bindings).toContain(
      JSON.stringify({ filters: { employeeUserId: "employee-a" }, rowCount: 1 }),
    );
  });

  it("enforces exact pending-upload retention and a bounded tenant-day quota in SQLite", () => {
    const migration = readFileSync(
      resolve(process.cwd(), "migrations/001_supervision.sql"),
      "utf8",
    );
    const fixture = `
      INSERT INTO users (id, tenant_id, email, role, created_at)
      VALUES ('employee-a', 'tenant-a', 'employee@example.test', 'employee',
              '2026-08-30T00:00:00.000Z');
      INSERT INTO customers (
        id, tenant_id, employee_user_id, customer_number, customer_alias,
        category, planned_follow_up_at, status, created_at
      ) VALUES (
        'customer-a', 'tenant-a', 'employee-a', 'C-001', '林女士', '沉香',
        '2026-08-31T00:00:00.000Z', 'pending', '2026-08-30T00:00:00.000Z'
      );
      INSERT INTO vault_upload_daily_quotas (
        tenant_id, upload_day, used_bytes, byte_limit, version, created_at
      ) VALUES ('tenant-a', '2026-08-30', 8, 12, 1, '2026-08-30T00:00:00.000Z');
      UPDATE vault_upload_daily_quotas SET used_bytes = 13
      WHERE tenant_id = 'tenant-a' AND upload_day = '2026-08-30';
    `;

    expect(() => execFileSync("sqlite3", [":memory:"], {
      input: migration + fixture,
      stdio: "pipe",
    })).toThrow();

    const badRetention = `
      INSERT INTO users (id, tenant_id, email, role, created_at)
      VALUES ('employee-a', 'tenant-a', 'employee@example.test', 'employee',
              '2026-08-30T00:00:00.000Z');
      INSERT INTO customers (
        id, tenant_id, employee_user_id, customer_number, customer_alias,
        category, planned_follow_up_at, status, created_at
      ) VALUES (
        'customer-a', 'tenant-a', 'employee-a', 'C-001', '林女士', '沉香',
        '2026-08-31T00:00:00.000Z', 'pending', '2026-08-30T00:00:00.000Z'
      );
      INSERT INTO vault_pending_uploads (
        id, tenant_id, customer_id, uploaded_by_user_id, vault_key, byte_size,
        uploaded_at, delete_after, upload_day, version, created_at
      ) VALUES (
        'pending-a', 'tenant-a', 'customer-a', 'employee-a', 'private/pending-a', 8,
        '2026-08-30T00:00:00.000Z', '2026-09-28T00:00:00.000Z', '2026-08-30',
        1, '2026-08-30T00:00:00.000Z'
      );
    `;
    expect(() => execFileSync("sqlite3", [":memory:"], {
      input: migration + badRetention,
      stdio: "pipe",
    })).toThrow();
  });

  it("atomically rejects one of two concurrent tenant-day quota reservations", async () => {
    const d1 = new SqliteD1();
    d1.database.exec(readFileSync(
      resolve(process.cwd(), "migrations/001_supervision.sql"),
      "utf8",
    ));
    d1.database.exec(`
      INSERT INTO users (id, tenant_id, email, role, created_at)
      VALUES ('employee-a', 'tenant-a', 'employee@example.test', 'employee',
              '2026-08-30T00:00:00.000Z');
      INSERT INTO customers (
        id, tenant_id, employee_user_id, customer_number, customer_alias,
        category, planned_follow_up_at, status, created_at
      ) VALUES (
        'customer-a', 'tenant-a', 'employee-a', 'C-001', '林女士', '沉香',
        '2026-08-31T00:00:00.000Z', 'pending', '2026-08-30T00:00:00.000Z'
      );
    `);
    const repo = new D1SupervisionRepository(d1);
    const pending = (id: string) => ({
      id,
      tenantId: "tenant-a",
      customerId: "customer-a",
      uploadedByUserId: "employee-a",
      vaultKey: `private/${id}`,
      byteSize: 8,
      uploadedAt: "2026-08-30T08:00:00.000Z",
      deleteAfter: "2026-09-29T08:00:00.000Z",
      uploadDay: "2026-08-30",
    });

    const results = await Promise.allSettled([
      repo.reserveVaultUploadBundle({ pendingUploads: [pending("pending-a")], dailyByteLimit: 12 }),
      repo.reserveVaultUploadBundle({ pendingUploads: [pending("pending-b")], dailyByteLimit: 12 }),
    ]);

    expect(results.filter((result) => result.status === "fulfilled")).toHaveLength(1);
    expect(results.filter((result) => result.status === "rejected")).toHaveLength(1);
    await expect(repo.getVaultDailyUploadUsage({
      tenantId: "tenant-a",
      uploadDay: "2026-08-30",
    })).resolves.toBe(8);
    const reserved = d1.database.prepare(
      "SELECT id, vault_key FROM vault_pending_uploads",
    ).get() as { id: string; vault_key: string };
    expect(d1.database.prepare(
      "SELECT COUNT(*) AS count FROM vault_pending_uploads",
    ).get()).toMatchObject({ count: 1 });

    await repo.createVaultUploadBundle({
      assets: [{
        id: reserved.id,
        tenantId: "tenant-a",
        customerId: "customer-a",
        uploadedByUserId: "employee-a",
        kind: "product_image",
        vaultKey: reserved.vault_key,
        byteSize: 8,
        uploadedAt: "2026-08-30T08:00:00.000Z",
        deleteAfter: "2026-09-29T08:00:00.000Z",
      }],
      audits: [{
        id: `audit-${reserved.id}`,
        tenantId: "tenant-a",
        actorUserId: "employee-a",
        action: "vault.uploaded",
        targetKind: "vault_asset",
        targetId: reserved.id,
        occurredAt: "2026-08-30T08:00:00.000Z",
      }],
    });
    await expect(repo.getVaultDailyUploadUsage({
      tenantId: "tenant-a",
      uploadDay: "2026-08-30",
    })).resolves.toBe(8);
    expect(d1.database.prepare(
      "SELECT COUNT(*) AS count FROM vault_pending_uploads",
    ).get()).toMatchObject({ count: 0 });

    await repo.reserveVaultUploadBundle({
      pendingUploads: [{
        ...pending("pending-cleanup"),
        uploadedAt: "2026-08-31T08:00:00.000Z",
        deleteAfter: "2026-09-30T08:00:00.000Z",
        uploadDay: "2026-08-31",
      }],
      dailyByteLimit: 12,
    });
    await repo.resolveVaultPendingUpload({
      tenantId: "tenant-a",
      pendingUploadId: "pending-cleanup",
    });
    await expect(repo.getVaultDailyUploadUsage({
      tenantId: "tenant-a",
      uploadDay: "2026-08-31",
    })).resolves.toBe(0);
  });

  it("commits a customer and audit through one D1 atomic batch", async () => {
    const d1 = new RecordingD1();
    const repo = new D1SupervisionRepository(d1, {
      now: () => "2026-08-30T06:30:00.000Z",
    });

    const result = await repo.createCustomerBundle({
      customer: {
        id: "customer-a",
        tenantId: "tenant-a",
        employeeUserId: "employee-a",
        customerNumber: "C-001",
        customerAlias: "林女士",
        category: "沉香",
        plannedFollowUpAt: "2026-08-31T06:30:00.000Z",
        status: "pending",
      },
      audit: {
        id: "audit-customer-a",
        tenantId: "tenant-a",
        actorUserId: "employee-a",
        action: "customer.created",
        targetKind: "customer",
        targetId: "customer-a",
        occurredAt: "2026-08-30T06:30:00.000Z",
      },
    });

    expect(result).toMatchObject({
      customer: { id: "customer-a" },
      audit: { actorUserId: "employee-a", targetId: "customer-a" },
    });
    expect(d1.batches).toHaveLength(1);
    expect(d1.batches[0]).toHaveLength(2);
    expect(d1.calls.map((call) => call.sql)).toEqual([
      expect.stringContaining("INSERT INTO customers"),
      expect.stringContaining("INSERT INTO audit_events"),
    ]);
    expect(d1.runCount).toBe(0);
  });

  it("commits follow-up, decision usage, vault reference, and audit in one batch", async () => {
    const d1 = new RecordingD1([customerRow]);
    const repo = new D1SupervisionRepository(d1, {
      now: () => "2026-08-30T06:30:00.000Z",
    });

    const result = await repo.appendFollowUpBundle({
      followUp: {
        id: "follow-up-bundle",
        tenantId: "tenant-a",
        customerId: "customer-a",
        employeeUserId: "employee-a",
        customerMessageVaultAssetId: "vault-bundle",
        customerMessageSummary: "品类：沉香；意图：learn_identity。",
        plannedFollowUpAt: "2026-08-31T06:30:00.000Z",
        result: "pending",
        nextAction: "明天回访",
        decisionCardId: "card-001",
        replyUsage: "not_used",
      },
      decisionUsage: {
        id: "usage-bundle",
        tenantId: "tenant-a",
        customerId: "customer-a",
        followUpId: "follow-up-bundle",
        employeeUserId: "employee-a",
        decisionCardId: "card-001",
        scenario: "silent_customer_followup",
        deliveryMode: "copy_only",
        providedReplyIds: ["reply-01"],
        replyUsage: "not_used",
      },
      vaultAsset: {
        id: "vault-bundle",
        tenantId: "tenant-a",
        customerId: "customer-a",
        uploadedByUserId: "employee-a",
        kind: "chat_text",
        vaultKey: "private/tenant-a/customer-a/vault-bundle",
        uploadedAt: "2026-08-30T06:30:00.000Z",
        deleteAfter: "2026-09-29T06:30:00.000Z",
      },
      audit: {
        id: "audit-follow-up-bundle",
        tenantId: "tenant-a",
        actorUserId: "employee-a",
        action: "follow_up.created",
        targetKind: "follow_up",
        targetId: "follow-up-bundle",
        occurredAt: "2026-08-30T06:30:00.000Z",
      },
    });

    expect(result).toMatchObject({
      followUp: { id: "follow-up-bundle" },
      decisionUsage: { followUpId: "follow-up-bundle" },
      vaultAsset: { kind: "chat_text" },
      audit: { actorUserId: "employee-a" },
    });
    expect(d1.batches).toHaveLength(1);
    expect(d1.batches[0]).toHaveLength(4);
    expect(d1.calls.slice(1).map((call) => call.sql)).toEqual([
      expect.stringContaining("INSERT INTO vault_assets"),
      expect.stringContaining("INSERT INTO follow_ups"),
      expect.stringContaining("INSERT INTO decision_usages"),
      expect.stringContaining("INSERT INTO audit_events"),
    ]);
    expect(d1.runCount).toBe(0);
  });

  it("fails closed before writes when the D1 binding lacks atomic batch support", async () => {
    const recording = new RecordingD1();
    const d1WithoutBatch: D1DatabaseLike = {
      prepare: recording.prepare.bind(recording),
    };
    const repo = new D1SupervisionRepository(d1WithoutBatch, {
      now: () => "2026-08-30T06:30:00.000Z",
    });

    await expect(repo.createCustomerBundle({
      customer: {
        id: "customer-no-batch",
        tenantId: "tenant-a",
        employeeUserId: "employee-a",
        customerNumber: "C-NO-BATCH",
        customerAlias: "林女士",
        category: "沉香",
        plannedFollowUpAt: "2026-08-31T06:30:00.000Z",
        status: "pending",
      },
      audit: {
        id: "audit-no-batch",
        tenantId: "tenant-a",
        actorUserId: "employee-a",
        action: "customer.created",
        targetKind: "customer",
        targetId: "customer-no-batch",
        occurredAt: "2026-08-30T06:30:00.000Z",
      },
    })).rejects.toThrow("D1 atomic batch binding unavailable");
    expect(recording.runCount).toBe(0);
  });

  it("commits a correction and its actor audit in one D1 batch", async () => {
    const d1 = new RecordingD1([followUpRow]);
    const repo = new D1SupervisionRepository(d1, {
      now: () => "2026-08-30T06:30:00.000Z",
    });

    await expect(repo.appendCorrectionBundle({
      correction: {
        id: "correction-bundle",
        tenantId: "tenant-a",
        followUpId: "f-1",
        actorUserId: "employee-a",
        reason: "补充正确结果",
      },
      audit: {
        id: "audit-correction-bundle",
        tenantId: "tenant-a",
        actorUserId: "employee-a",
        action: "correction.created",
        targetKind: "correction",
        targetId: "correction-bundle",
        occurredAt: "2026-08-30T06:30:00.000Z",
      },
    })).resolves.toMatchObject({
      correction: { customerId: "customer-a", followUpId: "f-1" },
      audit: { actorUserId: "employee-a" },
    });
    expect(d1.batches).toHaveLength(1);
    expect(d1.batches[0]).toHaveLength(2);
    expect(d1.runCount).toBe(0);
  });

  it("binds tenant and customer IDs instead of interpolating them", async () => {
    const d1 = new RecordingD1([customerRow]);
    const repo = new D1SupervisionRepository(d1);

    await expect(repo.getCustomer({
      tenantId: "tenant-a",
      customerId: "customer-a",
    })).resolves.toMatchObject({ tenantId: "tenant-a", id: "customer-a" });

    expect(d1.calls[0]?.sql).toContain("tenant_id = ?");
    expect(d1.calls[0]?.sql).toContain("id = ?");
    expect(d1.calls[0]?.sql).not.toContain("tenant-a");
    expect(d1.calls[0]?.bindings).toEqual(["tenant-a", "customer-a"]);
  });

  it("rejects malformed persisted customer data at the read boundary", async () => {
    const d1 = new RecordingD1([{ ...customerRow, version: 0 }]);
    const repo = new D1SupervisionRepository(d1);

    await expect(repo.getCustomer({
      tenantId: "tenant-a",
      customerId: "customer-a",
    })).rejects.toMatchObject({ name: "ZodError" });
  });

  it("does not coerce a missing persisted field into usable customer text", async () => {
    const d1 = new RecordingD1([{ ...customerRow, customer_alias: null }]);
    const repo = new D1SupervisionRepository(d1);

    await expect(repo.getCustomer({
      tenantId: "tenant-a",
      customerId: "customer-a",
    })).rejects.toMatchObject({ name: "ZodError" });
  });

  it("does not coerce malformed optional persisted fields", async () => {
    const d1 = new RecordingD1([[
      { ...followUpRow, chosen_reply_id: 42 },
    ], [], []]);
    const repo = new D1SupervisionRepository(d1);

    await expect(repo.listTimeline({
      tenantId: "tenant-a",
      customerId: "customer-a",
    })).rejects.toMatchObject({ name: "ZodError" });
  });

  it("rejects an arbitrary vault expiry before writing D1", async () => {
    const d1 = new RecordingD1();
    const repo = new D1SupervisionRepository(d1);

    await expect(repo.createVaultAsset({
      tenantId: "tenant-a",
      customerId: "customer-a",
      uploadedByUserId: "employee-a",
      kind: "chat_text",
      vaultKey: "tenant-a/vault-001",
      uploadedAt: "2026-08-30T06:30:00.000Z",
      deleteAfter: "2026-09-28T06:30:00.000Z",
    })).rejects.toMatchObject({ name: "ZodError" });
    expect(d1.calls).toHaveLength(0);
  });

  it("tenant-scopes expired-vault reads and validates every mapped row", async () => {
    const malformedVaultRow = {
      id: "vault-a",
      tenant_id: "tenant-a",
      customer_id: "customer-a",
      uploaded_by_user_id: "employee-a",
      kind: "chat_text",
      vault_key: "tenant-a/vault-a",
      uploaded_at: "2026-08-01T00:00:00.000Z",
      delete_after: "2026-08-30T00:00:00.000Z",
      deleted_at: null,
      deleted_by_user_id: null,
      version: 1,
      created_at: "2026-08-01T00:00:00.000Z",
    };
    const d1 = new RecordingD1([[malformedVaultRow]]);
    const repo = new D1SupervisionRepository(d1);

    await expect(repo.listExpiredVaultAssets({
      tenantId: "tenant-a",
      now: "2026-09-01T00:00:00.000Z",
      limit: 20,
    })).rejects.toMatchObject({ name: "ZodError" });
    expect(d1.calls[0]?.bindings).toEqual([
      "tenant-a",
      "2026-09-01T00:00:00.000Z",
      20,
    ]);
  });

  it("loads and parses a vault asset before applying deletion metadata", async () => {
    const d1 = new RecordingD1([vaultRow]);
    const repo = new D1SupervisionRepository(d1);

    await expect(repo.markVaultAssetDeleted({
      tenantId: "tenant-a",
      vaultAssetId: "vault-a",
      deletedAt: "not-an-iso-time",
    })).rejects.toMatchObject({ name: "ZodError" });

    expect(d1.calls).toHaveLength(1);
    expect(d1.calls[0]?.sql).toContain("SELECT * FROM vault_assets");
    expect(d1.calls[0]?.bindings).toEqual(["tenant-a", "vault-a"]);
  });

  it("fails vault deletion when the tenant-scoped update affects zero rows", async () => {
    const d1 = new RecordingD1([vaultRow], [
      { success: true, meta: { changes: 0 } },
    ]);
    const repo = new D1SupervisionRepository(d1);

    await expect(repo.markVaultAssetDeleted({
      tenantId: "tenant-a",
      vaultAssetId: "vault-a",
      deletedAt: "2026-09-01T00:00:00.000Z",
    })).rejects.toThrow("vault asset update failed");
  });

  it("uses one deterministic audit identity for overlapping vault finalizations", async () => {
    const d1 = new RecordingD1([
      vaultRow, null, null, null,
      vaultRow, null, null, null,
    ]);
    const repo = new D1SupervisionRepository(d1);
    const finalize = (auditId: string) => repo.finalizeVaultAssetDeletion({
      tenantId: "tenant-a",
      vaultAssetId: "vault-a",
      deletedAt: "2026-09-01T00:00:00.000Z",
      audit: {
        id: auditId,
        tenantId: "tenant-a",
        actorUserId: null,
        action: "vault.deleted",
        targetKind: "vault_asset",
        targetId: "vault-a",
        occurredAt: "2026-09-01T00:00:00.000Z",
      },
    });

    await finalize("random-audit-one");
    await finalize("random-audit-two");

    const auditInserts = d1.calls.filter((call) => call.sql.includes("INSERT INTO audit_events"));
    expect(auditInserts).toHaveLength(2);
    expect(auditInserts.map((call) => call.bindings[0]))
      .toEqual(["vault-deleted-vault-a", "vault-deleted-vault-a"]);
  });

  it("commits only one deletion audit under real overlapping SQLite finalizations", async () => {
    const d1 = new SqliteD1();
    d1.database.exec(readFileSync(
      resolve(process.cwd(), "migrations/001_supervision.sql"),
      "utf8",
    ));
    d1.database.exec(`
      INSERT INTO users (id, tenant_id, email, role, created_at)
      VALUES ('employee-a', 'tenant-a', 'employee@example.test', 'employee',
              '2026-08-01T00:00:00.000Z');
      INSERT INTO customers (
        id, tenant_id, employee_user_id, customer_number, customer_alias,
        category, planned_follow_up_at, status, created_at
      ) VALUES (
        'customer-a', 'tenant-a', 'employee-a', 'C-001', '林女士', '沉香',
        '2026-08-31T00:00:00.000Z', 'pending', '2026-08-01T00:00:00.000Z'
      );
      INSERT INTO vault_assets (
        id, tenant_id, customer_id, uploaded_by_user_id, kind, vault_key,
        byte_size, uploaded_at, delete_after, version, created_at
      ) VALUES (
        'vault-a', 'tenant-a', 'customer-a', 'employee-a', 'chat_text',
        'private/vault-a', 24, '2026-08-01T00:00:00.000Z',
        '2026-08-31T00:00:00.000Z', 1, '2026-08-01T00:00:00.000Z'
      );
    `);
    const repo = new D1SupervisionRepository(d1);
    const finalize = (id: string) => repo.finalizeVaultAssetDeletion({
      tenantId: "tenant-a",
      vaultAssetId: "vault-a",
      deletedAt: "2026-09-01T00:00:00.000Z",
      audit: {
        id,
        tenantId: "tenant-a",
        actorUserId: null,
        action: "vault.deleted",
        targetKind: "vault_asset",
        targetId: "vault-a",
        occurredAt: "2026-09-01T00:00:00.000Z",
      },
    });

    const results = await Promise.allSettled([finalize("audit-one"), finalize("audit-two")]);

    expect(results.filter((result) => result.status === "fulfilled")).toHaveLength(1);
    expect(results.filter((result) => result.status === "rejected")).toHaveLength(1);
    expect(d1.database.prepare(
      `SELECT COUNT(*) AS count FROM audit_events
       WHERE action = 'vault.deleted' AND target_id = 'vault-a'`,
    ).get()).toMatchObject({ count: 1 });
    expect(d1.database.prepare(
      "SELECT deleted_at FROM vault_assets WHERE tenant_id = 'tenant-a' AND id = 'vault-a'",
    ).get()).toMatchObject({ deleted_at: "2026-09-01T00:00:00.000Z" });
  });

  it("rejects decision usage whose customer differs from its follow-up", async () => {
    const d1 = new RecordingD1([followUpRow]);
    const repo = new D1SupervisionRepository(d1);

    await expect(repo.appendDecisionUsage({
      tenantId: "tenant-a",
      customerId: "customer-b",
      followUpId: "f-1",
      employeeUserId: "employee-a",
      decisionCardId: "card-001",
      scenario: "silent_customer_followup",
      deliveryMode: "copy_only",
      providedReplyIds: ["reply-01"],
      replyUsage: "not_used",
    })).rejects.toThrow("follow-up customer mismatch");
  });

  it("D1 migration rejects a decision usage linked to another customer", () => {
    const migration = readFileSync(
      resolve(process.cwd(), "migrations/001_supervision.sql"),
      "utf8",
    );
    const mismatchedFixture = `
      INSERT INTO users (id, tenant_id, email, role, created_at)
      VALUES ('u-1', 'tenant-a', 'u1@example.test', 'employee', '2026-08-30T00:00:00.000Z');
      INSERT INTO customers (
        id, tenant_id, employee_user_id, customer_number, customer_alias,
        category, planned_follow_up_at, status, created_at
      ) VALUES
        ('customer-a', 'tenant-a', 'u-1', 'C-001', '林女士', '沉香',
         '2026-08-31T00:00:00.000Z', 'pending', '2026-08-30T00:00:00.000Z'),
        ('customer-b', 'tenant-a', 'u-1', 'C-002', '陈女士', '沉香',
         '2026-08-31T00:00:00.000Z', 'pending', '2026-08-30T00:00:00.000Z');
      INSERT INTO vault_assets (
        id, tenant_id, customer_id, uploaded_by_user_id, kind, vault_key,
        uploaded_at, delete_after, created_at
      ) VALUES (
        'vault-f-1', 'tenant-a', 'customer-a', 'u-1', 'chat_text',
        'private/tenant-a/customer-a/vault-f-1',
        '2026-08-30T00:00:00.000Z', '2026-09-29T00:00:00.000Z',
        '2026-08-30T00:00:00.000Z'
      );
      INSERT INTO follow_ups (
        id, tenant_id, customer_id, employee_user_id,
        customer_message_vault_asset_id, customer_message_summary,
        planned_follow_up_at, result, next_action, decision_card_id,
        reply_usage, created_at
      ) VALUES (
        'f-1', 'tenant-a', 'customer-a', 'u-1', 'vault-f-1', '去身份化摘要',
        '2026-08-31T00:00:00.000Z', 'pending', '明天回访', 'card-001',
        'not_used', '2026-08-30T00:00:00.000Z'
      );
      INSERT INTO decision_usages (
        id, tenant_id, customer_id, follow_up_id, employee_user_id,
        decision_card_id, scenario, delivery_mode, provided_reply_ids_json,
        reply_usage, created_at
      ) VALUES (
        'du-1', 'tenant-a', 'customer-b', 'f-1', 'u-1', 'card-001',
        'silent_customer_followup', 'copy_only', '["reply-01"]',
        'not_used', '2026-08-30T00:00:00.000Z'
      );
    `;

    expect(() => execFileSync("sqlite3", [":memory:"], {
      input: migration + mismatchedFixture,
      stdio: "pipe",
    })).toThrow();
  });

  it("atomically upserts a nullable alert identity in one statement", async () => {
    const d1 = new RecordingD1([alertRow]);
    const repo = new D1SupervisionRepository(d1, {
      createId: () => "alert-candidate",
      now: () => "2026-08-30T01:00:00.000Z",
    });

    await expect(repo.upsertAlert({
      tenantId: "tenant-a",
      detectedByUserId: null,
      kind: "retention_failed",
      status: "open",
      resolutionNote: "第二次检测",
    })).resolves.toMatchObject({
      id: "alert-existing",
      version: 2,
      resolutionNote: "第二次检测",
    });

    expect(d1.calls).toHaveLength(1);
    expect(d1.calls[0]?.sql).toContain("INSERT INTO risk_alerts");
    expect(d1.calls[0]?.sql).toMatch(
      /ON CONFLICT \(\s*tenant_id,\s*kind,\s*COALESCE\(customer_id, ''\),\s*COALESCE\(employee_user_id, ''\)\s*\)/,
    );
    expect(d1.calls[0]?.bindings).toContain(null);
  });

  it("D1 migration gives nullable alert identities a unique key", () => {
    const migration = readFileSync(
      resolve(process.cwd(), "migrations/001_supervision.sql"),
      "utf8",
    );
    const duplicateAlerts = `
      INSERT INTO risk_alerts (
        id, tenant_id, customer_id, employee_user_id, detected_by_user_id,
        kind, status, version, created_at
      ) VALUES (
        'alert-1', 'tenant-a', NULL, NULL, NULL,
        'retention_failed', 'open', 1, '2026-08-30T00:00:00.000Z'
      );
      INSERT INTO risk_alerts (
        id, tenant_id, customer_id, employee_user_id, detected_by_user_id,
        kind, status, version, created_at
      ) VALUES (
        'alert-2', 'tenant-a', NULL, NULL, NULL,
        'retention_failed', 'open', 1, '2026-08-30T01:00:00.000Z'
      );
    `;

    expect(() => execFileSync("sqlite3", [":memory:"], {
      input: migration + duplicateAlerts,
      stdio: "pipe",
    })).toThrow();
  });

  it("binds zero instead of SQLite's unlimited negative LIMIT", async () => {
    const d1 = new RecordingD1([[]]);
    const repo = new D1SupervisionRepository(d1);

    await repo.listExpiredVaultAssets({
      tenantId: "tenant-a",
      now: "2026-09-01T00:00:00.000Z",
      limit: -1,
    });

    expect(d1.calls[0]?.bindings).toEqual([
      "tenant-a",
      "2026-09-01T00:00:00.000Z",
      0,
    ]);
  });

  it("caps oversized expired-vault batches at five hundred", async () => {
    const d1 = new RecordingD1([[]]);
    const repo = new D1SupervisionRepository(d1);

    await repo.listExpiredVaultAssets({
      tenantId: "tenant-a",
      now: "2026-09-01T00:00:00.000Z",
      limit: 999,
    });

    expect(d1.calls[0]?.bindings).toEqual([
      "tenant-a",
      "2026-09-01T00:00:00.000Z",
      500,
    ]);
  });
});
