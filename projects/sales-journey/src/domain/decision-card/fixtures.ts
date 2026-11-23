import type { EvidenceRef } from "./types";

/**
 * v1 示例只用于本地演示与测试。真实调用必须使用同一租户随请求提交的资料，
 * 不能据此推断商品、服务资格或任何价值结论。
 */
export const APPROVED_DECISION_CARD_EVIDENCE_FIXTURES: readonly EvidenceRef[] = [
  {
    id: "fixture-identity-bead",
    kind: "product_record",
    title: "身份珠说明资料",
    verifiedFields: ["identity_bead"],
    status: "approved",
  },
  {
    id: "fixture-scan-service",
    kind: "service_policy",
    title: "扫码资料说明",
    verifiedFields: ["scan_service"],
    status: "approved",
  },
  {
    id: "fixture-redesign-process",
    kind: "approved_copy",
    title: "改款资料收集说明",
    verifiedFields: ["reuse_or_return_process"],
    status: "approved",
  },
  {
    id: "fixture-reuse-or-return-process",
    kind: "approved_copy",
    title: "送回与仓验资料收集说明",
    verifiedFields: ["reuse_or_return_process"],
    status: "approved",
  },
];
