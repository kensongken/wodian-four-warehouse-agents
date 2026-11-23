import { join } from "node:path";

import {
  ConfiguredAnalyzer,
  createLocalDispatchRoster,
  JsonContentStore,
  LocalDispatchMailbox,
  type CreativeScriptAgent,
  type DeviceCapability,
  type EditPlanAnalyzer,
  type IntentInterpreter,
  type MaterialMatcher,
  type PromptEditInterpreter,
  type SegmentTagAnalyzer,
  type ShotPlanningAgent,
  type StoryAnalyzer,
} from "@wearable/mobile-agent";

import {
  createReviewServer,
  listenReviewServer,
  type CreativeReviewDependencies,
} from "./server";

const port = parsePort(process.env.REVIEW_APP_PORT);
const library = await JsonContentStore.open(
  process.env.CONTENT_LIBRARY_PATH ?? join(process.cwd(), "content-library.json"),
);
const interpreter: PromptEditInterpreter = {
  interpret: async ({ prompt, revision, allowedSegments }) =>
    ConfiguredAnalyzer.create({
      endpoint: process.env.EDITOR_MODEL_ENDPOINT,
      model: process.env.EDITOR_MODEL_NAME,
      apiKey: process.env.EDITOR_MODEL_API_KEY,
    }).interpretPrompt({ prompt, revision, segments: allowedSegments }),
};
const server = createReviewServer({
  library,
  interpreter,
  outputDir:
    process.env.REVIEW_RENDER_DIR ?? join(process.cwd(), "review-renders"),
  designerId: process.env.REVIEW_DESIGNER_ID ?? "designer_01",
  reviewToken: process.env.REVIEW_SESSION_TOKEN,
  customerCredentials: configuredCustomerCredentials(),
  creative: deterministicCreativeDependencies(),
});

await listenReviewServer(server, port, "127.0.0.1");
process.stdout.write(`Review workbench: http://127.0.0.1:${port}\n`);

function parsePort(value: string | undefined): number {
  if (!value) return 4173;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65_535) {
    throw new Error("invalid-review-app-port");
  }
  return parsed;
}

function configuredCustomerCredentials(): Readonly<Record<string, string>> {
  const customerId = process.env.REVIEW_CUSTOMER_ID;
  const customerToken = process.env.REVIEW_CUSTOMER_TOKEN;
  if ((customerId && !customerToken) || (!customerId && customerToken)) {
    throw new Error("customer-credential-config-incomplete");
  }
  return customerId && customerToken ? { [customerId]: customerToken } : {};
}

function deterministicCreativeDependencies(): CreativeReviewDependencies {
  const deviceCapabilities: DeviceCapability[] = [
    { deviceId: "watch_sim_01", deviceRole: "watch-director", carrier: "watch" },
    { deviceId: "stone_a_sim_01", deviceRole: "main-stone-a", carrier: "ring" },
    { deviceId: "stone_b_sim_01", deviceRole: "main-stone-b", carrier: "pendant" },
  ];
  const mailboxes = {
    "watch-director": new LocalDispatchMailbox("watch_sim_01"),
    "main-stone-a": new LocalDispatchMailbox("stone_a_sim_01"),
    "main-stone-b": new LocalDispatchMailbox("stone_b_sim_01"),
  };
  const privacyDevices = deviceCapabilities.map(({ deviceId }) => {
    let paused = false;
    return {
      deviceId,
      pausePrivacy() { paused = true; },
      resumePrivacy() { paused = false; },
      isPrivacyPaused() { return paused; },
    };
  });
  return {
    intentInterpreter: deterministicIntentInterpreter(),
    materialMatcher: deterministicMaterialMatcher(),
    scriptAgent: deterministicScriptAgent(),
    shotPlanningAgent: deterministicShotPlanningAgent(),
    editorial: deterministicEditorialAnalyzers(),
    creativeDeviceAdapters: {
      deviceCapabilities,
      privacyDevices,
      createRoster: (version) => createLocalDispatchRoster({ mailboxes, version }).roster,
    },
  };
}

function deterministicEditorialAnalyzers() {
  const segmentAnalyzer = { analyze: async () => [{
    startMs: 0,
    endMs: 1_200,
    transcript: "我觉得手工应该留下痕迹，我决定坚持再做一次。",
    signals: [] as const,
  }] };
  const tagAnalyzer: SegmentTagAnalyzer = { suggest: async (segment) => ({
    baseTypes: ["persona-copy", "handmade"], people: [], products: [],
    processStages: ["rework"], topics: ["craft"], emotions: ["hesitation", "persistence"],
    transcript: segment.transcript, technicalFlags: [],
    usage: { storyIds: [], variants: [], useCount: 0, lastUsedAt: null },
  }) };
  const storyAnalyzer: StoryAnalyzer = { propose: async (segments) => [
    { type: "persona-monologue", anchorSegmentIds: segments.map((item) => item.id) },
    { type: "inner-journey", anchorSegmentIds: segments.map((item) => item.id) },
    { type: "product-journey", anchorSegmentIds: segments.map((item) => item.id) },
  ] };
  const editAnalyzer: EditPlanAnalyzer = { plan: async ({ story }) => {
    const timeline = (outMs: number) => story.anchorSegmentIds.map((segmentId) => ({
      segmentId, inMs: 0, outMs, role: "anchor" as const,
    }));
    const cover = { segmentId: story.anchorSegmentIds[0]!, atMs: 100 };
    return {
      master: { timeline: timeline(400), title: "A-0 原视频", caption: "本地模拟", cover },
      xiaohongshu: { timeline: timeline(600), title: "A-0 小红书", caption: "本地模拟", cover },
      douyin: { timeline: timeline(700), title: "A-0 抖音", caption: "本地模拟", cover },
      wechatChannels: { timeline: timeline(800), title: "A-0 视频号", caption: "本地模拟", cover },
      stills: [],
    };
  } };
  return { segmentAnalyzer, tagAnalyzer, storyAnalyzer, editAnalyzer };
}

function deterministicIntentInterpreter(): IntentInterpreter {
  return {
    interpret: async ({ text }) => ({
      platforms: ["wechat-channels"],
      audience: "Jewelry collectors",
      theme: text.trim(),
      products: [],
      shootConditions: ["Local synthetic A-0 workbench"],
      prohibitedAreas: ["Customer faces and sensitive order information"],
      tone: "clear and factual",
      durationSeconds: 30,
    }),
  };
}

function deterministicMaterialMatcher(): MaterialMatcher {
  return {
    rank: async ({ segments }) => [...segments]
      .sort((left, right) => left.id.localeCompare(right.id))
      .map((segment, index) => ({
        segmentId: segment.id,
        reason: "Eligible local historical material",
        score: segments.length - index,
      })),
  };
}

function deterministicScriptAgent(): CreativeScriptAgent {
  const makeDraft = (
    opening: string,
    selectedSegmentIds: string[],
    changeExplanation: string | null,
  ) => ({
    title: "Local creative script",
    platformDirection: "wechat-channels",
    opening,
    narrativeOrder: [
      "customer-intent",
      ...selectedSegmentIds.map((_, index) => `historical-${index + 1}`),
    ],
    sections: [
      {
        id: "customer-intent",
        expression: opening,
        source: "customer-input" as const,
        referencedSegmentIds: [],
      },
      ...selectedSegmentIds.map((segmentId, index) => ({
        id: `historical-${index + 1}`,
        expression: "Use only this customer-confirmed historical segment as evidence.",
        source: "historical-material" as const,
        referencedSegmentIds: [segmentId],
      })),
    ],
    changeExplanation,
  });
  return {
    draft: async ({ intent, selectedHistoricalSegments }) => makeDraft(
      intent.theme,
      selectedHistoricalSegments.map((segment) => segment.id),
      null,
    ),
    revise: async ({ prompt, selectedHistoricalSegments }) => makeDraft(
      prompt.trim(),
      selectedHistoricalSegments.map((segment) => segment.id),
      "Applied the customer's local rewrite transcript or prompt.",
    ),
  };
}

function deterministicShotPlanningAgent(): ShotPlanningAgent {
  return {
    plan: async ({ script }) => ({
      steps: [{
        purpose: script.title,
        expressionSuggestion: script.opening,
        visualDirection: "Capture a synthetic jewelry workbench close-up.",
        durationSeconds: 30,
        mustCapture: ["The locally staged jewelry item"],
        optionalGapSuggestions: [{
          suggestion: "Add a second close-up only if convenient.",
          required: false,
        }],
        completionCriteria: ["One manually reviewable local take"],
        deviceRoles: ["watch-director", "main-stone-a", "main-stone-b"],
        privacyReminder: "Pause if a person or sensitive area enters frame.",
      }],
    }),
  };
}
