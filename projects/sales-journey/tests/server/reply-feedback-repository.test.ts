import { describe, expect, it } from "vitest";
import type { CreateReplyFeedbackInput } from "@/domain/supervision/contracts";
import { InMemoryReplyFeedbackRepository } from "@/server/reply-feedback/repository";
import { MemorySupervisionRepository } from "@/server/supervision/memory-repository";
import { validFeedback } from "../support/reply-feedback-fixtures";

const safeSupervisionFeedback = {
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

const quarantinedSupervisionFeedback = {
  tenantId: safeSupervisionFeedback.tenantId,
  employeeUserId: safeSupervisionFeedback.employeeUserId,
  customerId: safeSupervisionFeedback.customerId,
  decisionCardId: safeSupervisionFeedback.decisionCardId,
  cardInstanceId: "card-instance-quarantined",
  cardDigest: safeSupervisionFeedback.cardDigest,
  agentVersion: safeSupervisionFeedback.agentVersion,
  rating: safeSupervisionFeedback.rating,
  reasonCodes: safeSupervisionFeedback.reasonCodes,
  scenario: safeSupervisionFeedback.scenario,
  salesStage: safeSupervisionFeedback.salesStage,
  evidenceIds: safeSupervisionFeedback.evidenceIds,
  privacyStatus: "quarantined",
  privacyReasonCodes: ["residual_phone"],
} as const satisfies CreateReplyFeedbackInput;

it("returns only feedback for the requested tenant", async () => {
  const repository = new InMemoryReplyFeedbackRepository();
  await repository.save({ ...validFeedback, tenantId: "tenant-a" });
  await repository.save({ ...validFeedback, tenantId: "tenant-b", cardId: "card-b" });

  expect((await repository.listForTenant("tenant-a")).map((item) => item.cardId)).toEqual([
    "card-1",
  ]);
});

it("redacts the optional final reply before storage", async () => {
  const repository = new InMemoryReplyFeedbackRepository();
  await repository.save({ ...validFeedback, finalReply: "王女士 13800138000" });

  expect((await repository.listForTenant("tenant-a"))[0]).toMatchObject({
    privacyStatus: "safe",
    privacyReasonCodes: [],
    finalReply: "[姓名] [手机号]",
  });
});

it.each([
  ["备用号码：１３８００１３８０００", "residual_phone"],
  ["邮箱写作 alice＠example.com", "residual_email"],
  ["付款卡 ６２２２０２１２３４５６７８９０", "residual_payment_card"],
  ["收货地址：幸福花园 3 栋 201 室", "residual_address"],
  ["微 信：jade_sale88", "residual_wechat_or_contact"],
  ["客户姓名：Alice", "residual_name"],
  ["邮箱 alice @ example.com", "residual_email"],
  ["备用号码：１３８ ００１３ ８０００", "residual_phone"],
  ["联系方式：jade_sale88", "residual_wechat_or_contact"],
  ["请寄到北京市朝阳区望京SOHO T1", "residual_address"],
  ["张三 先生", "residual_name"],
  ["邮箱：[邮箱] alice", "residual_email"],
  ["地址：[地址] 幸福花园3栋201", "residual_address"],
  ["姓名：[姓名] Alice", "residual_name"],
  ["卡号：[敏感数字] jade88", "residual_payment_card"],
  ["张三\u200b先生", "residual_name"],
  ["李\u200b女士", "residual_name"],
  ["赵一\u2062先生", "residual_name"],
  ["钱二\u200e女士", "residual_name"],
  ["孙三\u2063先生", "residual_name"],
  ["周四\u00ad女士", "residual_name"],
  ["吴五\u034f先生", "residual_name"],
  ["邮\u2062箱：mailbox2062", "residual_email"],
  ["邮\u034f箱：mailbox034f", "residual_email"],
  ["型号：[邮\u2062箱] hidden2062", "residual_email"],
  ["型号：[邮\u034f箱] hidden034f", "residual_email"],
] as const)("quarantines unresolved private text without retaining it: %s", async (finalReply, reasonCode) => {
  const repository = new InMemoryReplyFeedbackRepository();
  const stored = await repository.save({ ...validFeedback, finalReply });

  expect(stored).toMatchObject({
    privacyStatus: "quarantined",
    privacyReasonCodes: [reasonCode],
  });
  expect(stored).not.toHaveProperty("finalReply");
  expect(JSON.stringify(await repository.listForTenant(validFeedback.tenantId)))
    .not.toContain(finalReply);
});

it("updates one evaluation per tenant, operator, card, and agent version", async () => {
  const repository = new InMemoryReplyFeedbackRepository();
  const first = await repository.save(validFeedback);
  const updated = await repository.save({
    ...validFeedback,
    rating: "needs_improvement",
    reasons: ["too_long"],
  });

  const records = await repository.listForTenant(validFeedback.tenantId);
  expect(records).toHaveLength(1);
  expect(records[0]).toMatchObject({ rating: "needs_improvement", reasons: ["too_long"] });
  expect(updated.feedbackId).toBe(first.feedbackId);
  expect(updated.createdAt).toBe(first.createdAt);
});

it("keeps an exact retry idempotent", async () => {
  const repository = new InMemoryReplyFeedbackRepository();
  const first = await repository.save(validFeedback);
  const retry = await repository.save(validFeedback);

  expect(await repository.listForTenant(validFeedback.tenantId)).toHaveLength(1);
  expect(retry).toEqual(first);
});

it("replaces quarantined metadata with a later safe sanitized reply for the same evaluation", async () => {
  const repository = new InMemoryReplyFeedbackRepository();
  const quarantined = await repository.saveWithOutcome({
    ...validFeedback,
    finalReply: "收货地址：幸福花园 3 栋 201 室",
  });
  const safe = await repository.saveWithOutcome({
    ...validFeedback,
    finalReply: "这款纹理之间有联系，我会再说明保养方式。",
  });

  expect(quarantined.record).toMatchObject({ privacyStatus: "quarantined" });
  expect(quarantined.record).not.toHaveProperty("finalReply");
  expect(safe).toMatchObject({
    outcome: "updated",
    record: {
      feedbackId: quarantined.record.feedbackId,
      createdAt: quarantined.record.createdAt,
      privacyStatus: "safe",
      privacyReasonCodes: [],
      finalReply: "这款纹理之间有联系，我会再说明保养方式。",
    },
  });
  expect(await repository.listForTenant(validFeedback.tenantId)).toHaveLength(1);
});

it("replaces a quarantined record when every immutable feedback field matches", async () => {
  const repository = new InMemoryReplyFeedbackRepository();
  const quarantined = await repository.saveWithOutcome({
    ...validFeedback,
    finalReply: "收货地址：幸福花园 3 栋 201 室",
  });
  const replacement = await repository.replaceQuarantinedWithSafe({
    ...validFeedback,
    finalReply: "这款纹理之间有联系，我会再说明保养方式。",
  });

  expect(replacement).toMatchObject({
    outcome: "updated",
    record: {
      feedbackId: quarantined.record.feedbackId,
      privacyStatus: "safe",
      finalReply: "这款纹理之间有联系，我会再说明保养方式。",
    },
  });
});

it.each([
  ["tenant", { tenantId: "tenant-b" }],
  ["operator", { operatorId: "operator-2" }],
  ["card", { cardId: "card-2" }],
  ["agent version", { agentVersion: "reply-agent.v2" }],
  ["scenario", { scenario: "scan_service" as const }],
  ["sales stage", { salesStage: "advance" as const }],
  ["evidence", { evidenceIds: ["evidence-other"] as string[] }],
] as const)("does not replace a quarantined record with mismatched immutable %s", async (_field, mismatch) => {
  const repository = new InMemoryReplyFeedbackRepository();
  await repository.saveWithOutcome({
    ...validFeedback,
    finalReply: "收货地址：幸福花园 3 栋 201 室",
  });

  const result = await repository.replaceQuarantinedWithSafe({
    ...validFeedback,
    ...mismatch,
    finalReply: "这款纹理之间有联系，我会再说明保养方式。",
  });

  expect(result).toBeUndefined();
  expect((await repository.listForTenant(validFeedback.tenantId))[0]?.privacyStatus)
    .toBe("quarantined");
});

describe("memory supervision reply feedback repository", () => {
  function createRepository(): MemorySupervisionRepository {
    const timestamps = [
      "2026-08-30T01:00:00.000Z",
      "2026-08-30T01:01:00.000Z",
      "2026-08-30T01:02:00.000Z",
      "2026-08-30T01:03:00.000Z",
    ];
    return new MemorySupervisionRepository({
      createId: () => "feedback-generated-a",
      now: () => timestamps.shift() ?? "2026-08-30T01:04:00.000Z",
    });
  }

  it("atomically audits successful feedback changes without retaining reply text", async () => {
    const repository = createRepository();
    const created = await repository.saveReplyFeedback({
      ...quarantinedSupervisionFeedback,
      privacyReasonCodes: ["residual_phone"],
    });
    const replaced = await repository.replaceQuarantinedReplyFeedback({
      ...quarantinedSupervisionFeedback,
      privacyStatus: "safe",
      privacyReasonCodes: [],
      sanitizedFinalReply: "仅用于证明审计不能保存这段回复",
    });

    const audits = await repository.listAuditEvents({
      tenantId: "tenant-a",
      targetKind: "reply_feedback",
      targetId: created.record.id,
    });
    expect(replaced?.outcome).toBe("updated");
    expect(audits.map(({ action, actorUserId, details }) => ({ action, actorUserId, details })))
      .toEqual([
        {
          action: "reply_feedback.created",
          actorUserId: "employee-a",
          details: {
            fromPrivacyStatus: null,
            toPrivacyStatus: "quarantined",
            reasonCodes: [],
            privacyReasonCodes: ["residual_phone"],
          },
        },
        {
          action: "reply_feedback.quarantine_released",
          actorUserId: "employee-a",
          details: {
            fromPrivacyStatus: "quarantined",
            toPrivacyStatus: "safe",
            reasonCodes: [],
            privacyReasonCodes: [],
          },
        },
      ]);
    expect(JSON.stringify(audits)).not.toContain("仅用于证明审计不能保存这段回复");
  });

  it("does not write a success audit when a feedback write fails", async () => {
    const repository = createRepository();
    const created = await repository.saveReplyFeedback(safeSupervisionFeedback);

    await expect(repository.saveReplyFeedback({
      ...safeSupervisionFeedback,
      customerId: "customer-other",
      rating: "needs_improvement",
      reasonCodes: ["other"],
    })).rejects.toThrow("immutable snapshot mismatch");

    expect(await repository.listAuditEvents({
      tenantId: "tenant-a",
      targetKind: "reply_feedback",
      targetId: created.record.id,
    })).toHaveLength(1);
  });

  it("creates, keeps exact retries unchanged, and updates one composite-key record", async () => {
    const repository = createRepository();

    const created = await repository.saveReplyFeedback(safeSupervisionFeedback);
    const unchanged = await repository.saveReplyFeedback(safeSupervisionFeedback);
    const updated = await repository.saveReplyFeedback({
      ...safeSupervisionFeedback,
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
    expect(await repository.listReplyFeedbackForOwner({ tenantId: "tenant-a" })).toHaveLength(1);
  });

  it("never returns reply text for quarantined feedback", async () => {
    const repository = createRepository();
    const saved = await repository.saveReplyFeedback(quarantinedSupervisionFeedback);

    expect(saved.record).toMatchObject({
      privacyStatus: "quarantined",
      privacyReasonCodes: ["residual_phone"],
    });
    expect(saved.record).not.toHaveProperty("sanitizedFinalReply");
  });

  it("upgrades only the exact immutable quarantined snapshot", async () => {
    const repository = createRepository();
    const quarantined = await repository.saveReplyFeedback(quarantinedSupervisionFeedback);

    const rejected = await repository.replaceQuarantinedReplyFeedback({
      ...safeSupervisionFeedback,
      cardInstanceId: quarantinedSupervisionFeedback.cardInstanceId,
      cardDigest: `sha256_${"b".repeat(64)}`,
    });
    expect(rejected).toBeUndefined();

    const upgraded = await repository.replaceQuarantinedReplyFeedback({
      ...safeSupervisionFeedback,
      cardInstanceId: quarantinedSupervisionFeedback.cardInstanceId,
    });
    expect(upgraded).toMatchObject({
      outcome: "updated",
      record: {
        id: quarantined.record.id,
        createdAt: quarantined.record.createdAt,
        privacyStatus: "safe",
        sanitizedFinalReply: safeSupervisionFeedback.sanitizedFinalReply,
      },
    });
  });

  it.each([
    ["customer", { customerId: "customer-b" }],
    ["decision card", { decisionCardId: "card-b" }],
    ["digest", { cardDigest: `sha256_${"b".repeat(64)}` }],
    ["scenario", { scenario: "scan_service" as const }],
    ["sales stage", { salesStage: "advance" as const }],
    ["ordered evidence", { evidenceIds: ["evidence-b", "evidence-a"] }],
  ] as const)("rejects ordinary-save changes to immutable %s", async (_field, mismatch) => {
    const repository = createRepository();
    const original = {
      ...safeSupervisionFeedback,
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
    const repository = createRepository();
    const quarantined = await repository.saveReplyFeedback(quarantinedSupervisionFeedback);

    await expect(repository.saveReplyFeedback({
      ...safeSupervisionFeedback,
      cardInstanceId: quarantinedSupervisionFeedback.cardInstanceId,
    })).rejects.toThrow(/guarded replacement/u);

    expect(await repository.listReplyFeedbackForOwner({
      tenantId: quarantinedSupervisionFeedback.tenantId,
    })).toEqual([quarantined.record]);
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
    const repository = createRepository();
    const quarantinedInput = {
      ...quarantinedSupervisionFeedback,
      evidenceIds: ["evidence-a", "evidence-b"],
    } as const satisfies CreateReplyFeedbackInput;
    const quarantined = await repository.saveReplyFeedback(quarantinedInput);

    const replacement = await repository.replaceQuarantinedReplyFeedback({
      ...safeSupervisionFeedback,
      cardInstanceId: quarantinedInput.cardInstanceId,
      evidenceIds: quarantinedInput.evidenceIds,
      ...mismatch,
    });

    expect(replacement).toBeUndefined();
    expect(await repository.listReplyFeedbackForOwner({ tenantId: quarantinedInput.tenantId }))
      .toEqual([quarantined.record]);
  });
});
