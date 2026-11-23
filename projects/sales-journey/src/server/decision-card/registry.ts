import { DecisionCardSchema } from "@/domain/decision-card/schema";
import {
  JEWELRY_CATEGORIES,
  localEvidenceId,
  type LocalEvidenceField,
} from "@/domain/decision-card/local-evidence";
import type {
  DecisionCard,
  EvidenceRef,
} from "@/domain/decision-card/types";
import {
  REPLY_AGENT_VERSION,
  type ReplyFeedbackRequest,
} from "@/domain/reply-feedback/schema";
import { isExplicitSyntheticLocalMode } from "@/server/synthetic-local-mode";

export { REPLY_AGENT_VERSION };
export const GENERATED_CARD_TTL_MS = 30 * 60 * 1000;

const FIELD_LABEL: Record<LocalEvidenceField, string> = {
  identity_bead: "身份珠说明",
  scan_service: "扫码服务说明",
  reuse_or_return_process: "送回与仓验流程",
  certificate: "证书与检测报告",
  material: "材质与工艺说明",
  after_sales_policy: "售后与养护规则",
  price_or_payment: "价格与活动规则",
};

const APPROVED_EVIDENCE_FIELDS = Object.keys(FIELD_LABEL) as LocalEvidenceField[];
const LEGACY_DEMO_EVIDENCE: EvidenceRef[] = [
  {
    id: "evidence-identity-1",
    kind: "product_record",
    title: "已批准的身份珠说明",
    verifiedFields: ["identity_bead"],
    status: "approved",
  },
  {
    id: "evidence-scan-1",
    kind: "service_policy",
    title: "已批准的扫码服务说明",
    verifiedFields: ["scan_service"],
    status: "approved",
  },
  {
    id: "evidence-redesign-1",
    kind: "service_policy",
    title: "已批准的改款流程说明",
    verifiedFields: ["reuse_or_return_process"],
    status: "approved",
  },
  {
    id: "evidence-reuse-1",
    kind: "service_policy",
    title: "已批准的送回与受控再利用流程",
    verifiedFields: ["reuse_or_return_process"],
    status: "approved",
  },
];
const APPROVED_EVIDENCE = new Map<string, EvidenceRef>(
  [
    ...JEWELRY_CATEGORIES.flatMap((category) =>
      APPROVED_EVIDENCE_FIELDS.map((field) => ({
        id: localEvidenceId(category, field),
        kind: "approved_copy" as const,
        title: `${category}${FIELD_LABEL[field]}（本地已批准演示资料）`,
        verifiedFields: [field],
        status: "approved" as const,
      })),
    ),
    ...LEGACY_DEMO_EVIDENCE,
  ].map((evidence) => [evidence.id, evidence]),
);

export function resolveApprovedEvidence(
  tenantId: string,
  evidenceIds: readonly string[],
): EvidenceRef[] | undefined {
  if (tenantId.trim() === "") {
    return undefined;
  }
  if (new Set(evidenceIds).size !== evidenceIds.length) {
    return undefined;
  }
  if (evidenceIds.length > 0 && !isExplicitSyntheticLocalMode()) {
    return undefined;
  }

  const resolved: EvidenceRef[] = [];
  for (const evidenceId of evidenceIds) {
    const evidence = APPROVED_EVIDENCE.get(evidenceId);
    if (evidence === undefined) {
      return undefined;
    }
    resolved.push(evidence);
  }
  return resolved;
}

export function evidenceTitlesFor(evidence: readonly EvidenceRef[]): Record<string, string> {
  return Object.fromEntries(evidence.map((item) => [item.id, item.title]));
}

type GeneratedCardRecord = {
  tenantId: string;
  operatorId: string;
  cardId: string;
  agentVersion: string;
  scenario: DecisionCard["scenario"];
  salesStage: DecisionCard["salesStage"];
  evidenceIds: string[];
  expiresAt: number;
};

export type GeneratedCardMatch = "valid" | "unknown" | "expired" | "mismatch";

function cardKey(input: Pick<GeneratedCardRecord, "tenantId" | "operatorId" | "cardId" | "agentVersion">): string {
  return [input.tenantId, input.operatorId, input.cardId, input.agentVersion].join("\u0000");
}

export class InMemoryGeneratedCardRegistry {
  readonly #records = new Map<string, GeneratedCardRecord>();

  register(cardInput: DecisionCard, agentVersion: string): void {
    const card = DecisionCardSchema.parse(cardInput);
    const record: GeneratedCardRecord = {
      tenantId: card.audit.tenantId,
      operatorId: card.audit.operatorId,
      cardId: card.cardId,
      agentVersion,
      scenario: card.scenario,
      salesStage: card.salesStage,
      evidenceIds: [...card.audit.evidenceIds],
      expiresAt: Date.now() + GENERATED_CARD_TTL_MS,
    };
    this.#records.set(cardKey(record), record);
  }

  matchFeedback(input: ReplyFeedbackRequest): GeneratedCardMatch {
    const record = this.#records.get(cardKey(input));
    if (record === undefined) {
      return "unknown";
    }
    if (
      record.scenario !== input.scenario
      || record.salesStage !== input.salesStage
      || record.evidenceIds.length !== input.evidenceIds.length
      || record.evidenceIds.some((id, index) => id !== input.evidenceIds[index])
    ) {
      return "mismatch";
    }
    if (Date.now() > record.expiresAt) {
      return "expired";
    }
    return "valid";
  }
}

const registryScope = globalThis as typeof globalThis & {
  __salesDecisionCardRegistry?: InMemoryGeneratedCardRegistry;
};

export const generatedCardRegistry =
  registryScope.__salesDecisionCardRegistry ??= new InMemoryGeneratedCardRegistry();
