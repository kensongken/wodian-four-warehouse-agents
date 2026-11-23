import { z } from "zod";
import { PHONE_RUNTIME_PROFILE } from "./studio-runtime-profile";

export const APPROVED_WORKER_RUNTIME_DEPENDENCY_LOCK_SHA256 =
  "774b30f7f04e3d78faf72f75fca0d0b5823f8244d751c6c10f9c97f45fd85521";
export const APPROVED_WORKER_RUNTIME_WORKER_DEPENDENCY_LOCK_SHA256 =
  "7b395d3286c1ce2012c5fb2507ad2a3ce4accb010dab69d3de2a973ed84b5041";

const NonEmptyIdSchema = z.string().trim().min(1);
const DigestSchema = z.string().regex(/^[a-f0-9]{64}$/);
const WorkerRuntimeApprovalPacketIdSchema = z.string().regex(
  /^worker-runtime-bundle-v1-sha256-[a-f0-9]{64}$/,
);
const DateTimeSchema = z.string().datetime();
const ModelRootRelativePathSchema = z.string().trim().min(1).superRefine((value, ctx) => {
  const components = value.split("/");
  if (
    value.startsWith("/")
    || value.includes("\\")
    || /[\u0000-\u001f\u007f]/u.test(value)
    || components.some((component) => component === "" || component === "." || component === "..")
  ) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "worker-runtime-model-root-relative-path-required",
    });
  }
});

export const StudioWorkerRuntimeSidecarV1Schema = z.object({
  relativePath: ModelRootRelativePathSchema,
  sha256: DigestSchema,
  byteLength: z.number().int().positive().safe(),
}).strict();

export const StudioWorkerRuntimeProbeBindingV1Schema = z.object({
  receiptPath: ModelRootRelativePathSchema,
  receiptSha256: DigestSchema,
  runtimeTreeSha256: DigestSchema,
  modelSha256: DigestSchema,
  promptVersion: NonEmptyIdSchema,
  inputSha256: DigestSchema,
}).strict();

export const StudioWorkerRuntimeReceiptBindingV1Schema = z.object({
  id: NonEmptyIdSchema,
  version: NonEmptyIdSchema,
  sha256: DigestSchema,
}).strict();

const StudioWorkerRuntimeProbeRuntimeV1Schema = z.object({
  id: NonEmptyIdSchema,
  version: NonEmptyIdSchema,
  treeSha256: DigestSchema,
}).strict();

const StudioWorkerRuntimeProbeModelV1Schema = z.object({
  id: NonEmptyIdSchema,
  version: NonEmptyIdSchema,
  sha256: DigestSchema,
}).strict();

export const StudioWorkerRuntimeProbeReceiptV1Schema = z.object({
  schemaVersion: z.literal(1),
  runtime: StudioWorkerRuntimeProbeRuntimeV1Schema,
  model: StudioWorkerRuntimeProbeModelV1Schema,
  promptVersion: NonEmptyIdSchema,
  inputSha256: DigestSchema,
  outputSha256: DigestSchema,
  observationCount: z.number().int().positive().safe(),
  observationKinds: z.array(NonEmptyIdSchema).min(1),
  runtimeMs: z.number().int().nonnegative().safe(),
  maximumResidentSetBytes: z.number().int().nonnegative().safe(),
  syntheticInput: z.literal(true),
  customerContentUsed: z.literal(false),
  realModel: z.literal(true),
  formalWorkerPath: z.literal(true),
  networkDenied: z.literal(true),
  providerMode: z.literal("local"),
  publishedClaimed: z.literal(false),
}).strict();

const validateWorkerRuntimeApproval = (
  value: {
    id: string;
    version: string;
    pythonVersion: string;
    platform: string;
    architecture: string;
    minimumOsVersion: string;
    runtimeProfileId?: string;
    dependencyLockSha256: string;
    workerDependencyLockSha256: string;
    archive: { relativePath: string };
    manifest: { relativePath: string };
    packageInventory: { relativePath: string };
    derivationReceipt: { relativePath: string };
    treeSha256: string;
    licenseReview: "pending" | "approved" | "rejected";
    enabled: boolean;
    approved: boolean;
    approvedBy: string | null;
    approvedAt: string | null;
    probe: z.infer<typeof StudioWorkerRuntimeProbeBindingV1Schema> | null;
  },
  ctx: z.RefinementCtx,
): void => {
  const phone=value.runtimeProfileId===PHONE_RUNTIME_PROFILE.runtimeProfileId;
  if ((!phone && value.id===PHONE_RUNTIME_PROFILE.runtimeId) || (phone && (value.id!==PHONE_RUNTIME_PROFILE.runtimeId || value.version!==PHONE_RUNTIME_PROFILE.runtimeVersion || value.pythonVersion!==PHONE_RUNTIME_PROFILE.pythonVersion || value.platform!==PHONE_RUNTIME_PROFILE.platform || value.architecture!==PHONE_RUNTIME_PROFILE.architecture || value.minimumOsVersion!==PHONE_RUNTIME_PROFILE.minimumOsVersion))) {
    ctx.addIssue({code:z.ZodIssueCode.custom,message:"worker-runtime-profile-binding-mismatch",path:["runtimeProfileId"]});
  }
  const expectedDependency=phone?PHONE_RUNTIME_PROFILE.dependencyLockSha256:APPROVED_WORKER_RUNTIME_DEPENDENCY_LOCK_SHA256;
  const expectedWorker=phone?PHONE_RUNTIME_PROFILE.workerDependencyLockSha256:APPROVED_WORKER_RUNTIME_WORKER_DEPENDENCY_LOCK_SHA256;
  if (value.dependencyLockSha256 !== expectedDependency) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "worker-runtime-dependency-lock-not-approved",
      path: ["dependencyLockSha256"],
    });
  }
  if (
    value.workerDependencyLockSha256
    !== expectedWorker
  ) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "worker-runtime-worker-dependency-lock-not-approved",
      path: ["workerDependencyLockSha256"],
    });
  }
  const sidecarPaths = [
    value.archive.relativePath,
    value.manifest.relativePath,
    value.packageInventory.relativePath,
    value.derivationReceipt.relativePath,
  ];
  if (new Set(sidecarPaths).size !== sidecarPaths.length) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "duplicate-worker-runtime-sidecar-path",
      path: ["archive"],
    });
  }
  if (value.approved && value.licenseReview !== "approved") {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "approved-worker-runtime-requires-approved-license-review",
      path: ["licenseReview"],
    });
  }
  if (value.approved && (value.approvedBy === null || value.approvedAt === null)) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "approved-worker-runtime-requires-approval-record",
      path: ["approvedBy"],
    });
  }
  if (value.enabled && !value.approved) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "enabled-worker-runtime-must-be-approved",
      path: ["enabled"],
    });
  }
  if (value.probe !== null && value.probe.runtimeTreeSha256 !== value.treeSha256) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "worker-runtime-probe-tree-binding-mismatch",
      path: ["probe", "runtimeTreeSha256"],
    });
  }
};

export const StudioWorkerRuntimeBundleV1Schema = z.object({
  runtimeProfileId: z.literal(PHONE_RUNTIME_PROFILE.runtimeProfileId).optional(),
  schemaVersion: z.literal(1),
  id: NonEmptyIdSchema,
  version: NonEmptyIdSchema,
  pythonVersion: z.string().regex(/^3\.12\.\d+$/),
  platform: z.literal("darwin"),
  architecture: z.literal("arm64"),
  minimumOsVersion: NonEmptyIdSchema,
  dependencyLockSha256: DigestSchema,
  workerDependencyLockSha256: DigestSchema,
  // Digest suffix of derivationReceipt.materialPacketId; this does not alter the approved bundle packet core.
  materialInventorySha256: DigestSchema,
  approvalPacketId: WorkerRuntimeApprovalPacketIdSchema,
  archive: StudioWorkerRuntimeSidecarV1Schema,
  manifest: StudioWorkerRuntimeSidecarV1Schema,
  packageInventory: StudioWorkerRuntimeSidecarV1Schema,
  derivationReceipt: StudioWorkerRuntimeSidecarV1Schema,
  treeSha256: DigestSchema,
  licenseReview: z.enum(["pending", "approved", "rejected"]),
  enabled: z.boolean(),
  approved: z.boolean(),
  approvedBy: NonEmptyIdSchema.nullable(),
  approvedAt: DateTimeSchema.nullable(),
  probe: StudioWorkerRuntimeProbeBindingV1Schema.nullable(),
}).strict().superRefine(validateWorkerRuntimeApproval);

export type StudioWorkerRuntimeSidecarV1 = z.infer<typeof StudioWorkerRuntimeSidecarV1Schema>;
export type StudioWorkerRuntimeProbeBindingV1 = z.infer<typeof StudioWorkerRuntimeProbeBindingV1Schema>;
export type StudioWorkerRuntimeReceiptBindingV1 = z.infer<
  typeof StudioWorkerRuntimeReceiptBindingV1Schema
>;
export type StudioWorkerRuntimeProbeReceiptV1 = z.infer<
  typeof StudioWorkerRuntimeProbeReceiptV1Schema
>;
export type StudioWorkerRuntimeBundleV1 = z.infer<typeof StudioWorkerRuntimeBundleV1Schema>;
