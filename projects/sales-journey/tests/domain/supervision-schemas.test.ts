import { describe, expect, it } from "vitest";

import {
  AppendCorrectionInputSchema,
  AppendFollowUpInputSchema,
  CreateCustomerInputSchema,
  OwnerFilterSchema,
  VaultAssetRecordSchema,
} from "@/domain/supervision/schemas";

describe("员工监督写入校验", () => {
  it("does not accept tenant or employee identity from an employee write", () => {
    expect(CreateCustomerInputSchema.safeParse({
      tenantId: "forged",
      employeeUserId: "forged",
      customerAlias: "林女士",
    }).success).toBe(false);
  });

  it("requires a customer alias and planned follow-up time", () => {
    expect(CreateCustomerInputSchema.parse({
      customerAlias: "林女士",
      plannedFollowUpAt: "2026-08-31T06:30:00.000Z",
      category: "沉香",
    })).toMatchObject({
      customerAlias: "林女士",
      category: "沉香",
      plannedFollowUpAt: "2026-08-31T06:30:00.000Z",
    });
  });

  it("accepts a complete employee follow-up without server-owned identity", () => {
    const result = AppendFollowUpInputSchema.parse({
      customerMessage: "想看看翡翠的证书。",
      recentContext: "客户昨天到店试戴过。",
      plannedFollowUpAt: "2026-08-31T06:30:00.000Z",
      actualFollowUpAt: "2026-08-31T07:00:00.000Z",
      result: "interested",
      nextAction: "发送已核实的证书资料。",
      decisionCardId: "card-001",
      replyUsage: "edited",
      chosenReplyId: "reply-02",
      finalEditedText: "我把已核实的证书资料整理给您。",
    });

    expect(result.result).toBe("interested");
  });

  it("rejects tenant or employee identity on a follow-up", () => {
    expect(AppendFollowUpInputSchema.safeParse({
      customerMessage: "想看看翡翠的证书。",
      plannedFollowUpAt: "2026-08-31T06:30:00.000Z",
      result: "pending",
      nextAction: "明天回访。",
      decisionCardId: "card-001",
      replyUsage: "not_used",
      tenantId: "forged",
    }).success).toBe(false);
  });

  it("accepts an append-only correction reference but rejects actor identity", () => {
    expect(AppendCorrectionInputSchema.parse({
      followUpId: "follow-up-001",
      reason: "实际跟进时间录入错误。",
    })).toMatchObject({ followUpId: "follow-up-001" });
    expect(AppendCorrectionInputSchema.safeParse({
      followUpId: "follow-up-001",
      reason: "实际跟进时间录入错误。",
      actorUserId: "forged",
    }).success).toBe(false);
  });
});

describe("老板数据筛选校验", () => {
  it("accepts date, employee, category, result, and customer-number filters", () => {
    expect(OwnerFilterSchema.parse({
      from: "2026-08-01T00:00:00.000Z",
      to: "2026-08-31T23:59:59.999Z",
      employeeUserId: "employee-001",
      customerNumber: "C-001",
      category: "翡翠",
      result: "won",
    })).toMatchObject({ result: "won", category: "翡翠" });
  });

  it("rejects a tenant override and malformed owner filters", () => {
    expect(OwnerFilterSchema.safeParse({
      tenantId: "forged",
      from: "not-a-date",
      category: "未知品类",
    }).success).toBe(false);
  });

  it("compares owner filter instants by epoch and normalizes offsets and precision", () => {
    expect(OwnerFilterSchema.parse({
      from: "2026-08-30T01:00:00+08:00",
      to: "2026-08-29T18:00:00.1Z",
    })).toMatchObject({
      from: "2026-08-29T17:00:00.000Z",
      to: "2026-08-29T18:00:00.100Z",
    });
    expect(OwnerFilterSchema.parse({
      from: "2026-08-30T08:00:00+08:00",
      to: "2026-08-30T00:00:00Z",
    })).toEqual({
      from: "2026-08-30T00:00:00.000Z",
      to: "2026-08-30T00:00:00.000Z",
    });
    expect(OwnerFilterSchema.safeParse({
      from: "2026-08-30T00:00:00.001Z",
      to: "2026-08-30T08:00:00+08:00",
    }).success).toBe(false);
  });
});

describe("保险柜资产记录校验", () => {
  const exactThirtyDayAsset = {
    id: "vault-001",
    tenantId: "tenant-001",
    customerId: "customer-001",
    uploadedByUserId: "employee-001",
    kind: "chat_text",
    vaultKey: "tenant-001/vault-001",
    uploadedAt: "2026-01-31T06:30:00.000Z",
    deleteAfter: "2026-03-02T06:30:00.000Z",
    version: 1,
    createdAt: "2026-01-31T06:30:00.000Z",
  };

  it("rejects a vault asset whose expiry is not exactly thirty days after upload", () => {
    expect(VaultAssetRecordSchema.safeParse({
      ...exactThirtyDayAsset,
      deleteAfter: "2026-03-01T06:30:00.000Z",
    }).success).toBe(false);
  });

  it("accepts a vault asset with the exact thirty-day expiry", () => {
    expect(VaultAssetRecordSchema.parse(exactThirtyDayAsset)).toMatchObject({
      deleteAfter: "2026-03-02T06:30:00.000Z",
    });
  });
});
