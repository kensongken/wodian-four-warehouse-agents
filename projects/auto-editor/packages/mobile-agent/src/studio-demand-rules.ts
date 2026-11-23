import {
  GapListSchema,
  MatchAssessmentSchema,
  StudioEvidenceDegradedReasonSchema,
  StudioEvidenceRecordSchema,
  type ContentDemand,
  type GapList,
  type MatchAssessment,
  type MaterialCard,
  type MaterialInventory,
  type StudioCapability,
  type StudioEvidenceDegradedReason,
  type StudioEvidenceRecord,
} from "@wearable/contracts";
import { Buffer } from "node:buffer";
import type { StudioEditorialVisualEvidence } from "./studio-capabilities";

export const VISUAL_CONTENT_MISMATCH_NEED = "画面内容与商品介绍目标不一致：当前素材主要是飞机、机翼、天空或黄昏风景";

export function visualDemandMismatchReason(
  contentType: MaterialCard["contentType"],
  goal: string,
  evidence: readonly StudioEditorialVisualEvidence[],
): string | null {
  if (contentType !== "single-product" || evidence.length === 0) return null;
  const asksForProduct = /产品|商品|珠宝|首饰|戒指|项链|手镯|耳环|product|jewelry/iu.test(goal);
  if (!asksForProduct) return null;
  const observations = evidence.flatMap((item) => item.observations)
    .map((value) => value.toLocaleLowerCase());
  const hasProduct = observations.some((value) => value.startsWith("visible-product|"));
  const scenicOnly = observations.some((value) =>
    /飞机|机翼|天空|云层|海面|地平线|城市灯光|airplane|aircraft|wing|sky|cloud|sunset/iu.test(value)
    && !/珠宝|首饰|戒指|项链|手镯|耳环|商品展示|产品展示|jewelry|ring|necklace|bracelet|earring/iu.test(value));
  return !hasProduct || scenicOnly ? VISUAL_CONTENT_MISMATCH_NEED : null;
}

export function normalizeDemandText(value: string): string {
  return value.normalize("NFKC").trim().replace(/\s+/gu, " ");
}

function materialFileNameCandidates(value: string): string[] {
  const candidates = [normalizeDemandText(value).toLocaleLowerCase()];
  const canBeMisdecodedUtf8 = [...value].every((character) => character.codePointAt(0)! <= 0xff);
  if (canBeMisdecodedUtf8) {
    const repaired = Buffer.from(value, "latin1").toString("utf8");
    if (!repaired.includes("\uFFFD")) {
      candidates.push(normalizeDemandText(repaired).toLocaleLowerCase());
    }
  }
  return [...new Set(candidates)];
}

export function explicitlyRequestedMaterialCards(
  cards: readonly MaterialCard[],
  demand: ContentDemand,
): MaterialCard[] {
  const goal = normalizeDemandText(demand.goal).toLocaleLowerCase();
  return cards.filter((card) => card.observations.some((observation) => {
    const fileName = /(?:素材|文件)[:：](.+)$/u.exec(observation)?.[1]?.trim();
    return Boolean(fileName && materialFileNameCandidates(fileName).some((candidate) => goal.includes(candidate)));
  }));
}

function supportsSingleSourceDraft(card: MaterialCard): boolean {
  return card.technicalUsable && card.sourceEndMs - card.sourceStartMs >= 10_000;
}

export type TwoDraftRenderability = Readonly<{
  canRender: boolean;
  budgetExceeded: boolean;
}>;

export type AssessDemandEvidenceInput = Readonly<{
  identity: Pick<MatchAssessment, "id" | "customerId" | "sessionId" | "version">;
  demand: ContentDemand;
  inventory: MaterialInventory;
  cards: readonly MaterialCard[];
  capability: StudioCapability;
  createdAt: string;
  canRenderTwoDrafts: (
    cards: readonly MaterialCard[],
    requiredEvidence: readonly Readonly<{
      requirement: string;
      materialCardIds: readonly string[];
    }>[],
  ) => TwoDraftRenderability;
}>;

export type AssessDemandFromEvidenceInput = AssessDemandEvidenceInput & Readonly<{
  evidenceByCardId: ReadonlyMap<string, readonly StudioEvidenceRecord[]>;
  degradedReasons: readonly StudioEvidenceDegradedReason[];
}>;

export type DemandEvidenceResult = Readonly<{
  assessment: MatchAssessment;
  gapList: GapList | null;
}>;

export function assessDemandEvidence(input: AssessDemandEvidenceInput): MatchAssessment {
  const explicitlyRequested = explicitlyRequestedMaterialCards(input.cards, input.demand);
  const singleSourceRequested = explicitlyRequested.length === 1;
  const usable = (singleSourceRequested ? explicitlyRequested : input.cards).filter((card) =>
    card.contentType === input.demand.contentType &&
    card.technicalUsable &&
    (singleSourceRequested
      ? supportsSingleSourceDraft(card)
      : card.sourceEndMs - card.sourceStartMs >= 8_000 && card.sourceEndMs - card.sourceStartMs <= 20_000));
  const searchable = (card: MaterialCard) => normalizeDemandText([
    ...card.observations,
    ...card.confirmedFacts,
  ].join(" "));
  const violatesAvoid = (card: MaterialCard) =>
    input.demand.mustAvoid.some((term) => searchable(card).includes(normalizeDemandText(term)));
  const allowed = usable.filter((card) => !violatesAvoid(card));
  const excluded = usable.filter(violatesAvoid);
  const matchedRequired = input.demand.mustInclude.map((requirement) => ({
    requirement,
    cards: allowed.filter((card) => searchable(card).includes(normalizeDemandText(requirement))),
  }));
  const unmet = matchedRequired.filter((row) => row.cards.length === 0).map((row) => row.requirement);
  const renderability = singleSourceRequested
    ? { canRender: allowed.length === 1 && supportsSingleSourceDraft(allowed[0]!), budgetExceeded: false }
    : input.canRenderTwoDrafts(allowed, matchedRequired.map((row) => ({
        requirement: row.requirement,
        materialCardIds: row.cards.map((card) => card.id),
      })));
  const verdict: MatchAssessment["verdict"] = allowed.length === 0
    ? "blocked"
    : renderability.canRender && unmet.length === 0 && !renderability.budgetExceeded
      ? "ready"
      : "partial";
  return buildAssessment(input, allowed, excluded, matchedRequired, unmet, renderability, verdict, singleSourceRequested);
}

/**
 * Provider-backed matching keeps suggestions visible and traceable, but only
 * human-confirmed and verified technical evidence can satisfy a requirement.
 */
export function assessDemandFromEvidence(input: AssessDemandFromEvidenceInput): DemandEvidenceResult {
  const evidenceByCardId = validateEvidenceMap(input.cards, input.evidenceByCardId);
  const degradedReasons = validateDegradedReasons(input.degradedReasons);
  const explicitlyRequested = explicitlyRequestedMaterialCards(input.cards, input.demand);
  const singleSourceRequested = explicitlyRequested.length === 1;
  const usable = (singleSourceRequested ? explicitlyRequested : input.cards).filter((card) =>
    card.contentType === input.demand.contentType &&
    card.technicalUsable &&
    (singleSourceRequested
      ? supportsSingleSourceDraft(card)
      : card.sourceEndMs - card.sourceStartMs >= 8_000 && card.sourceEndMs - card.sourceStartMs <= 20_000));
  const recordsFor = (card: MaterialCard) => evidenceByCardId.get(card.id)!;
  const normalizedContains = (value: string, term: string) =>
    normalizeDemandText(value).includes(normalizeDemandText(term));
  const evidencePriority = (kind: StudioEvidenceRecord["kind"]): number =>
    kind === "permission" ? 0 : kind === "human-confirmed-fact" ? 1 :
      kind === "verified-technical-fact" ? 2 : 3;
  const violatesAvoid = (card: MaterialCard) => input.demand.mustAvoid.some((term) =>
    recordsFor(card).some((record) => record.kind !== "permission" && normalizedContains(record.value, term)));
  const allowed = usable.filter((card) => !violatesAvoid(card));
  const excluded = usable.filter(violatesAvoid);
  const requirements = input.demand.mustInclude.map((requirement) => {
    const sources = allowed.flatMap((card) => recordsFor(card)
      .filter((record) => record.kind !== "permission" &&
        normalizeDemandText(record.value) === normalizeDemandText(requirement)))
      .sort((left, right) => evidencePriority(left.kind) - evidencePriority(right.kind));
    const trusted = sources.filter((source) =>
      source.kind === "human-confirmed-fact" || source.kind === "verified-technical-fact");
    return { requirement, sources, trusted };
  });
  const unmet = requirements.flatMap((row) => row.trusted.length > 0
    ? []
    : row.sources.length > 0
      ? ["待人工确认必须项证据：" + row.requirement]
      : ["缺少必须项证据：" + row.requirement]);
  const renderability = singleSourceRequested
    ? { canRender: allowed.length === 1 && supportsSingleSourceDraft(allowed[0]!), budgetExceeded: false }
    : input.canRenderTwoDrafts(allowed, requirements.map((row) => ({
        requirement: row.requirement,
        materialCardIds: [...new Set(row.trusted.map((source) => source.materialCardId))],
      })));
  const missingCount = singleSourceRequested ? 0 : Math.max(0, 4 - allowed.length);
  const planningBoundary = "受众与预期效果仅为策划上下文，不是已核实素材事实";
  const budgetMessage = "搜索预算耗尽，需要人工复核或缩小候选范围";
  const degradedNeeds = degradedReasons.map(degradedNeed);
  const verdict: MatchAssessment["verdict"] = allowed.length === 0
    ? "blocked"
    : renderability.canRender && unmet.length === 0 && degradedNeeds.length === 0 && !renderability.budgetExceeded
      ? "ready"
      : "partial";
  const unmetNeeds = [
    ...(allowed.length === 0 ? ["没有与需求类型匹配的可用素材"] : []),
    ...(missingCount ? ["还缺 " + missingCount + " 段可用素材"] : []),
    ...(excluded.length ? [excluded.length + " 段素材包含明确要求避开的内容"] : []),
    ...(renderability.budgetExceeded ? [budgetMessage] : []),
    ...(allowed.length > 0 && !renderability.canRender && !renderability.budgetExceeded
      ? ["现有素材无法组成两套 30—60 秒且选片不同的草稿"] : []),
    ...unmet,
    ...degradedNeeds,
  ];
  const assessment = MatchAssessmentSchema.parse({
    ...input.identity,
    demandId: input.demand.id,
    inventoryId: input.inventory.id,
    verdict,
    matchedEvidence: [
      ...(allowed.length ? [{
        requirement: "内容类型：" + input.demand.contentType,
        materialCardIds: allowed.map((card) => card.id),
        explanation: allowed.length + " 段素材已通过权限、类型、时长和技术可用性检查",
        sources: allowed.map((card) => sourceProjection(
          recordsFor(card).find((record) => record.kind === "permission")!,
        )),
      }] : []),
      ...requirements.filter((row) => row.sources.length > 0).map((row) => ({
        requirement: row.requirement,
        materialCardIds: [...new Set(row.sources.map((source) => source.materialCardId))],
        explanation: row.trusted.length > 0
          ? "该必须项能追溯到当前已核实素材证据"
          : "发现模型建议，但必须经过人工确认才能作为事实",
        sources: row.sources.map(sourceProjection),
      })),
    ],
    unmetNeeds,
    recommendedCaptures: [
      ...(missingCount ? ["从历史素材库再找 " + missingCount + " 段符合当前内容类型的片段；找不到就缩小范围或暂停，不补拍"] : []),
      ...(excluded.length ? ["从历史素材库换选不包含禁用内容的片段"] : []),
      ...(renderability.budgetExceeded ? [budgetMessage] : []),
      ...unmet.map((need) => need.startsWith("待人工确认")
        ? "人工复核并确认现有模型建议"
        : "只从历史素材库查找能证明缺失必须项的片段；找不到就调整需求或暂停，不补拍"),
      ...degradedReasons.map(degradedRecommendedAction),
    ],
    safeScope: verdict === "ready"
      ? "现有素材可用于“" + input.demand.goal + "”；不补拍；" + planningBoundary + "，仍需人工审核"
      : "不补拍；只能从历史素材库补足证据或缩小内容范围，不能补写缺失内容；" + planningBoundary,
    capability: input.capability,
    createdAt: input.createdAt,
  });
  const gaps = unmetNeeds.map((missingEvidence, index) => ({
    id: "gap_" + input.identity.id + "_" + index,
    missingEvidence,
    impactWithoutCapture: "该缺口未关闭前，不能把相关建议当作已确认素材事实；不补拍，只能找历史素材、缩小范围或暂停",
    recommendedCapture: missingEvidence.startsWith("待人工确认")
      ? "人工复核并确认现有模型建议"
      : isDegradedNeed(missingEvidence)
        ? degradedRecommendedAction(degradedReasons.find((reason) => degradedNeed(reason) === missingEvidence)!)
        : "从历史素材库查找能直接证明该缺口的素材；找不到就缩小范围或暂停，不补拍",
  }));
  const gapList = gaps.length === 0 ? null : GapListSchema.parse({
    id: "gap-list_" + input.identity.id,
    customerId: input.identity.customerId,
    sessionId: input.identity.sessionId,
    version: input.identity.version,
    contentType: input.demand.contentType,
    goal: input.demand.goal,
    gaps,
    capability: input.capability,
    createdAt: input.createdAt,
  });
  return { assessment, gapList };
}

function validateEvidenceMap(
  cards: readonly MaterialCard[],
  evidenceByCardId: ReadonlyMap<string, readonly StudioEvidenceRecord[]>,
): Map<string, StudioEvidenceRecord[]> {
  if (!(evidenceByCardId instanceof Map)) throw new Error("studio-evidence-record-invalid");
  const cardById = new Map(cards.map((card) => [card.id, card]));
  const validated = new Map<string, StudioEvidenceRecord[]>();
  for (const [cardId, recordsValue] of evidenceByCardId.entries()) {
    const card = cardById.get(cardId);
    if (!card || !Array.isArray(recordsValue)) throw new Error("studio-evidence-record-binding-mismatch");
    const records = recordsValue.map((record) => {
      const parsed = StudioEvidenceRecordSchema.safeParse(record);
      if (!parsed.success) throw new Error("studio-evidence-record-invalid");
      const value = parsed.data;
      if (value.materialCardId !== card.id || value.sourceAssetId !== card.sourceAssetId ||
          value.sourceDigest !== card.sourceDigest || value.sourceStartMs !== card.sourceStartMs ||
          value.sourceEndMs !== card.sourceEndMs) {
        throw new Error("studio-evidence-record-binding-mismatch");
      }
      if (value.kind === "permission" && value.value !== "authorized-current-source" ||
          value.kind === "human-confirmed-fact" && !card.confirmedFacts.includes(value.value) ||
          value.kind === "verified-technical-fact" && (!card.observations.includes(value.value) ||
            (value.value.startsWith("scene-count:") ? value.layer !== "scene" : value.layer !== "technical")) ||
          value.kind === "model-suggestion" &&
            (!card.pendingFacts.includes(value.value) || card.confirmedFacts.includes(value.value))) {
        throw new Error("studio-evidence-record-kind-mismatch");
      }
      return value;
    });
    if (records.filter((record) => record.kind === "permission").length !== 1) {
      throw new Error("studio-evidence-record-kind-mismatch");
    }
    validated.set(cardId, records);
  }
  if (validated.size !== cards.length || cards.some((card) => !validated.has(card.id))) {
    throw new Error("studio-evidence-record-binding-mismatch");
  }
  return validated;
}

function validateDegradedReasons(
  reasonsValue: readonly StudioEvidenceDegradedReason[],
): StudioEvidenceDegradedReason[] {
  if (!Array.isArray(reasonsValue)) throw new Error("studio-evidence-degraded-reason-invalid");
  const reasons = reasonsValue.map((reason) => {
    const parsed = StudioEvidenceDegradedReasonSchema.safeParse(reason);
    if (!parsed.success) throw new Error("studio-evidence-degraded-reason-invalid");
    return parsed.data;
  });
  return [...new Map(reasons.map((reason) => [reason.layer + "\0" + reason.code, reason])).values()]
    .sort((left, right) => compareText(left.layer, right.layer) || compareText(left.code, right.code));
}

function degradedNeed(reason: StudioEvidenceDegradedReason): string {
  return reason.code === "studio-evidence-required-provider-missing"
    ? "缺少必需模型证据：" + reason.layer
    : reason.code === "studio-evidence-required-provider-offline"
      ? "必需模型证据处于离线模拟：" + reason.layer
      : "必需模型证据仅为 synthetic：" + reason.layer;
}

function degradedRecommendedAction(reason: StudioEvidenceDegradedReason): string {
  return reason.code === "studio-evidence-required-provider-missing"
    ? "配置并运行 " + reason.layer + " 本地模型证据"
    : reason.code === "studio-evidence-required-provider-offline"
      ? "将 " + reason.layer + " 从离线模拟切换为已批准本地模型后重新分析"
      : "使用非 synthetic 的 " + reason.layer + " 当前素材回执重新分析";
}

function isDegradedNeed(value: string): boolean {
  return value.startsWith("缺少必需模型证据") ||
    value.startsWith("必需模型证据处于离线模拟") ||
    value.startsWith("必需模型证据仅为 synthetic");
}

function compareText(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

function sourceProjection(record: StudioEvidenceRecord) {
  return {
    materialCardId: record.materialCardId,
    sourceAssetId: record.sourceAssetId,
    sourceDigest: record.sourceDigest,
    sourceStartMs: record.sourceStartMs,
    sourceEndMs: record.sourceEndMs,
    evidenceKind: record.kind,
    reason: record.reason,
  };
}

function buildAssessment(
  input: AssessDemandEvidenceInput,
  allowed: readonly MaterialCard[],
  excluded: readonly MaterialCard[],
  required: readonly { requirement: string; cards: readonly MaterialCard[] }[],
  unmet: readonly string[],
  renderability: TwoDraftRenderability,
  verdict: MatchAssessment["verdict"],
  singleSourceRequested = false,
): MatchAssessment {
  const missingCount = singleSourceRequested ? 0 : Math.max(0, 4 - allowed.length);
  const planningBoundary = "受众与预期效果仅为策划上下文，不是已核实素材事实";
  const budgetMessage = "搜索预算耗尽，需要人工复核或缩小候选范围";
  return MatchAssessmentSchema.parse({
    ...input.identity,
    demandId: input.demand.id,
    inventoryId: input.inventory.id,
    verdict,
    matchedEvidence: [
      ...(allowed.length ? [{
        requirement: `内容类型：${input.demand.contentType}`,
        materialCardIds: allowed.map((card) => card.id),
        explanation: `${allowed.length} 段素材已通过权限、类型、时长和技术可用性检查`,
      }] : []),
      ...required.filter((row) => row.cards.length).map((row) => ({
        requirement: row.requirement,
        materialCardIds: row.cards.map((card) => card.id),
        explanation: "该必须项能追溯到当前素材证据",
      })),
    ],
    unmetNeeds: [
      ...(allowed.length === 0 ? ["没有与需求类型匹配的可用素材"] : []),
      ...(missingCount ? [`还缺 ${missingCount} 段可用素材`] : []),
      ...(excluded.length ? [`${excluded.length} 段素材包含明确要求避开的内容`] : []),
      ...(renderability.budgetExceeded ? [budgetMessage] : []),
      ...(allowed.length > 0 && !renderability.canRender && !renderability.budgetExceeded
        ? ["现有素材无法组成两套 30—60 秒且选片不同的草稿"]
        : []),
      ...unmet.map((value) => `缺少必须项证据：${value}`),
    ],
    recommendedCaptures: [
      ...(missingCount ? [`从历史素材库再找 ${missingCount} 段符合当前内容类型的片段；找不到就缩小范围或暂停，不补拍`] : []),
      ...(excluded.length ? ["从历史素材库换选不包含禁用内容的片段"] : []),
      ...(renderability.budgetExceeded ? [budgetMessage] : []),
      ...(allowed.length > 0 && !renderability.canRender && !renderability.budgetExceeded
        ? ["重新组合历史素材；若仍不够，就缩短或收窄内容范围，不补拍"]
        : []),
      ...unmet.map((value) => `只从历史素材库查找能证明“${value}”的片段；找不到就调整需求或暂停，不补拍`),
    ],
    safeScope: renderability.budgetExceeded
      ? `${budgetMessage}；当前结果不能证明素材不可用；${planningBoundary}`
      : verdict === "ready"
        ? `现有素材可用于“${input.demand.goal}”；不补拍；${planningBoundary}，仍需人工审核`
        : `不补拍；只能从历史素材库补足证据或缩小内容范围，不能补写缺失内容；${planningBoundary}`,
    capability: input.capability,
    createdAt: input.createdAt,
  });
}
