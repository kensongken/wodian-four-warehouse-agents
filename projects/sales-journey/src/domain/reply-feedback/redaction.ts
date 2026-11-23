const NAME = /[\u4e00-\u9fff]{1,4}(?:先生|女士|小姐|老师)/g;
const LABELED_NAME = /((?:客户姓名|姓名|收件人|联系人|称呼)\s*(?::|：|为|是|\s)\s*)[\u4e00-\u9fff]{2,4}(?![\u4e00-\u9fff])/g;
const CUSTOMER_NAMED = /((?:客户)\s*(?:叫|名叫)\s*)[\u4e00-\u9fff]{2,4}(?![\u4e00-\u9fff])/g;
const ADDRESS = /(?:[\u4e00-\u9fff]{2,}(?:省|自治区|特别行政区))?(?:[\u4e00-\u9fff]{1,8}市)?[\u4e00-\u9fff]{1,8}(?:区|县)[\u4e00-\u9fff]{1,16}(?:大街|大道|胡同|路|街|巷|弄|道)\s*\d{1,5}\s*号/g;
const PHONE = /(?<!\d)1[3-9](?:[- ]?\d){9}(?!\d)/g;
const EMAIL = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi;
const CONTACT_MARKER = /(?:微信(?:号)?|WeChat|(?<![A-Z0-9_])wx|电话|手机号|手机|联系人|请联系|麻烦联系|联系客户|联系客服|联系销售|联系您)/i;
const CONTACT_SEGMENT_SEPARATOR = /([。！？.!?；;\r\n]+)/;
const SENSITIVE_DIGITS = /(?<!\d)(?:\d[- ]?){12,18}\d(?!\d)/g;

const DETECTION_SEPARATOR = "[\\s\\u200b\\u200c\\u200d\\u2060\\ufeff\\-\\u2010-\\u2015\\u00b7]*";
const RESIDUAL_PHONE = new RegExp(`(?<!\\d)1[3-9](?:${DETECTION_SEPARATOR}\\d){9}(?!\\d)`);
const RESIDUAL_EMAIL = /[A-Z0-9._%+-]+\s*(?:@|\[\s*at\s*\]|\(\s*at\s*\))\s*[A-Z0-9.-]+(?:\s*[.。]\s*[A-Z]{2,})+/i;
const RESIDUAL_PAYMENT_CARD = new RegExp(`(?<!\\d)(?:\\d${DETECTION_SEPARATOR}){12,18}\\d(?!\\d)`);
const RESIDUAL_CONTACT_MARKER = /(?:联系方式|联系人|微\s*信(?:号)?|W\s*e\s*C\s*h\s*a\s*t|(?<![A-Z0-9_])W\s*X(?=$|[^A-Z0-9_]))/i;
const RESIDUAL_NAME_TITLE = /(?:[\p{Script=Han}]{1,4}|[A-Z][A-Z .'-]{1,40})\s*(?:先生|女士|小姐|老师)/iu;
const PRIVACY_SEGMENT_BOUNDARY = /[。！？.!?；;\r\n]/;
const PUNCTUATION_OR_SPACE_ONLY = /^[\s\p{P}]*$/u;

export const PRIVACY_REASON_CODES = [
  "residual_phone",
  "residual_email",
  "residual_payment_card",
  "residual_address",
  "residual_wechat_or_contact",
  "residual_name",
] as const;

export type PrivacyReasonCode = typeof PRIVACY_REASON_CODES[number];

export type FeedbackPrivacyResult =
  | {
    privacyStatus: "safe";
    privacyReasonCodes: [];
    sanitizedFinalReply: string;
  }
  | {
    privacyStatus: "quarantined";
    privacyReasonCodes: PrivacyReasonCode[];
  };

type ExplicitLabelRule = {
  labels: string;
  canonicalPlaceholder: string;
  reasonCode: PrivacyReasonCode;
};

const EXPLICIT_LABEL_RULES: ExplicitLabelRule[] = [
  {
    labels: "手机号码|备用号码|联系电话|手机号|电话|手机|号码",
    canonicalPlaceholder: "[手机号]",
    reasonCode: "residual_phone",
  },
  {
    labels: "电子邮箱|邮箱|邮件",
    canonicalPlaceholder: "[邮箱]",
    reasonCode: "residual_email",
  },
  {
    labels: "银行卡|支付卡|卡号",
    canonicalPlaceholder: "[敏感数字]",
    reasonCode: "residual_payment_card",
  },
  {
    labels: "地址|寄到|收货",
    canonicalPlaceholder: "[地址]",
    reasonCode: "residual_address",
  },
  {
    labels: "客户姓名|收件人|姓名|称呼",
    canonicalPlaceholder: "[姓名]",
    reasonCode: "residual_name",
  },
];

const CLIENT_PLACEHOLDER_RULES: Array<{
  pattern: RegExp;
  reasonCode: PrivacyReasonCode;
}> = [
  { pattern: /\[\s*手机号\s*\]/, reasonCode: "residual_phone" },
  { pattern: /\[\s*邮箱\s*\]/, reasonCode: "residual_email" },
  { pattern: /\[\s*敏感数字\s*\]/, reasonCode: "residual_payment_card" },
  { pattern: /\[\s*地址\s*\]/, reasonCode: "residual_address" },
  { pattern: /\[\s*联系信息\s*\]/, reasonCode: "residual_wechat_or_contact" },
  { pattern: /\[\s*姓名\s*\]/, reasonCode: "residual_name" },
];

function normalizeForPrivacyChecks(text: string): string {
  return text
    .normalize("NFKC")
    .replace(/\p{Default_Ignorable_Code_Point}/gu, "");
}

function orderedReasonCodes(reasons: Iterable<PrivacyReasonCode>): PrivacyReasonCode[] {
  const reasonSet = new Set(reasons);
  return PRIVACY_REASON_CODES.filter((reasonCode) => reasonSet.has(reasonCode));
}

function redactContactSegments(text: string): string {
  return text
    .split(CONTACT_SEGMENT_SEPARATOR)
    .map((segment) => (CONTACT_MARKER.test(segment) ? "[联系信息]" : segment))
    .join("");
}

export function redactFeedbackText(text: string): string {
  const withoutEmails = text.replace(EMAIL, "[邮箱]");

  return redactContactSegments(withoutEmails)
    .replace(LABELED_NAME, "$1[姓名]")
    .replace(CUSTOMER_NAMED, "$1[姓名]")
    .replace(NAME, "[姓名]")
    .replace(ADDRESS, "[地址]")
    .replace(PHONE, "[手机号]")
    .replace(SENSITIVE_DIGITS, "[敏感数字]");
}

function hasUnsafeExplicitLabel(text: string, rule: ExplicitLabelRule): boolean {
  const labelPattern = new RegExp(
    `(?<!\\[)(?:${rule.labels})\\s*(?:(?:[:：]|为|是)\\s*)?`,
    "gu",
  );

  for (const match of text.matchAll(labelPattern)) {
    const remainingText = text.slice((match.index ?? 0) + match[0].length);
    const boundaryIndex = remainingText.search(PRIVACY_SEGMENT_BOUNDARY);
    const valueSegment = (boundaryIndex === -1
      ? remainingText
      : remainingText.slice(0, boundaryIndex)).trim();

    if (!valueSegment.startsWith(rule.canonicalPlaceholder)) return true;
    const trailingText = valueSegment.slice(rule.canonicalPlaceholder.length);
    if (!PUNCTUATION_OR_SPACE_ONLY.test(trailingText)) return true;
  }

  return false;
}

function detectClientProvidedPlaceholders(normalizedInput: string): PrivacyReasonCode[] {
  return orderedReasonCodes(CLIENT_PLACEHOLDER_RULES
    .filter(({ pattern }) => pattern.test(normalizedInput))
    .map(({ reasonCode }) => reasonCode));
}

function detectNormalizedFirstStageGap(
  sanitizedFinalReply: string,
  normalizedSanitizedFinalReply: string,
): PrivacyReasonCode[] {
  const normalizedOriginalSanitization = normalizeForPrivacyChecks(sanitizedFinalReply);
  if (normalizedOriginalSanitization === normalizedSanitizedFinalReply) return [];

  const reasons = detectResidualPrivacy(normalizedOriginalSanitization);
  for (const { pattern, reasonCode } of CLIENT_PLACEHOLDER_RULES) {
    if (pattern.test(normalizedSanitizedFinalReply) && !pattern.test(normalizedOriginalSanitization)) {
      reasons.push(reasonCode);
    }
  }

  return orderedReasonCodes(reasons);
}

function detectResidualPrivacy(text: string): PrivacyReasonCode[] {
  const detectionText = normalizeForPrivacyChecks(text);
  const reasons: PrivacyReasonCode[] = [];

  if (RESIDUAL_PHONE.test(detectionText)) {
    reasons.push("residual_phone");
  }
  if (RESIDUAL_EMAIL.test(detectionText)) {
    reasons.push("residual_email");
  }
  if (RESIDUAL_PAYMENT_CARD.test(detectionText)) {
    reasons.push("residual_payment_card");
  }
  if (RESIDUAL_CONTACT_MARKER.test(detectionText)) {
    reasons.push("residual_wechat_or_contact");
  }
  if (RESIDUAL_NAME_TITLE.test(detectionText)) {
    reasons.push("residual_name");
  }

  for (const rule of EXPLICIT_LABEL_RULES) {
    if (hasUnsafeExplicitLabel(detectionText, rule)) reasons.push(rule.reasonCode);
  }

  return orderedReasonCodes(reasons);
}

export function sanitizeFeedbackFinalReply(text: string): FeedbackPrivacyResult {
  const normalizedInput = normalizeForPrivacyChecks(text);
  const sanitizedFinalReply = redactFeedbackText(text);
  const normalizedSanitizedFinalReply = redactFeedbackText(normalizedInput);
  const privacyReasonCodes = orderedReasonCodes([
    ...detectClientProvidedPlaceholders(normalizedInput),
    ...detectNormalizedFirstStageGap(sanitizedFinalReply, normalizedSanitizedFinalReply),
    ...detectResidualPrivacy(sanitizedFinalReply),
  ]);

  if (privacyReasonCodes.length > 0) {
    return { privacyStatus: "quarantined", privacyReasonCodes };
  }

  return {
    privacyStatus: "safe",
    privacyReasonCodes: [],
    sanitizedFinalReply,
  };
}
