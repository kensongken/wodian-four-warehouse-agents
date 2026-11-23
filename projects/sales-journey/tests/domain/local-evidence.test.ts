import { describe, expect, it } from "vitest";

import {
  approvedEvidenceFromDraft,
  evidenceSelectionFromDraft,
  JEWELRY_CATEGORIES,
} from "@/domain/decision-card/local-evidence";

describe("本地已核验资料", () => {
  it("支持六个首批品类", () => {
    expect(JEWELRY_CATEGORIES).toEqual([
      "沉香",
      "绿松石",
      "南红",
      "和田玉",
      "彩宝",
      "翡翠",
    ]);
  });

  it("保留人工确认草稿并支持服务端批准资料选择", () => {
    expect(
      approvedEvidenceFromDraft({
        category: "沉香",
        title: "身份珠服务说明",
        field: "identity_bead",
        humanApproved: true,
      }),
    ).toMatchObject({
      kind: "approved_copy",
      title: "身份珠服务说明",
      verifiedFields: ["identity_bead"],
      status: "approved",
    });
    expect(
      evidenceSelectionFromDraft({
        category: "沉香",
        title: "身份珠服务说明",
        field: "identity_bead",
        selectedForRequest: true,
      }),
    ).toMatchObject({
      kind: "approved_copy",
      title: "身份珠服务说明",
      verifiedFields: ["identity_bead"],
      status: "approved",
    });
  });

  it.each([
    ["certificate", "检测证书"],
    ["material", "材质与工艺说明"],
    ["after_sales_policy", "售后保养说明"],
    ["price_or_payment", "价格与活动规则"],
  ] as const)("支持人工确认后的%s资料", (field, title) => {
    expect(
      approvedEvidenceFromDraft({
        category: "彩宝",
        title,
        field,
        humanApproved: true,
      }),
    ).toMatchObject({ verifiedFields: [field], status: "approved" });
  });

  it("未确认、未选择或空标题的资料不进入请求", () => {
    expect(
      approvedEvidenceFromDraft({
        category: "翡翠",
        title: "扫码说明",
        field: "scan_service",
        humanApproved: false,
      }),
    ).toBeUndefined();
    expect(
      evidenceSelectionFromDraft({
        category: "彩宝",
        title: "   ",
        field: "scan_service",
        selectedForRequest: true,
      }),
    ).toBeUndefined();
    expect(
      evidenceSelectionFromDraft({
        category: "翡翠",
        title: "扫码说明",
        field: "scan_service",
        selectedForRequest: false,
      }),
    ).toBeUndefined();
  });
});
