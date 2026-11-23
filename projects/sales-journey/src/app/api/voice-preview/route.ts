import { z } from "zod";

import { DecisionCardSchema } from "@/domain/decision-card/schema";
import { VoicePreviewError, synthesizeLocalPreview } from "@/server/voice/local-speech";

type VoicePreviewRequest = { card: unknown };

const VoicePreviewRequestSchema = z.object({ card: z.unknown() }).strict();
const INVALID_REQUEST_ERROR = "INVALID_VOICE_PREVIEW_REQUEST";
const VOICE_ERROR_MESSAGE = "本机未找到可用的中文女声。";

function invalidRequest(): Response {
  return Response.json({ error: INVALID_REQUEST_ERROR }, { status: 400 });
}

function voiceError(error: VoicePreviewError): Response {
  return Response.json(
    { error: error.code, message: VOICE_ERROR_MESSAGE },
    { status: error.code === "CHINESE_FEMALE_VOICE_UNAVAILABLE" ? 503 : 500 },
  );
}

function contentDisposition(fileName: string): string {
  const extension = fileName.endsWith(".aiff") ? "aiff" : "m4a";
  return `inline; filename="sales-reply.${extension}"; filename*=UTF-8''${encodeURIComponent(fileName)}`;
}

/**
 * Generates an ephemeral local preview exclusively from a validated copy-only
 * decision card. It does not accept or inspect arbitrary text or deliver audio.
 */
export async function POST(request: Request): Promise<Response> {
  let body: VoicePreviewRequest;

  try {
    const parsedRequest = VoicePreviewRequestSchema.safeParse(await request.json());
    if (!parsedRequest.success) {
      return invalidRequest();
    }
    body = parsedRequest.data;
  } catch {
    return invalidRequest();
  }

  const parsedCard = DecisionCardSchema.safeParse(body.card);
  if (
    !parsedCard.success ||
    parsedCard.data.deliveryMode !== "copy_only" ||
    parsedCard.data.replyOptions[0] === undefined
  ) {
    return invalidRequest();
  }

  try {
    const audio = await synthesizeLocalPreview(parsedCard.data.replyOptions[0].text, "warm");
    return new Response(audio.audio as unknown as BodyInit, {
      headers: {
        "content-type": audio.contentType,
        "content-disposition": contentDisposition(audio.fileName),
        "x-voice-downloadable": String(audio.downloadable),
      },
    });
  } catch (error) {
    if (error instanceof VoicePreviewError) {
      return voiceError(error);
    }

    return voiceError(new VoicePreviewError("VOICE_GENERATION_FAILED"));
  }
}
