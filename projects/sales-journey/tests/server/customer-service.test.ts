import { describe, expect, it } from "vitest";

import { calculateVaultDeleteAfter, type AuditEvent } from "@/domain/supervision/contracts";
import type { CurrentUser } from "@/server/auth/current-user";
import {
  ProductStrategyFreshnessUnavailableError,
  StaleProductStrategyError,
  type ProductStrategyFreshnessGuard,
} from "@/server/product-strategy/freshness";
import {
  createCustomerService,
  CustomerNotFoundError,
  type IdempotencyKeyring,
  type PrivateTextVault,
} from "@/server/supervision/customer-service";
import { MemorySupervisionRepository } from "@/server/supervision/memory-repository";
import type {
  AppendFollowUpBundleInput,
  AppendFollowUpBundleResult,
  CreateCustomerBundleInput,
  CreateCustomerBundleResult,
} from "@/server/supervision/repository";
import { safeCard } from "../support/decision-card-fixtures";

const NOW = "2026-08-30T08:00:00.000Z";
const FUTURE = "2026-08-31T08:00:00.000Z";
const TOKEN_SECRET = "test-only-usage-token-secret-with-32-bytes";
const ROTATED_TOKEN_SECRET = "rotated-usage-token-secret-with-32-bytes";
const IDEMPOTENCY_SECRET = "stable-idempotency-secret-v1-with-32-bytes";
const ROTATED_IDEMPOTENCY_SECRET = "stable-idempotency-secret-v2-with-32-bytes";
const IDEMPOTENCY_KEYRING = [{ version: "v1", secret: IDEMPOTENCY_SECRET }] as const;

function decodeUsagePayload(token: string): Record<string, unknown> {
  const [encodedPayload] = token.split(".");
  if (!encodedPayload) throw new Error("test token has no payload");
  return JSON.parse(Buffer.from(encodedPayload, "base64url").toString("utf8")) as Record<
    string,
    unknown
  >;
}

async function signedUsageTokenWith(
  token: string,
  overrides: Record<string, unknown>,
): Promise<string> {
  const encodedPayload = Buffer.from(JSON.stringify({
    ...decodeUsagePayload(token),
    ...overrides,
  })).toString("base64url");
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(TOKEN_SECRET),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(encodedPayload),
  );
  return `${encodedPayload}.${Buffer.from(signature).toString("base64url")}`;
}

const employeeOne: CurrentUser = {
  accountUserId: "employee-1",
  email: "employee-1@shop.test",
  tenantId: "tenant-a",
  role: "employee",
};

const employeeTwo: CurrentUser = {
  accountUserId: "employee-2",
  email: "employee-2@shop.test",
  tenantId: "tenant-a",
  role: "employee",
};

const owner: CurrentUser = {
  accountUserId: "owner-1",
  email: "owner@shop.test",
  tenantId: "tenant-a",
  role: "owner",
};

class RecordingTextVault implements PrivateTextVault {
  readonly writes: Array<{ vaultKey: string; content: string; deleteAfter: string }> = [];
  readonly deletedKeys: string[] = [];
  readonly storedKeys = new Set<string>();

  async putText(input: { vaultKey: string; content: string; deleteAfter: string }): Promise<void> {
    this.writes.push(input);
    this.storedKeys.add(input.vaultKey);
  }

  async deleteText(vaultKey: string): Promise<void> {
    this.deletedKeys.push(vaultKey);
    this.storedKeys.delete(vaultKey);
  }
}

class CustomerCommitThenThrowRepository extends MemorySupervisionRepository {
  override async createCustomerBundle(
    input: CreateCustomerBundleInput,
  ): Promise<CreateCustomerBundleResult> {
    await super.createCustomerBundle(input);
    throw new Error("customer commit acknowledgement lost");
  }
}

class FollowUpCommitThenThrowRepository extends MemorySupervisionRepository {
  override async appendFollowUpBundle(
    input: AppendFollowUpBundleInput,
  ): Promise<AppendFollowUpBundleResult> {
    await super.appendFollowUpBundle(input);
    throw new Error("follow-up commit acknowledgement lost");
  }
}

class ConcurrentCustomerWinnerRepository extends MemorySupervisionRepository {
  override async createCustomerBundle(
    input: CreateCustomerBundleInput,
  ): Promise<CreateCustomerBundleResult> {
    if (!input.customer.id || !input.audit.id || !input.vaultAsset?.id) {
      throw new Error("test requires explicit candidate ids");
    }
    const winnerCustomerId = `${input.customer.id}-winner`;
    await super.createCustomerBundle({
      customer: {
        ...input.customer,
        id: winnerCustomerId,
        customerNumber: `${input.customer.customerNumber}-WINNER`,
      },
      vaultAsset: {
        ...input.vaultAsset,
        id: `${input.vaultAsset.id}-winner`,
        customerId: winnerCustomerId,
        vaultKey: `${input.vaultAsset.vaultKey}-winner`,
      },
      audit: {
        ...input.audit,
        id: `${input.audit.id}-winner`,
        targetId: winnerCustomerId,
      },
    });
    throw new Error("concurrent customer won");
  }
}

class ConcurrentFollowUpWinnerRepository extends MemorySupervisionRepository {
  override async appendFollowUpBundle(
    input: AppendFollowUpBundleInput,
  ): Promise<AppendFollowUpBundleResult> {
    if (!input.followUp.id || !input.decisionUsage.id || !input.audit.id || !input.vaultAsset.id) {
      throw new Error("test requires explicit candidate ids");
    }
    const winnerFollowUpId = `${input.followUp.id}-winner`;
    const winnerVaultId = `${input.vaultAsset.id}-winner`;
    await super.appendFollowUpBundle({
      followUp: {
        ...input.followUp,
        id: winnerFollowUpId,
        customerMessageVaultAssetId: winnerVaultId,
      },
      decisionUsage: {
        ...input.decisionUsage,
        id: `${input.decisionUsage.id}-winner`,
        followUpId: winnerFollowUpId,
      },
      vaultAsset: {
        ...input.vaultAsset,
        id: winnerVaultId,
        vaultKey: `${input.vaultAsset.vaultKey}-winner`,
      },
      audit: {
        ...input.audit,
        id: `${input.audit.id}-winner`,
        targetId: winnerFollowUpId,
      },
    });
    throw new Error("concurrent follow-up won");
  }
}

function createService(
  repository = new MemorySupervisionRepository(),
  usageTokenSecret = TOKEN_SECRET,
  idempotencyKeyring: IdempotencyKeyring = IDEMPOTENCY_KEYRING,
  productStrategyFreshnessGuard?: ProductStrategyFreshnessGuard,
) {
  const vault = new RecordingTextVault();
  const service = createCustomerService({
    repository,
    vault,
    usageTokenSecret,
    idempotencyKeyring,
    productStrategyFreshnessGuard,
    now: () => NOW,
  });
  return { repository, service, vault };
}

describe("employee customer service", () => {
  it("creates a customer and audit with server-derived employee identity idempotently", async () => {
    const { repository, service } = createService();
    const command = {
      requestId: "request-create-customer-1",
      customerAlias: "林女士",
      category: "沉香" as const,
      plannedFollowUpAt: FUTURE,
    };

    const first = await service.createCustomer(employeeOne, command);
    const retried = await service.createCustomer(employeeOne, command);

    expect(first.customer).toMatchObject({
      tenantId: "tenant-a",
      employeeUserId: "employee-1",
      customerAlias: "林女士",
    });
    expect(first.audit).toMatchObject({
      action: "customer.created",
      actorUserId: "employee-1",
      targetId: first.customer.id,
    });
    expect(retried.customer.id).toBe(first.customer.id);
    await expect(repository.listEmployeeCustomers({
      tenantId: "tenant-a",
      employeeUserId: "employee-1",
    })).resolves.toHaveLength(1);
  });

  it("keeps a committed request idempotent when the short-lived usage-token secret rotates", async () => {
    const repository = new MemorySupervisionRepository();
    const command = {
      requestId: "request-survives-token-rotation",
      customerAlias: "令牌轮换客户",
      category: "沉香" as const,
      plannedFollowUpAt: FUTURE,
    };
    const beforeRotation = createService(repository, TOKEN_SECRET).service;
    const afterRotation = createService(repository, ROTATED_TOKEN_SECRET).service;

    const created = await beforeRotation.createCustomer(employeeOne, command);
    const retried = await afterRotation.createCustomer(employeeOne, command);

    expect(retried.customer.id).toBe(created.customer.id);
    await expect(repository.listEmployeeCustomers({
      tenantId: employeeOne.tenantId,
      employeeUserId: employeeOne.accountUserId,
    })).resolves.toHaveLength(1);
  });

  it("returns a committed follow-up retry after token rotation without revalidating the old token", async () => {
    const repository = new MemorySupervisionRepository();
    const beforeRotation = createService(repository, TOKEN_SECRET).service;
    const afterRotation = createService(repository, ROTATED_TOKEN_SECRET).service;
    const { customer } = await beforeRotation.createCustomer(employeeOne, {
      requestId: "customer-before-follow-up-token-rotation",
      customerAlias: "跟进重试客户",
      category: "沉香",
      plannedFollowUpAt: FUTURE,
    });
    const usage = await beforeRotation.issueDecisionUsageToken(employeeOne, customer.id, safeCard);
    const command = {
      requestId: "follow-up-survives-token-rotation",
      usageToken: usage.token,
      customerMessage: "仍按原请求重试。",
      plannedFollowUpAt: FUTURE,
      result: "pending" as const,
      nextAction: "稍后联系",
      decisionCardId: safeCard.cardId,
      cardInstanceId: usage.cardInstanceId,
      cardDigest: usage.cardDigest,
      replyUsage: "not_used" as const,
    };

    const created = await beforeRotation.appendFollowUp(employeeOne, customer.id, command);
    const retried = await afterRotation.appendFollowUp(employeeOne, customer.id, command);

    expect(retried.followUp.id).toBe(created.followUp.id);
    await expect(repository.listTimeline({
      tenantId: employeeOne.tenantId,
      customerId: customer.id,
    })).resolves.toHaveLength(2);
  });

  it("finds an older idempotency version while a new version signs fresh requests", async () => {
    const repository = new MemorySupervisionRepository();
    const command = {
      requestId: "request-survives-idempotency-version-rollover",
      customerAlias: "幂等版本客户",
      category: "沉香" as const,
      plannedFollowUpAt: FUTURE,
    };
    const v1Service = createService(repository).service;
    const v2WithV1Fallback = createService(repository, TOKEN_SECRET, [{
      version: "v2",
      secret: ROTATED_IDEMPOTENCY_SECRET,
    }, ...IDEMPOTENCY_KEYRING]).service;

    const created = await v1Service.createCustomer(employeeOne, command);
    const retried = await v2WithV1Fallback.createCustomer(employeeOne, command);

    expect(retried.customer.id).toBe(created.customer.id);
    await expect(repository.listEmployeeCustomers({
      tenantId: employeeOne.tenantId,
      employeeUserId: employeeOne.accountUserId,
    })).resolves.toHaveLength(1);
  });

  it("uses random record ids instead of deriving identifiers from a client request id", async () => {
    const command = {
      requestId: "林敏-13800138000",
      customerAlias: "林女士",
      category: "沉香" as const,
      plannedFollowUpAt: FUTURE,
    };
    const first = await createService().service.createCustomer(employeeOne, command);
    const second = await createService().service.createCustomer(employeeOne, command);

    expect(second.customer.id).not.toBe(first.customer.id);
    expect(second.audit.id).not.toBe(first.audit.id);
    expect(JSON.stringify({ first, second })).not.toContain("林敏-13800138000");
  });

  it("keeps a committed customer identity vault object when commit acknowledgement is lost", async () => {
    const repository = new CustomerCommitThenThrowRepository();
    const { service, vault } = createService(repository);

    const result = await service.createCustomer(employeeOne, {
      requestId: "customer-commit-then-throw",
      customerAlias: "钱女士",
      realName: "钱敏",
      category: "沉香",
      plannedFollowUpAt: FUTURE,
    });

    expect(result.vaultAsset).toBeDefined();
    expect(vault.writes.map((write) => write.vaultKey)).toContain(result.vaultAsset!.vaultKey);
    expect(vault.storedKeys.has(result.vaultAsset!.vaultKey)).toBe(true);
    expect(vault.deletedKeys).not.toContain(result.vaultAsset!.vaultKey);
  });

  it("deletes only the losing customer vault candidate after a concurrent winner", async () => {
    const repository = new ConcurrentCustomerWinnerRepository();
    const { service, vault } = createService(repository);

    const result = await service.createCustomer(employeeOne, {
      requestId: "customer-concurrent-winner",
      customerAlias: "冯女士",
      realName: "冯敏",
      category: "沉香",
      plannedFollowUpAt: FUTURE,
    });
    const candidateVaultKey = vault.writes[0]!.vaultKey;

    expect(result.vaultAsset!.vaultKey).not.toBe(candidateVaultKey);
    expect(vault.deletedKeys).toContain(candidateVaultKey);
    expect(vault.deletedKeys).not.toContain(result.vaultAsset!.vaultKey);
  });

  it("does not leave a customer behind when its matching audit cannot be committed", async () => {
    const duplicateAudit: AuditEvent = {
      id: "audit-duplicate",
      tenantId: "tenant-a",
      actorUserId: "employee-1",
      action: "existing.event",
      targetKind: "customer",
      targetId: "existing-customer",
      occurredAt: NOW,
      version: 1,
      createdAt: NOW,
    };
    const repository = new MemorySupervisionRepository({ seed: { audits: [duplicateAudit] } });

    await expect(repository.createCustomerBundle({
      customer: {
        id: "customer-new",
        tenantId: "tenant-a",
        employeeUserId: "employee-1",
        customerNumber: "C-NEW",
        customerAlias: "周女士",
        category: "沉香",
        plannedFollowUpAt: FUTURE,
        status: "pending",
        createdAt: NOW,
      },
      audit: {
        id: "audit-duplicate",
        tenantId: "tenant-a",
        actorUserId: "employee-1",
        action: "customer.created",
        targetKind: "customer",
        targetId: "customer-new",
        occurredAt: NOW,
        createdAt: NOW,
      },
    })).rejects.toThrow("audit already exists");
    await expect(repository.getCustomer({
      tenantId: "tenant-a",
      customerId: "customer-new",
    })).resolves.toBeUndefined();
  });

  it("keeps raw customer text only in the private vault and records a safe intent summary", async () => {
    const { repository, service, vault } = createService();
    const { customer } = await service.createCustomer(employeeOne, {
      requestId: "request-create-customer-2",
      customerAlias: "陈女士",
      category: "沉香",
      plannedFollowUpAt: FUTURE,
    });
    const usage = await service.issueDecisionUsageToken(employeeOne, customer.id, safeCard);

    const result = await service.appendFollowUp(employeeOne, customer.id, {
      requestId: "陈敏-13800138000",
      usageToken: usage.token,
      customerMessage: "我叫陈敏，预算三万元，明天下午有空。",
      recentContext: "微信里还提到了孩子的学校。",
      plannedFollowUpAt: FUTURE,
      actualFollowUpAt: NOW,
      result: "interested",
      nextAction: "确认到店时间",
      decisionCardId: safeCard.cardId,
      replyUsage: "adopted",
      chosenReplyId: safeCard.replyOptions[0]!.id,
    });

    expect(vault.writes).toHaveLength(1);
    expect(vault.writes[0]).toMatchObject({
      content: expect.stringContaining("我叫陈敏"),
      deleteAfter: calculateVaultDeleteAfter(NOW),
    });
    expect(result.followUp.customerMessageVaultAssetId).toBe(result.vaultAsset.id);
    expect(result.followUp.customerMessageSummary).toBe("品类：沉香；意图：learn_identity。");
    expect(result.decisionUsage).toMatchObject({
      safetyStatus: safeCard.safety.status,
      safetyRuleIds: safeCard.safety.rules,
    });
    expect(JSON.stringify(result)).not.toContain("我叫陈敏");
    expect(JSON.stringify(result)).not.toContain("陈敏");
    expect(JSON.stringify(result)).not.toContain("孩子的学校");
    expect(JSON.stringify(result)).not.toContain("13800138000");

    const timeline = await repository.listTimeline({
      tenantId: "tenant-a",
      customerId: customer.id,
    });
    expect(timeline.map((entry) => entry.kind)).toEqual(["follow_up", "decision_usage"]);
    expect(JSON.stringify(timeline)).not.toContain("我叫陈敏");
    expect(JSON.stringify(timeline)).not.toContain("陈敏");
    expect(JSON.stringify(timeline)).not.toContain("孩子的学校");
    expect(JSON.stringify(timeline)).not.toContain("13800138000");
  });

  it("rejects a future actual-follow-up time before vault or repository writes", async () => {
    const { repository, service, vault } = createService();
    const { customer } = await service.createCustomer(employeeOne, {
      requestId: "customer-for-future-actual",
      customerAlias: "张女士",
      category: "沉香",
      plannedFollowUpAt: FUTURE,
    });
    const usage = await service.issueDecisionUsageToken(employeeOne, customer.id, safeCard);

    await expect(service.appendFollowUp(employeeOne, customer.id, {
      requestId: "follow-up-with-future-actual",
      usageToken: usage.token,
      customerMessage: "稍后再联系。",
      plannedFollowUpAt: FUTURE,
      actualFollowUpAt: "2026-08-30T08:00:00.001Z",
      result: "pending",
      nextAction: "两天后回访",
      decisionCardId: safeCard.cardId,
      replyUsage: "not_used",
    })).rejects.toMatchObject({
      code: "FUTURE_ACTUAL_FOLLOW_UP",
      status: 400,
    });

    expect(vault.writes).toHaveLength(0);
    await expect(repository.listTimeline({
      tenantId: "tenant-a",
      customerId: customer.id,
    })).resolves.toEqual([]);
  });

  it("uses a separate stable customer fingerprint for duplicate review signals", async () => {
    const { service } = createService();

    const first = await service.createCustomer(employeeOne, {
      requestId: "duplicate-review-a",
      customerAlias: "同一客户",
      category: "沉香",
      plannedFollowUpAt: FUTURE,
    });
    const second = await service.createCustomer(employeeTwo, {
      requestId: "duplicate-review-b",
      customerAlias: "同一客户",
      category: "绿松石",
      plannedFollowUpAt: "2026-09-01T08:00:00.000Z",
    });

    expect(first.customer.customerFingerprint).toBe(second.customer.customerFingerprint);
    expect(first.customer.requestFingerprint).not.toBe(second.customer.requestFingerprint);
  });

  it("keeps a committed follow-up vault object when commit acknowledgement is lost", async () => {
    const repository = new FollowUpCommitThenThrowRepository();
    const { service, vault } = createService(repository);
    const { customer } = await service.createCustomer(employeeOne, {
      requestId: "customer-for-follow-up-commit",
      customerAlias: "郑女士",
      category: "沉香",
      plannedFollowUpAt: FUTURE,
    });
    const usage = await service.issueDecisionUsageToken(employeeOne, customer.id, safeCard);

    const result = await service.appendFollowUp(employeeOne, customer.id, {
      requestId: "follow-up-commit-then-throw",
      usageToken: usage.token,
      cardInstanceId: usage.cardInstanceId,
      customerMessage: "稍后再联系。",
      plannedFollowUpAt: FUTURE,
      result: "pending",
      nextAction: "两天后回访",
      decisionCardId: safeCard.cardId,
      replyUsage: "not_used",
    });

    expect(vault.writes.map((write) => write.vaultKey)).toContain(result.vaultAsset.vaultKey);
    expect(vault.storedKeys.has(result.vaultAsset.vaultKey)).toBe(true);
    expect(vault.deletedKeys).not.toContain(result.vaultAsset.vaultKey);
  });

  it("deletes only the losing follow-up vault candidate after a concurrent winner", async () => {
    const repository = new ConcurrentFollowUpWinnerRepository();
    const { service, vault } = createService(repository);
    const { customer } = await service.createCustomer(employeeOne, {
      requestId: "customer-for-concurrent-follow-up",
      customerAlias: "蒋女士",
      category: "沉香",
      plannedFollowUpAt: FUTURE,
    });
    const usage = await service.issueDecisionUsageToken(employeeOne, customer.id, safeCard);

    const result = await service.appendFollowUp(employeeOne, customer.id, {
      requestId: "follow-up-concurrent-winner",
      usageToken: usage.token,
      cardInstanceId: usage.cardInstanceId,
      customerMessage: "稍后再联系。",
      plannedFollowUpAt: FUTURE,
      result: "pending",
      nextAction: "两天后回访",
      decisionCardId: safeCard.cardId,
      replyUsage: "not_used",
    });
    const candidateVaultKey = vault.writes[0]!.vaultKey;

    expect(result.vaultAsset.vaultKey).not.toBe(candidateVaultKey);
    expect(vault.deletedKeys).toContain(candidateVaultKey);
    expect(vault.deletedKeys).not.toContain(result.vaultAsset.vaultKey);
  });

  it("does not expose one employee's customer to another employee", async () => {
    const { service } = createService();
    const { customer } = await service.createCustomer(employeeOne, {
      requestId: "request-create-customer-3",
      customerAlias: "吴女士",
      category: "沉香",
      plannedFollowUpAt: FUTURE,
    });

    await expect(service.getCustomerTimeline(employeeTwo, customer.id))
      .rejects.toBeInstanceOf(CustomerNotFoundError);
    await expect(service.getCustomerTimeline(owner, customer.id))
      .resolves.toMatchObject({ customer: { id: customer.id } });
  });

  it("rejects a tampered or expired decision-usage token", async () => {
    const { service } = createService();
    const { customer } = await service.createCustomer(employeeOne, {
      requestId: "request-create-customer-4",
      customerAlias: "孙女士",
      category: "沉香",
      plannedFollowUpAt: FUTURE,
    });
    const usage = await service.issueDecisionUsageToken(employeeOne, customer.id, safeCard);
    const [encodedPayload, signature] = usage.token.split(".") as [string, string];
    const tamperedSignature = `${signature[0] === "A" ? "B" : "A"}${signature.slice(1)}`;
    const command = {
      requestId: "request-follow-up-2",
      usageToken: `${encodedPayload}.${tamperedSignature}`,
      customerMessage: "想再考虑一下。",
      plannedFollowUpAt: FUTURE,
      result: "pending" as const,
      nextAction: "两天后回访",
      decisionCardId: safeCard.cardId,
      replyUsage: "not_used" as const,
    };

    await expect(service.appendFollowUp(employeeOne, customer.id, command))
      .rejects.toMatchObject({ code: "INVALID_USAGE_TOKEN" });

    const laterService = createCustomerService({
      repository: new MemorySupervisionRepository({ seed: { customers: [customer] } }),
      vault: new RecordingTextVault(),
      usageTokenSecret: TOKEN_SECRET,
      idempotencyKeyring: [{ version: "v1", secret: IDEMPOTENCY_SECRET }],
      now: () => "2026-08-30T08:16:00.000Z",
    });
    await expect(laterService.appendFollowUp(employeeOne, customer.id, {
      ...command,
      usageToken: usage.token,
    })).rejects.toMatchObject({ code: "EXPIRED_USAGE_TOKEN" });
  });

  it("signs the server-provided product seal and rejects a mismatched card seal", async () => {
    const { service } = createService();
    const { customer } = await service.createCustomer(employeeOne, {
      requestId: "customer-product-seal",
      customerAlias: "商品封条客户",
      category: "南红",
      plannedFollowUpAt: FUTURE,
    });
    const seal = {
      productCode: "SYNTHETIC-001",
      strategyInputVersion: 1,
      contentSha256: "a".repeat(64),
      status: "preview_only" as const,
    };
    const sealedCard = { ...safeCard, productStrategySeal: seal };
    const usage = await service.issueDecisionUsageToken(
      employeeOne,
      customer.id,
      sealedCard,
      undefined,
      seal,
    );

    expect(decodeUsagePayload(usage.token)).toMatchObject({
      version: "decision-usage.v3",
      productStrategySeal: seal,
    });
    await expect(service.issueDecisionUsageToken(
      employeeOne,
      customer.id,
      sealedCard,
      undefined,
      { ...seal, strategyInputVersion: 2 },
    )).rejects.toMatchObject({ code: "INVALID_USAGE_TOKEN" });
  });

  it("rejects a stale sealed follow-up before vault or repository writes", async () => {
    const repository = new MemorySupervisionRepository();
    const guard: ProductStrategyFreshnessGuard = {
      assertCurrent: async () => { throw new StaleProductStrategyError(); },
    };
    const { service, vault } = createService(repository, TOKEN_SECRET, IDEMPOTENCY_KEYRING, guard);
    const { customer } = await service.createCustomer(employeeOne, {
      requestId: "customer-before-stale-seal",
      customerAlias: "过期封条客户",
      category: "南红",
      plannedFollowUpAt: FUTURE,
    });
    const seal = {
      productCode: "SYNTHETIC-001",
      strategyInputVersion: 1,
      contentSha256: "a".repeat(64),
      status: "preview_only" as const,
    };
    const card = { ...safeCard, productStrategySeal: seal };
    const usage = await service.issueDecisionUsageToken(
      employeeOne, customer.id, card, undefined, seal,
    );

    await expect(service.appendFollowUp(employeeOne, customer.id, {
      requestId: "stale-seal-follow-up",
      usageToken: usage.token,
      customerMessage: "想再确认商品资料。",
      plannedFollowUpAt: FUTURE,
      result: "pending",
      nextAction: "重新生成销售建议",
      decisionCardId: card.cardId,
      replyUsage: "not_used",
    })).rejects.toMatchObject({ code: "STALE_PRODUCT_STRATEGY" });
    expect(vault.writes).toHaveLength(0);
    await expect(repository.listTimeline({ tenantId: "tenant-a", customerId: customer.id }))
      .resolves.toHaveLength(0);
  });

  it("fails closed without a guard only for product-bound cards", async () => {
    const { service } = createService();
    const { customer } = await service.createCustomer(employeeOne, {
      requestId: "customer-missing-freshness-guard",
      customerAlias: "缺少守卫客户",
      category: "南红",
      plannedFollowUpAt: FUTURE,
    });
    const seal = {
      productCode: "SYNTHETIC-001",
      strategyInputVersion: 1,
      contentSha256: "a".repeat(64),
      status: "preview_only" as const,
    };
    const card = { ...safeCard, productStrategySeal: seal };
    const sealedUsage = await service.issueDecisionUsageToken(
      employeeOne, customer.id, card, undefined, seal,
    );

    await expect(service.appendFollowUp(employeeOne, customer.id, {
      requestId: "missing-freshness-guard",
      usageToken: sealedUsage.token,
      customerMessage: "想再确认商品资料。",
      plannedFollowUpAt: FUTURE,
      result: "pending",
      nextAction: "稍后重试",
      decisionCardId: card.cardId,
      replyUsage: "not_used",
    })).rejects.toBeInstanceOf(ProductStrategyFreshnessUnavailableError);

    const manualUsage = await service.issueDecisionUsageToken(employeeOne, customer.id, safeCard);
    await expect(service.appendFollowUp(employeeOne, customer.id, {
      requestId: "manual-card-without-freshness-guard",
      usageToken: manualUsage.token,
      customerMessage: "人工描述商品。",
      plannedFollowUpAt: FUTURE,
      result: "pending",
      nextAction: "人工核对",
      decisionCardId: safeCard.cardId,
      replyUsage: "not_used",
    })).resolves.toMatchObject({ followUp: { customerId: customer.id } });
  });

  it("returns a committed sealed follow-up after the product becomes stale", async () => {
    let stale = false;
    const guard: ProductStrategyFreshnessGuard = {
      assertCurrent: async () => {
        if (stale) throw new StaleProductStrategyError();
      },
    };
    const { service } = createService(
      new MemorySupervisionRepository(), TOKEN_SECRET, IDEMPOTENCY_KEYRING, guard,
    );
    const { customer } = await service.createCustomer(employeeOne, {
      requestId: "customer-sealed-idempotency",
      customerAlias: "封条重放客户",
      category: "南红",
      plannedFollowUpAt: FUTURE,
    });
    const seal = {
      productCode: "SYNTHETIC-001",
      strategyInputVersion: 1,
      contentSha256: "a".repeat(64),
      status: "preview_only" as const,
    };
    const card = { ...safeCard, productStrategySeal: seal };
    const usage = await service.issueDecisionUsageToken(
      employeeOne, customer.id, card, undefined, seal,
    );
    const command = {
      requestId: "sealed-idempotent-follow-up",
      usageToken: usage.token,
      customerMessage: "想再确认商品资料。",
      plannedFollowUpAt: FUTURE,
      result: "pending" as const,
      nextAction: "稍后回访",
      decisionCardId: card.cardId,
      replyUsage: "not_used" as const,
    };
    const committed = await service.appendFollowUp(employeeOne, customer.id, command);
    stale = true;

    await expect(service.appendFollowUp(employeeOne, customer.id, command))
      .resolves.toEqual(committed);
  });

  it.each([
    [
      "a future offset instant",
      "2026-08-30T00:15:00.000-08:00",
      "2026-08-30T08:14:59.999Z",
      undefined,
    ],
    [
      "the exact expiry boundary",
      "2026-08-30T16:15:00.000+08:00",
      "2026-08-30T08:15:00.000Z",
      "EXPIRED_USAGE_TOKEN",
    ],
  ] as const)("uses epoch expiry for follow-up proof at %s", async (
    _case,
    expiresAt,
    trustedNow,
    expectedError,
  ) => {
    let clock = NOW;
    const repository = new MemorySupervisionRepository();
    const service = createCustomerService({
      repository,
      vault: new RecordingTextVault(),
      usageTokenSecret: TOKEN_SECRET,
      idempotencyKeyring: IDEMPOTENCY_KEYRING,
      now: () => clock,
    });
    const { customer } = await service.createCustomer(employeeOne, {
      requestId: `customer-offset-${expiresAt}`,
      customerAlias: "时区测试客户",
      category: "沉香",
      plannedFollowUpAt: FUTURE,
    });
    const usage = await service.issueDecisionUsageToken(employeeOne, customer.id, safeCard);
    const offsetToken = await signedUsageTokenWith(usage.token, { expiresAt });
    clock = trustedNow;
    const operation = service.appendFollowUp(employeeOne, customer.id, {
      requestId: `follow-up-offset-${expiresAt}`,
      usageToken: offsetToken,
      customerMessage: "我先考虑一下。",
      plannedFollowUpAt: FUTURE,
      result: "pending",
      nextAction: "两天后回访",
      decisionCardId: safeCard.cardId,
      replyUsage: "not_used",
    });

    if (expectedError) {
      await expect(operation).rejects.toMatchObject({ code: expectedError });
    } else {
      await expect(operation).resolves.toMatchObject({
        decisionUsage: { cardInstanceId: usage.cardInstanceId },
      });
    }
  });

  it("binds a usage token to its authenticated employee, customer, and card", async () => {
    const { service } = createService();
    const first = await service.createCustomer(employeeOne, {
      requestId: "request-token-customer-1",
      customerAlias: "甲客户",
      category: "沉香",
      plannedFollowUpAt: FUTURE,
    });
    const second = await service.createCustomer(employeeOne, {
      requestId: "request-token-customer-2",
      customerAlias: "乙客户",
      category: "沉香",
      plannedFollowUpAt: FUTURE,
    });
    const usage = await service.issueDecisionUsageToken(employeeOne, first.customer.id, safeCard);
    const command = {
      requestId: "request-token-follow-up",
      usageToken: usage.token,
      customerMessage: "想再了解一下。",
      plannedFollowUpAt: FUTURE,
      result: "pending" as const,
      nextAction: "两天后回访",
      decisionCardId: safeCard.cardId,
      replyUsage: "not_used" as const,
    };

    await expect(service.appendFollowUp(employeeOne, second.customer.id, command))
      .rejects.toMatchObject({ code: "INVALID_USAGE_TOKEN" });
    await expect(service.appendFollowUp(employeeOne, first.customer.id, {
      ...command,
      decisionCardId: "card-forged",
    })).rejects.toMatchObject({ code: "INVALID_USAGE_TOKEN" });
    await expect(service.appendFollowUp(employeeTwo, first.customer.id, command))
      .rejects.toBeInstanceOf(CustomerNotFoundError);
  });

  it("issues no usage token when the signed journey stage differs from the card", async () => {
    const { service } = createService();
    const { customer } = await service.createCustomer(employeeOne, {
      requestId: "request-token-stage-mismatch",
      customerAlias: "阶段绑定客户",
      category: "沉香",
      plannedFollowUpAt: FUTURE,
    });

    await expect(service.issueDecisionUsageToken(employeeOne, customer.id, safeCard, {
      scenario: safeCard.scenario,
      salesStage: "advance",
      evidenceIds: safeCard.audit.evidenceIds,
    })).rejects.toMatchObject({ code: "INVALID_USAGE_TOKEN" });
  });

  it("returns the original receipt for an exact retry after expiry and conflicts on mismatch", async () => {
    let clock = NOW;
    const repository = new MemorySupervisionRepository();
    const vault = new RecordingTextVault();
    const service = createCustomerService({
      repository,
      vault,
      usageTokenSecret: TOKEN_SECRET,
      idempotencyKeyring: [{ version: "v1", secret: IDEMPOTENCY_SECRET }],
      now: () => clock,
    });
    const { customer } = await service.createCustomer(employeeOne, {
      requestId: "customer-expired-retry",
      customerAlias: "赵女士",
      category: "沉香",
      plannedFollowUpAt: FUTURE,
    });
    const usage = await service.issueDecisionUsageToken(employeeOne, customer.id, safeCard);
    const command = {
      requestId: "follow-up-lost-response",
      usageToken: usage.token,
      cardInstanceId: usage.cardInstanceId,
      customerMessage: "我先考虑两天。",
      plannedFollowUpAt: FUTURE,
      result: "pending" as const,
      nextAction: "两天后回访",
      decisionCardId: safeCard.cardId,
      replyUsage: "not_used" as const,
    };
    const first = await service.appendFollowUp(employeeOne, customer.id, command);

    clock = "2026-08-30T08:16:00.000Z";
    const retried = await service.appendFollowUp(employeeOne, customer.id, command);
    expect(retried).toEqual(first);
    expect(vault.writes).toHaveLength(1);

    await expect(service.appendFollowUp(employeeOne, customer.id, {
      ...command,
      nextAction: "改成明天立即回访",
    })).rejects.toMatchObject({ code: "IDEMPOTENCY_CONFLICT", status: 409 });
  });
});
