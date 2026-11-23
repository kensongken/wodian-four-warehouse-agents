import type { ContentSegment, EditRevision } from "@wearable/contracts";

import { PromptEditSchema, type PromptEdit } from "./prompt-revision";

export type AnalyzerConfig = Readonly<{
  endpoint: string;
  model: string;
  apiKey: string;
}>;

export class ConfiguredAnalyzer {
  private constructor(private readonly config: AnalyzerConfig) {}

  static create(config: Partial<AnalyzerConfig>): ConfiguredAnalyzer {
    const endpoint = config.endpoint?.trim();
    const model = config.model?.trim();
    const apiKey = config.apiKey?.trim();
    if (!endpoint || !model || !apiKey) {
      throw new Error("production-model-unconfigured");
    }
    return new ConfiguredAnalyzer({ endpoint, model, apiKey });
  }

  async interpretPrompt(input: Readonly<{
    prompt: string;
    revision: EditRevision;
    segments: ContentSegment[];
  }>): Promise<PromptEdit> {
    const segments = input.segments
      .filter((segment) => segment.permissionState === "allowed")
      .map((segment) => ({
        id: segment.id,
        assetId: segment.assetId,
        batchId: segment.batchId,
        startMs: segment.startMs,
        endMs: segment.endMs,
        permissionState: segment.permissionState,
        tags: segment.tags,
      }));
    const response = await fetch(this.config.endpoint, {
      method: "POST",
      headers: {
        authorization: `Bearer ${this.config.apiKey}`,
        "content-type": "application/json",
      },
      body: JSON.stringify({
        model: this.config.model,
        input: {
          prompt: input.prompt,
          revision: input.revision,
          segments,
        },
      }),
    });
    if (!response.ok) {
      throw new Error(`production-model-request-failed:${response.status}`);
    }
    return PromptEditSchema.parse(await response.json());
  }
}
