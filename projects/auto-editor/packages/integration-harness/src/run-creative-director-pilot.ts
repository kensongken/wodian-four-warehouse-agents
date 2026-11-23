import { execFile } from "node:child_process";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import {
  runCreativeDirectorPilot,
  type CreativeDirectorPilotReport,
} from "./creative-director-pilot";

const packageDirectory = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = resolve(packageDirectory, "../..");
const requestedDirectory = process.argv.slice(2).find((argument) => argument !== "--");
const outputDirectory = requestedDirectory
  ? resolve(requestedDirectory)
  : resolve(repositoryRoot, ".superpowers/evidence/task-4-creative-handoff-a0");
const acceptancePath = resolve(
  repositoryRoot,
  "docs/acceptance/2026-09-04-three-terminal-creative-director-a0.md",
);

await mkdir(outputDirectory, { recursive: true });
await mkdir(dirname(acceptancePath), { recursive: true });
const runDirectory = await mkdtemp(resolve(outputDirectory, "run-"));
const report = await runCreativeDirectorPilot(runDirectory);
const { stdout: candidateShaOutput } = await promisify(execFile)("git", ["rev-parse", "HEAD"], {
  cwd: repositoryRoot,
});
const candidateSha = candidateShaOutput.trim();
const jsonPath = resolve(outputDirectory, "creative-director-pilot-report.json");
await writeFile(jsonPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
await writeFile(acceptancePath, acceptanceMarkdown(report, jsonPath, candidateSha), "utf8");
process.stdout.write(`${JSON.stringify({ jsonPath, acceptancePath, candidateSha, report }, null, 2)}\n`);

function acceptanceMarkdown(report: CreativeDirectorPilotReport, jsonPath: string, candidateSha: string): string {
  const currentArtifactCount = report.storyTypes.length * report.variantKinds.length;
  return `# 三端创意导演 A-0 验收

## 验收结论

本报告只证明 **A-0 本地模拟验证**。固定候选代码为 \`${candidateSha}\`。在合成素材、本地 JSON 内容库、本地三端模拟器与本地 FFmpeg 环境中，生产 HTTP 链路已跑通。

## 已证明的链路

- 客户先查看库存建议，再精确选择 \`${report.historicalConsent.exactSelectedIds.join(", ")}\`；未选择素材没有进入脚本。
- 脚本 V1 和 V2 均保留，只确认 V${report.script.confirmedVersion}；未选历史素材引用数为 ${report.script.unselectedReferenceCount}。
- 确认前回执数为 ${report.dispatch.preConfirmationReceiptCount}；确认后三个模拟端共收到 ${report.dispatch.receiptCount} 份分步骤同版本任务。
- 现场动作保留：完成 ${report.fieldActions.completed}、重拍 ${report.fieldActions.retake}、跳过 ${report.fieldActions.skipped}、备注 ${report.fieldActions.notes}；未强制补拍。
- 旧版现场任务被生产 API 拒绝；重拍后只接收 take 2，另一个镜头允许跳过。
- 内容总编从持久化素材生成 ${report.storyTypes.join("、")} 两类故事；每条故事均有原视频、小红书、视频号三个真实 MP4，当前可审片产物共 ${currentArtifactCount} 个。
- 提示词改剪与精确审批分开执行；手机只收到本地发布包回执，\`publishedClaimed=${report.publishedClaimed}\`。

JSON 运行记录：\`${jsonPath}\`

## 可复现命令

- \`pnpm test\`（连续两次）
- \`pnpm run typecheck\`
- \`pnpm --filter @wearable/integration-harness run creative-pilot\`
- \`node --check packages/review-app/public/app.js\`

## 尚未证明的门槛

- 未证明真实手表和主石设备的蓝牙、续航、断网与长时间稳定性。
- 未证明真实客户理解并完成授权、脚本确认、现场动作和撤权的可用性。
- 未证明真实平台登录、上传、审核、公开发布或流量结果。本验收仅交付手机收件回执，且 \`publishedClaimed=${report.publishedClaimed}\`。
`;
}
