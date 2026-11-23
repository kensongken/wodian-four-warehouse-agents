"use client";

import { type FormEvent, useRef, useState } from "react";

import { DecisionCardView } from "@/components/decision-card-view";
import { SupervisionReplyFeedbackForm } from "@/components/reply-feedback-form";
import { ProductStrategyPicker } from "@/components/product-strategy-picker";
import { DecisionCardSchema } from "@/domain/decision-card/schema";
import { scenarioLabelFor } from "@/domain/decision-card/scenarios/catalog";
import { SALES_SCENARIOS, type DecisionCard, type SalesScenario } from "@/domain/decision-card/types";
import { JEWELRY_CATEGORIES, type JewelryCategory } from "@/domain/decision-card/local-evidence";
import type {
  FollowUpResult,
  ReplyUsage,
} from "@/domain/supervision/contracts";
import type { EmployeeCustomerView } from "@/domain/supervision/employee-view";

type SyncStatus = "idle" | "saving" | "saved" | "failed";

export type EmployeeDecisionSession = Readonly<{
  card: DecisionCard;
  usageToken: string;
  usageTokenExpiresAt: string;
  cardInstanceId: string;
  cardDigest: string;
  generatedFor: Readonly<{
    scenario: SalesScenario;
    productCode?: string;
    productDescription: string;
    customerMessage: string;
  }>;
}>;

type FollowUpFormProps = Readonly<{
  customer: EmployeeCustomerView;
  employeeLabel: string;
  decision?: EmployeeDecisionSession;
}>;

function requestId(prefix: string): string {
  return `${prefix}-${crypto.randomUUID()}`;
}

function toLocalDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function toIso(localDateTime: string): string | undefined {
  if (localDateTime === "") return undefined;
  const date = new Date(localDateTime);
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString();
}

function decisionSessionFrom(
  payload: unknown,
  generatedFor: EmployeeDecisionSession["generatedFor"],
): EmployeeDecisionSession | undefined {
  if (!payload || typeof payload !== "object") return undefined;
  const source = payload as Record<string, unknown>;
  const card = DecisionCardSchema.safeParse(source.card);
  if (
    !card.success
    || typeof source.usageToken !== "string"
    || typeof source.usageTokenExpiresAt !== "string"
    || typeof source.cardInstanceId !== "string"
    || typeof source.cardDigest !== "string"
  ) {
    return undefined;
  }
  return {
    card: card.data,
    usageToken: source.usageToken,
    usageTokenExpiresAt: source.usageTokenExpiresAt,
    cardInstanceId: source.cardInstanceId,
    cardDigest: source.cardDigest,
    generatedFor,
  };
}

function syncMessage(status: SyncStatus): React.ReactNode {
  if (status === "saved") return <p className="sync-success" role="status">已同步</p>;
  if (status === "failed") return <p className="sync-failed" role="alert">尚未同步，请重试</p>;
  return null;
}

export function FollowUpForm({ customer, employeeLabel, decision }: FollowUpFormProps) {
  const [decisionSession, setDecisionSession] = useState(decision);
  const [scenario, setScenario] = useState<SalesScenario>(
    decision?.generatedFor.scenario ?? "identity_bead",
  );
  const [productDescription, setProductDescription] = useState(
    decision?.generatedFor.productDescription ?? "",
  );
  const [productCode, setProductCode] = useState(decision?.generatedFor.productCode ?? "");
  const [customerMessage, setCustomerMessage] = useState(
    decision?.generatedFor.customerMessage ?? "",
  );
  const [recentContext, setRecentContext] = useState("");
  const [plannedFollowUpAt, setPlannedFollowUpAt] = useState(
    toLocalDateTime(customer.plannedFollowUpAt),
  );
  const [actualFollowUpAt, setActualFollowUpAt] = useState("");
  const [result, setResult] = useState<FollowUpResult>("pending");
  const [nextAction, setNextAction] = useState("");
  const [replyUsage, setReplyUsage] = useState<ReplyUsage>();
  const [chosenReplyId, setChosenReplyId] = useState<string>();
  const [finalEditedText, setFinalEditedText] = useState("");
  const [images, setImages] = useState<File[]>([]);
  const [syncStatus, setSyncStatus] = useState<SyncStatus>("idle");
  const [savedFingerprint, setSavedFingerprint] = useState<string>();
  const [decisionError, setDecisionError] = useState("");
  const [isGenerating, setIsGenerating] = useState(false);
  const generationVersion = useRef(0);
  const [followUpRequestId, setFollowUpRequestId] = useState(() => requestId("follow-up"));
  const inputFingerprint = JSON.stringify({
    decisionCardId: decisionSession?.card.cardId,
    cardInstanceId: decisionSession?.cardInstanceId,
    cardDigest: decisionSession?.cardDigest,
    scenario,
    productCode,
    productDescription,
    customerMessage,
    recentContext,
    plannedFollowUpAt,
    actualFollowUpAt,
    result,
    nextAction,
    replyUsage,
    chosenReplyId,
    finalEditedText,
    images: images.map((image) => [image.name, image.size, image.lastModified]),
  });
  const visibleSyncStatus: SyncStatus = syncStatus === "saved"
    && savedFingerprint !== inputFingerprint
    ? "idle"
    : syncStatus;
  const isCurrentSaved = visibleSyncStatus === "saved";
  const selectedReplyText = decisionSession?.card.replyOptions.find(
    (reply) => reply.id === chosenReplyId,
  )?.text;
  const defaultFeedbackFinalReply = replyUsage === "adopted"
    ? selectedReplyText
    : replyUsage === "edited" ? finalEditedText : undefined;

  function clearDecisionSession(message: string): void {
    setDecisionSession(undefined);
    setChosenReplyId(undefined);
    setDecisionError(message);
    setSyncStatus("idle");
  }

  function invalidateDecisionSession(): void {
    generationVersion.current += 1;
    setIsGenerating(false);
    if (decisionSession !== undefined) {
      clearDecisionSession("内容已变化，请重新生成销售建议。");
    } else if (isGenerating) {
      setDecisionError("内容已变化，请重新生成销售建议。");
    }
  }

  async function generateDecision(): Promise<void> {
    const requestVersion = generationVersion.current + 1;
    generationVersion.current = requestVersion;
    const generatedFor = { scenario, productCode: productCode.trim() || undefined, productDescription, customerMessage };
    setDecisionError("");
    setSyncStatus("idle");
    setIsGenerating(true);
    try {
      const response = await fetch("/api/decision-cards", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          contractVersion: "decision-card.v1",
          requestId: requestId("decision"),
          customerId: customer.id,
          scenario,
          productCode: productCode.trim() || undefined,
          productDescription: productDescription.trim() || undefined,
          latestCustomerMessage: customerMessage,
          conversation: [],
          operatorStyle: { tone: "warm", maxReplyCharacters: 180 },
          evidence: [],
        }),
      });
      const payload: unknown = await response.json().catch(() => undefined);
      if (requestVersion !== generationVersion.current) return;
      const nextDecision = response.ok
        ? decisionSessionFrom(payload, generatedFor)
        : undefined;
      if (!nextDecision) {
        const sourceError = payload && typeof payload === "object" && "error" in payload
          ? payload.error
          : undefined;
        setDecisionError(sourceError === "PRODUCT_STRATEGY_INPUT_NOT_FOUND"
          ? "该商品编号尚未同步，请核对编号或等待分门别类完成处理。"
          : "无法生成销售建议，请核对输入后重试。");
        return;
      }
      setDecisionSession(nextDecision);
      setChosenReplyId(nextDecision.card.replyOptions[0]?.id);
    } catch {
      if (requestVersion === generationVersion.current) {
        setDecisionError("无法生成销售建议，请检查网络后重试。");
      }
    } finally {
      if (requestVersion === generationVersion.current) setIsGenerating(false);
    }
  }

  function changeReplyUsage(nextUsage: ReplyUsage): void {
    setReplyUsage(nextUsage);
    if (nextUsage === "not_used") {
      setChosenReplyId(undefined);
      setFinalEditedText("");
    } else if (chosenReplyId === undefined) {
      setChosenReplyId(decisionSession?.card.replyOptions[0]?.id);
    }
    if (nextUsage === "adopted") setFinalEditedText("");
  }

  async function uploadImages(): Promise<void> {
    if (images.length === 0) return;
    const form = new FormData();
    form.set("customerId", customer.id);
    for (const image of images) form.append("files", image);
    const response = await fetch("/api/supervision/uploads", { method: "POST", body: form });
    if (!response.ok) throw new Error("upload failed");
  }

  async function saveFollowUp(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (syncStatus === "saving" || isCurrentSaved) return;
    if (
      !decisionSession
      || !replyUsage
      || (replyUsage !== "not_used" && !chosenReplyId)
      || (replyUsage === "edited" && finalEditedText.trim() === "")
    ) {
      setSyncStatus("failed");
      return;
    }
    const usageExpiry = Date.parse(decisionSession.usageTokenExpiresAt);
    if (Number.isFinite(usageExpiry) && usageExpiry <= Date.now()) {
      clearDecisionSession("销售建议已过期，请重新生成。");
      return;
    }
    const planned = toIso(plannedFollowUpAt);
    if (!planned) {
      setSyncStatus("failed");
      return;
    }

    setSyncStatus("saving");
    try {
      await uploadImages();
      const response = await fetch(
        `/api/supervision/customers/${encodeURIComponent(customer.id)}/follow-ups`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            requestId: followUpRequestId,
            usageToken: decisionSession.usageToken,
            customerMessage,
            recentContext: recentContext.trim() || undefined,
            plannedFollowUpAt: planned,
            actualFollowUpAt: toIso(actualFollowUpAt),
            result,
            nextAction,
            decisionCardId: decisionSession.card.cardId,
            cardInstanceId: decisionSession.cardInstanceId,
            cardDigest: decisionSession.cardDigest,
            replyUsage,
            chosenReplyId: replyUsage === "not_used" ? undefined : chosenReplyId,
            finalEditedText: replyUsage === "edited" ? finalEditedText : undefined,
          }),
        },
      );
      if (!response.ok) {
        const payload = await response.json().catch(() => undefined) as
          | { error?: unknown }
          | undefined;
        if (payload?.error === "EXPIRED_USAGE_TOKEN") {
          clearDecisionSession("销售建议已过期，请重新生成。");
          return;
        }
        if (payload?.error === "STALE_PRODUCT_STRATEGY") {
          clearDecisionSession("商品资料已更新，请重新生成决策卡后再保存跟进。");
          return;
        }
        throw new Error("sync failed");
      }
      setSavedFingerprint(inputFingerprint);
      setSyncStatus("saved");
      setFollowUpRequestId(requestId("follow-up"));
    } catch {
      setSyncStatus("failed");
    }
  }

  return (
    <section className="employee-panel follow-up-panel" aria-label="员工客户跟进">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">本次跟进</p>
          <h2>记录客户问题与处理结果</h2>
        </div>
        <span className="employee-chip">负责人：{employeeLabel}</span>
      </div>

      <form className="follow-up-form" onSubmit={saveFollowUp}>
        <div className="form-grid two-columns">
          <label>
            业务场景
            <select
              value={scenario}
              onChange={(event) => {
                const nextScenario = event.target.value as SalesScenario;
                if (nextScenario !== scenario) invalidateDecisionSession();
                setScenario(nextScenario);
              }}
            >
              {SALES_SCENARIOS.map((item) => (
                <option key={item} value={item}>{scenarioLabelFor(item)}</option>
              ))}
            </select>
          </label>
          <ProductStrategyPicker
            value={productCode}
            onChange={(nextCode) => {
              if (nextCode !== productCode) invalidateDecisionSession();
              setProductCode(nextCode);
            }}
          />
          <label>
            商品描述
            <input
              value={productDescription}
              onChange={(event) => {
                if (event.target.value !== productDescription) invalidateDecisionSession();
                setProductDescription(event.target.value);
              }}
              required={decisionSession === undefined && productCode.trim() === ""}
            />
          </label>
        </div>

        <label>
          客户问题
          <textarea
            value={customerMessage}
            onChange={(event) => {
              if (event.target.value !== customerMessage) invalidateDecisionSession();
              setCustomerMessage(event.target.value);
            }}
            required
          />
        </label>
        <label>
          最近沟通背景（可选，仅存保险柜）
          <textarea value={recentContext} onChange={(event) => setRecentContext(event.target.value)} />
        </label>
        <label>
          客户商品图片（可选）
          <input
            type="file"
            accept="image/jpeg,image/png,image/webp"
            multiple
            onChange={(event) => setImages(Array.from(event.target.files ?? []))}
          />
        </label>

        {decisionSession === undefined ? (
          <button
            className="secondary-action"
            type="button"
            onClick={generateDecision}
            disabled={
              isGenerating
              || (productCode.trim() === "" && productDescription.trim() === "")
              || customerMessage.trim() === ""
            }
          >
            {isGenerating ? "正在生成…" : "生成销售建议"}
          </button>
        ) : null}
        {decisionError !== "" ? <p role="alert">{decisionError}</p> : null}

        {decisionSession !== undefined ? (
          <>
            <DecisionCardView
              card={decisionSession.card}
              selectedReplyId={chosenReplyId}
              onSelectReply={setChosenReplyId}
              showTrialRecorder={false}
            />
            <SupervisionReplyFeedbackForm
              key={decisionSession.cardInstanceId}
              customerId={customer.id}
              usageToken={decisionSession.usageToken}
              defaultFinalReply={defaultFeedbackFinalReply}
              cardInstanceId={decisionSession.cardInstanceId}
            />
          </>
        ) : null}

        <fieldset>
          <legend>决策卡使用情况</legend>
          <div className="choice-row">
            <label><input type="radio" name="reply-usage" checked={replyUsage === "adopted"} onChange={() => changeReplyUsage("adopted")} />已采用</label>
            <label><input type="radio" name="reply-usage" checked={replyUsage === "edited"} onChange={() => changeReplyUsage("edited")} />已改写</label>
            <label><input type="radio" name="reply-usage" checked={replyUsage === "not_used"} onChange={() => changeReplyUsage("not_used")} />未使用</label>
          </div>
          {replyUsage === "edited" ? (
            <label>
              最终回复
              <textarea
                value={finalEditedText}
                onChange={(event) => setFinalEditedText(event.target.value)}
                required
              />
            </label>
          ) : null}
        </fieldset>

        <div className="form-grid two-columns">
          <label>
            计划跟进时间
            <input type="datetime-local" value={plannedFollowUpAt} onChange={(event) => setPlannedFollowUpAt(event.target.value)} required />
          </label>
          <label>
            实际跟进时间
            <input type="datetime-local" value={actualFollowUpAt} onChange={(event) => setActualFollowUpAt(event.target.value)} />
          </label>
          <label>
            跟进结果
            <select value={result} onChange={(event) => setResult(event.target.value as FollowUpResult)}>
              <option value="pending">待跟进</option>
              <option value="interested">意向中</option>
              <option value="won">已成交</option>
              <option value="lost">未成交</option>
            </select>
          </label>
          <label>
            下一步动作
            <input value={nextAction} onChange={(event) => setNextAction(event.target.value)} required />
          </label>
        </div>

        <button
          className="primary-action"
          type="submit"
          disabled={syncStatus === "saving" || isCurrentSaved}
        >
          {syncStatus === "saving"
            ? "正在同步…"
            : isCurrentSaved ? "已保存" : "保存并同步给老板"}
        </button>
        {syncMessage(visibleSyncStatus)}
      </form>
    </section>
  );
}

type CustomerCreateFormProps = Readonly<{ employeeLabel: string }>;

export function CustomerCreateForm({ employeeLabel }: CustomerCreateFormProps) {
  const [customerAlias, setCustomerAlias] = useState("");
  const [realName, setRealName] = useState("");
  const [category, setCategory] = useState<JewelryCategory>("沉香");
  const [plannedFollowUpAt, setPlannedFollowUpAt] = useState("");
  const [syncStatus, setSyncStatus] = useState<SyncStatus>("idle");
  const [customerId, setCustomerId] = useState<string>();
  const [customerRequestId, setCustomerRequestId] = useState(() => requestId("customer"));
  const [savedFingerprint, setSavedFingerprint] = useState<string>();
  const inputFingerprint = JSON.stringify({
    customerAlias,
    realName,
    category,
    plannedFollowUpAt,
  });
  const visibleSyncStatus: SyncStatus = syncStatus === "saved"
    && savedFingerprint !== inputFingerprint
    ? "idle"
    : syncStatus;
  const isCurrentSaved = visibleSyncStatus === "saved";

  async function createCustomer(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (syncStatus === "saving" || isCurrentSaved) return;
    const planned = toIso(plannedFollowUpAt);
    if (!planned) {
      setSyncStatus("failed");
      return;
    }
    setSyncStatus("saving");
    try {
      const response = await fetch("/api/supervision/customers", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          requestId: customerRequestId,
          customerAlias,
          realName: realName.trim() || undefined,
          category,
          plannedFollowUpAt: planned,
        }),
      });
      const payload = await response.json().catch(() => undefined) as
        | { customer?: { id?: unknown } }
        | undefined;
      if (!response.ok || typeof payload?.customer?.id !== "string") throw new Error("create failed");
      setCustomerId(payload.customer.id);
      setRealName("");
      setSavedFingerprint(JSON.stringify({
        customerAlias,
        realName: "",
        category,
        plannedFollowUpAt,
      }));
      setSyncStatus("saved");
      setCustomerRequestId(requestId("customer"));
    } catch {
      setSyncStatus("failed");
    }
  }

  return (
    <section className="employee-panel narrow-panel">
      <div className="panel-heading">
        <div>
          <p className="eyebrow">安全建档</p>
          <h2>建立客户档案</h2>
        </div>
        <span className="employee-chip">负责人：{employeeLabel}</span>
      </div>
      <p className="privacy-note">页面日常只显示称呼与客户编号；真实姓名只进入保险柜。</p>
      <form className="follow-up-form" onSubmit={createCustomer}>
        <label>客户称呼<input value={customerAlias} onChange={(event) => setCustomerAlias(event.target.value)} required disabled={syncStatus === "saving"} /></label>
        <label>
          真实姓名（仅存保险柜，可选）
          <input value={realName} onChange={(event) => setRealName(event.target.value)} autoComplete="off" disabled={syncStatus === "saving"} />
        </label>
        <label>
          珠宝品类
          <select value={category} onChange={(event) => setCategory(event.target.value as JewelryCategory)} disabled={syncStatus === "saving"}>
            {JEWELRY_CATEGORIES.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label>
          计划跟进时间
          <input type="datetime-local" value={plannedFollowUpAt} onChange={(event) => setPlannedFollowUpAt(event.target.value)} required disabled={syncStatus === "saving"} />
        </label>
        <button
          className="primary-action"
          type="submit"
          disabled={syncStatus === "saving" || isCurrentSaved}
        >
          {syncStatus === "saving" ? "正在建立…" : isCurrentSaved ? "已建立" : "建立客户档案"}
        </button>
        {syncMessage(visibleSyncStatus)}
        {isCurrentSaved && customerId !== undefined ? (
          <div className="created-customer">
            <p>客户已建立</p>
            <a href={`/employee/customers/${encodeURIComponent(customerId)}`}>进入客户跟进</a>
          </div>
        ) : null}
      </form>
    </section>
  );
}
