import { createHash } from "node:crypto";
import { chmod, lstat, mkdir } from "node:fs/promises";
import { join, resolve } from "node:path";

import {
  ExternalEditHandoffSchema,
  ExternalEditPackageSchema,
  type DraftPackage,
  type ExternalEditHandoff,
  type ExternalEditPackage,
  type ExternalEditHandoffStatus,
} from "@wearable/contracts";
import { z } from "zod";

import type { ExternalEditPackageBuilder } from "./external-edit-package";

const InstallReceiptSchema = z.object({
  adapterId: z.string().min(1),
  adapterVersion: z.string().min(1),
  appVersion: z.string().min(1),
  packageDigest: z.string().regex(/^[a-f0-9]{64}$/),
  draftDigest: z.string().regex(/^[a-f0-9]{64}$/),
}).strict();

export type JianyingInstallReceipt = Readonly<z.infer<typeof InstallReceiptSchema>>;

export type JianyingDraftAdapter = Readonly<{
  id: string;
  adapterVersion: string;
  supports(input: { appVersion: string; platform: "darwin" }): boolean;
  install(input: { packageRoot: string; draftRoot: string; installRoot: string; package: ExternalEditPackage }): Promise<JianyingInstallReceipt>;
}>;

export type JianyingAppInfo = Readonly<{ appVersion: string; appPath: string; draftRoot: string }>;
type BridgeHealth = "uninstalled" | "unsupported-version" | "supported-version";
type BridgeMode = "jianying-draft" | "file-package";

export type JianyingAutomationResult = Readonly<{
  attempted: boolean;
  projectCreated: boolean;
  materialImported: boolean;
  timelineImported: boolean;
  error:
    | "jianying-accessibility-permission-required"
    | "jianying-ui-version-unsupported"
    | "jianying-automation-failed"
    | "jianying-timeline-step-required"
    | null;
  /** Internal automation checkpoint. It is diagnostic only and never means acceptance on its own. */
  stage?: string | null;
}>;

export type JianyingDesktopAutomation = (input: Readonly<{
  app: JianyingAppInfo;
  mediaPath: string;
  subtitlePath: string | null;
  projectName: string;
}>) => Promise<JianyingAutomationResult>;

export type PreparedExternalEdit = Readonly<{
  handoff: ExternalEditHandoff;
  health: BridgeHealth;
  mode: BridgeMode;
  status: Extract<ExternalEditHandoffStatus, "ready" | "degraded">;
  degradedReason: string | null;
  package: ExternalEditPackage;
  /** Internal only. HTTP handlers must omit this field from browser responses. */
  packageRoot: string;
  receipt: JianyingInstallReceipt | null;
  preparationId: string;
}>;

export type JianyingOpenResult = Readonly<{
  opened: boolean;
  targetKind: "jianying" | "package-folder";
  jianyingOpened: boolean;
  packageFolderOpened: boolean;
  returnFolderOpened: boolean;
  automation: JianyingAutomationResult;
  error: "jianying-open-failed" | null;
}>;

type JianyingDesktopBridgeOptions = Readonly<{
  root: string;
  packageBuilder: Pick<ExternalEditPackageBuilder, "build">;
  findApp: () => Promise<JianyingAppInfo | null>;
  openTarget: (input: { kind: "jianying" | "folder"; target: string }) => Promise<void>;
  resolveReturnFolder?: (handoff: ExternalEditHandoff) => Promise<string>;
  automate?: JianyingDesktopAutomation;
  adapters?: readonly JianyingDraftAdapter[];
}>;

type OpenTarget = Readonly<{
  role: "jianying" | "package-folder" | "return-folder";
  kind: "jianying" | "folder";
  target: string;
}>;

type AutomationInput = Parameters<JianyingDesktopAutomation>[0];

const NOT_ATTEMPTED_AUTOMATION: JianyingAutomationResult = {
  attempted: false,
  projectCreated: false,
  materialImported: false,
  timelineImported: false,
  error: null,
};

function token(value: string): string {
  return createHash("sha256").update(value).digest("hex").slice(0, 24);
}

export class JianyingDesktopBridge {
  private readonly root: string;
  private readonly packageBuilder: JianyingDesktopBridgeOptions["packageBuilder"];
  private readonly findApp: JianyingDesktopBridgeOptions["findApp"];
  private readonly openTarget: JianyingDesktopBridgeOptions["openTarget"];
  private readonly automate: JianyingDesktopBridgeOptions["automate"];
  private readonly adapters: readonly JianyingDraftAdapter[];
  private readonly resolveReturnFolder: NonNullable<JianyingDesktopBridgeOptions["resolveReturnFolder"]>;
  private readonly openTargets = new Map<string, readonly OpenTarget[]>();
  private readonly automationInputs = new Map<string, AutomationInput>();
  private readonly automationResults = new Map<string, JianyingAutomationResult>();

  constructor(options: JianyingDesktopBridgeOptions) {
    this.root = resolve(options.root);
    this.packageBuilder = options.packageBuilder;
    this.findApp = options.findApp;
    this.openTarget = options.openTarget;
    this.automate = options.automate;
    this.resolveReturnFolder = options.resolveReturnFolder ?? (async () => "");
    this.adapters = options.adapters ?? [];
  }

  async prepare(handoffValue: ExternalEditHandoff, parentDraft: DraftPackage): Promise<PreparedExternalEdit> {
    const handoff = ExternalEditHandoffSchema.parse(handoffValue);
    const packageResult = await this.packageBuilder.build(handoff, parentDraft);
    const packageValue = ExternalEditPackageSchema.parse(packageResult.package);
    if (packageValue.handoffId !== handoff.id || packageValue.customerId !== handoff.customerId
      || packageValue.parentDraftId !== handoff.parentDraftId) {
      throw new Error("external-edit-package-binding-mismatch");
    }

    const app = await this.findApp();
    let health: BridgeHealth;
    let mode: BridgeMode = "file-package";
    let status: "ready" | "degraded" = "degraded";
    let degradedReason: string | null;
    let receipt: JianyingInstallReceipt | null = null;
    const targets: OpenTarget[] = [];
    const returnFolder = await this.resolveReturnFolder(handoff);

    if (!app) {
      health = "uninstalled";
      degradedReason = "jianying-not-installed";
    } else {
      targets.push({ role: "jianying", kind: "jianying", target: app.appPath });
      const adapter = this.adapters.find((candidate) => candidate.supports({ appVersion: app.appVersion, platform: "darwin" }));
      if (!adapter) {
        health = "unsupported-version";
        degradedReason = "jianying-version-not-verified";
      } else {
        health = "supported-version";
        const installRoot = join(this.root, "installs", token(adapter.id), token(handoff.id));
        await this.ensureBridgeDirectory(installRoot);
        try {
          const candidateReceipt = InstallReceiptSchema.parse(await adapter.install({
            packageRoot: packageResult.packageRoot,
            draftRoot: app.draftRoot,
            installRoot,
            package: packageValue,
          }));
          if (candidateReceipt.adapterId !== adapter.id
            || candidateReceipt.adapterVersion !== adapter.adapterVersion
            || candidateReceipt.appVersion !== app.appVersion
            || candidateReceipt.packageDigest !== packageValue.packageDigest) {
            throw new Error("jianying-install-receipt-mismatch");
          }
          receipt = candidateReceipt;
          mode = "jianying-draft";
          status = "ready";
          degradedReason = null;
        } catch (error) {
          health = "supported-version";
          degradedReason = error instanceof Error && error.message === "jianying-install-receipt-mismatch"
            ? "jianying-install-receipt-mismatch"
            : "jianying-adapter-failed";
        }
      }
    }

    targets.push({ role: "package-folder", kind: "folder", target: packageResult.packageRoot });
    if (returnFolder) targets.push({ role: "return-folder", kind: "folder", target: returnFolder });

    const timestamp = new Date().toISOString();
    const updatedHandoff = ExternalEditHandoffSchema.parse({
      ...handoff,
      mode,
      adapterId: mode === "jianying-draft" ? receipt!.adapterId : null,
      adapterVersion: mode === "jianying-draft" ? receipt!.adapterVersion : null,
      status,
      packageDigest: packageValue.packageDigest,
      updatedAt: timestamp,
    });
    const preparationId = `${handoff.id}:${packageValue.packageDigest}`;
    this.openTargets.set(preparationId, targets);
    const parentVideo = packageValue.files.find((file) => file.role === "parent-video");
    const subtitle = packageValue.files.find((file) => file.role === "subtitle");
    if (app && parentVideo && this.automate) {
      this.automationInputs.set(preparationId, {
        app,
        mediaPath: join(packageResult.packageRoot, parentVideo.relativePath),
        subtitlePath: subtitle ? join(packageResult.packageRoot, subtitle.relativePath) : null,
        projectName: `内容总编-${handoff.id.slice(0, 8)}`,
      });
    }
    return {
      handoff: updatedHandoff,
      health,
      mode,
      status,
      degradedReason,
      package: packageValue,
      packageRoot: packageResult.packageRoot,
      receipt,
      preparationId,
    };
  }

  async open(prepared: PreparedExternalEdit): Promise<JianyingOpenResult> {
    const targets = this.openTargets.get(prepared.preparationId);
    if (!targets?.length) return {
      opened: false,
      targetKind: prepared.mode === "jianying-draft" ? "jianying" : "package-folder",
      jianyingOpened: false,
      packageFolderOpened: false,
      returnFolderOpened: false,
      automation: NOT_ATTEMPTED_AUTOMATION,
      error: "jianying-open-failed",
    };
    const opened = new Set<"jianying" | "package-folder" | "return-folder">();
    const jianyingTarget = targets.find((target) => target.role === "jianying");
    if (jianyingTarget) {
      try {
        await this.openTarget({ kind: jianyingTarget.kind, target: jianyingTarget.target });
        opened.add("jianying");
      } catch {
        // Folder fallbacks below remain available if Jianying cannot launch.
      }
    }
    let automation = this.automationResults.get(prepared.preparationId) ?? NOT_ATTEMPTED_AUTOMATION;
    const automationInput = this.automationInputs.get(prepared.preparationId);
    if (opened.has("jianying") && automationInput && this.automate && !this.automationResults.has(prepared.preparationId)) {
      try {
        automation = await this.automate(automationInput);
      } catch {
        automation = {
          attempted: true,
          projectCreated: false,
          materialImported: false,
          timelineImported: false,
          error: "jianying-automation-failed",
        };
      }
      this.automationResults.set(prepared.preparationId, automation);
    }
    // `open` is only called after the person explicitly chooses to leave the
    // background workbench. Keep the return folder visible in that foreground
    // handoff so Jianying's exported MP4 has one obvious destination.
    const folders = targets.filter((target) => target.role !== "jianying"
      && (target.role !== "package-folder" || !automation.materialImported));
    for (const target of folders) {
      try {
        await this.openTarget({ kind: target.kind, target: target.target });
        opened.add(target.role);
      } catch {
        // Try the remaining folders so one Finder failure does not hide the return location.
      }
    }
    const jianyingOpened = opened.has("jianying");
    const packageFolderOpened = opened.has("package-folder");
    const returnFolderOpened = opened.has("return-folder");
    return {
      opened: opened.size > 0,
      targetKind: jianyingOpened ? "jianying" : "package-folder",
      jianyingOpened,
      packageFolderOpened,
      returnFolderOpened,
      automation,
      error: opened.size > 0 ? null : "jianying-open-failed",
    };
  }

  private async ensureBridgeDirectory(path: string): Promise<void> {
    await mkdir(path, { recursive: true, mode: 0o700 });
    const info = await lstat(path);
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error("jianying-bridge-root-invalid");
    await chmod(path, 0o700);
  }
}
