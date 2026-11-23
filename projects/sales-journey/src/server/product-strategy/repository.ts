import { SalesStrategyInputV1Schema, type SalesStrategyInputV1 } from "@/domain/product-strategy/input-schema";

export type ProductStrategyReceipt = Readonly<{
  tenantId: string;
  outboxEventId: string;
  payloadSha256: string;
  sourceEventId: string;
  productCode: string;
  strategyInputVersion: number;
  productDataVersion: number;
  contentSha256: string;
  status: "preview_only";
  receivedAt: string;
  strategyInput: SalesStrategyInputV1;
}>;

export type ReceiptOutcome = "created" | "duplicate";

export class ProductStrategyEventConflictError extends Error {
  constructor() {
    super("Outbox event id was reused with different content.");
    this.name = "ProductStrategyEventConflictError";
  }
}

export interface ProductStrategyReceiptRepository {
  accept(
    receipt: ProductStrategyReceipt,
  ): Promise<Readonly<{ outcome: ReceiptOutcome; record: ProductStrategyReceipt }>>;
  find(tenantId: string, outboxEventId: string): Promise<ProductStrategyReceipt | undefined>;
  findLatest(tenantId: string, productCode: string): Promise<ProductStrategyReceipt | undefined>;
  listLatest(tenantId: string, limit: number): Promise<ProductStrategyReceipt[]>;
}

export class MemoryProductStrategyReceiptRepository
implements ProductStrategyReceiptRepository {
  private readonly records = new Map<string, ProductStrategyReceipt>();

  async accept(
    receipt: ProductStrategyReceipt,
  ): Promise<Readonly<{ outcome: ReceiptOutcome; record: ProductStrategyReceipt }>> {
    const key = `${receipt.tenantId}\0${receipt.outboxEventId}`;
    const existing = this.records.get(key);
    if (existing) {
      if (existing.payloadSha256 !== receipt.payloadSha256) {
        throw new ProductStrategyEventConflictError();
      }
      return { outcome: "duplicate", record: existing };
    }
    this.records.set(key, receipt);
    return { outcome: "created", record: receipt };
  }

  async find(
    tenantId: string,
    outboxEventId: string,
  ): Promise<ProductStrategyReceipt | undefined> {
    return this.records.get(`${tenantId}\0${outboxEventId}`);
  }

  async findLatest(tenantId: string, productCode: string): Promise<ProductStrategyReceipt | undefined> {
    return [...this.records.values()]
      .filter((record) => record.tenantId === tenantId && record.productCode === productCode)
      .sort((left, right) => right.strategyInputVersion - left.strategyInputVersion)[0];
  }

  async listLatest(tenantId: string, limit: number): Promise<ProductStrategyReceipt[]> {
    const latest = new Map<string, ProductStrategyReceipt>();
    for (const record of this.records.values()) {
      if (record.tenantId !== tenantId) continue;
      const current = latest.get(record.productCode);
      if (!current || record.strategyInputVersion > current.strategyInputVersion) {
        latest.set(record.productCode, record);
      }
    }
    return [...latest.values()]
      .sort((left, right) => left.productCode.localeCompare(right.productCode))
      .slice(0, limit);
  }
}

type D1ReceiptRow = Readonly<Record<string, unknown>>;

function requiredString(row: D1ReceiptRow, key: string): string {
  const value = row[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new Error("Invalid product strategy receipt row.");
  }
  return value;
}

function requiredInteger(row: D1ReceiptRow, key: string): number {
  const value = row[key];
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new Error("Invalid product strategy receipt row.");
  }
  return value;
}

function mapD1Receipt(row: D1ReceiptRow): ProductStrategyReceipt {
  const status = requiredString(row, "status");
  if (status !== "preview_only") {
    throw new Error("Invalid product strategy receipt row.");
  }
  return {
    tenantId: requiredString(row, "tenant_id"),
    outboxEventId: requiredString(row, "outbox_event_id"),
    payloadSha256: requiredString(row, "payload_sha256"),
    sourceEventId: requiredString(row, "source_event_id"),
    productCode: requiredString(row, "product_code"),
    strategyInputVersion: requiredInteger(row, "strategy_input_version"),
    productDataVersion: requiredInteger(row, "product_data_version"),
    contentSha256: requiredString(row, "content_sha256"),
    status,
    receivedAt: requiredString(row, "received_at"),
    strategyInput: SalesStrategyInputV1Schema.parse(
      JSON.parse(requiredString(row, "strategy_input_json")),
    ),
  };
}

export class D1ProductStrategyReceiptRepository
implements ProductStrategyReceiptRepository {
  constructor(private readonly d1: D1DatabaseLike) {}

  async accept(
    receipt: ProductStrategyReceipt,
  ): Promise<Readonly<{ outcome: ReceiptOutcome; record: ProductStrategyReceipt }>> {
    const existing = await this.find(receipt.tenantId, receipt.outboxEventId);
    if (existing) return this.replayOutcome(existing, receipt.payloadSha256);
    try {
      const result = await this.d1.prepare(
        `INSERT INTO product_strategy_receipts (
          tenant_id, outbox_event_id, payload_sha256, source_event_id,
          product_code, strategy_input_version, product_data_version,
          content_sha256, status, received_at, strategy_input_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      ).bind(
        receipt.tenantId,
        receipt.outboxEventId,
        receipt.payloadSha256,
        receipt.sourceEventId,
        receipt.productCode,
        receipt.strategyInputVersion,
        receipt.productDataVersion,
        receipt.contentSha256,
        receipt.status,
        receipt.receivedAt,
        JSON.stringify(receipt.strategyInput),
      ).run();
      if (!result.success) throw new Error("D1 product strategy receipt write failed.");
      return { outcome: "created", record: receipt };
    } catch (writeError) {
      const raced = await this.find(receipt.tenantId, receipt.outboxEventId);
      if (raced) return this.replayOutcome(raced, receipt.payloadSha256);
      throw writeError;
    }
  }

  async find(
    tenantId: string,
    outboxEventId: string,
  ): Promise<ProductStrategyReceipt | undefined> {
    const row = await this.d1.prepare(
      `SELECT tenant_id, outbox_event_id, payload_sha256, source_event_id,
        product_code, strategy_input_version, product_data_version,
        content_sha256, status, received_at, strategy_input_json
       FROM product_strategy_receipts
       WHERE tenant_id = ? AND outbox_event_id = ?
       LIMIT 1`,
    ).bind(tenantId, outboxEventId).first<D1ReceiptRow>();
    return row ? mapD1Receipt(row) : undefined;
  }

  async findLatest(
    tenantId: string,
    productCode: string,
  ): Promise<ProductStrategyReceipt | undefined> {
    const row = await this.d1.prepare(
      `SELECT tenant_id, outbox_event_id, payload_sha256, source_event_id,
        product_code, strategy_input_version, product_data_version,
        content_sha256, status, received_at, strategy_input_json
       FROM product_strategy_receipts
       WHERE tenant_id = ? AND product_code = ?
       ORDER BY strategy_input_version DESC
       LIMIT 1`,
    ).bind(tenantId, productCode).first<D1ReceiptRow>();
    return row ? mapD1Receipt(row) : undefined;
  }

  async listLatest(tenantId: string, limit: number): Promise<ProductStrategyReceipt[]> {
    const rows = await this.d1.prepare(
      `SELECT tenant_id, outbox_event_id, payload_sha256, source_event_id,
        product_code, strategy_input_version, product_data_version,
        content_sha256, status, received_at, strategy_input_json
       FROM product_strategy_receipts
       WHERE tenant_id = ?
       ORDER BY product_code ASC, strategy_input_version DESC`,
    ).bind(tenantId).all<D1ReceiptRow>();
    const latest = new Map<string, ProductStrategyReceipt>();
    for (const row of rows.results) {
      const record = mapD1Receipt(row);
      if (!latest.has(record.productCode)) latest.set(record.productCode, record);
      if (latest.size >= limit) break;
    }
    return [...latest.values()];
  }

  private replayOutcome(
    existing: ProductStrategyReceipt,
    payloadSha256: string,
  ): Readonly<{ outcome: ReceiptOutcome; record: ProductStrategyReceipt }> {
    if (existing.payloadSha256 !== payloadSha256) {
      throw new ProductStrategyEventConflictError();
    }
    return { outcome: "duplicate", record: existing };
  }
}
import type { D1DatabaseLike } from "@/server/supervision/d1-repository";
