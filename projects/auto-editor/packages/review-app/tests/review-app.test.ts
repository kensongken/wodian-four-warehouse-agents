import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createContext, runInContext } from "node:vm";

import type {
  ContentSegment,
  EditRevision,
  StoryCandidate,
  VariantArtifact,
} from "@wearable/contracts";
import {
  JsonContentStore,
  calculateRenderInputDigest,
  type PromptEditInterpreter,
} from "@wearable/mobile-agent";
import request from "supertest";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { createReviewServer } from "../src/server";

const listeningServers = new Set<ReturnType<typeof createReviewServer>>();

describe("designer review app", () => {
  let testDirectory: string;
  let library: JsonContentStore;

  beforeEach(async () => {
    testDirectory = await mkdtemp(join(tmpdir(), "review-app-"));
    library = await JsonContentStore.open(join(testDirectory, "library.json"));
    await library.putSegments([
      makeSegment("new_quote_01"),
      makeSegment("new_process_01"),
      makeSegment("old_quote_01", "batch_history"),
    ]);
    for (const id of ["new_quote_01", "new_process_01", "old_quote_01"]) {
      const path = join(testDirectory, `asset_${id}.mp4`);
      await writeFile(path, `source-${id}`);
      await library.putAsset({
        id: `asset_${id}`, batchId: id === "old_quote_01" ? "batch_history" : "batch_new",
        sessionId: "session_01", digest: digestText(`source-${id}`), localPath: path,
        mediaKind: "video", importedAt: "2026-09-04T00:00:00.000Z",
        permissionState: "allowed", origin: id === "old_quote_01" ? "historical" : "new",
        technicalFlags: [],
      });
    }
    await library.putStories([makeStory()]);
    await library.putRevision(makeRevision());
  });

  afterEach(async () => {
    for (const server of listeningServers) {
      await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    }
    listeningServers.clear();
    await rm(testDirectory, { recursive: true, force: true });
  });

  it("shows the creative flow, three production views, and the app profile", async () => {
    const app = await createApp(library, testDirectory);
    const html = await request(app).get("/").expect(200);

    expect(html.text).toContain("创作");
    expect(html.text).toContain("素材");
    expect(html.text).toContain("审片");
    expect(html.text).toContain("发布");
    expect(html.text).toContain("我的");
    expect((html.text.match(/data-view=/g) ?? [])).toHaveLength(5);
  });

  it("keeps the full editorial workflow clear inside the app shell", async () => {
    const app = await createApp(library, testDirectory);
    const html = await request(app).get("/").expect(200);
    const javascript = await request(app).get("/app.js").expect(200);

    expect(html.text).toContain("今日待审");
    expect(html.text).toContain("生产进度");
    expect(html.text).toContain("EDITORIAL DESK");
    expect(javascript.text).toContain("原视频");
    expect(javascript.text).toContain("给总编下指令");
    expect(javascript.text).toContain("加强产品镜头");
  });

  it("presents the review flow as a mobile app instead of a desktop dashboard", async () => {
    const app = await createApp(library, testDirectory);
    const html = await request(app).get("/").expect(200);

    expect(html.text).toContain('aria-label="App 底部导航"');
    expect(html.text).toContain("今日待审");
    expect(html.text).toContain("生产进度");
    expect(html.text).not.toContain("总编驾驶舱");
  });

  it("shows one platform cut at a time with app-style edit and approval actions", async () => {
    const context = await loadReviewUiContext();
    const html = runInContext(`(() => {
      selectedVariantKind = "editorial-master";
      currentApprovedData = { approvals: [] };
      return storyDetail({
        id: "story_01",
        type: "product-journey",
        revisions: [{ id: "revision_01", revision: 1, variants: [
          { kind: "editorial-master", title: "原片", caption: "完整故事", timeline: [{ inMs: 0, outMs: 1000 }] },
          { kind: "xiaohongshu", title: "小红书", caption: "种草版", timeline: [{ inMs: 0, outMs: 800 }] },
          { kind: "wechat-channels", title: "视频号", caption: "口播版", timeline: [{ inMs: 0, outMs: 900 }] }
        ] }],
        artifacts: [
          { id: "artifact_master", revisionId: "revision_01", kind: "editorial-master", status: "active" },
          { id: "artifact_xhs", revisionId: "revision_01", kind: "xiaohongshu", status: "active" },
          { id: "artifact_channels", revisionId: "revision_01", kind: "wechat-channels", status: "active" }
        ],
        lineage: { new: [], history: [] },
        diffs: []
      });
    })()`, context) as string;

    expect(html).toContain('role="tablist"');
    expect(html).toContain('role="tabpanel"');
    expect(html).toContain('aria-controls="variant-panel-editorial-master"');
    expect(html).toContain('tabindex="0"');
    expect(html).toContain('role="dialog"');
    expect(html).toContain('aria-modal="true"');
    expect(html).toContain("要求改剪");
    expect(html).toContain("通过此版本");
    expect((html.match(/<video/g) ?? [])).toHaveLength(1);
  });

  it("shows prompt failures beside the submit action and restores the button", async () => {
    const context = await loadReviewUiContext();
    const feedback = { textContent: "" };
    const button = { disabled: false, textContent: "生成新版本" };
    const form = {
      dataset: { story: "story_01" },
      prompt: { value: "加强产品镜头" },
      querySelector: (selector: string) => selector === ".action-feedback" ? feedback : button,
    };
    Object.assign(context, { testForm: form });

    await runInContext(`api = async () => { throw new Error("剪辑服务暂时不可用") };
      submitPrompt({ preventDefault() {}, currentTarget: testForm })`, context);

    expect(feedback.textContent).toContain("剪辑服务暂时不可用");
    expect(button.disabled).toBe(false);
  });

  it("shows approval failures beside the approval action and restores the button", async () => {
    const context = await loadReviewUiContext();
    const feedback = { textContent: "" };
    const dock = { querySelector: () => feedback };
    const button = { disabled: false, textContent: "通过此版本", dataset: { approve: "artifact_01" }, closest: () => dock };
    Object.assign(context, { testButton: button });

    await runInContext(`api = async () => { throw new Error("审批记录失败") };
      approveArtifact({ currentTarget: testButton })`, context);

    expect(feedback.textContent).toContain("审批记录失败");
    expect(button.disabled).toBe(false);
  });

  it("closes the edit dialog with Escape and restores focus to its opener", async () => {
    const context = await loadReviewUiContext();
    let hidden = "false";
    let focused = false;
    const panel = { classList: { remove() {} }, setAttribute: (_name: string, value: string) => { hidden = value; } };
    const trigger = { focus: () => { focused = true; } };
    Object.assign(context, { testPanel: panel, testTrigger: trigger });

    runInContext(`editorTrigger = testTrigger;
      handleEditorKeydown({ key: "Escape", preventDefault() {}, currentTarget: testPanel })`, context);

    expect(hidden).toBe("true");
    expect(focused).toBe(true);
  });

  it("does not offer an invalidated render for approval", async () => {
    const context = await loadReviewUiContext();
    const html = runInContext(`storyDetail({
      id: "story_01",
      type: "product-journey",
      revisions: [{ id: "revision_01", revision: 1, variants: [
        { kind: "editorial-master", title: "旧版本", caption: "已被改剪替代", timeline: [{ inMs: 0, outMs: 1000 }] }
      ] }],
      artifacts: [{ id: "artifact_old", revisionId: "revision_01", kind: "editorial-master", status: "invalidated" }],
      lineage: { new: [], history: [] },
      diffs: []
    })`, context) as string;

    expect(html).toContain("已失效");
    expect(html).not.toContain("data-approve");
  });

  it("scopes pipeline stories and approvals to the selected batch", async () => {
    const context = await loadReviewUiContext();
    const scoped = runInContext(`scopeProductionData(
      { stories: [{ id: "story_current", batchId: "batch_new" }] },
      { stories: [{ id: "story_current", batchId: "batch_new" }, { id: "story_old", batchId: "batch_old" }] },
      { approvals: [{ id: "approval_current", storyId: "story_current" }, { id: "approval_old", storyId: "story_old" }] }
    )`, context) as { review: { stories: Array<{ id: string }> }; approved: { approvals: Array<{ id: string }> } };

    expect(scoped.review.stories.map((story) => story.id)).toEqual(["story_current"]);
    expect(scoped.approved.approvals.map((approval) => approval.id)).toEqual(["approval_current"]);
  });

  it("labels approved content from the story record instead of guessing from its id", async () => {
    const context = await loadReviewUiContext();
    const html = runInContext(`(() => {
      currentReviewData = { stories: [{ id: "story_batch_new_1", type: "product-journey" }] };
      return approvalCard({ storyId: "story_batch_new_1", variant: "wechat-channels", revision: 1, packages: [] });
    })()`, context) as string;

    expect(html).toContain("产品历程 · V1");
    expect(html).not.toContain("内容故事 · V1");
  });

  it("creates a rendered revision through a prompt and never approves it implicitly", async () => {
    const app = await createApp(library, testDirectory);
    const response = await request(app)
      .post("/api/stories/story_01/revisions")
      .set("x-review-token", "test-token")
      .send({
        prompt: "视频号版开头换成去年返工口播",
        designerId: "designer_01",
      })
      .expect(201);

    expect(response.body.revision.revision).toBe(2);
    expect(response.body.artifacts).toHaveLength(4);
    expect(JSON.stringify(response.body)).not.toContain(testDirectory);
    for (const forbidden of ["videoPath", "coverPath", "stillPaths"]) {
      expect(JSON.stringify(response.body)).not.toContain(`\"${forbidden}\"`);
    }
    expect(await library.listApprovals()).toEqual([]);
    expect(await library.listArtifacts("revision_story_01_2")).toHaveLength(4);
  });

  it("keeps approval as a separate explicit action", async () => {
    const app = await createApp(library, testDirectory);
    const revisionResponse = await request(app)
      .post("/api/stories/story_01/revisions")
      .set("x-review-token", "test-token")
      .send({ prompt: "change opening", designerId: "designer_01" })
      .expect(201);
    const artifact = revisionResponse.body.artifacts.find(
      (item: VariantArtifact) => item.kind === "wechat-channels",
    );

    const response = await request(app)
      .post(`/api/artifacts/${artifact.id}/approve`)
      .set("x-review-token", "test-token")
      .send({ reviewerId: "designer_01" })
      .expect(201);

    expect(response.body.approval).toMatchObject({
      revision: 2,
      variant: "wechat-channels",
      status: "active",
    });
    expect(await library.listApprovals()).toHaveLength(1);
  });

  it("exports an approved legacy revision using identities only and resolves every file path server-side", async () => {
    const app = await createApp(library, testDirectory);
    const revisionResponse = await request(app)
      .post("/api/stories/story_01/revisions")
      .set("x-review-token", "test-token")
      .send({ prompt: "prepare a legacy export", designerId: "designer_01" })
      .expect(201);
    const artifact = revisionResponse.body.artifacts.find(
      (item: VariantArtifact) => item.kind === "wechat-channels",
    );
    const otherArtifact = revisionResponse.body.artifacts.find(
      (item: VariantArtifact) => item.kind === "xiaohongshu",
    );
    const approved = await request(app)
      .post(`/api/artifacts/${artifact.id}/approve`)
      .set("x-review-token", "test-token")
      .send({})
      .expect(201);

    const exported = await request(app)
      .post(`/api/approvals/${approved.body.approval.id}/export`)
      .set("x-review-token", "test-token")
      .send({ artifactId: artifact.id, revisionId: revisionResponse.body.revision.id })
      .expect(201);

    expect(exported.body.package).toMatchObject({
      approvalId: approved.body.approval.id,
      artifactId: artifact.id,
    });
    expect(JSON.stringify(exported.body)).not.toContain(testDirectory);
    for (const forbidden of ["videoPath", "coverPath", "stillPaths"]) {
      expect(JSON.stringify(exported.body)).not.toContain(`\"${forbidden}\"`);
    }
    await request(app)
      .post(`/api/approvals/${approved.body.approval.id}/export`)
      .set("x-review-token", "test-token")
      .send({ artifactId: otherArtifact.id, revisionId: revisionResponse.body.revision.id })
      .expect(400, { error: "approval-artifact-mismatch" });
    await request(app)
      .post(`/api/approvals/${approved.body.approval.id}/export`)
      .set("x-review-token", "test-token")
      .send({ artifactId: artifact.id, revisionId: revisionResponse.body.revision.id,
        coverPath: "/client/guessed-cover.jpg", stillPaths: ["/client/guessed-still.jpg"] })
      .expect(400, { error: "export-path-input-forbidden" });
  });

  it("serves only a registered artifact through the local preview route", async () => {
    const app = await createApp(library, testDirectory);
    const revisionResponse = await request(app)
      .post("/api/stories/story_01/revisions")
      .set("x-review-token", "test-token")
      .send({ prompt: "preview it", designerId: "designer_01" })
      .expect(201);
    const artifact = revisionResponse.body.artifacts[0] as VariantArtifact;

    const response = await request(app)
      .get(`/api/artifacts/${artifact.id}/media`)
      .set("host", "127.0.0.1")
      .set("referer", "http://127.0.0.1/")
      .set("x-review-token", "test-token")
      .expect(200)
      .expect("content-type", "video/mp4");

    expect(response.body.length).toBeGreaterThan(0);
  });

  it("exposes the four JSON business views", async () => {
    const app = await createApp(library, testDirectory);

    const batch = (await request(app).get("/api/batches/batch_new").expect(200)).body;
    const libraryView = (await request(app).get("/api/library").expect(200)).body;
    expect(batch).toHaveProperty("assets");
    expect(libraryView).toHaveProperty("segments");
    for (const value of [batch, libraryView]) {
      expect(JSON.stringify(value)).not.toContain(testDirectory);
      expect(JSON.stringify(value)).not.toContain('"localPath"');
    }
    expect((await request(app).get("/api/review").expect(200)).body).toHaveProperty("stories");
    expect((await request(app).get("/api/approved").expect(200)).body).toHaveProperty("approvals");
  });

  it("does not disclose the review token to a rebound host", async () => {
    const app = await createApp(library, testDirectory);
    await request(app).get("/api/session").set("host", "attacker.example").expect(400);
    await request(app).get("/api/session")
      .set("host", "127.0.0.1")
      .set("origin", "http://attacker.example")
      .expect(400);
  });
});

async function loadReviewUiContext() {
  const workspace = fakeElement();
  const elements = new Map([
    ["#workspace", workspace],
    ["#status", fakeElement()],
    ["#batch-id", { ...fakeElement(), value: "batch_new" }],
    ["#pipeline", fakeElement()],
    ["#view-title", fakeElement()],
    ["#view-description", fakeElement()],
    ["#today-label", fakeElement()],
    ["#refresh", fakeElement()],
    ['[data-view="review"]', fakeElement()],
  ]);
  const document = {
    querySelector: (selector: string) => elements.get(selector) ?? fakeElement(),
    querySelectorAll: () => [],
  };
  const context = createContext({ document, window: { confirm: () => true }, Intl, Date, encodeURIComponent, fetch: async () => ({ ok: true, json: async () => ({}) }) });
  const source = await readFile(join(process.cwd(), "packages/review-app/public/app.js"), "utf8");
  runInContext(source, context);
  return context;
}

function fakeElement() {
  return {
    textContent: "",
    innerHTML: "",
    value: "",
    dataset: {},
    classList: { add() {}, remove() {} },
    addEventListener() {},
    click() {},
    querySelectorAll: () => [],
    querySelector: () => fakeElement(),
    focus() {},
  };
}

async function createApp(library: JsonContentStore, testDirectory: string) {
  const interpreter: PromptEditInterpreter = {
    interpret: async () => ({
      targetVariants: ["wechat-channels"],
      replacementTimelines: {
        "wechat-channels": [
          timelineItem("old_quote_01"),
          timelineItem("new_process_01"),
        ],
      },
      removePeople: [],
      explanation: "use historical rework quote",
    }),
  };
  const app = createReviewServer({
    library,
    interpreter,
    reviewToken: "test-token",
    designerId: "designer_01",
    outputDir: join(testDirectory, "renders"),
    renderer: async (revision, segments, assets) => {
      const artifacts = revision.variants.map((variant) => ({
        id: `artifact_${revision.id}_${variant.kind}`,
        revisionId: revision.id,
        kind: variant.kind,
        videoPath: join(testDirectory, `${variant.kind}.mp4`),
        digest: `digest_${revision.id}_${variant.kind}`,
        sourceSegmentIds: [...new Set(variant.timeline.map((item) => item.segmentId))],
        coverPath: join(testDirectory, `cover-${variant.kind}.jpg`),
        coverDigest: digestText("synthetic-cover"),
        stillPaths: [],
        stillDigests: [],
        visualSourceSegmentIds: [variant.cover.segmentId],
        renderInputDigest: calculateRenderInputDigest(revision, segments, assets),
        status: "active" as const,
      }));
      await Promise.all(
        artifacts.flatMap((artifact) => [
          writeFile(artifact.videoPath, Buffer.from("synthetic-video-preview")),
          writeFile(artifact.coverPath, Buffer.from("synthetic-cover")),
        ]),
      );
      for (const artifact of artifacts) artifact.digest = digestText("synthetic-video-preview");
      return { artifacts, stillPaths: [] };
    },
  });
  // Keep one real listener for the whole test instead of asking Supertest to
  // repeatedly bind and close this server between requests under the full suite.
  await new Promise<void>((resolve, reject) => {
    app.once("error", reject);
    app.listen(0, "127.0.0.1", () => {
      app.off("error", reject);
      resolve();
    });
  });
  listeningServers.add(app);
  return app;
}

function digestText(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function makeStory(): StoryCandidate {
  return {
    id: "story_01",
    batchId: "batch_new",
    type: "product-journey",
    anchorSegmentIds: ["new_quote_01", "new_process_01"],
    historicalSegmentIds: ["old_quote_01"],
    historicalReasons: { old_quote_01: "same-product-before-stage" },
    status: "candidate",
  };
}

function makeRevision(): EditRevision {
  return {
    id: "revision_story_01_1",
    storyId: "story_01",
    revision: 1,
    parentRevisionId: null,
    designerPrompt: null,
    changeExplanation: null,
    variants: ["editorial-master", "xiaohongshu", "douyin", "wechat-channels"].map(
      (kind, index) => ({
        kind: kind as "editorial-master" | "xiaohongshu" | "douyin" | "wechat-channels",
        timeline: [timelineItem("new_quote_01"), { ...timelineItem("new_process_01"), outMs: 1_000 + index * 100 }],
        title: `${kind} title`,
        caption: `${kind} caption`,
        cover: { segmentId: "new_process_01", atMs: 100 },
      }),
    ),
    selectedStills: [],
    createdAt: "2026-09-04T00:00:00.000Z",
  };
}

function timelineItem(segmentId: string) {
  return { segmentId, inMs: 0, outMs: 800, role: "anchor" as const };
}

function makeSegment(id: string, batchId = "batch_new"): ContentSegment {
  return {
    id,
    assetId: `asset_${id}`,
    batchId,
    startMs: 0,
    endMs: 1_000,
    permissionState: "allowed",
    tags: {
      baseTypes: ["persona-copy"],
      people: [],
      products: [{ id: "prod_01", status: "confirmed" }],
      processStages: ["making"],
      topics: [],
      emotions: [],
      transcript: "",
      technicalFlags: [],
      usage: { storyIds: [], variants: [], useCount: 0, lastUsedAt: null },
    },
  };
}
