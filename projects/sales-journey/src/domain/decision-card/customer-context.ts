import type { DecisionCardRequest } from "./types";

const CUSTOMER_REFERENCED_CATEGORIES = [
  "沉香",
  "绿松石",
  "南红",
  "和田玉",
  "彩宝",
  "翡翠",
] as const;

export function customerCategoryContext(
  request: Pick<DecisionCardRequest, "latestCustomerMessage" | "conversation">,
): string {
  const customerText = [
    request.latestCustomerMessage,
    ...request.conversation
      .filter((turn) => turn.speaker === "customer")
      .map((turn) => turn.text),
  ].join("\n");
  const category = CUSTOMER_REFERENCED_CATEGORIES.find((candidate) =>
    customerText.includes(candidate),
  );

  return category === undefined ? "" : `就您提到的${category}，`;
}
