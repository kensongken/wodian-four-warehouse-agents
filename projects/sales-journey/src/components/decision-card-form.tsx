"use client";

import { FormEvent, useState } from "react";

import { DecisionCardSchema } from "@/domain/decision-card/schema";
import type { DecisionCard, SalesScenario } from "@/domain/decision-card/types";
import {
  JourneyWriteBackSchema,
  type JourneyWriteBack,
  type SalesJourneyContext,
} from "@/domain/sales-journey/schema";
import {
  evidenceSelectionFromDraft,
  JEWELRY_CATEGORIES,
  type JewelryCategory,
  type LocalEvidenceField,
} from "@/domain/decision-card/local-evidence";
import {
  SCENARIO_GROUPS,
  scenarioDefinitionFor,
  scenarioOptionsForGroup,
} from "@/domain/decision-card/scenarios/catalog";
import type { ScenarioGroupId } from "@/domain/decision-card/scenarios/contracts";
import { REPLY_AGENT_VERSION } from "@/domain/reply-feedback/schema";

import { DecisionCardView } from "./decision-card-view";
import { ProductStrategyPicker } from "./product-strategy-picker";

type DecisionCardFormProps = {
  journey?: SalesJourneyContext;
};

const EVIDENCE_FIELDS: ReadonlyArray<{ value: LocalEvidenceField; label: string }> = [
  { value: "identity_bead", label: "身份珠说明" },
  { value: "scan_service", label: "扫码服务说明" },
  { value: "reuse_or_return_process", label: "送回、仓验与后续处理流程" },
  { value: "certificate", label: "证书/检测报告" },
  { value: "material", label: "材质与工艺说明" },
  { value: "after_sales_policy", label: "售后保养说明" },
  { value: "price_or_payment", label: "价格与活动规则" },
];

type GenerationResult = {
  card: DecisionCard;
  evidenceTitles: Record<string, string>;
};

function cardFromResponse(payload: unknown): GenerationResult | undefined {
  if (typeof payload !== "object" || payload === null || !("card" in payload)) {
    return undefined;
  }

  const parsed = DecisionCardSchema.safeParse(payload.card);
  if (!parsed.success) {
    return undefined;
  }

  const evidenceTitles = "evidenceTitles" in payload
    && typeof payload.evidenceTitles === "object"
    && payload.evidenceTitles !== null
    ? Object.fromEntries(
        Object.entries(payload.evidenceTitles).filter(
          (entry): entry is [string, string] => typeof entry[1] === "string",
        ),
      )
    : {};
  return { card: parsed.data, evidenceTitles };
}

export function DecisionCardForm({ journey }: DecisionCardFormProps = {}) {
  const [scenarioGroup, setScenarioGroup] = useState<ScenarioGroupId>("product_and_purchase");
  const [scenario, setScenario] = useState<SalesScenario>("first_consultation");
  const [productDescription, setProductDescription] = useState("");
  const [productCode, setProductCode] = useState("");
  const [latestCustomerMessage, setLatestCustomerMessage] = useState("");
  const [evidenceCategory, setEvidenceCategory] = useState<JewelryCategory>("沉香");
  const [evidenceTitle, setEvidenceTitle] = useState("");
  const [evidenceField, setEvidenceField] = useState<LocalEvidenceField>("identity_bead");
  const [evidenceSelected, setEvidenceSelected] = useState(false);
  const [card, setCard] = useState<DecisionCard>();
  const [citationTitles, setCitationTitles] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [confirmedJourneyWriteBack, setConfirmedJourneyWriteBack] = useState<JourneyWriteBack>();
  const scenarioOptions = scenarioOptionsForGroup(scenarioGroup);
  const selectedScenario = scenarioDefinitionFor(scenario);

  function changeScenarioGroup(nextGroup: ScenarioGroupId): void {
    setScenarioGroup(nextGroup);
    setScenario(scenarioOptionsForGroup(nextGroup)[0].id);
  }

  function confirmJourneyProposal(writeBack: JourneyWriteBack): void {
    setConfirmedJourneyWriteBack(JourneyWriteBackSchema.parse(writeBack));
  }

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setError("");
    setCard(undefined);
    setCitationTitles({});
    setConfirmedJourneyWriteBack(undefined);
    setIsSubmitting(true);

    const evidenceSelection = evidenceSelectionFromDraft({
      category: evidenceCategory,
      title: evidenceTitle,
      field: evidenceField,
      selectedForRequest: evidenceSelected,
    });

    try {
      const response = await fetch("/api/decision-cards", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          contractVersion: "decision-card.v1",
          requestId: `browser-${Date.now()}`,
          scenario,
          productCode: productCode.trim() || undefined,
          productDescription: productDescription.trim() || undefined,
          latestCustomerMessage,
          conversation: [],
          operatorStyle: {
            tone: "warm",
            maxReplyCharacters: 180,
          },
          evidence: evidenceSelection === undefined ? [] : [evidenceSelection],
        }),
      });
      const payload: unknown = await response.json();
      const result = response.ok ? cardFromResponse(payload) : undefined;

      if (result === undefined) {
        const sourceError = typeof payload === "object" && payload !== null && "error" in payload
          ? payload.error
          : undefined;
        setError(sourceError === "PRODUCT_STRATEGY_INPUT_NOT_FOUND"
          ? "该商品编号尚未同步，请核对编号或等待分门别类完成处理。"
          : "无法生成决策卡。请补全信息，或交由人工核对后再试。");
        return;
      }

      setCard(result.card);
      setCitationTitles(result.evidenceTitles);
    } catch {
      setError("暂时无法生成决策卡。请检查网络后重试，或交由人工处理。");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="decision-workspace">
      <form className="decision-form" onSubmit={submit}>
        <div className="form-intro">
          <h2>填写本次咨询</h2>
          <p>系统只生成中文建议草稿，不会自动发送任何消息。</p>
        </div>

        <div className="scenario-picker">
          <label htmlFor="scenario-group">业务大类</label>
          <select
            id="scenario-group"
            value={scenarioGroup}
            onChange={(event) => changeScenarioGroup(event.target.value as ScenarioGroupId)}
          >
            {SCENARIO_GROUPS.map((group) => (
              <option key={group.id} value={group.id}>
                {group.label}
              </option>
            ))}
          </select>

          <label htmlFor="scenario">具体场景</label>
          <select
            id="scenario"
            aria-describedby="scenario-description"
            value={scenario}
            onChange={(event) => setScenario(event.target.value as SalesScenario)}
          >
            {scenarioOptions.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
          <p id="scenario-description" className="scenario-description">
            {selectedScenario.description}
          </p>
        </div>

        <ProductStrategyPicker value={productCode} onChange={setProductCode} />
        <p className="scenario-description">填入编号后，系统只使用服务端已同步快照；材质、价格和库存仍需人工确认。</p>

        <label htmlFor="product-description">商品描述</label>
        <input
          id="product-description"
          value={productDescription}
          onChange={(event) => setProductDescription(event.target.value)}
          required={productCode.trim() === ""}
        />
        <p className="scenario-description">没有商品编号时才需填写。</p>

        <label htmlFor="latest-customer-message">客户最新消息</label>
        <textarea
          id="latest-customer-message"
          value={latestCustomerMessage}
          onChange={(event) => setLatestCustomerMessage(event.target.value)}
          required
        />

        <fieldset>
          <legend>本次可引用资料（可选）</legend>
          <p>这里只选择候选资料；是否已批准、属于本租户及可引用字段都由服务端登记表核验。</p>

          <label htmlFor="evidence-category">资料品类</label>
          <select
            id="evidence-category"
            value={evidenceCategory}
            onChange={(event) => setEvidenceCategory(event.target.value as JewelryCategory)}
          >
            {JEWELRY_CATEGORIES.map((category) => (
              <option key={category} value={category}>
                {category}
              </option>
            ))}
          </select>

          <label htmlFor="evidence-title">资料标题</label>
          <input
            id="evidence-title"
            value={evidenceTitle}
            onChange={(event) => setEvidenceTitle(event.target.value)}
          />

          <label htmlFor="evidence-field">资料类型</label>
          <select
            id="evidence-field"
            value={evidenceField}
            onChange={(event) => setEvidenceField(event.target.value as LocalEvidenceField)}
          >
            {EVIDENCE_FIELDS.map((field) => (
              <option key={field.value} value={field.value}>
                {field.label}
              </option>
            ))}
          </select>

          <label htmlFor="evidence-human-approved">
            <input
              id="evidence-human-approved"
              type="checkbox"
              checked={evidenceSelected}
              onChange={(event) => setEvidenceSelected(event.target.checked)}
            />
            本次选择引用服务端已批准的演示资料
          </label>
        </fieldset>

        <button className="primary-action" type="submit" disabled={isSubmitting}>
          {isSubmitting ? "正在生成…" : "生成决策卡"}
        </button>
      </form>

      {error !== "" ? <p role="alert">{error}</p> : null}
      {card !== undefined ? (
        <>
          <DecisionCardView
            card={card}
            evidenceTitles={citationTitles}
            journey={journey}
            agentVersion={journey === undefined ? undefined : REPLY_AGENT_VERSION}
            onConfirmJourneyWriteBack={
              journey === undefined ? undefined : confirmJourneyProposal
            }
          />
          {confirmedJourneyWriteBack !== undefined ? (
            <p role="status">已确认本次旅程建议（本地演示）</p>
          ) : null}
        </>
      ) : (
        <aside className="empty-card" aria-label="销售建议预览">
          <p className="eyebrow">销售建议预览</p>
          <h2>填写左侧咨询后生成</h2>
          <p>系统会把建议回复、引用资料、核验提醒和语音试听整理在这里。</p>
        </aside>
      )}
    </div>
  );
}
