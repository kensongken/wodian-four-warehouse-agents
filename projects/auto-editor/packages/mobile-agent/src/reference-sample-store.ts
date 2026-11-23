import { createHash, randomUUID } from "node:crypto";
import { mkdir, open as openFile, readFile, realpath, rename, unlink, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";

import {
  ApprovalSchema,
  ReferenceDraftApprovalSchema,
  ReferenceReusableElementSchema,
  ReferenceDraftPackageSchema,
  ReferenceExecutionReportSchema,
  ReferenceMaterialMappingPlanSchema,
  ReferenceMobileReviewV1Schema,
  ReferenceRightsDeclarationSchema,
  ReferenceSampleSchema,
  ReferenceSourceKindSchema,
  ReferenceStyleAnalysisSchema,
  ReferenceStyleSelectionSchema,
  ReferenceVariantKindSchema,
  ReferenceVariantPlanSchema,
  type Approval,
  type ReferenceDraftApproval,
  type ReferenceDraftPackage,
  type ReferenceExecutionReport,
  type ReferenceMaterialMappingPlan,
  type ReferenceMobileReviewV1,
  type ReferenceRightsDeclaration,
  type ReferenceSample,
  type ReferenceStyleAnalysis,
  type ReferenceStyleSelection,
  type ReferenceVariantPlan,
} from "@wearable/contracts";
import { z } from "zod";

import { confirmReferenceMaterialMappingPlan } from "./reference-material-mapper";
import type { StudioScope } from "./studio-capabilities";

const DigestSchema = z.string().regex(/^[a-f0-9]{64}$/);
const OperationIdSchema = z.string().min(1).max(512);
const OperationKeySchema = z.string().regex(/^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$/);
const OperationReasonSchema = z.string().regex(/^[a-z0-9][a-z0-9._:-]{0,127}$/);
const OperationMethodSchema = z.enum([
  "declareRights", "analyze", "confirmStyle", "createMappingPlan",
  "confirmMappingPlan", "render", "reviseWithPrompt", "approveDraft", "invalidateForAsset",
]);
const OperationResultIdentitySchema = z.object({
  id: OperationIdSchema,
  version: z.number().int().positive(),
}).strict();
const OperationMobileResultIdentitySchema = OperationResultIdentitySchema.extend({
  reportId: OperationIdSchema,
  reportVersion: z.number().int().positive(),
}).strict();
const OperationMappingIdentitySchema = OperationResultIdentitySchema.extend({
  contentDigest: DigestSchema,
}).strict();
const OperationSampleIdentitySchema = OperationResultIdentitySchema.extend({
  sourceDigest: DigestSchema,
}).strict();
const MAX_INVALIDATION_IDENTITIES = 256;
export const REFERENCE_WORKFLOW_INTEGRITY_VERSION = 2 as const;
const OperationMappingIdentityListSchema = z.array(OperationMappingIdentitySchema)
  .max(MAX_INVALIDATION_IDENTITIES, "reference-invalidation-identities-too-many")
  .superRefine((values, ctx) => {
    if (new Set(values.map((value) => value.id)).size !== values.length) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "reference-invalidation-identity-duplicate",
      });
      return;
    }
    for (let index = 1; index < values.length; index += 1) {
      if (compareOperationMappingIdentity(values[index - 1]!, values[index]!) >= 0) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "reference-invalidation-identities-not-canonical",
        });
        return;
      }
    }
  });
const OperationSampleIdentityListSchema = z.array(OperationSampleIdentitySchema)
  .max(MAX_INVALIDATION_IDENTITIES, "reference-invalidation-identities-too-many")
  .superRefine((values, ctx) => {
    if (new Set(values.map((value) => value.id)).size !== values.length) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: "reference-invalidation-identity-duplicate" });
      return;
    }
    for (let index = 1; index < values.length; index += 1) {
      if (compareOperationSampleIdentity(values[index - 1]!, values[index]!) >= 0) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "reference-invalidation-identities-not-canonical",
        });
        return;
      }
    }
  });
const ReferenceEditorOperationIntentSchema = z.discriminatedUnion("method", [
  z.object({
    method: z.literal("declareRights"),
    referenceSampleId: OperationIdSchema,
    sourceKind: ReferenceSourceKindSchema,
    reusableElements: z.array(ReferenceReusableElementSchema).max(3)
      .refine((values) => new Set(values).size === values.length, "duplicate-reference-reusable-element"),
    sampleBindingDigest: DigestSchema,
    expectedResult: OperationResultIdentitySchema,
  }).strict(),
  z.object({
    method: z.literal("analyze"), sampleId: OperationIdSchema,
    inputBindingDigest: DigestSchema, expectedResult: OperationResultIdentitySchema,
  }).strict(),
  z.object({
    method: z.literal("confirmStyle"),
    analysisId: OperationIdSchema,
    selectedRuleIds: z.array(OperationIdSchema).min(1).max(256)
      .refine((values) => new Set(values).size === values.length, "duplicate-selected-reference-rule-id"),
    inputBindingDigest: DigestSchema,
    expectedResult: OperationResultIdentitySchema,
  }).strict(),
  z.object({
    method: z.literal("createMappingPlan"), sampleId: OperationIdSchema,
    inputBindingDigest: DigestSchema, expectedResult: OperationResultIdentitySchema,
  }).strict(),
  z.object({
    method: z.literal("confirmMappingPlan"), planId: OperationIdSchema,
    inputBindingDigest: DigestSchema, expectedResult: OperationResultIdentitySchema,
  }).strict(),
  z.object({
    method: z.literal("render"), planId: OperationIdSchema,
    inputBindingDigest: DigestSchema, expectedResult: OperationMobileResultIdentitySchema,
  }).strict(),
  z.object({
    method: z.literal("reviseWithPrompt"),
    sampleId: OperationIdSchema,
    target: ReferenceVariantKindSchema,
    promptDigest: DigestSchema,
    // Optional only for reading integrity-v2 receipts written before revisions
    // were bound to the exact current report and draft identity.
    report: OperationResultIdentitySchema.optional(),
    draft: OperationResultIdentitySchema.extend({ outputDigest: DigestSchema }).strict().optional(),
    inputBindingDigest: DigestSchema,
    expectedResult: OperationMobileResultIdentitySchema,
  }).strict(),
  z.object({
    method: z.literal("approveDraft"),
    report: z.object({
      id: OperationIdSchema,
      version: z.number().int().positive(),
    }).strict(),
    draft: z.object({
      id: OperationIdSchema,
      version: z.number().int().positive(),
      outputDigest: DigestSchema,
    }).strict(),
    mappingPlan: z.object({
      id: OperationIdSchema,
      version: z.number().int().positive(),
      contentDigest: DigestSchema,
      permissionSetDigest: DigestSchema,
    }).strict(),
    variant: ReferenceVariantKindSchema,
    fileDigest: DigestSchema,
    expectedResult: OperationResultIdentitySchema,
  }).strict(),
  z.object({
    method: z.literal("invalidateForAsset"),
    assetId: OperationIdSchema,
    reason: OperationReasonSchema,
    // Optional only for reading integrity-v2 ledgers written before sample-source
    // invalidation existed. Every newly resolved operation includes this list.
    affectedSamples: OperationSampleIdentityListSchema.optional(),
    affectedMappings: OperationMappingIdentityListSchema,
  }).strict(),
]);
const ReferenceEditorOperationSchema = z.object({
  id: OperationIdSchema,
  customerId: OperationIdSchema,
  sessionId: OperationIdSchema,
  version: z.literal(1),
  operationKey: OperationKeySchema,
  operationKeyDigest: DigestSchema,
  actorId: OperationIdSchema,
  subjectId: OperationIdSchema,
  resultDigest: DigestSchema,
  method: OperationMethodSchema,
  intent: ReferenceEditorOperationIntentSchema,
  intentDigest: DigestSchema,
  result: z.discriminatedUnion("kind", [
    z.object({ kind: z.literal("rights"), id: z.string().min(1), version: z.number().int().positive() }).strict(),
    z.object({ kind: z.literal("analysis"), id: z.string().min(1), version: z.number().int().positive() }).strict(),
    z.object({ kind: z.literal("selection"), id: z.string().min(1), version: z.number().int().positive() }).strict(),
    z.object({ kind: z.literal("mapping"), id: z.string().min(1), version: z.number().int().positive() }).strict(),
    z.object({ kind: z.literal("report"), id: z.string().min(1), version: z.number().int().positive() }).strict(),
    z.object({ kind: z.literal("approval"), id: z.string().min(1), version: z.literal(1) }).strict(),
    z.object({
      kind: z.literal("invalidation"),
      assetId: OperationIdSchema,
      reason: OperationReasonSchema,
      invalidatedSamples: OperationSampleIdentityListSchema.optional(),
      affectedSamples: OperationSampleIdentityListSchema.optional(),
      affectedMappings: OperationMappingIdentityListSchema,
      invalidatedMappings: OperationMappingIdentityListSchema,
    }).strict(),
    z.object({ kind: z.literal("void") }).strict(),
  ]),
}).strict().superRefine((operation, ctx) => {
  if (operation.method !== operation.intent.method) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["intent", "method"],
      message: "reference-operation-intent-method-mismatch",
    });
  }
});
const ReferenceWorkflowApprovalSchema = z.union([ReferenceDraftApprovalSchema, ApprovalSchema]);

const SnapshotPayloadSchema = z.object({
  samples: z.array(ReferenceSampleSchema),
  rightsDeclarations: z.array(ReferenceRightsDeclarationSchema),
  analyses: z.array(ReferenceStyleAnalysisSchema),
  styleSelections: z.array(ReferenceStyleSelectionSchema),
  mappingPlans: z.array(ReferenceMaterialMappingPlanSchema),
  variantPlans: z.array(ReferenceVariantPlanSchema),
  drafts: z.array(ReferenceDraftPackageSchema),
  executionReports: z.array(ReferenceExecutionReportSchema),
  approvals: z.array(ReferenceWorkflowApprovalSchema),
  mobileReviews: z.array(ReferenceMobileReviewV1Schema),
  editorOperations: z.array(ReferenceEditorOperationSchema).default([]),
}).strict();
const StoredSnapshotSchema = SnapshotPayloadSchema.extend({
  digest: DigestSchema,
  integrityVersion: z.literal(REFERENCE_WORKFLOW_INTEGRITY_VERSION).optional(),
}).strict();
const ReferenceOperationReservationSchema = z.object({
  id: z.string().min(1),
  customerId: z.string().min(1),
  sessionId: z.string().min(1),
  version: z.literal(1),
  operationKeyDigest: DigestSchema,
  bodyDigest: DigestSchema,
  kind: z.enum(["dedicated-upload", "historical-pointer"]),
}).strict();

type ReferenceWorkflowPayload = z.infer<typeof SnapshotPayloadSchema>;
type ReferenceCollectionName = keyof ReferenceWorkflowPayload;
export type ReferenceEditorOperation = z.infer<typeof ReferenceEditorOperationSchema>;
export type ReferenceEditorOperationMethod = ReferenceEditorOperation["method"];
export type ReferenceEditorOperationIntent = z.infer<typeof ReferenceEditorOperationIntentSchema>;
export type ReferenceEditorOperationMetadata = Omit<ReferenceEditorOperation, "result" | "resultDigest">;
export type ReferenceWorkflowApproval = Approval | ReferenceDraftApproval;

export function referenceEditorOperationId(
  scope: Pick<StudioScope, "customerId" | "sessionId">,
  method: ReferenceEditorOperationMethod,
  operationKey: string,
): string {
  return `reference_editor_${method}_${sha256(`${scope.customerId}\0${scope.sessionId}\0${operationKey}`)}`;
}

export function referenceEditorOperationMetadata(
  scope: StudioScope,
  method: ReferenceEditorOperationMethod,
  operationKeyValue: string,
  intentValue: unknown,
): ReferenceEditorOperationMetadata {
  const customerId = OperationIdSchema.parse(scope.customerId);
  const sessionId = OperationIdSchema.parse(scope.sessionId);
  const actorId = OperationIdSchema.parse(scope.actorId);
  const operationKey = OperationKeySchema.parse(operationKeyValue);
  const rawIntent = intentValue && typeof intentValue === "object" && "method" in intentValue
    ? intentValue
    : { ...(intentValue as Record<string, unknown>), method };
  const intent = ReferenceEditorOperationIntentSchema.parse(rawIntent);
  if (intent.method !== method) throw new Error("reference-operation-intent-method-mismatch");
  const subjectId = operationIntentSubject(intent);
  const binding = { customerId, sessionId, method, actorId, operationKey, subjectId, intent };
  return {
    id: referenceEditorOperationId({ customerId, sessionId }, method, operationKey),
    customerId,
    sessionId,
    version: 1,
    operationKey,
    operationKeyDigest: sha256(operationKey),
    actorId,
    subjectId,
    method,
    intent,
    intentDigest: sha256(canonicalJson(binding)),
  };
}

export function referenceEditorOperationRequestIntent(intent: ReferenceEditorOperationIntent): unknown {
  switch (intent.method) {
    case "declareRights": return {
      method: intent.method,
      referenceSampleId: intent.referenceSampleId,
      sourceKind: intent.sourceKind,
      reusableElements: intent.reusableElements,
    };
    case "analyze":
    case "createMappingPlan": return { method: intent.method, sampleId: intent.sampleId };
    case "confirmStyle": return {
      method: intent.method,
      analysisId: intent.analysisId,
      selectedRuleIds: intent.selectedRuleIds,
    };
    case "confirmMappingPlan":
    case "render": return { method: intent.method, planId: intent.planId };
    case "reviseWithPrompt": return intent.report && intent.draft
      ? {
          method: intent.method,
          promptDigest: intent.promptDigest,
          report: intent.report,
          draft: intent.draft,
        }
      : {
          method: intent.method,
          sampleId: intent.sampleId,
          target: intent.target,
          promptDigest: intent.promptDigest,
        };
    case "approveDraft": return {
      method: intent.method,
      report: intent.report,
      draft: intent.draft,
    };
    case "invalidateForAsset": return {
      method: intent.method,
      assetId: intent.assetId,
      reason: intent.reason,
    };
  }
}

export type ReferenceWorkflowSnapshot = Readonly<{
  samples: readonly ReferenceSample[];
  rightsDeclarations: readonly ReferenceRightsDeclaration[];
  analyses: readonly ReferenceStyleAnalysis[];
  styleSelections: readonly ReferenceStyleSelection[];
  mappingPlans: readonly ReferenceMaterialMappingPlan[];
  variantPlans: readonly ReferenceVariantPlan[];
  drafts: readonly ReferenceDraftPackage[];
  executionReports: readonly ReferenceExecutionReport[];
  approvals: readonly ReferenceWorkflowApproval[];
  mobileReviews: readonly ReferenceMobileReviewV1[];
  editorOperations: readonly ReferenceEditorOperation[];
  digest: string;
}>;
type ReferenceWorkflowView = Omit<ReferenceWorkflowSnapshot, "digest">;

export interface ReferenceSampleStore {
  boundScope(): Readonly<{ customerId: string; sessionId: string }>;
  operationCoordinatorIdentity?(): string;
  snapshot(): Promise<ReferenceWorkflowSnapshot>;
  putSample(value: ReferenceSample): Promise<ReferenceSample>;
  putRights(value: ReferenceRightsDeclaration): Promise<ReferenceRightsDeclaration>;
  putAnalysis(value: ReferenceStyleAnalysis): Promise<ReferenceStyleAnalysis>;
  putSelection(value: ReferenceStyleSelection): Promise<ReferenceStyleSelection>;
  putMappingPlan(value: ReferenceMaterialMappingPlan): Promise<ReferenceMaterialMappingPlan>;
  putVariantPlan(value: ReferenceVariantPlan): Promise<ReferenceVariantPlan>;
  putDraft(value: ReferenceDraftPackage): Promise<ReferenceDraftPackage>;
  putExecutionReport(value: ReferenceExecutionReport): Promise<ReferenceExecutionReport>;
  putApproval(value: Approval): Promise<Approval>;
  putMobileReview(value: ReferenceMobileReviewV1): Promise<ReferenceMobileReviewV1>;
  listSamples(): Promise<ReferenceSample[]>;
  listRightsDeclarations(): Promise<ReferenceRightsDeclaration[]>;
  listAnalyses(): Promise<ReferenceStyleAnalysis[]>;
  listSelections(): Promise<ReferenceStyleSelection[]>;
  listMappingPlans(): Promise<ReferenceMaterialMappingPlan[]>;
  listVariantPlans(): Promise<ReferenceVariantPlan[]>;
  listDrafts(): Promise<ReferenceDraftPackage[]>;
  listExecutionReports(): Promise<ReferenceExecutionReport[]>;
  listApprovals(): Promise<ReferenceWorkflowApproval[]>;
  listMobileReviews(): Promise<ReferenceMobileReviewV1[]>;
  reserveOperation(value: ReferenceOperationReservationInput): Promise<void>;
  commit(next: ReferenceWorkflowSnapshot, expectedDigest: string): Promise<void>;
}

export type ReferenceOperationReservationInput = Readonly<{
  operationKey: string;
  bodyDigest: string;
  kind: "dedicated-upload" | "historical-pointer";
}>;

type ReferenceSampleStoreOptions = Readonly<{
  atomicRename?: (from: string, to: string) => Promise<void>;
}>;

/**
 * One customer/session-scoped snapshot. All process-local writers sharing a
 * storage root queue behind the same lock, including writers from other store
 * instances and other customer scopes. This is deliberately not cross-process
 * CAS: the local Mac runtime must have exactly one Node writer for a root.
 */
export class JsonReferenceSampleStore implements ReferenceSampleStore {
  private static readonly rootQueues = new Map<string, Promise<void>>();

  private constructor(
    private readonly root: string,
    private readonly scope: StudioScope,
    private readonly snapshotPath: string,
    private readonly atomicRename: (from: string, to: string) => Promise<void>,
  ) {}

  static async open(
    rootValue: string,
    scopeValue: StudioScope,
    options: ReferenceSampleStoreOptions = {},
  ): Promise<JsonReferenceSampleStore> {
    const scope = parseScope(scopeValue);
    if (typeof rootValue !== "string" || !rootValue.trim()) {
      throw new Error("reference-store-root-required");
    }
    const requestedRoot = resolve(rootValue);
    await mkdir(requestedRoot, { recursive: true });
    const root = await realpath(requestedRoot);
    const snapshotPath = join(
      root,
      "reference-workflows",
      sha256(scope.customerId),
      sha256(scope.sessionId),
      "snapshot.json",
    );
    return new JsonReferenceSampleStore(
      root,
      scope,
      snapshotPath,
      options.atomicRename ?? rename,
    );
  }

  async snapshot(): Promise<ReferenceWorkflowSnapshot> {
    return this.withRootQueue(() => this.readSnapshotUnlocked());
  }

  async withCurrentSnapshot<T>(work: (snapshot: ReferenceWorkflowSnapshot) => Promise<T> | T): Promise<T> {
    return this.withRootQueue(async () => work(await this.readSnapshotUnlocked()));
  }

  readonly boundScope = (): Readonly<{ customerId: string; sessionId: string }> => {
    return { customerId: this.scope.customerId, sessionId: this.scope.sessionId };
  };

  readonly operationCoordinatorIdentity = (): string => `reference-store-${sha256(this.root)}`;

  async putSample(value: ReferenceSample): Promise<ReferenceSample> {
    return this.putObject("samples", ReferenceSampleSchema, value);
  }

  async putRights(value: ReferenceRightsDeclaration): Promise<ReferenceRightsDeclaration> {
    return this.putObject("rightsDeclarations", ReferenceRightsDeclarationSchema, value);
  }

  async putAnalysis(value: ReferenceStyleAnalysis): Promise<ReferenceStyleAnalysis> {
    return this.putObject("analyses", ReferenceStyleAnalysisSchema, value);
  }

  async putSelection(value: ReferenceStyleSelection): Promise<ReferenceStyleSelection> {
    return this.putObject("styleSelections", ReferenceStyleSelectionSchema, value);
  }

  async putMappingPlan(value: ReferenceMaterialMappingPlan): Promise<ReferenceMaterialMappingPlan> {
    return this.putObject("mappingPlans", ReferenceMaterialMappingPlanSchema, value);
  }

  async putVariantPlan(value: ReferenceVariantPlan): Promise<ReferenceVariantPlan> {
    return this.putObject("variantPlans", ReferenceVariantPlanSchema, value);
  }

  async putDraft(value: ReferenceDraftPackage): Promise<ReferenceDraftPackage> {
    return this.putObject("drafts", ReferenceDraftPackageSchema, value);
  }

  async putExecutionReport(value: ReferenceExecutionReport): Promise<ReferenceExecutionReport> {
    return this.putObject("executionReports", ReferenceExecutionReportSchema, value);
  }

  async putApproval(value: Approval): Promise<Approval> {
    return this.putObject("approvals", ApprovalSchema, value);
  }

  async putMobileReview(value: ReferenceMobileReviewV1): Promise<ReferenceMobileReviewV1> {
    return this.putObject("mobileReviews", ReferenceMobileReviewV1Schema, value);
  }

  async listSamples(): Promise<ReferenceSample[]> {
    return [...(await this.snapshot()).samples];
  }

  async listRightsDeclarations(): Promise<ReferenceRightsDeclaration[]> {
    return [...(await this.snapshot()).rightsDeclarations];
  }

  async listAnalyses(): Promise<ReferenceStyleAnalysis[]> {
    return [...(await this.snapshot()).analyses];
  }

  async listSelections(): Promise<ReferenceStyleSelection[]> {
    return [...(await this.snapshot()).styleSelections];
  }

  async listMappingPlans(): Promise<ReferenceMaterialMappingPlan[]> {
    return [...(await this.snapshot()).mappingPlans];
  }

  async listVariantPlans(): Promise<ReferenceVariantPlan[]> {
    return [...(await this.snapshot()).variantPlans];
  }

  async listDrafts(): Promise<ReferenceDraftPackage[]> {
    return [...(await this.snapshot()).drafts];
  }

  async listExecutionReports(): Promise<ReferenceExecutionReport[]> {
    return [...(await this.snapshot()).executionReports];
  }

  async listApprovals(): Promise<ReferenceWorkflowApproval[]> {
    return [...(await this.snapshot()).approvals];
  }

  async listMobileReviews(): Promise<ReferenceMobileReviewV1[]> {
    return [...(await this.snapshot()).mobileReviews];
  }

  async reserveOperation(value: ReferenceOperationReservationInput): Promise<void> {
    if (!value || typeof value !== "object" || typeof value.operationKey !== "string" ||
        !value.operationKey.trim()) {
      throw new Error("reference-operation-key-required");
    }
    const operationKeyDigest = sha256(value.operationKey);
    const requested = ReferenceOperationReservationSchema.parse({
      id: `reference_operation_${operationKeyDigest}`,
      customerId: this.scope.customerId,
      sessionId: this.scope.sessionId,
      version: 1,
      operationKeyDigest,
      bodyDigest: value.bodyDigest,
      kind: value.kind,
    });
    const path = join(dirname(this.snapshotPath), "operations", `${operationKeyDigest}.json`);
    await this.withRootQueue(async () => {
      const raw = await readFile(path, "utf8").catch((error: unknown) => {
        if (hasCode(error, "ENOENT")) return null;
        throw error;
      });
      if (raw !== null) {
        const existing = ReferenceOperationReservationSchema.parse(JSON.parse(raw));
        if (canonicalJson(existing) !== canonicalJson(requested)) {
          throw new Error("reference-operation-key-conflict");
        }
        return;
      }
      await this.writeAtomicJson(path, requested);
    });
  }

  async commit(next: ReferenceWorkflowSnapshot, expectedDigest: string): Promise<void> {
    DigestSchema.parse(expectedDigest);
    await this.withRootQueue(async () => {
      const current = await this.readSnapshotUnlocked();
      if (current.digest !== expectedDigest) throw new Error("reference-state-conflict");
      assertOperationKeyReuse(current, next);
      const checked = this.prepareSnapshot(next);
      assertAppendOnlySnapshot(current, checked);
      assertMappingStateTransitions(current, checked);
      assertAssetInvalidationTransitions(current, checked);
      assertInvalidationAuthorization(current, checked);
      assertReferenceApprovalAuthorization(current, checked);
      assertNoInvalidatedAssetReuse(checked);
      await this.writeSnapshotUnlocked(checked);
    });
  }

  private async putObject<T extends { id: string }>(
    collection: ReferenceCollectionName,
    schema: z.ZodType<T>,
    value: T,
  ): Promise<T> {
    const checked = schema.parse(value);
    assertObjectScope(this.scope, checked);
    const invalidatedWrite = [
      "samples", "styleSelections", "mappingPlans", "variantPlans", "drafts", "executionReports", "approvals", "mobileReviews",
    ].includes(collection) && "status" in checked && checked.status === "invalidated";
    if (invalidatedWrite && collection !== "mappingPlans") {
      throw new Error("reference-invalidation-commit-required");
    }
    return this.withRootQueue(async () => {
      const current = await this.readSnapshotUnlocked();
      const records = current[collection] as unknown as ReadonlyArray<{
        id: string;
        version?: number;
      }>;
      const version = "version" in checked ? checked.version : undefined;
      const existing = records.find((record) =>
        record.id === checked.id && (version === undefined || record.version === version));
      if (existing) {
        if (canonicalJson(existing) !== canonicalJson(checked)) {
          throw new Error("reference-immutable-object-conflict");
        }
        return schema.parse(existing);
      }
      if (invalidatedWrite) {
        throw new Error("reference-invalidation-commit-required");
      }
      if (collection === "mappingPlans") {
        const mapping = checked as unknown as ReferenceMaterialMappingPlan;
        if (mapping.status !== "draft" || mapping.version !== 1 ||
            records.some((record) => record.id === mapping.id)) {
          throw new Error("reference-mapping-put-requires-initial-draft");
        }
      }
      const nextRecords = [...records, checked].sort(compareVersionedRecords);
      const next = this.prepareSnapshot({
        ...current,
        [collection]: nextRecords,
      } as unknown as ReferenceWorkflowSnapshot);
      assertAppendOnlySnapshot(current, next);
      assertNoInvalidatedAssetReuse(next);
      await this.writeSnapshotUnlocked(next);
      return checked;
    });
  }

  private prepareSnapshot(value: ReferenceWorkflowSnapshot): ReferenceWorkflowSnapshot {
    const payload = SnapshotPayloadSchema.parse({
      samples: value.samples,
      rightsDeclarations: value.rightsDeclarations,
      analyses: value.analyses,
      styleSelections: value.styleSelections,
      mappingPlans: value.mappingPlans,
      variantPlans: value.variantPlans,
      drafts: value.drafts,
      executionReports: value.executionReports,
      approvals: value.approvals,
      mobileReviews: value.mobileReviews,
      editorOperations: value.editorOperations ?? [],
    });
    for (const [collection, records] of Object.entries(payload)) {
      assertImmutableIdentity(records, collection === "approvals");
    }
    for (const records of Object.values(payload)) {
      for (const record of records) assertObjectScope(this.scope, record);
    }
    assertMappingStateInvariants(payload);
    assertUniqueCurrentConfirmedMapping(payload);
    assertUniqueCurrentExecutionReport(payload);
    assertCurrentAssetMappingCapacity(payload);
    assertReferenceDraftIntegrity(payload);
    assertReferenceVariantIntegrity(payload);
    assertReferenceReportIntegrity(payload);
    assertReferenceMobileIntegrity(payload);
    assertReferenceEditorOperationIntegrity(payload);
    return { ...payload, digest: workflowDigest(payload) };
  }

  private async readSnapshotUnlocked(): Promise<ReferenceWorkflowSnapshot> {
    const raw = await readFile(this.snapshotPath, "utf8").catch((error: unknown) => {
      if (hasCode(error, "ENOENT")) return null;
      throw error;
    });
    if (raw === null) return emptySnapshot();
    const rawSnapshot = JSON.parse(raw) as Record<string, unknown>;
    const isLegacyWithoutEditorOperations = !("editorOperations" in rawSnapshot);
    assertStoredOperationMetadata(rawSnapshot.editorOperations);
    const stored = StoredSnapshotSchema.parse(rawSnapshot);
    if (stored.integrityVersion === undefined && stored.editorOperations.length > 0) {
      throw new Error("reference-snapshot-integrity-version-required");
    }
    const checked = this.prepareSnapshot(stored);
    assertNoInvalidatedAssetReuse(checked);
    const expectedDigest = isLegacyWithoutEditorOperations
      ? sha256(canonicalJson(Object.fromEntries(
        Object.entries(checked).filter(([key]) => key !== "digest" && key !== "editorOperations"),
      )))
      : checked.digest;
    if (stored.digest !== expectedDigest) throw new Error("reference-state-digest-mismatch");
    return checked;
  }

  private async writeSnapshotUnlocked(snapshot: ReferenceWorkflowSnapshot): Promise<void> {
    await this.writeAtomicJson(this.snapshotPath, {
      ...snapshot,
      integrityVersion: REFERENCE_WORKFLOW_INTEGRITY_VERSION,
    });
  }

  private async writeAtomicJson(path: string, value: unknown): Promise<void> {
    await mkdir(dirname(path), { recursive: true });
    const pending = `${path}.next-${randomUUID()}`;
    try {
      await writeFile(pending, `${canonicalJson(value)}\n`, { encoding: "utf8", flag: "wx" });
      const handle = await openFile(pending, "r+");
      try {
        await handle.sync();
      } finally {
        await handle.close();
      }
      await this.atomicRename(pending, path);
    } finally {
      await unlink(pending).catch(() => undefined);
    }
  }

  private async withRootQueue<T>(work: () => Promise<T>): Promise<T> {
    const previous = JsonReferenceSampleStore.rootQueues.get(this.root) ?? Promise.resolve();
    let release!: () => void;
    const gate = new Promise<void>((resolveGate) => { release = resolveGate; });
    const tail = previous.then(() => gate, () => gate);
    JsonReferenceSampleStore.rootQueues.set(this.root, tail);
    await previous.catch(() => undefined);
    try {
      return await work();
    } finally {
      release();
      if (JsonReferenceSampleStore.rootQueues.get(this.root) === tail) {
        JsonReferenceSampleStore.rootQueues.delete(this.root);
      }
    }
  }
}

function assertStoredOperationMetadata(value: unknown): void {
  if (value === undefined) return;
  if (!Array.isArray(value)) throw new Error("reference-operation-metadata-missing");
  for (const operation of value) {
    if (!operation || typeof operation !== "object") throw new Error("reference-operation-metadata-missing");
    const record = operation as Record<string, unknown>;
    if (typeof record.actorId !== "string" || !record.actorId ||
        typeof record.subjectId !== "string" || !record.subjectId ||
        typeof record.operationKey !== "string" || !record.operationKey ||
        !record.intent || typeof record.intent !== "object" ||
        typeof record.resultDigest !== "string" || !DigestSchema.safeParse(record.resultDigest).success) {
      throw new Error("reference-operation-metadata-missing");
    }
    try {
      const method = OperationMethodSchema.parse(record.method);
      const expected = referenceEditorOperationMetadata({
        customerId: OperationIdSchema.parse(record.customerId),
        sessionId: OperationIdSchema.parse(record.sessionId),
        actorId: record.actorId,
      }, method, record.operationKey, record.intent);
      assertOperationMetadataMatches(record, expected);
    } catch (error) {
      throw new Error("reference-operation-intent-binding-mismatch", { cause: error });
    }
  }
}

export function referenceWorkflowDigest(
  value: Omit<ReferenceWorkflowSnapshot, "digest"> | ReferenceWorkflowPayload,
): string {
  const { integrityVersion: _integrityVersion, ...payload } = value as ReferenceWorkflowPayload & {
    integrityVersion?: unknown;
  };
  return workflowDigest(SnapshotPayloadSchema.parse(payload));
}

function emptySnapshot(): ReferenceWorkflowSnapshot {
  const payload: ReferenceWorkflowPayload = {
    samples: [],
    rightsDeclarations: [],
    analyses: [],
    styleSelections: [],
    mappingPlans: [],
    variantPlans: [],
    drafts: [],
    executionReports: [],
    approvals: [],
    mobileReviews: [],
    editorOperations: [],
  };
  return { ...payload, digest: workflowDigest(payload) };
}

function workflowDigest(payload: ReferenceWorkflowPayload): string {
  return sha256(canonicalJson(payload));
}

function assertImmutableIdentity(records: readonly unknown[], approvalAudit = false): void {
  const identities = new Set<string>();
  for (const value of records) {
    if (!value || typeof value !== "object" || !("id" in value)) continue;
    const record = value as { id: unknown; version?: unknown; status?: unknown };
    const identity = approvalAudit
      ? `${String(record.id)}\0${String(record.status)}`
      : `${String(record.id)}\0${String(record.version ?? "unversioned")}`;
    if (identities.has(identity)) throw new Error("reference-duplicate-object-version");
    identities.add(identity);
  }
}

function assertMappingStateInvariants(payload: ReferenceWorkflowView): void {
  const plansById = new Map<string, ReferenceMaterialMappingPlan[]>();
  for (const plan of payload.mappingPlans) {
    const versions = plansById.get(plan.id) ?? [];
    versions.push(plan);
    plansById.set(plan.id, versions);
  }
  for (const versions of plansById.values()) {
    let invalidated = false;
    for (const plan of versions.sort((left, right) => left.version - right.version)) {
      if (invalidated) throw new Error("reference-invalidated-mapping-terminal");
      if (plan.status === "invalidated") invalidated = true;
    }
  }

  const samplesById = new Map<string, ReferenceSample[]>();
  for (const sample of payload.samples) {
    const versions = samplesById.get(sample.id) ?? [];
    versions.push(sample);
    samplesById.set(sample.id, versions);
  }
  for (const versions of samplesById.values()) {
    let invalidated = false;
    for (const sample of versions.sort((left, right) => left.version - right.version)) {
      if (invalidated) throw new Error("reference-invalidated-sample-terminal");
      if (sample.status === "invalidated") invalidated = true;
    }
  }

}

function assertNoInvalidatedAssetReuse(payload: Readonly<{
  samples: readonly ReferenceSample[];
  mappingPlans: readonly ReferenceMaterialMappingPlan[];
  editorOperations: readonly ReferenceEditorOperation[];
}>): void {
  const invalidatedAssetIds = new Set(payload.editorOperations.flatMap((operation) =>
    operation.intent.method === "invalidateForAsset" ? [operation.intent.assetId] : []));
  for (const plan of latestRecordsById(payload.mappingPlans)) {
    if (plan.status === "invalidated") continue;
    if (plan.mappings.some((mapping) =>
      mapping.sourceAssetId !== null && invalidatedAssetIds.has(mapping.sourceAssetId))) {
      throw new Error("reference-invalidated-asset-reused");
    }
  }
  for (const sample of latestRecordsById(payload.samples)) {
    if (invalidatedAssetIds.has(sample.sourceAssetId) && sample.status !== "invalidated") {
      throw new Error("reference-invalidated-asset-reused");
    }
  }
}

function appendedEditorOperations(
  current: ReferenceWorkflowSnapshot,
  next: ReferenceWorkflowSnapshot,
): ReferenceEditorOperation[] {
  if (next.editorOperations.length < current.editorOperations.length) {
    throw new Error("reference-immutable-object-conflict");
  }
  for (const [index, operation] of current.editorOperations.entries()) {
    if (canonicalJson(next.editorOperations[index]) !== canonicalJson(operation)) {
      throw new Error("reference-immutable-object-conflict");
    }
  }
  return next.editorOperations.slice(current.editorOperations.length);
}

function assertOperationKeyReuse(
  current: ReferenceWorkflowSnapshot,
  next: ReferenceWorkflowSnapshot,
): void {
  const existingById = new Map(current.editorOperations.map((operation) => [operation.id, operation]));
  for (const operation of next.editorOperations.slice(current.editorOperations.length)) {
    const existing = existingById.get(operation.id);
    if (existing && canonicalJson(existing) !== canonicalJson(operation)) {
      throw new Error("reference-operation-key-conflict");
    }
    existingById.set(operation.id, operation);
  }
}

function assertReferenceApprovalAuthorization(
  current: ReferenceWorkflowSnapshot,
  next: ReferenceWorkflowSnapshot,
): void {
  const appended = appendedApprovalRecords(current.approvals, next.approvals)
    .filter((approval): approval is ReferenceDraftApproval =>
      isReferenceDraftApproval(approval) && approval.status === "active");
  const expected = appendedEditorOperations(current, next).flatMap((operation) => {
    if (operation.intent.method !== "approveDraft" || operation.result.kind !== "approval") return [];
    const result = operation.result;
    const approval = next.approvals.find((candidate) =>
      isReferenceDraftApproval(candidate) && candidate.id === result.id &&
      candidate.version === result.version && candidate.status === "active");
    if (!approval || !isReferenceDraftApproval(approval)) {
      throw new Error("reference-operation-result-missing");
    }
    return [approval];
  });
  if (canonicalJson(appended) !== canonicalJson(expected)) {
    throw new Error("reference-approval-operation-required");
  }
}

function assertMappingStateTransitions(
  current: ReferenceWorkflowSnapshot,
  next: ReferenceWorkflowSnapshot,
): void {
  const confirmedByOperation = new Set(appendedEditorOperations(current, next).flatMap((operation) =>
    operation.intent.method === "confirmMappingPlan" && operation.result.kind === "mapping"
      ? [`${operation.result.id}\0${operation.result.version}`]
      : []));
  for (const plan of appendedVersionedRecords(current.mappingPlans, next.mappingPlans)) {
    if (plan.status === "draft") {
      if (plan.version !== 1 || current.mappingPlans.some((candidate) => candidate.id === plan.id)) {
        throw new Error("reference-mapping-draft-must-be-initial");
      }
      continue;
    }
    if (plan.status === "confirmed" && !confirmedByOperation.has(`${plan.id}\0${plan.version}`)) {
      throw new Error("reference-mapping-confirmation-operation-required");
    }
  }
}

function assertUniqueCurrentConfirmedMapping(payload: ReferenceWorkflowView): void {
  const confirmedByChain = new Map<string, string>();
  for (const plan of latestRecordsById(payload.mappingPlans)) {
    if (plan.status !== "confirmed") continue;
    const chain = canonicalJson(plan.bindings);
    const existingId = confirmedByChain.get(chain);
    if (existingId && existingId !== plan.id) {
      throw new Error("reference-current-mapping-ambiguous");
    }
    confirmedByChain.set(chain, plan.id);
  }
}

function assertUniqueCurrentExecutionReport(payload: ReferenceWorkflowView): void {
  const reportsByMapping = new Map<string, ReferenceExecutionReport[]>();
  for (const report of latestRecordsById(payload.executionReports).filter((candidate) =>
    candidate.status === "reviewable")) {
    const key = canonicalJson(report.bindings.mappingPlan);
    const reports = reportsByMapping.get(key) ?? [];
    reports.push(report);
    reportsByMapping.set(key, reports);
  }
  for (const reports of reportsByMapping.values()) {
    const highestVersion = Math.max(...reports.map((report) => report.version));
    if (reports.filter((report) => report.version === highestVersion).length > 1) {
      throw new Error("reference-current-report-ambiguous");
    }
  }
}

function assertCurrentAssetMappingCapacity(payload: ReferenceWorkflowView): void {
  const countByAsset = new Map<string, number>();
  for (const sample of latestRecordsById(payload.samples)) {
    if (sample.status === "invalidated") continue;
    const count = (countByAsset.get(sample.sourceAssetId) ?? 0) + 1;
    if (count > MAX_INVALIDATION_IDENTITIES) {
      throw new Error("reference-invalidation-identity-capacity-exceeded");
    }
    countByAsset.set(sample.sourceAssetId, count);
  }
  for (const plan of latestRecordsById(payload.mappingPlans)) {
    if (plan.status === "invalidated") continue;
    const assetIds = new Set(plan.mappings.flatMap((mapping) =>
      mapping.sourceAssetId === null ? [] : [mapping.sourceAssetId]));
    for (const assetId of assetIds) {
      // Samples and mappings have separate bounded identity lists in the
      // receipt, so mapping capacity is counted independently.
      const count = latestRecordsById(payload.mappingPlans).filter((candidate) =>
        candidate.status !== "invalidated" && candidate.mappings.some((mapping) =>
          mapping.sourceAssetId === assetId)).length;
      if (count > MAX_INVALIDATION_IDENTITIES) {
        throw new Error("reference-invalidation-identity-capacity-exceeded");
      }
    }
  }
}

type ExpectedInvalidationRecords = {
  samples: ReferenceSample[];
  styleSelections: ReferenceStyleSelection[];
  mappingPlans: ReferenceMaterialMappingPlan[];
  variantPlans: ReferenceVariantPlan[];
  drafts: ReferenceDraftPackage[];
  executionReports: ReferenceExecutionReport[];
  approvals: ReferenceWorkflowApproval[];
  mobileReviews: ReferenceMobileReviewV1[];
};

function emptyInvalidationRecords(): ExpectedInvalidationRecords {
  return {
    samples: [],
    styleSelections: [],
    mappingPlans: [],
    variantPlans: [],
    drafts: [],
    executionReports: [],
    approvals: [],
    mobileReviews: [],
  };
}

function assertInvalidationAuthorization(
  current: ReferenceWorkflowSnapshot,
  next: ReferenceWorkflowSnapshot,
): void {
  const expected = emptyInvalidationRecords();
  let staged = current;
  for (const operation of appendedEditorOperations(current, next)) {
    const transition = expectedInvalidationsForOperation(staged, next, operation);
    expected.samples.push(...transition.samples);
    expected.styleSelections.push(...transition.styleSelections);
    expected.mappingPlans.push(...transition.mappingPlans);
    expected.variantPlans.push(...transition.variantPlans);
    expected.drafts.push(...transition.drafts);
    expected.executionReports.push(...transition.executionReports);
    expected.approvals.push(...transition.approvals);
    expected.mobileReviews.push(...transition.mobileReviews);
    staged = stageExpectedOperation(staged, next, operation, transition);
    assertStagedOperationIntegrity(staged);
  }
  assertCanonicalRecordSet(
    appendedVersionedRecords(current.samples, next.samples)
      .filter((sample) => sample.status === "invalidated"),
    expected.samples,
    (sample) => `${sample.id}\0${sample.version}`,
    "reference-invalidation-downstream-mismatch",
  );
  assertCanonicalRecordSet(
    appendedVersionedRecords(current.styleSelections, next.styleSelections)
      .filter((selection) => selection.status === "invalidated"),
    expected.styleSelections,
    (selection) => `${selection.id}\0${selection.version}`,
    "reference-invalidation-downstream-mismatch",
  );
  assertCanonicalRecordSet(
    appendedVersionedRecords(current.mappingPlans, next.mappingPlans)
      .filter((plan) => plan.status === "invalidated"),
    expected.mappingPlans,
    (plan) => `${plan.id}\0${plan.version}`,
    "reference-invalidation-unattributed-transition",
  );
  assertCanonicalRecordSet(
    appendedVersionedRecords(current.variantPlans, next.variantPlans)
      .filter((plan) => plan.status === "invalidated"),
    expected.variantPlans,
    (plan) => `${plan.id}\0${plan.version}`,
    "reference-invalidation-downstream-mismatch",
  );
  assertCanonicalRecordSet(
    appendedVersionedRecords(current.drafts, next.drafts)
      .filter((draft) => draft.status === "invalidated"),
    expected.drafts,
    (draft) => `${draft.id}\0${draft.version}`,
    "reference-invalidation-downstream-mismatch",
  );
  assertCanonicalRecordSet(
    appendedVersionedRecords(current.executionReports, next.executionReports)
      .filter((report) => report.status === "invalidated"),
    expected.executionReports,
    (report) => `${report.id}\0${report.version}`,
    "reference-invalidation-downstream-mismatch",
  );
  assertCanonicalRecordSet(
    appendedApprovalRecords(current.approvals, next.approvals)
      .filter((approval) => approval.status === "invalidated"),
    expected.approvals,
    (approval) => `${approval.id}\0${approval.status}`,
    "reference-invalidation-downstream-mismatch",
  );
  assertCanonicalRecordSet(
    appendedVersionedRecords(current.mobileReviews, next.mobileReviews)
      .filter((review) => review.status === "invalidated"),
    expected.mobileReviews,
    (review) => `${review.id}\0${review.version}`,
    "reference-invalidation-downstream-mismatch",
  );
}

function expectedInvalidationsForOperation(
  current: ReferenceWorkflowSnapshot,
  next: ReferenceWorkflowSnapshot,
  operation: ReferenceEditorOperation,
): ExpectedInvalidationRecords {
  const active = latestRecordsById(current.mappingPlans).filter((plan) => plan.status !== "invalidated");
  const resultAlreadyExisted = (
    records: readonly { id: string; version: number }[],
    result: Readonly<{ id: string; version: number }>,
  ) => records.some((record) => record.id === result.id && record.version === result.version);
  const intent = operation.intent;
  const result = operation.result;
  switch (intent.method) {
    case "invalidateForAsset": {
      const samples = intent.affectedSamples === undefined
        ? []
        : latestRecordsById(current.samples).filter((sample) =>
          sample.status !== "invalidated" && sample.sourceAssetId === intent.assetId);
      const sampleIds = new Set(samples.map((sample) => sample.id));
      const mappings = active.filter((plan) =>
        sampleIds.has(plan.bindings.sample.id) || plan.mappings.some((mapping) =>
          mapping.sourceAssetId === intent.assetId));
      const transition = expectedFullMappingInvalidation(current, mappings, operation.operationKey, true);
      return {
        ...transition,
        samples: samples.map((sample) => ({
          ...sample,
          version: sample.version + 1,
          status: "invalidated" as const,
        })),
        styleSelections: expectedStyleSelectionInvalidation(current, (selection) =>
          sampleIds.has(selection.bindings.sample.id)),
      };
    }
    case "declareRights": {
      if (result.kind !== "rights" || resultAlreadyExisted(current.rightsDeclarations, result)) {
        return emptyInvalidationRecords();
      }
      const transition = expectedFullMappingInvalidation(current, active.filter((plan) =>
        plan.bindings.sample.id === intent.referenceSampleId), operation.operationKey, true);
      return {
        ...transition,
        styleSelections: expectedStyleSelectionInvalidation(current, (selection) =>
          selection.bindings.sample.id === intent.referenceSampleId),
      };
    }
    case "analyze": {
      if (result.kind !== "analysis" || resultAlreadyExisted(current.analyses, result)) {
        return emptyInvalidationRecords();
      }
      const transition = expectedFullMappingInvalidation(current, active.filter((plan) =>
        plan.bindings.sample.id === intent.sampleId &&
        (plan.bindings.analysis.id !== result.id || plan.bindings.analysis.version !== result.version)),
      operation.operationKey, true);
      return {
        ...transition,
        styleSelections: expectedStyleSelectionInvalidation(current, (selection) =>
          selection.bindings.sample.id === intent.sampleId &&
          (selection.bindings.analysis.id !== result.id ||
           selection.bindings.analysis.version !== result.version)),
      };
    }
    case "confirmStyle": {
      if (result.kind !== "selection" || resultAlreadyExisted(current.styleSelections, result)) {
        return emptyInvalidationRecords();
      }
      const selection = exactVersion(next.styleSelections, result);
      return expectedFullMappingInvalidation(current, active.filter((plan) =>
        plan.bindings.sample.id === selection.bindings.sample.id), operation.operationKey, true);
    }
    case "createMappingPlan":
      if (result.kind !== "mapping" || resultAlreadyExisted(current.mappingPlans, result)) {
        return emptyInvalidationRecords();
      }
      return expectedFullMappingInvalidation(current, active.filter((plan) =>
        plan.bindings.sample.id === intent.sampleId), operation.operationKey, true);
    case "confirmMappingPlan": {
      if (result.kind !== "mapping" || resultAlreadyExisted(current.mappingPlans, result)) {
        return emptyInvalidationRecords();
      }
      const draft = current.mappingPlans.find((plan) =>
        plan.id === result.id && plan.version === result.version - 1 && plan.status === "draft");
      return draft
        ? expectedFullMappingInvalidation(current, [draft], operation.operationKey, false)
        : emptyInvalidationRecords();
    }
    case "approveDraft":
      return emptyInvalidationRecords();
    case "render":
    case "reviseWithPrompt": {
      if (result.kind !== "report" || resultAlreadyExisted(current.executionReports, result)) {
        return emptyInvalidationRecords();
      }
      const report = exactVersion(next.executionReports, result);
      const mapping = current.mappingPlans.find((plan) =>
        plan.id === report.bindings.mappingPlan.id && plan.version === report.bindings.mappingPlan.version);
      if (!mapping) return emptyInvalidationRecords();
      const kinds = intent.method === "render" || intent.target === "editorial-master"
        ? new Set<ReferenceVariantPlan["kind"]>(["editorial-master", "xiaohongshu", "douyin", "wechat-channels"])
        : new Set<ReferenceVariantPlan["kind"]>([intent.target]);
      return expectedArtifactInvalidation(current, mapping, kinds, operation.operationKey);
    }
  }
}

function stageExpectedOperation(
  current: ReferenceWorkflowSnapshot,
  next: ReferenceWorkflowSnapshot,
  operation: ReferenceEditorOperation,
  transition: ExpectedInvalidationRecords,
): ReferenceWorkflowSnapshot {
  let samples = appendExpectedVersioned(current.samples, transition.samples);
  let rightsDeclarations = appendExpectedVersioned(current.rightsDeclarations, []);
  let analyses = appendExpectedVersioned(current.analyses, []);
  let styleSelections = appendExpectedVersioned(current.styleSelections, transition.styleSelections);
  let mappingPlans = appendExpectedVersioned(current.mappingPlans, transition.mappingPlans);
  let variantPlans = appendExpectedVersioned(current.variantPlans, transition.variantPlans);
  let drafts = appendExpectedVersioned(current.drafts, transition.drafts);
  let executionReports = appendExpectedVersioned(current.executionReports, transition.executionReports);
  let approvals = appendExpectedApprovals(current.approvals, transition.approvals);
  let mobileReviews = appendExpectedVersioned(current.mobileReviews, transition.mobileReviews);

  const result = operation.result;
  if (result.kind === "rights") {
    const rights = exactVersion(next.rightsDeclarations, result);
    samples = appendExpectedVersioned(samples, [exactVersion(next.samples, rights.sample)]);
    rightsDeclarations = appendExpectedVersioned(
      rightsDeclarations,
      [rights],
    );
  } else if (result.kind === "analysis") {
    analyses = appendExpectedVersioned(analyses, [exactVersion(next.analyses, result)]);
  } else if (result.kind === "selection") {
    styleSelections = appendExpectedVersioned(
      styleSelections,
      [exactVersion(next.styleSelections, result)],
    );
  } else if (result.kind === "mapping") {
    mappingPlans = appendExpectedVersioned(mappingPlans, [exactVersion(next.mappingPlans, result)]);
  } else if (result.kind === "report") {
    const report = exactVersion(next.executionReports, result);
    executionReports = appendExpectedVersioned(executionReports, [report]);
    variantPlans = appendExpectedVersioned(variantPlans, report.artifacts.map((artifact) =>
      exactVersion(next.variantPlans, { id: artifact.variantPlanId, version: artifact.variantPlanVersion })));
    drafts = appendExpectedVersioned(drafts, report.artifacts.map((artifact) =>
      exactVersion(next.drafts, { id: artifact.draftId, version: artifact.draftVersion })));
    if (operation.intent.method !== "render" && operation.intent.method !== "reviseWithPrompt") {
      throw new Error("reference-operation-result-kind-mismatch");
    }
    mobileReviews = appendExpectedVersioned(mobileReviews, [
      exactVersion(next.mobileReviews, operation.intent.expectedResult),
    ]);
  } else if (result.kind === "approval") {
    const approval = next.approvals.find((candidate) =>
      isReferenceDraftApproval(candidate) && candidate.id === result.id && candidate.version === result.version &&
      candidate.status === "active");
    if (!approval) throw new Error("reference-operation-result-missing");
    approvals = appendExpectedApprovals(approvals, [approval]);
  }

  return {
    ...current,
    samples,
    rightsDeclarations,
    analyses,
    styleSelections,
    mappingPlans,
    variantPlans,
    drafts,
    executionReports,
    approvals,
    mobileReviews,
    editorOperations: [...current.editorOperations, operation],
  };
}

function assertStagedOperationIntegrity(snapshot: ReferenceWorkflowSnapshot): void {
  assertMappingStateInvariants(snapshot);
  assertUniqueCurrentConfirmedMapping(snapshot);
  assertUniqueCurrentExecutionReport(snapshot);
  assertCurrentAssetMappingCapacity(snapshot);
  assertReferenceDraftIntegrity(snapshot);
  assertReferenceVariantIntegrity(snapshot);
  assertReferenceReportIntegrity(snapshot);
  assertReferenceMobileIntegrity(snapshot);
  assertReferenceEditorOperationIntegrity(snapshot);
  assertNoInvalidatedAssetReuse(snapshot);
}

function appendExpectedVersioned<T extends { id: string; version: number }>(
  current: readonly T[],
  additions: readonly T[],
): T[] {
  const records = [...current];
  const identities = new Set(records.map((record) => `${record.id}\0${record.version}`));
  for (const addition of additions) {
    const identity = `${addition.id}\0${addition.version}`;
    if (!identities.has(identity)) {
      identities.add(identity);
      records.push(addition);
    }
  }
  return records;
}

function appendExpectedApprovals(
  current: readonly ReferenceWorkflowApproval[],
  additions: readonly ReferenceWorkflowApproval[],
): ReferenceWorkflowApproval[] {
  const records = [...current];
  const identities = new Set(records.map((approval) => `${approval.id}\0${approval.status}`));
  for (const addition of additions) {
    const identity = `${addition.id}\0${addition.status}`;
    if (!identities.has(identity)) {
      identities.add(identity);
      records.push(addition);
    }
  }
  return records;
}

function expectedStyleSelectionInvalidation(
  current: ReferenceWorkflowSnapshot,
  predicate: (selection: ReferenceStyleSelection) => boolean,
): ReferenceStyleSelection[] {
  return latestRecordsById(current.styleSelections.filter(predicate))
    .filter((selection) => selection.status === "confirmed")
    .map((selection) => ({
      ...selection,
      version: selection.version + 1,
      status: "invalidated" as const,
    }));
}

function expectedFullMappingInvalidation(
  current: ReferenceWorkflowSnapshot,
  mappings: readonly ReferenceMaterialMappingPlan[],
  operationKey: string,
  invalidateMappings: boolean,
): ExpectedInvalidationRecords {
  const downstream = expectedInvalidationDownstreamTransition(current, mappings, operationKey);
  return {
    samples: [],
    styleSelections: [],
    mappingPlans: invalidateMappings ? mappings.map((plan) => ({
      ...plan,
      version: plan.version + 1,
      status: "invalidated" as const,
    })) : [],
    variantPlans: downstream.variants,
    drafts: downstream.drafts,
    executionReports: downstream.reports,
    approvals: downstream.approvals,
    mobileReviews: downstream.mobileReviews,
  };
}

function assertAssetInvalidationTransitions(
  current: ReferenceWorkflowSnapshot,
  next: ReferenceWorkflowSnapshot,
): void {
  let staged = current;
  for (const operation of appendedEditorOperations(current, next)) {
    if (operation.intent.method === "invalidateForAsset") {
      if (operation.result.kind !== "invalidation") {
        throw new Error("reference-operation-result-kind-mismatch");
      }
      const intent = operation.intent;
      const expectedSamples = intent.affectedSamples === undefined
        ? []
        : latestRecordsById(staged.samples).filter((sample) =>
          sample.status !== "invalidated" && sample.sourceAssetId === intent.assetId);
      const expectedAffectedSamples = expectedSamples
        .map(operationSampleIdentity).sort(compareOperationSampleIdentity);
      if (canonicalJson(intent.affectedSamples ?? []) !== canonicalJson(expectedAffectedSamples) ||
          canonicalJson(operation.result.affectedSamples ?? []) !== canonicalJson(expectedAffectedSamples)) {
        throw new Error("reference-invalidation-affected-set-mismatch");
      }
      const expectedInvalidatedSamples = expectedSamples.map((sample) => ({
        ...sample,
        version: sample.version + 1,
        status: "invalidated" as const,
      })).map(operationSampleIdentity).sort(compareOperationSampleIdentity);
      if (canonicalJson(operation.result.invalidatedSamples ?? []) !== canonicalJson(expectedInvalidatedSamples)) {
        throw new Error("reference-invalidation-invalidated-set-mismatch");
      }
      const affectedSampleIds = new Set(expectedSamples.map((sample) => sample.id));
      const expectedPlans = latestRecordsById(staged.mappingPlans).filter((plan) =>
        plan.status !== "invalidated" &&
        (affectedSampleIds.has(plan.bindings.sample.id) ||
          plan.mappings.some((mapping) => mapping.sourceAssetId === intent.assetId)));
      const expectedAffected = expectedPlans
        .map(operationMappingIdentity).sort(compareOperationMappingIdentity);
      if (canonicalJson(intent.affectedMappings) !== canonicalJson(expectedAffected) ||
          canonicalJson(operation.result.affectedMappings) !== canonicalJson(expectedAffected)) {
        throw new Error("reference-invalidation-affected-set-mismatch");
      }
      const expectedInvalidated = expectedPlans.map((plan) => ({
        ...plan,
        version: plan.version + 1,
        status: "invalidated" as const,
      })).map(operationMappingIdentity).sort(compareOperationMappingIdentity);
      if (canonicalJson(operation.result.invalidatedMappings) !== canonicalJson(expectedInvalidated)) {
        throw new Error("reference-invalidation-invalidated-set-mismatch");
      }
    }
    const transition = expectedInvalidationsForOperation(staged, next, operation);
    staged = stageExpectedOperation(staged, next, operation, transition);
  }
}

function expectedInvalidationDownstreamTransition(
  current: ReferenceWorkflowSnapshot,
  mappings: readonly ReferenceMaterialMappingPlan[],
  operationKey: string,
): Readonly<{
  variants: ReferenceVariantPlan[];
  drafts: ReferenceDraftPackage[];
  reports: ReferenceExecutionReport[];
  approvals: ReferenceWorkflowApproval[];
  mobileReviews: ReferenceMobileReviewV1[];
  mappingKeys: ReadonlySet<string>;
  affectedDraftIds: ReadonlySet<string>;
}> {
  const mappingIds = new Set(mappings.map((plan) => plan.id));
  const mappingKeys = new Set(current.mappingPlans.filter((plan) => mappingIds.has(plan.id))
    .map((plan) => `${plan.id}\0${plan.version}`));
  const reports = latestRecordsById(current.executionReports).filter((report) =>
    report.status === "reviewable" &&
    mappingKeys.has(`${report.bindings.mappingPlan.id}\0${report.bindings.mappingPlan.version}`));
  const variants = latestRecordsById(current.variantPlans).filter((plan) =>
    plan.status !== "invalidated" &&
    mappingKeys.has(`${plan.bindings.mappingPlan.id}\0${plan.bindings.mappingPlan.version}`));
  const invalidVariants = variants.map((plan) => ({
    ...plan,
    version: plan.version + 1,
    status: "invalidated" as const,
  }));
  const invalidVariantById = new Map(invalidVariants.map((plan) => [plan.id, plan]));
  const drafts = current.drafts.filter((draft) =>
    draft.status !== "invalidated" && invalidVariantById.has(draft.bindings.variantPlan.id));
  const invalidDrafts = drafts.map((draft) => {
    const variant = invalidVariantById.get(draft.bindings.variantPlan.id)!;
    return {
      ...draft,
      id: `${draft.id}-invalidated-${sha256(operationKey).slice(0, 12)}`,
      bindings: {
        ...draft.bindings,
        variantPlan: { ...draft.bindings.variantPlan, version: variant.version },
      },
      status: "invalidated" as const,
    };
  });
  const invalidReports = reports.map((report) => ({
    ...report,
    version: report.version + 1,
    status: "invalidated" as const,
  }));
  const reportVersions = new Map(invalidReports.map((report) => [report.id, report]));
  const affectedDraftIds = new Set(drafts.map((draft) => draft.id));
  const invalidApprovals = current.approvals.filter((approval) =>
    approval.status === "active" && affectedDraftIds.has(approvalDraftId(approval)))
    .map((approval) => ({ ...approval, status: "invalidated" as const }));
  const invalidMobileReviews = current.mobileReviews.filter((review) =>
    reportVersions.get(review.bindings.executionReport.id)?.version ===
      review.bindings.executionReport.version + 1 && review.status !== "invalidated")
    .map((review) => {
      const report = reportVersions.get(review.bindings.executionReport.id)!;
      return {
        ...review,
        id: `${review.id}-invalidated-${sha256(operationKey).slice(0, 12)}`,
        bindings: {
          ...review.bindings,
          executionReport: { ...review.bindings.executionReport, version: report.version },
        },
        artifacts: review.artifacts.map((artifact) => ({ ...artifact, status: "invalidated" as const })),
        executionReport: { ...review.executionReport, version: report.version },
        status: "invalidated" as const,
      };
    });

  return {
    variants: invalidVariants,
    drafts: invalidDrafts,
    reports: invalidReports,
    approvals: invalidApprovals,
    mobileReviews: invalidMobileReviews,
    mappingKeys,
    affectedDraftIds,
  };
}

function expectedArtifactInvalidation(
  current: ReferenceWorkflowSnapshot,
  mapping: ReferenceMaterialMappingPlan,
  kinds: ReadonlySet<ReferenceVariantPlan["kind"]>,
  operationKey: string,
): ExpectedInvalidationRecords {
  const report = current.executionReports.filter((candidate) =>
    candidate.bindings.mappingPlan.id === mapping.id &&
    candidate.bindings.mappingPlan.version === mapping.version)
    .sort((left, right) => right.version - left.version)[0];
  if (!report || report.status !== "reviewable") return emptyInvalidationRecords();
  const targets = report.artifacts.filter((artifact) => kinds.has(artifact.kind));
  if (targets.length === 0) return emptyInvalidationRecords();
  const variants = targets.map((artifact) => {
    const plan = current.variantPlans.find((candidate) =>
      candidate.id === artifact.variantPlanId && candidate.version === artifact.variantPlanVersion);
    if (!plan) throw new Error("reference-render-audit-artifact-missing");
    return { ...plan, version: plan.version + 1, status: "invalidated" as const };
  });
  const variantsById = new Map(variants.map((plan) => [plan.id, plan]));
  const drafts = targets.map((artifact) => {
    const draft = current.drafts.find((candidate) =>
      candidate.id === artifact.draftId && candidate.version === artifact.draftVersion);
    const variant = variantsById.get(artifact.variantPlanId);
    if (!draft || !variant) throw new Error("reference-render-audit-artifact-missing");
    return {
      ...draft,
      id: `${draft.id}-invalidated-${sha256(operationKey).slice(0, 12)}`,
      bindings: {
        ...draft.bindings,
        variantPlan: { ...draft.bindings.variantPlan, version: variant.version },
      },
      status: "invalidated" as const,
    };
  });
  const invalidReport = { ...report, version: report.version + 1, status: "invalidated" as const };
  const affectedDraftIds = new Set(targets.map((artifact) => artifact.draftId));
  const approvals = current.approvals.filter((approval) =>
    approval.status === "active" && affectedDraftIds.has(approvalDraftId(approval)))
    .map((approval) => ({ ...approval, status: "invalidated" as const }));
  const mobileReviews = current.mobileReviews.filter((review) =>
    review.bindings.executionReport.id === report.id &&
    review.bindings.executionReport.version === report.version && review.status !== "invalidated")
    .map((review) => ({
      ...review,
      id: `${review.id}-invalidated-${sha256(operationKey).slice(0, 12)}`,
      bindings: {
        ...review.bindings,
        executionReport: { ...review.bindings.executionReport, version: invalidReport.version },
      },
      artifacts: review.artifacts.map((artifact) =>
        kinds.has(artifact.kind) ? { ...artifact, status: "invalidated" as const } : artifact),
      executionReport: { ...review.executionReport, version: invalidReport.version },
      status: "invalidated" as const,
    }));
  return {
    samples: [],
    styleSelections: [],
    mappingPlans: [],
    variantPlans: variants,
    drafts,
    executionReports: [invalidReport],
    approvals,
    mobileReviews,
  };
}

function latestRecordsById<T extends { id: string; version: number }>(records: readonly T[]): T[] {
  const latest = new Map<string, T>();
  for (const record of records) {
    const current = latest.get(record.id);
    if (!current || record.version > current.version) latest.set(record.id, record);
  }
  return [...latest.values()];
}

function appendedVersionedRecords<T extends { id: string; version: number }>(
  current: readonly T[],
  next: readonly T[],
): T[] {
  const identities = new Set(current.map((record) => `${record.id}\0${record.version}`));
  return next.filter((record) => !identities.has(`${record.id}\0${record.version}`));
}

function appendedApprovalRecords(
  current: readonly ReferenceWorkflowApproval[],
  next: readonly ReferenceWorkflowApproval[],
): ReferenceWorkflowApproval[] {
  const identities = new Set(current.map((approval) => `${approval.id}\0${approval.status}`));
  return next.filter((approval) => !identities.has(`${approval.id}\0${approval.status}`));
}

function isReferenceDraftApproval(
  approval: ReferenceWorkflowApproval,
): approval is ReferenceDraftApproval {
  return "kind" in approval && approval.kind === "reference-draft";
}

function approvalDraftId(approval: ReferenceWorkflowApproval): string {
  return isReferenceDraftApproval(approval) ? approval.bindings.draft.id : approval.artifactId;
}

function referenceApprovalMatchesReview(
  approval: ReferenceWorkflowApproval,
  review: ReferenceMobileReviewV1,
  draft: ReferenceDraftPackage,
  variant: ReferenceVariantPlan["kind"],
  allowLegacyApproval: boolean,
): boolean {
  if (isReferenceDraftApproval(approval)) {
    return approval.bindings.executionReport.id === review.bindings.executionReport.id &&
      approval.bindings.executionReport.version === review.bindings.executionReport.version &&
      approval.bindings.mappingPlan.id === review.bindings.mappingPlan.id &&
      approval.bindings.mappingPlan.version === review.bindings.mappingPlan.version &&
      approval.bindings.mappingPlan.contentDigest === review.bindings.mappingPlan.contentDigest &&
      approval.bindings.mappingPlan.permissionSetDigest === review.bindings.mappingPlan.permissionSetDigest &&
      approval.bindings.draft.id === draft.id && approval.bindings.draft.version === draft.version &&
      approval.bindings.draft.outputDigest === draft.outputDigest && approval.fileDigest === draft.outputDigest &&
      approval.variant === variant;
  }
  return allowLegacyApproval && approval.storyId === review.bindings.sample.id &&
    approval.revisionId === review.bindings.mappingPlan.id &&
    approval.revision === review.bindings.mappingPlan.version &&
    approval.artifactId === draft.id && approval.artifactDigest === draft.outputDigest &&
    approval.renderInputDigest === review.bindings.mappingPlan.contentDigest &&
    approval.variant === variant;
}

function assertCanonicalRecordSet<T>(
  actual: readonly T[],
  expected: readonly T[],
  identity: (value: T) => string,
  error: string,
): void {
  const normalize = (values: readonly T[]) => values
    .map((value) => ({ identity: identity(value), value }))
    .sort((left, right) => left.identity < right.identity ? -1 : left.identity > right.identity ? 1 : 0);
  if (canonicalJson(normalize(actual)) !== canonicalJson(normalize(expected))) throw new Error(error);
}

function assertReferenceDraftIntegrity(payload: ReferenceWorkflowView): void {
  for (const draft of payload.drafts) {
    const mappingPlan = payload.mappingPlans.find((candidate) =>
      candidate.id === draft.bindings.mappingPlan.id &&
      candidate.version === draft.bindings.mappingPlan.version);
    if (!mappingPlan) throw new Error("reference-draft-mapping-plan-missing");
    if (canonicalJson(mappingPlanBinding(mappingPlan)) !== canonicalJson(draft.bindings.mappingPlan)) {
      throw new Error("reference-draft-mapping-plan-mismatch");
    }
    const variantPlan = payload.variantPlans.find((candidate) =>
      candidate.id === draft.bindings.variantPlan.id &&
      candidate.version === draft.bindings.variantPlan.version);
    if (!variantPlan) throw new Error("reference-draft-variant-plan-missing");
    const variantBinding = {
      id: variantPlan.id,
      version: variantPlan.version,
      customerId: variantPlan.customerId,
      sessionId: variantPlan.sessionId,
      kind: variantPlan.kind,
    };
    if (canonicalJson(variantBinding) !== canonicalJson(draft.bindings.variantPlan) ||
        canonicalJson(variantPlan.bindings.mappingPlan) !== canonicalJson(draft.bindings.mappingPlan)) {
      throw new Error("reference-draft-variant-plan-mismatch");
    }
    assertVariantMappingIntegrity(mappingPlan, variantPlan);
    const sources = new Map(variantPlan.authorizedSources.map((source) => [source.id, source]));
    const expectedLineage: Array<Omit<ReferenceDraftPackage["lineage"][number], "id">> =
      variantPlan.timeline.map((entry) => {
        const source = sources.get(entry.sourceBindingId);
        if (!source) throw new Error("reference-draft-lineage-mismatch");
        return {
          use: "video" as const,
          timelineEntryId: entry.id,
          mappingId: entry.mappingId,
          sourceBindingId: entry.sourceBindingId,
          sourceAssetId: source.sourceAssetId,
          sourceDigest: source.sourceDigest,
          sourceStartMs: entry.sourceStartMs,
          sourceEndMs: entry.sourceEndMs,
        };
      });
    if (variantPlan.audioOverlay) {
      const source = sources.get(variantPlan.audioOverlay.sourceBindingId);
      if (!source) throw new Error("reference-draft-lineage-mismatch");
      expectedLineage.push({
        use: "audio-overlay",
        timelineEntryId: null,
        mappingId: source.mappingId,
        sourceBindingId: source.id,
        sourceAssetId: source.sourceAssetId,
        sourceDigest: source.sourceDigest,
        sourceStartMs: variantPlan.audioOverlay.sourceStartMs,
        sourceEndMs: variantPlan.audioOverlay.sourceEndMs,
      });
    }
    const actualLineage = draft.lineage.map(({ id: _id, ...lineage }) => lineage);
    if (canonicalJson(actualLineage) !== canonicalJson(expectedLineage)) {
      throw new Error("reference-draft-lineage-mismatch");
    }
  }
}

function assertReferenceVariantIntegrity(payload: ReferenceWorkflowView): void {
  for (const variantPlan of payload.variantPlans) {
    const mappingPlan = payload.mappingPlans.find((candidate) =>
      candidate.id === variantPlan.bindings.mappingPlan.id &&
      candidate.version === variantPlan.bindings.mappingPlan.version);
    if (!mappingPlan) throw new Error("reference-variant-mapping-plan-missing");
    if (canonicalJson(mappingPlanBinding(mappingPlan)) !== canonicalJson(variantPlan.bindings.mappingPlan)) {
      throw new Error("reference-variant-mapping-plan-mismatch");
    }
    assertVariantMappingIntegrity(mappingPlan, variantPlan);
  }
}

function assertReferenceReportIntegrity(payload: ReferenceWorkflowView): void {
  for (const report of payload.executionReports) {
    const seen = new Set<string>();
    for (const artifact of report.artifacts) {
      if (seen.has(artifact.kind)) throw new Error("reference-report-artifact-set-mismatch");
      seen.add(artifact.kind);
      const plan = payload.variantPlans.find((candidate) =>
        candidate.id === artifact.variantPlanId && candidate.version === artifact.variantPlanVersion);
      const draft = payload.drafts.find((candidate) =>
        candidate.id === artifact.draftId && candidate.version === artifact.draftVersion);
      if (!plan || !draft) throw new Error("reference-report-artifact-missing");
      if (plan.kind !== artifact.kind || draft.outputDigest !== artifact.outputDigest ||
          draft.bindings.variantPlan.id !== plan.id || draft.bindings.variantPlan.version !== plan.version ||
          canonicalJson(plan.bindings) !== canonicalJson(report.bindings)) {
        throw new Error("reference-report-artifact-mismatch");
      }
    }
  }
}

function assertReferenceMobileIntegrity(payload: ReferenceWorkflowView): void {
  const invalidatedApprovalIds = new Set(payload.approvals
    .filter((approval) => approval.status === "invalidated").map((approval) => approval.id));
  for (const review of payload.mobileReviews) {
    const report = payload.executionReports.find((candidate) =>
      candidate.id === review.bindings.executionReport.id &&
      candidate.version === review.bindings.executionReport.version);
    if (!report) throw new Error("reference-mobile-report-missing");
    const embedded = {
      id: report.id,
      version: report.version,
      artifacts: report.artifacts,
      rhythm: report.rhythm,
      transitions: report.transitions,
      narrative: report.narrative,
      audio: report.audio,
      mappingCounts: report.mappingCounts,
      platformChanges: report.platformChanges,
    };
    const { executionReport: _executionReport, ...reviewBindings } = review.bindings;
    if (canonicalJson(review.executionReport) !== canonicalJson(embedded) ||
        canonicalJson(reviewBindings) !== canonicalJson(report.bindings) ||
        (review.status === "reviewable" && report.status !== "reviewable") ||
        (review.status === "invalidated" && report.status !== "invalidated")) {
      throw new Error("reference-mobile-report-mismatch");
    }
    const allowLegacyApproval = legacyApprovalCanBindReview(payload.executionReports, review);
    for (const artifact of review.artifacts) {
      const reportArtifact = report.artifacts.find((candidate) => candidate.kind === artifact.kind);
      const draft = payload.drafts.find((candidate) =>
        candidate.id === artifact.draftId && candidate.version === artifact.draftVersion);
      const { status: _status, mediaRoute: _mediaRoute, ...identity } = artifact;
      if (!reportArtifact || !draft || canonicalJson(identity) !== canonicalJson(reportArtifact)) {
        throw new Error("reference-mobile-artifact-mismatch");
      }
      const exactApproval = payload.approvals.find((approval) =>
        referenceApprovalMatchesReview(approval, review, draft, artifact.kind, allowLegacyApproval));
      const activeApproval = payload.approvals.find((approval) => approval.status === "active" &&
        !invalidatedApprovalIds.has(approval.id) &&
        referenceApprovalMatchesReview(approval, review, draft, artifact.kind, allowLegacyApproval));
      const supersededAudit = payload.mobileReviews.some((candidate) => candidate.status === "invalidated" &&
        candidate.bindings.mappingPlan.id === review.bindings.mappingPlan.id &&
        candidate.artifacts.some((candidateArtifact) =>
          candidateArtifact.kind === artifact.kind && candidateArtifact.draftId === artifact.draftId &&
          candidateArtifact.draftVersion === artifact.draftVersion &&
          candidateArtifact.outputDigest === artifact.outputDigest));
      const approvalValid = review.status === "invalidated" || supersededAudit ? exactApproval : activeApproval;
      if (artifact.status === "approved" && (!approvalValid || draft.status === "invalidated")) {
        throw new Error("reference-mobile-approval-mismatch");
      }
      if (artifact.status === "reviewable" && draft.status !== "reviewable") {
        throw new Error("reference-mobile-draft-status-mismatch");
      }
      if (artifact.status === "invalidated" && review.status !== "invalidated") {
        throw new Error("reference-mobile-draft-status-mismatch");
      }
    }
    if (review.status === "approved" && !review.artifacts.every((artifact) => artifact.status === "approved")) {
      throw new Error("reference-mobile-approval-mismatch");
    }
    if (review.status !== "invalidated" &&
        review.artifacts.every((artifact) => artifact.status === "approved") && review.status !== "approved") {
      throw new Error("reference-mobile-approval-mismatch");
    }
    if (review.status === "reviewable" && review.artifacts.some((artifact) => artifact.status === "invalidated")) {
      throw new Error("reference-mobile-draft-status-mismatch");
    }
  }
}

function legacyApprovalCanBindReview(
  reports: readonly ReferenceExecutionReport[],
  review: ReferenceMobileReviewV1,
): boolean {
  if (review.status === "invalidated") return true;
  const mappingReports = reports.filter((candidate) =>
    candidate.customerId === review.customerId && candidate.sessionId === review.sessionId &&
    candidate.bindings.mappingPlan.id === review.bindings.mappingPlan.id &&
    candidate.bindings.mappingPlan.version === review.bindings.mappingPlan.version);
  const reviewableReports = latestRecordsById(mappingReports)
    .filter((candidate) => candidate.status === "reviewable");
  const highestVersion = reviewableReports.length === 0
    ? null
    : Math.max(...reviewableReports.map((candidate) => candidate.version));
  const currentReports = highestVersion === null
    ? []
    : reviewableReports.filter((candidate) => candidate.version === highestVersion);
  const reviewIsCurrent = currentReports.length === 1 && currentReports.some((candidate) =>
    candidate.id === review.bindings.executionReport.id &&
    candidate.version === review.bindings.executionReport.version);
  if (!reviewIsCurrent) return true;
  return !mappingReports.some((candidate) =>
    (candidate.id !== review.bindings.executionReport.id ||
      candidate.version !== review.bindings.executionReport.version));
}

function assertReferenceEditorOperationIntegrity(payload: ReferenceWorkflowView): void {
  for (const operation of payload.editorOperations) {
    const expected = referenceEditorOperationMetadata({
      customerId: operation.customerId,
      sessionId: operation.sessionId,
      actorId: operation.actorId,
    }, operation.method, operation.operationKey, operation.intent);
    assertOperationMetadataMatches(operation, expected);
    if (operation.intent.method === "invalidateForAsset") {
      const intent = operation.intent;
      if (operation.result.kind !== "invalidation") {
        throw new Error("reference-operation-result-kind-mismatch");
      }
      assertOperationResultDigest(operation, operation.result);
      if (operation.result.assetId !== intent.assetId ||
          operation.result.reason !== intent.reason ||
          canonicalJson(operation.result.affectedSamples ?? []) !== canonicalJson(intent.affectedSamples ?? []) ||
          canonicalJson(operation.result.affectedMappings) !== canonicalJson(intent.affectedMappings) ||
          (operation.result.invalidatedSamples ?? []).length !== (intent.affectedSamples ?? []).length ||
          operation.result.invalidatedMappings.length !== intent.affectedMappings.length) {
        throw new Error("reference-operation-result-binding-mismatch");
      }
      for (const [index, affected] of (intent.affectedSamples ?? []).entries()) {
        const before = exactVersion(payload.samples, affected);
        const invalidatedIdentity = operation.result.invalidatedSamples?.[index];
        if (before.sourceDigest !== affected.sourceDigest || before.status === "invalidated" ||
            before.sourceAssetId !== intent.assetId || !invalidatedIdentity ||
            invalidatedIdentity.id !== affected.id || invalidatedIdentity.version !== affected.version + 1) {
          throw new Error("reference-operation-result-binding-mismatch");
        }
        const invalidated = exactVersion(payload.samples, invalidatedIdentity);
        if (invalidated.status !== "invalidated" || invalidated.sourceDigest !== affected.sourceDigest ||
            invalidated.sourceDigest !== invalidatedIdentity.sourceDigest) {
          throw new Error("reference-operation-result-binding-mismatch");
        }
      }
      const affectedSampleIds = new Set((intent.affectedSamples ?? []).map((sample) => sample.id));
      for (const [index, affected] of intent.affectedMappings.entries()) {
        const before = exactVersion(payload.mappingPlans, affected);
        const invalidatedIdentity = operation.result.invalidatedMappings[index];
        if (before.contentDigest !== affected.contentDigest || before.status === "invalidated" ||
            (!affectedSampleIds.has(before.bindings.sample.id) &&
              !before.mappings.some((mapping) => mapping.sourceAssetId === intent.assetId)) ||
            !invalidatedIdentity || invalidatedIdentity.id !== affected.id ||
            invalidatedIdentity.version !== affected.version + 1) {
          throw new Error("reference-operation-result-binding-mismatch");
        }
        const invalidated = exactVersion(payload.mappingPlans, invalidatedIdentity);
        if (invalidated.status !== "invalidated" || invalidated.contentDigest !== affected.contentDigest ||
            invalidated.contentDigest !== invalidatedIdentity.contentDigest) {
          throw new Error("reference-operation-result-binding-mismatch");
        }
      }
      continue;
    }
    if (operation.intent.method === "approveDraft") {
      const intent = operation.intent;
      if (operation.result.kind !== "approval") {
        throw new Error("reference-operation-result-kind-mismatch");
      }
      const operationResult = operation.result;
      const approval = payload.approvals.find((candidate) =>
        isReferenceDraftApproval(candidate) && candidate.id === operationResult.id &&
        candidate.version === operationResult.version && candidate.status === "active");
      if (!approval || !isReferenceDraftApproval(approval)) {
        throw new Error("reference-operation-result-missing");
      }
      assertOperationResultScope(operation, approval);
      assertOperationResultDigest(operation, approval);
      const report = payload.executionReports.find((candidate) =>
        candidate.id === intent.report.id && candidate.version === intent.report.version);
      const draft = payload.drafts.find((candidate) =>
        candidate.id === intent.draft.id && candidate.version === intent.draft.version);
      const mapping = payload.mappingPlans.find((candidate) =>
        candidate.id === intent.mappingPlan.id && candidate.version === intent.mappingPlan.version);
      if (!report || !draft || !mapping) {
        throw new Error("reference-operation-result-binding-mismatch");
      }
      const reportArtifact = report.artifacts.find((artifact) => artifact.kind === intent.variant);
      const latestReport = latestRecordsById(payload.executionReports)
        .find((candidate) => candidate.id === report.id);
      const latestDraft = latestRecordsById(payload.drafts)
        .find((candidate) => candidate.id === draft.id);
      const latestMapping = latestRecordsById(payload.mappingPlans)
        .find((candidate) => candidate.id === mapping.id);
      const invalidatedApproval = payload.approvals.find((candidate) =>
        isReferenceDraftApproval(candidate) && candidate.id === approval.id &&
        candidate.version === approval.version && candidate.status === "invalidated");
      const hasExactInvalidationAudit = invalidatedApproval !== undefined &&
        canonicalJson({ ...invalidatedApproval, status: "active" }) === canonicalJson(approval);
      const approvalInputsRemainCurrent = latestReport?.version === report.version &&
        latestDraft?.version === draft.version && latestMapping?.version === mapping.version;
      if (!sameResultIdentity(intent.expectedResult, operation.result) ||
          canonicalJson(approval.bindings.executionReport) !== canonicalJson(intent.report) ||
          canonicalJson(approval.bindings.draft) !== canonicalJson(intent.draft) ||
          canonicalJson(approval.bindings.mappingPlan) !== canonicalJson(intent.mappingPlan) ||
          approval.variant !== intent.variant || approval.fileDigest !== intent.fileDigest ||
          approval.reviewerId !== operation.actorId || approval.publishClaimed !== false ||
          report.status !== "reviewable" || draft.status !== "reviewable" || mapping.status !== "confirmed" ||
          (!approvalInputsRemainCurrent && !hasExactInvalidationAudit) ||
          mapping.contentDigest !== intent.mappingPlan.contentDigest ||
          mapping.permissionSetDigest !== intent.mappingPlan.permissionSetDigest ||
          draft.outputDigest !== intent.draft.outputDigest || intent.fileDigest !== draft.outputDigest ||
          draft.bindings.variantPlan.kind !== intent.variant ||
          draft.bindings.mappingPlan.id !== mapping.id ||
          draft.bindings.mappingPlan.version !== mapping.version ||
          draft.bindings.mappingPlan.contentDigest !== mapping.contentDigest ||
          draft.bindings.mappingPlan.permissionSetDigest !== mapping.permissionSetDigest ||
          !reportArtifact || reportArtifact.draftId !== draft.id ||
          reportArtifact.draftVersion !== draft.version ||
          reportArtifact.outputDigest !== draft.outputDigest ||
          report.bindings.mappingPlan.id !== mapping.id ||
          report.bindings.mappingPlan.version !== mapping.version ||
          report.bindings.mappingPlan.contentDigest !== mapping.contentDigest ||
          report.bindings.mappingPlan.permissionSetDigest !== mapping.permissionSetDigest) {
        throw new Error("reference-operation-result-binding-mismatch");
      }
      continue;
    }
    if (operation.intent.method === "declareRights") {
      if (operation.result.kind !== "rights") throw new Error("reference-operation-result-kind-mismatch");
      const result = exactVersion(payload.rightsDeclarations, operation.result);
      assertOperationResultScope(operation, result);
      assertOperationResultDigest(operation, result);
      if (!sameResultIdentity(operation.intent.expectedResult, operation.result) ||
          result.sample.id !== operation.intent.referenceSampleId || result.actorId !== operation.actorId ||
          result.sourceKind !== operation.intent.sourceKind ||
          canonicalJson(result.reusableElements) !== canonicalJson(operation.intent.reusableElements) ||
          sha256(canonicalJson(result.sample)) !== operation.intent.sampleBindingDigest ||
          !payload.samples.some((sample) => canonicalJson(referenceSampleBinding(sample)) === canonicalJson(result.sample))) {
        throw new Error("reference-operation-result-binding-mismatch");
      }
      continue;
    }
    if (operation.intent.method === "analyze") {
      if (operation.result.kind !== "analysis") throw new Error("reference-operation-result-kind-mismatch");
      const result = exactVersion(payload.analyses, operation.result);
      assertOperationResultScope(operation, result);
      assertOperationResultDigest(operation, result);
      if (!sameResultIdentity(operation.intent.expectedResult, operation.result) ||
          result.bindings.sample.id !== operation.intent.sampleId ||
          sha256(canonicalJson(result.bindings)) !== operation.intent.inputBindingDigest ||
          !hasExactAnalysisInputs(payload, result.bindings)) {
        throw new Error("reference-operation-result-binding-mismatch");
      }
      continue;
    }
    if (operation.intent.method === "confirmStyle") {
      if (operation.result.kind !== "selection") throw new Error("reference-operation-result-kind-mismatch");
      const result = exactVersion(payload.styleSelections, operation.result);
      assertOperationResultScope(operation, result);
      assertOperationResultDigest(operation, result);
      if (!sameResultIdentity(operation.intent.expectedResult, operation.result) ||
          result.bindings.analysis.id !== operation.intent.analysisId || result.status !== "confirmed" ||
          result.actorId !== operation.actorId ||
          canonicalJson(result.selectedRuleIds) !== canonicalJson(operation.intent.selectedRuleIds) ||
          sha256(canonicalJson(result.bindings)) !== operation.intent.inputBindingDigest ||
          !hasExactSelectionInputs(payload, result)) {
        throw new Error("reference-operation-result-binding-mismatch");
      }
      continue;
    }
    if (operation.intent.method === "createMappingPlan") {
      if (operation.result.kind !== "mapping") throw new Error("reference-operation-result-kind-mismatch");
      const result = exactVersion(payload.mappingPlans, operation.result);
      assertOperationResultScope(operation, result);
      assertOperationResultDigest(operation, result);
      if (!sameResultIdentity(operation.intent.expectedResult, operation.result) ||
          result.bindings.sample.id !== operation.intent.sampleId || result.status !== "draft" ||
          sha256(canonicalJson(result.bindings)) !== operation.intent.inputBindingDigest ||
          !hasExactMappingInputs(payload, result.bindings)) {
        throw new Error("reference-operation-result-binding-mismatch");
      }
      continue;
    }
    if (operation.intent.method === "confirmMappingPlan") {
      if (operation.result.kind !== "mapping") throw new Error("reference-operation-result-kind-mismatch");
      const result = exactVersion(payload.mappingPlans, operation.result);
      assertOperationResultScope(operation, result);
      assertOperationResultDigest(operation, result);
      const draft = payload.mappingPlans.find((candidate) =>
        candidate.id === result.id && candidate.version === result.version - 1);
      let expectedConfirmed: ReferenceMaterialMappingPlan | null = null;
      if (draft?.status === "draft" && result.confirmedAt !== null) {
        try {
          expectedConfirmed = {
            ...confirmReferenceMaterialMappingPlan(draft, {
              id: draft.id,
              version: draft.version,
              contentDigest: draft.contentDigest,
            }, operation.actorId, result.confirmedAt),
            version: draft.version + 1,
          };
        } catch {
          expectedConfirmed = null;
        }
      }
      if (!sameResultIdentity(operation.intent.expectedResult, operation.result) ||
          result.id !== operation.intent.planId || result.status !== "confirmed" ||
          result.confirmedByActorId !== operation.actorId || !draft || draft.status !== "draft" ||
          sha256(canonicalJson(draft)) !== operation.intent.inputBindingDigest ||
          canonicalJson(mappingImmutableContent(draft)) !== canonicalJson(mappingImmutableContent(result)) ||
          !expectedConfirmed || canonicalJson(result) !== canonicalJson(expectedConfirmed) ||
          !hasExactMappingInputs(payload, result.bindings)) {
        throw new Error("reference-operation-result-binding-mismatch");
      }
      continue;
    }
    if (operation.result.kind !== "report") throw new Error("reference-operation-result-kind-mismatch");
    const result = exactVersion(payload.executionReports, operation.result);
    assertOperationResultScope(operation, result);
    assertOperationResultDigest(operation, result);
    const resultIntent = operation.intent;
    if (resultIntent.method !== "render" && resultIntent.method !== "reviseWithPrompt") {
      throw new Error("reference-operation-result-kind-mismatch");
    }
    const mobile = payload.mobileReviews.find((candidate) =>
      candidate.id === resultIntent.expectedResult.id &&
      candidate.version === resultIntent.expectedResult.version);
    const operationMetadata = result.operation;
    let expectedInputBindingDigest = sha256(canonicalJson(result.bindings));
    let exactRevisionIdentity = true;
    if (resultIntent.method === "reviseWithPrompt" && (resultIntent.report || resultIntent.draft)) {
      if (!resultIntent.report || !resultIntent.draft) {
        exactRevisionIdentity = false;
      } else {
        const priorReport = payload.executionReports.find((candidate) =>
          candidate.id === resultIntent.report!.id && candidate.version === resultIntent.report!.version);
        const priorDraft = payload.drafts.find((candidate) =>
          candidate.id === resultIntent.draft!.id && candidate.version === resultIntent.draft!.version);
        const priorArtifact = priorReport?.artifacts.find((artifact) =>
          artifact.kind === resultIntent.target && artifact.draftId === resultIntent.draft!.id &&
          artifact.draftVersion === resultIntent.draft!.version &&
          artifact.outputDigest === resultIntent.draft!.outputDigest);
        exactRevisionIdentity = Boolean(priorReport && priorReport.status === "reviewable" &&
          priorDraft && priorDraft.status === "reviewable" && priorArtifact &&
          priorDraft.outputDigest === resultIntent.draft.outputDigest &&
          priorReport.bindings.mappingPlan.id === result.bindings.mappingPlan.id &&
          priorReport.bindings.mappingPlan.version === result.bindings.mappingPlan.version);
        expectedInputBindingDigest = sha256(canonicalJson({
          workflow: result.bindings,
          current: {
            report: resultIntent.report,
            draft: resultIntent.draft,
            target: resultIntent.target,
          },
          promptDigest: resultIntent.promptDigest,
        }));
      }
    }
    const methodValid = resultIntent.method === "render"
      ? result.bindings.mappingPlan.id === resultIntent.planId &&
        operationMetadata?.target === null && operationMetadata.promptDigest === null
      : result.bindings.sample.id === resultIntent.sampleId &&
        operationMetadata?.target === resultIntent.target &&
        operationMetadata.promptDigest === resultIntent.promptDigest;
    if (!sameResultIdentity({
      id: resultIntent.expectedResult.reportId,
      version: resultIntent.expectedResult.reportVersion,
    }, operation.result) || !methodValid || result.status !== "reviewable" ||
        !exactRevisionIdentity || expectedInputBindingDigest !== resultIntent.inputBindingDigest ||
        !operationMetadata || operationMetadata.operationId !== operation.id ||
        operationMetadata.method !== resultIntent.method ||
        operationMetadata.inputBindingDigest !== resultIntent.inputBindingDigest ||
        !mobile || mobile.bindings.executionReport.id !== result.id ||
        mobile.bindings.executionReport.version !== result.version) {
      throw new Error("reference-operation-result-binding-mismatch");
    }
  }
}

function assertOperationMetadataMatches(
  operation: Record<string, unknown> | ReferenceEditorOperation,
  expected: ReferenceEditorOperationMetadata,
): void {
  const actual = {
    id: operation.id,
    customerId: operation.customerId,
    sessionId: operation.sessionId,
    version: operation.version,
    operationKey: operation.operationKey,
    operationKeyDigest: operation.operationKeyDigest,
    actorId: operation.actorId,
    subjectId: operation.subjectId,
    method: operation.method,
    intent: operation.intent,
    intentDigest: operation.intentDigest,
  };
  if (canonicalJson(actual) !== canonicalJson(expected)) {
    throw new Error("reference-operation-intent-binding-mismatch");
  }
}

function assertOperationResultDigest(operation: ReferenceEditorOperation, result: unknown): void {
  if (operation.resultDigest !== sha256(canonicalJson(result))) {
    throw new Error("reference-operation-result-digest-mismatch");
  }
}

function exactVersion<T extends { id: string; version: number }>(
  values: readonly T[],
  identity: Readonly<{ id: string; version: number }>,
): T {
  const result = values.find((candidate) => candidate.id === identity.id && candidate.version === identity.version);
  if (!result) throw new Error("reference-operation-result-missing");
  return result;
}

function assertOperationResultScope(
  operation: ReferenceEditorOperation,
  result: Readonly<{ customerId: string; sessionId: string }>,
): void {
  if (result.customerId !== operation.customerId || result.sessionId !== operation.sessionId) {
    throw new Error("reference-operation-result-scope-mismatch");
  }
}

function sameResultIdentity(
  expected: Readonly<{ id: string; version: number }>,
  actual: Readonly<{ id: string; version: number }>,
): boolean {
  return expected.id === actual.id && expected.version === actual.version;
}

function hasExactAnalysisInputs(
  payload: ReferenceWorkflowView,
  bindings: ReferenceStyleAnalysis["bindings"],
): boolean {
  return payload.samples.some((sample) =>
    canonicalJson(referenceSampleBinding(sample)) === canonicalJson(bindings.sample)) &&
    payload.rightsDeclarations.some((rights) =>
      canonicalJson(referenceRightsBinding(rights)) === canonicalJson(bindings.rights));
}

function hasExactSelectionInputs(
  payload: ReferenceWorkflowView,
  selection: ReferenceStyleSelection,
): boolean {
  const analysis = payload.analyses.find((candidate) =>
    canonicalJson(scopedContentBinding(candidate)) === canonicalJson(selection.bindings.analysis));
  return Boolean(analysis &&
    hasExactAnalysisInputs(payload, {
      sample: selection.bindings.sample,
      rights: selection.bindings.rights,
    }) &&
    canonicalJson(analysis.bindings.sample) === canonicalJson(selection.bindings.sample) &&
    canonicalJson(analysis.bindings.rights) === canonicalJson(selection.bindings.rights) &&
    selection.selectedRuleIds.every((ruleId) => analysis.rules.some((rule) => rule.id === ruleId)));
}

function hasExactMappingInputs(
  payload: ReferenceWorkflowView,
  bindings: ReferenceMaterialMappingPlan["bindings"],
): boolean {
  const analysis = payload.analyses.find((candidate) =>
    canonicalJson(scopedContentBinding(candidate)) === canonicalJson(bindings.analysis));
  const selection = payload.styleSelections.find((candidate) =>
    candidate.status === "confirmed" &&
    canonicalJson(styleSelectionBinding(candidate)) === canonicalJson(bindings.styleSelection));
  return Boolean(analysis && selection &&
    hasExactAnalysisInputs(payload, { sample: bindings.sample, rights: bindings.rights }) &&
    canonicalJson(analysis.bindings.sample) === canonicalJson(bindings.sample) &&
    canonicalJson(analysis.bindings.rights) === canonicalJson(bindings.rights) &&
    canonicalJson(selection.bindings.sample) === canonicalJson(bindings.sample) &&
    canonicalJson(selection.bindings.rights) === canonicalJson(bindings.rights) &&
    canonicalJson(selection.bindings.analysis) === canonicalJson(bindings.analysis));
}

function referenceSampleBinding(sample: ReferenceSample) {
  return {
    id: sample.id,
    version: sample.version,
    customerId: sample.customerId,
    sessionId: sample.sessionId,
    sourceAssetId: sample.sourceAssetId,
    sourceDigest: sample.sourceDigest,
    sourceKind: sample.sourceKind,
    collectionMode: sample.collectionMode,
    candidateScope: sample.candidateScope,
  };
}

function referenceRightsBinding(rights: ReferenceRightsDeclaration) {
  return {
    id: rights.id,
    version: rights.version,
    customerId: rights.customerId,
    sessionId: rights.sessionId,
    referenceSampleId: rights.sample.id,
    referenceSampleVersion: rights.sample.version,
    referenceSampleDigest: rights.sample.sourceDigest,
    sourceKind: rights.sourceKind,
    reusableElements: rights.reusableElements,
    contentDigest: rights.contentDigest,
  };
}

function scopedContentBinding(
  value: Readonly<{ id: string; version: number; customerId: string; sessionId: string; contentDigest: string }>,
) {
  return {
    id: value.id,
    version: value.version,
    customerId: value.customerId,
    sessionId: value.sessionId,
    contentDigest: value.contentDigest,
  };
}

function styleSelectionBinding(selection: ReferenceStyleSelection) {
  return {
    ...scopedContentBinding(selection),
    status: selection.status,
    confirmation: {
      receiptId: selection.confirmationReceiptId,
      actorId: selection.actorId,
      confirmedAt: selection.confirmedAt,
    },
  };
}

function mappingImmutableContent(plan: ReferenceMaterialMappingPlan) {
  const {
    version: _version,
    status: _status,
    confirmedByActorId: _confirmedByActorId,
    confirmationReceiptId: _confirmationReceiptId,
    confirmedAt: _confirmedAt,
    ...content
  } = plan;
  return content;
}

function mappingPlanBinding(plan: ReferenceMaterialMappingPlan): ReferenceDraftPackage["bindings"]["mappingPlan"] {
  if (plan.status !== "confirmed" || !plan.confirmationReceiptId || !plan.confirmedByActorId || !plan.confirmedAt) {
    throw new Error("reference-draft-mapping-plan-mismatch");
  }
  const permissions = new Map<string, ReferenceDraftPackage["bindings"]["mappingPlan"]["permissions"][number]>();
  for (const mapping of plan.mappings) {
    if (!mapping.permission) continue;
    const { view: _view, use: _use, ...identity } = mapping.permission;
    permissions.set(identity.id, identity);
  }
  return {
    id: plan.id,
    version: plan.version,
    customerId: plan.customerId,
    sessionId: plan.sessionId,
    contentDigest: plan.contentDigest,
    permissionSetDigest: plan.permissionSetDigest,
    permissions: [...permissions.values()].sort((left, right) =>
      left.id < right.id ? -1 : left.id > right.id ? 1 : 0),
    status: plan.status,
    confirmation: {
      receiptId: plan.confirmationReceiptId,
      actorId: plan.confirmedByActorId,
      confirmedAt: plan.confirmedAt,
    },
  };
}

function assertVariantMappingIntegrity(
  mappingPlan: ReferenceMaterialMappingPlan,
  variantPlan: ReferenceVariantPlan,
): void {
  const mappings = new Map(mappingPlan.mappings.map((mapping) => [mapping.id, mapping]));
  const sources = new Map(variantPlan.authorizedSources.map((source) => [source.id, source]));
  for (const source of variantPlan.authorizedSources) {
    const mapping = mappings.get(source.mappingId);
    if (!mapping || mapping.sourceBindingId !== source.id ||
        mapping.sourceAssetId !== source.sourceAssetId || mapping.sourceDigest !== source.sourceDigest ||
        mapping.sourceProvenance !== source.provenance ||
        canonicalJson(mapping.permission) !== canonicalJson(source.permission) ||
        canonicalJson(mapping.reuseRights) !== canonicalJson(source.reuseRights)) {
      throw new Error("reference-variant-mapping-mismatch");
    }
  }
  for (const entry of variantPlan.timeline) {
    const mapping = mappings.get(entry.mappingId);
    const source = sources.get(entry.sourceBindingId);
    if (!mapping || !source || source.mappingId !== mapping.id ||
        mapping.ruleId !== entry.ruleId || mapping.sourceBindingId !== entry.sourceBindingId ||
        mapping.sourceStartMs === null || mapping.sourceEndMs === null ||
        entry.sourceStartMs < mapping.sourceStartMs || entry.sourceEndMs > mapping.sourceEndMs) {
      throw new Error("reference-variant-mapping-mismatch");
    }
  }
  if (variantPlan.audioOverlay) {
    const source = sources.get(variantPlan.audioOverlay.sourceBindingId);
    const mapping = source ? mappings.get(source.mappingId) : undefined;
    if (!source || !mapping || mapping.sourceBindingId !== source.id ||
        mapping.sourceStartMs === null || mapping.sourceEndMs === null ||
        variantPlan.audioOverlay.sourceStartMs < mapping.sourceStartMs ||
        variantPlan.audioOverlay.sourceEndMs > mapping.sourceEndMs) {
      throw new Error("reference-variant-mapping-mismatch");
    }
  }
}

function assertAppendOnlySnapshot(
  current: ReferenceWorkflowSnapshot,
  next: ReferenceWorkflowSnapshot,
): void {
  for (const collection of Object.keys(SnapshotPayloadSchema.shape) as ReferenceCollectionName[]) {
    const currentRecords = current[collection] as unknown as ReadonlyArray<{
      id: string;
      version?: number;
    }>;
    const nextRecords = next[collection] as unknown as ReadonlyArray<{
      id: string;
      version?: number;
    }>;
    const identity = (record: { id: string; version?: number; status?: unknown }) =>
      collection === "approvals" ? `${record.id}\0${String(record.status)}` : recordIdentity(record);
    const nextByIdentity = new Map(nextRecords.map((record) => [identity(record), record]));
    for (const record of currentRecords) {
      const retained = nextByIdentity.get(identity(record));
      if (!retained || canonicalJson(retained) !== canonicalJson(record)) {
        throw new Error("reference-immutable-object-conflict");
      }
    }
    for (const record of nextRecords) {
      if (currentRecords.some((existing) => identity(existing) === identity(record))) continue;
      // Approval audit entries are append-only state transitions keyed by id + status.
      // A reference approval keeps its immutable approval version when it is later invalidated.
      if (collection === "approvals") continue;
      if (record.version === undefined) continue;
      const priorVersions = currentRecords
        .filter((existing) => existing.id === record.id && existing.version !== undefined)
        .map((existing) => existing.version!);
      if (priorVersions.length > 0 && record.version <= Math.max(...priorVersions)) {
        throw new Error("reference-immutable-object-conflict");
      }
    }
  }
}

function recordIdentity(record: { id: string; version?: number }): string {
  return `${record.id}\0${String(record.version ?? "unversioned")}`;
}

function assertObjectScope(scope: StudioScope, value: object): void {
  if ("customerId" in value && value.customerId !== scope.customerId) {
    throw new Error("reference-object-customer-mismatch");
  }
  if ("sessionId" in value && value.sessionId !== scope.sessionId) {
    throw new Error("reference-object-session-mismatch");
  }
}

function parseScope(value: StudioScope): StudioScope {
  if (!value || typeof value !== "object") throw new Error("reference-scope-required");
  for (const [field, error] of [
    [value.actorId, "actor-id-required"],
    [value.customerId, "customer-id-required"],
    [value.sessionId, "session-id-required"],
  ] as const) {
    if (typeof field !== "string" || !field.trim()) throw new Error(error);
  }
  return { actorId: value.actorId, customerId: value.customerId, sessionId: value.sessionId };
}

function operationMappingIdentity(plan: ReferenceMaterialMappingPlan) {
  return { id: plan.id, version: plan.version, contentDigest: plan.contentDigest };
}

function operationSampleIdentity(sample: ReferenceSample) {
  return { id: sample.id, version: sample.version, sourceDigest: sample.sourceDigest };
}

function compareOperationSampleIdentity(
  left: Readonly<{ id: string; version: number; sourceDigest: string }>,
  right: Readonly<{ id: string; version: number; sourceDigest: string }>,
): number {
  return left.id < right.id ? -1 : left.id > right.id ? 1
    : left.version - right.version || (left.sourceDigest < right.sourceDigest ? -1
      : left.sourceDigest > right.sourceDigest ? 1 : 0);
}

function compareOperationMappingIdentity(
  left: Readonly<{ id: string; version: number; contentDigest: string }>,
  right: Readonly<{ id: string; version: number; contentDigest: string }>,
): number {
  return left.id < right.id ? -1 : left.id > right.id ? 1
    : left.version - right.version || (left.contentDigest < right.contentDigest ? -1
      : left.contentDigest > right.contentDigest ? 1 : 0);
}

function compareVersionedRecords(
  left: { id: string; version?: number },
  right: { id: string; version?: number },
): number {
  const idOrder = left.id < right.id ? -1 : left.id > right.id ? 1 : 0;
  return idOrder || (left.version ?? 0) - (right.version ?? 0);
}

function canonicalJson(value: unknown): string {
  return JSON.stringify(canonicalize(value));
}

function operationIntentSubject(intent: ReferenceEditorOperationIntent): string {
  switch (intent.method) {
    case "declareRights": return intent.referenceSampleId;
    case "analyze":
    case "createMappingPlan":
    case "reviseWithPrompt": return intent.sampleId;
    case "confirmStyle": return intent.analysisId;
    case "confirmMappingPlan":
    case "render": return intent.planId;
    case "approveDraft": return intent.draft.id;
    case "invalidateForAsset": return intent.assetId;
  }
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
      .map(([key, child]) => [key, canonicalize(child)]),
  );
}

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function hasCode(error: unknown, code: string): boolean {
  return Boolean(error && typeof error === "object" && "code" in error && error.code === code);
}
