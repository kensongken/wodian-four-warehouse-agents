import type { DecisionCardRequest } from "./types";

export type ResolvedOperatorStyle = {
  tone: "professional" | "warm" | "direct";
  maxReplyCharacters: number;
  preferredGreeting?: string;
};

const DEFAULT_STYLE: ResolvedOperatorStyle = { tone: "warm", maxReplyCharacters: 180 };
const GREETINGS = ["亲", "您好", "宝", "您"] as const;
const GREETING_SUFFIXES = ["，", ",", "！", "!"] as const;
const MIN_SAFE_REPLY_CHARACTERS = 10;

function isApprovedGreeting(greeting: string | undefined): greeting is (typeof GREETINGS)[number] {
  return GREETINGS.some((approvedGreeting) => approvedGreeting === greeting);
}

function beginsWithGreeting(text: string, greeting: (typeof GREETINGS)[number]): boolean {
  const trimmed = text.trimStart();

  return GREETING_SUFFIXES.some((suffix) => trimmed.startsWith(`${greeting}${suffix}`));
}

export function resolveOperatorStyle({
  conversation,
  operatorStyle,
}: Pick<DecisionCardRequest, "conversation" | "operatorStyle">): ResolvedOperatorStyle {
  if (operatorStyle !== undefined) {
    const style = {
      tone: operatorStyle.tone,
      maxReplyCharacters: Math.max(
        operatorStyle.maxReplyCharacters,
        MIN_SAFE_REPLY_CHARACTERS,
      ),
    };

    return isApprovedGreeting(operatorStyle.preferredGreeting)
      ? { ...style, preferredGreeting: operatorStyle.preferredGreeting }
      : style;
  }

  const operatorTurns = conversation.filter((turn) => turn.speaker === "operator");
  const preferredGreeting = GREETINGS.find((greeting) =>
    operatorTurns.some((turn) => beginsWithGreeting(turn.text, greeting)),
  );

  return preferredGreeting === undefined ? DEFAULT_STYLE : { ...DEFAULT_STYLE, preferredGreeting };
}
