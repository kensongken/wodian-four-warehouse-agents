import { customerCategoryContext } from "./customer-context";
import type { DecisionCard, DecisionCardRequest } from "./types";
import type { VerificationField } from "./types";
import type { ResolvedOperatorStyle } from "./style";

export type ReviewFocus = {
  field: VerificationField;
  operatorPrompt: string;
};

const OPERATOR_PROMPT: Partial<Record<VerificationField, string>> = {
  material: "请核对材质或处理方式的可引用资料。",
  quality: "请核对品质相关的可引用资料。",
  origin: "请核对产地相关的可引用资料。",
  certificate: "请核对真伪或证书相关的可引用资料。",
  service_policy: "请核对当前适用的服务规则。",
  reuse_or_return_terms: "请核对送回仓验和后续处理规则。",
};

const GENERIC_OPERATOR_PROMPT = "请核对当前相关资料和适用规则。";

const SHORT_SAFE_CONTINUATION = "请人工核验后再确认。";

export function buildSafeContinuation(
  request: DecisionCardRequest,
  __: DecisionCard["verificationNeeded"],
  style: ResolvedOperatorStyle,
): string {
  const greeting = style.preferredGreeting === undefined ? "" : `${style.preferredGreeting}，`;
  const continuation = `${greeting}${customerCategoryContext(request)}我先把您关心的这一点和现有资料整理好，核对后给您明确答复；您可以先把相关图片或页面截图发我。`;

  return continuation.length <= style.maxReplyCharacters
    ? continuation
    : SHORT_SAFE_CONTINUATION;
}

export function selectReviewFocus(
  verification: DecisionCard["verificationNeeded"],
): ReviewFocus | undefined {
  const first = verification[0];

  return first === undefined
    ? undefined
    : {
        field: first.field,
        operatorPrompt: OPERATOR_PROMPT[first.field] ?? GENERIC_OPERATOR_PROMPT,
      };
}
