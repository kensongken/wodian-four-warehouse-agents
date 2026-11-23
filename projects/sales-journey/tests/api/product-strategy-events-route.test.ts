import { createHash, createHmac } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { POST } from "@/app/api/integrations/product-image-engine/events/route";
import {
  MemoryProductStrategyReceiptRepository,
} from "@/server/product-strategy/repository";
import { configureProductStrategyRuntime } from "@/server/product-strategy/runtime";
import type { ProductStrategyInputFetcher } from "@/server/product-strategy/fetcher";
import { SalesStrategyInputV1Schema } from "@/domain/product-strategy/input-schema";

const secret = "synthetic-sales-delivery-secret-with-32-bytes";
const tenantId = "22222222-2222-4222-8222-222222222222";
const outboxEventId = "66666666-6666-4666-8666-666666666666";
const nowSeconds = 1_788_112_000;
const path = "/api/integrations/product-image-engine/events";
const strategyInput = SalesStrategyInputV1Schema.parse(JSON.parse(readFileSync(resolve(
  process.cwd(),
  "fixtures/integrations/sales-strategy-input.synthetic.json",
), "utf8")));
const threeStageStrategyInput = SalesStrategyInputV1Schema.parse(JSON.parse(readFileSync(resolve(
  process.cwd(),
  "fixtures/integrations/sales-strategy-input.three-stage.synthetic.json",
), "utf8")));

const event = Object.freeze({
  schema_version: 1,
  event_id: "55555555-5555-4555-8555-555555555555",
  occurred_at: "2026-08-31T08:00:00Z",
  tenant_id: tenantId,
  product_code: "SYNTHETIC-001",
  strategy_input_version: 1,
  product_data_version: 1,
  status: "preview_only",
  content_sha256: "cc6dd3ee6d2647b155087973b20b839b052c0c146daa1d9ba4fde67d45aa5138",
  automatic_customer_contact_allowed: false,
  automatic_publication_allowed: false,
  automatic_wechat_send_allowed: false,
  automatic_quote_allowed: false,
});

let repository: MemoryProductStrategyReceiptRepository;
let fetchCalls: unknown[][];
let fetcher: ProductStrategyInputFetcher;
let restoreRuntime: (() => void) | undefined;

function signature(body: string, timestamp = nowSeconds): string {
  const bodySha256 = createHash("sha256").update(body).digest("hex");
  const message = [
    timestamp,
    "product-image-engine",
    tenantId,
    "POST",
    path,
    bodySha256,
  ].join("\n");
  return createHmac("sha256", secret).update(message).digest("hex");
}

function request(
  payload: unknown = event,
  overrides: Readonly<Record<string, string>> = {},
): Request {
  const body = JSON.stringify(payload);
  const timestamp = overrides["X-Signature-Timestamp"] ?? String(nowSeconds);
  return new Request(`http://localhost${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Source-System": "product-image-engine",
      "X-Tenant-Id": tenantId,
      "X-Event-Topic": "sales.strategy.input.updated.v1",
      "X-Outbox-Event-Id": outboxEventId,
      "X-Signature-Timestamp": timestamp,
      "X-Signature": signature(body, Number(timestamp)),
      ...overrides,
    },
    body,
  });
}

beforeEach(() => {
  repository = new MemoryProductStrategyReceiptRepository();
  fetchCalls = [];
  fetcher = {
    fetch: async (...args) => {
      fetchCalls.push(args);
      return strategyInput;
    },
  };
  restoreRuntime = configureProductStrategyRuntime(() => ({
    repository,
    fetcher,
    signingSecret: secret,
    nowSeconds: () => nowSeconds,
  }));
});

afterEach(() => {
  restoreRuntime?.();
  restoreRuntime = undefined;
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

it("fails closed without recording when the fetched snapshot does not match the notice", async () => {
  fetcher = { fetch: async () => ({ ...strategyInput, product_data_version: 2 }) };

  const response = await POST(request());

  expect(response.status).toBe(409);
  expect(await response.json()).toEqual({ error: "STRATEGY_INPUT_IDENTITY_MISMATCH" });
  expect(await repository.find(tenantId, outboxEventId)).toBeUndefined();
});

it("asks the sender to retry when the full snapshot source is temporarily unavailable", async () => {
  const { ProductStrategyFetchError } = await import("@/server/product-strategy/fetcher");
  fetcher = {
    fetch: async () => {
      throw new ProductStrategyFetchError("unavailable", "synthetic outage");
    },
  };

  const response = await POST(request());

  expect(response.status).toBe(503);
  expect(await response.json()).toEqual({ error: "STRATEGY_INPUT_SOURCE_UNAVAILABLE" });
  expect(await repository.find(tenantId, outboxEventId)).toBeUndefined();
});

it("allows an explicitly desensitized local receiver but never production fallback", async () => {
  restoreRuntime?.();
  restoreRuntime = undefined;
  vi.stubEnv("NODE_ENV", "development");
  vi.stubEnv("PRODUCT_STRATEGY_LOCAL_SYNTHETIC_ACCEPTANCE", "desensitized-only");
  vi.stubEnv("PRODUCT_STRATEGY_SIGNING_SECRET", secret);
  vi.stubEnv("PRODUCT_IMAGE_ENGINE_BASE_URL", "http://synthetic-product-image-engine.local");
  vi.stubGlobal("fetch", vi.fn(async () => Response.json(strategyInput)));
  const localTimestamp = Math.floor(Date.now() / 1_000);

  const local = await POST(request(event, {
    "X-Signature-Timestamp": String(localTimestamp),
  }));
  vi.stubEnv("NODE_ENV", "production");
  const production = await POST(request({ ...event, product_data_version: 2 }));

  expect(local.status).toBe(202);
  expect(production.status).toBe(503);
  expect(await production.json()).toEqual({
    error: "PRODUCT_STRATEGY_RUNTIME_UNAVAILABLE",
  });
});

it("accepts a signed preview notification and records its product identity", async () => {
  const response = await POST(request());

  expect(response.status).toBe(202);
  expect(await response.json()).toEqual({
    accepted: true,
    outcome: "created",
    outboxEventId,
    tenantId,
    productCode: "SYNTHETIC-001",
    strategyInputVersion: 1,
    status: "preview_only",
  });
  expect(await repository.find(tenantId, outboxEventId)).toMatchObject({
    tenantId,
    outboxEventId,
    productCode: "SYNTHETIC-001",
    status: "preview_only",
    strategyInput,
  });
  expect(fetchCalls).toEqual([[tenantId, "SYNTHETIC-001", 1]]);
});

it("stores the snapshot derived from the pinned album candidate after its matching notice", async () => {
  const notification = JSON.parse(readFileSync(resolve(
    process.cwd(),
    "fixtures/integrations/sales-strategy-input-updated.three-stage.synthetic.json",
  ), "utf8"));
  fetcher = { fetch: async () => threeStageStrategyInput };

  const response = await POST(request(notification));

  expect(response.status).toBe(202);
  expect(await repository.find(tenantId, outboxEventId)).toMatchObject({
    sourceEventId: notification.event_id,
    contentSha256: threeStageStrategyInput.content_sha256,
    strategyInput: {
      source_album_event_id: "fa2ee0c2-d145-5e1b-b304-91a731e145c5",
      images: [{ sha256: "a".repeat(64) }],
    },
  });
});

it("returns duplicate success for an exact outbox replay", async () => {
  expect((await POST(request())).status).toBe(202);

  const replay = await POST(request());

  expect(replay.status).toBe(200);
  expect((await replay.json()).outcome).toBe("duplicate");
  expect(fetchCalls).toHaveLength(1);
});

it("rejects an outbox id reused with different content", async () => {
  expect((await POST(request())).status).toBe(202);

  const conflict = await POST(request({ ...event, product_data_version: 2 }));

  expect(conflict.status).toBe(409);
  expect(await conflict.json()).toEqual({ error: "EVENT_IDENTITY_CONFLICT" });
});

it("rejects bad signatures before storing anything", async () => {
  const response = await POST(request(event, { "X-Signature": "0".repeat(64) }));

  expect(response.status).toBe(401);
  expect(await response.json()).toEqual({ error: "INVALID_SOURCE_SIGNATURE" });
  expect(await repository.find(tenantId, outboxEventId)).toBeUndefined();
});

it("rejects sales_ready and automatic actions", async () => {
  const response = await POST(request({
    ...event,
    status: "sales_ready",
    automatic_quote_allowed: true,
  }));

  expect(response.status).toBe(422);
  expect(await response.json()).toEqual({ error: "INVALID_STRATEGY_NOTIFICATION" });
});

it("rejects mismatched tenant scope and unsupported topics", async () => {
  const tenantMismatch = await POST(request({
    ...event,
    tenant_id: "33333333-3333-4333-8333-333333333333",
  }));
  const topicMismatch = await POST(request(event, {
    "X-Event-Topic": "product.materials.updated.v1",
  }));

  expect(tenantMismatch.status).toBe(403);
  expect(await tenantMismatch.json()).toEqual({ error: "TENANT_SCOPE_MISMATCH" });
  expect(topicMismatch.status).toBe(422);
  expect(await topicMismatch.json()).toEqual({ error: "UNSUPPORTED_EVENT_TOPIC" });
});

it("fails closed when the product strategy secret is reused by another service", async () => {
  restoreRuntime?.();
  restoreRuntime = configureProductStrategyRuntime(() => ({
    repository,
    fetcher,
    signingSecret: secret,
    disallowedSecrets: [secret],
    nowSeconds: () => nowSeconds,
  }));

  const response = await POST(request());

  expect(response.status).toBe(503);
  expect(await response.json()).toEqual({
    error: "PRODUCT_STRATEGY_RUNTIME_UNAVAILABLE",
  });
});
