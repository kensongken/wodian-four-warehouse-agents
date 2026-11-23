import { createHash, randomUUID } from "node:crypto";
import { lstatSync, readlinkSync, realpathSync } from "node:fs";
import { open as openFile, link, mkdir, readFile, realpath, rename, unlink, writeFile } from "node:fs/promises";
import { basename, dirname, resolve } from "node:path";

import {
  ApprovalSchema,
  AuditEventSchema,
  ContentSegmentSchema,
  CreativeBriefSchema,
  CreativeIntentSchema,
  CreativeIntentAnswerOperationSchema,
  CreativeEditorialRestartAuthorizationSchema,
  CreativeSessionEventSchema,
  CreativeProcessingTaskSchema,
  CreativeSessionSchema,
  DeviceBriefSchema,
  DirectorCardSchema,
  DispatchReceiptSchema,
  EditRevisionSchema,
  EvidenceCardSchema,
  EvidenceDecisionSchema,
  ExportDeliverySchema,
  ExportPackageSchema,
  LibraryAssetSchema,
  MaterialSuggestionSetSchema,
  MaterialUseDecisionSchema,
  RejectedEvidenceSuggestionSchema,
  ScriptPackageSchema,
  ShotPlanSchema,
  StoryCandidateSchema,
  UntypedSegmentSchema,
  VariantArtifactSchema,
  isSameCreativeEventIdentity,
  resultingScriptVersionForCreativeFieldEvent,
  type Approval,
  type AuditEvent,
  type ContentSegment,
  type CreativeBrief,
  type CreativeIntent,
  type CreativeIntentAnswerOperation,
  type CreativeEditorialRestartAuthorization,
  type CreativeSession,
  type CreativeSessionEvent,
  type CreativeProcessingTask,
  type DeviceBrief,
  type DirectorCard,
  type DispatchReceipt,
  type EditRevision,
  type EvidenceCard,
  type EvidenceDecision,
  type ExportDelivery,
  type ExportPackage,
  type LibraryAsset,
  type MaterialSuggestionSet,
  type MaterialUseDecision,
  type RejectedEvidenceSuggestion,
  type ScriptPackage,
  type ShotPlan,
  type StoryCandidate,
  type UntypedSegment,
  type VariantArtifact,
} from "@wearable/contracts";
import { z } from "zod";

import type {
  ApprovalInvalidation,
  ContentLibrary,
  CreativeActionCommitResult,
  CreativeDispatchReservation,
  CreativeBundleCommit,
  CreativeCaptureBundleCommit,
  CreativeEditorialBundleCommit,
  CreativeEditorialResult,
  CreativeRevisionHead,
  CreativeMaterialState,
  EvidenceDerivationCommit,
  EvidenceReviewCommitResult,
  MaterialSuggestionCatalogPrecondition,
  StoryAuthorizationInput,
  StoryAuthorizationResult,
  PrivacyInvalidationCommit,
  PrivacyInvalidationOutcome,
  RevisionBundleCommit,
} from "./content-library";
import { canonicalCaptureContextKey } from "./asset-identity";
import { calculateRenderInputDigests, verifyRenderedRevision } from "./ffmpeg-renderer";
import { assertDifferentTimelines } from "./variant-timelines";
import { currentMaterialViewingDecision } from "./material-advisor";
import { resolveHistoricalMaterialAuthorizationFromRecords, validateHistoricalMaterialAuthorization } from "./historical-authorization";
import {
  canonicalHistoricalMaterialSources,
  isEligibleHistoricalSource,
  resolveHistoricalMaterialCatalog,
} from "./historical-source";
import { createMaterialCatalogRaceError } from "./material-catalog-race-error";
import { resolveCreativeSteps } from "./creative-handoff";
import {
  creativeEditorialInputDigest,
  frozenHistoricalCandidateDigest,
  frozenHistoricalEvidenceDigest,
} from "./creative-editorial-evidence";
import {
  hasConsistentCreativeSessionStatus,
  isCreativeSessionTransitionAllowed,
} from "./creative-session-policy";

const LibrarySnapshotSchema = z.object({
  assets: z.array(LibraryAssetSchema),
  untypedSegments: z.array(UntypedSegmentSchema),
  segments: z.array(ContentSegmentSchema),
  stories: z.array(StoryCandidateSchema),
  revisions: z.array(EditRevisionSchema),
  artifacts: z.array(VariantArtifactSchema),
  creativeRenderBindings: z.array(z.object({
    id: z.string().min(1),
    creativeSessionId: z.string().min(1),
    taskId: z.string().min(1),
    outputRoot: z.string().min(1),
  })).default([]),
  creativeRevisionHeads: z.array(z.object({
    storyId: z.string().min(1), creativeSessionId: z.string().min(1), taskId: z.string().min(1),
    revisionId: z.string().min(1), revision: z.number().int().positive(),
    renderInputDigest: z.string().min(1).optional(),
    sourceEvidenceDigest: z.string().min(1).optional(),
  })).default([]),
  approvals: z.array(ApprovalSchema),
  auditEvents: z.array(AuditEventSchema),
  evidenceCards: z.array(EvidenceCardSchema).default([]),
  evidenceDecisions: z.array(EvidenceDecisionSchema).default([]),
  rejectedEvidenceSuggestions: z.array(RejectedEvidenceSuggestionSchema).default([]),
  exportPackages: z.array(ExportPackageSchema),
  deliveries: z.array(ExportDeliverySchema),
  creativeSessions: z.array(CreativeSessionSchema).default([]),
  creativeIntents: z.array(CreativeIntentSchema).default([]),
  creativeIntentAnswerOperations: z.array(CreativeIntentAnswerOperationSchema).default([]),
  materialSuggestionSets: z.array(MaterialSuggestionSetSchema).default([]),
  materialUseDecisions: z.array(MaterialUseDecisionSchema).default([]),
  creativeBriefs: z.array(CreativeBriefSchema).default([]),
  scriptPackages: z.array(ScriptPackageSchema).default([]),
  shotPlans: z.array(ShotPlanSchema).default([]),
  directorCards: z.array(DirectorCardSchema).default([]),
  deviceBriefs: z.array(DeviceBriefSchema).default([]),
  dispatchReceipts: z.array(DispatchReceiptSchema).default([]),
  creativeSessionEvents: z.array(CreativeSessionEventSchema).default([]),
  creativeProcessingTasks: z.array(CreativeProcessingTaskSchema).default([]),
  creativeEditorialRestartAuthorizations: z.array(
    CreativeEditorialRestartAuthorizationSchema,
  ).default([]),
});

type LibrarySnapshot = z.infer<typeof LibrarySnapshotSchema>;

/** Only failures of persisted current output may enter durable manual recovery. */
class CurrentCreativeOutputError extends Error {
  constructor(readonly taskId: string) {
    super("creative-editorial-current-output-invalid");
  }
}

type DispatchReservationState = {
  id: string;
  interrupted: boolean;
  finalizing: boolean;
  finished: Promise<void>;
  resolve(): void;
};

type AdmittedWriterState = {
  sessionId?: string;
  finished: Promise<void>;
  resolve(): void;
};

function createDispatchReservationState(): DispatchReservationState {
  let resolve!: () => void;
  const finished = new Promise<void>((done) => { resolve = done; });
  return {
    id: `dispatch_reservation_${randomUUID()}`,
    interrupted: false,
    finalizing: false,
    finished,
    resolve,
  };
}

function createAdmittedWriterState(sessionId?: string): AdmittedWriterState {
  let resolve!: () => void;
  const finished = new Promise<void>((done) => { resolve = done; });
  return { sessionId, finished, resolve };
}

const emptySnapshot = (): LibrarySnapshot => ({
  assets: [],
  untypedSegments: [],
  segments: [],
  stories: [],
  revisions: [],
  artifacts: [],
  creativeRenderBindings: [],
  creativeRevisionHeads: [],
  approvals: [],
  auditEvents: [],
  evidenceCards: [],
  evidenceDecisions: [],
  rejectedEvidenceSuggestions: [],
  exportPackages: [],
  deliveries: [],
  creativeSessions: [],
  creativeIntents: [],
  creativeIntentAnswerOperations: [],
  materialSuggestionSets: [],
  materialUseDecisions: [],
  creativeBriefs: [],
  scriptPackages: [],
  shotPlans: [],
  directorCards: [],
  deviceBriefs: [],
  dispatchReceipts: [],
  creativeSessionEvents: [],
  creativeProcessingTasks: [],
  creativeEditorialRestartAuthorizations: [],
});

const sharedDispatchReservations = new Map<string, Map<string, DispatchReservationState>>();
const sharedAdmittedWriters = new Map<string, Set<AdmittedWriterState>>();
const sharedCoordinatorOperations = new Map<string, Promise<void>>();
const sharedPathOperations = new Map<string, Promise<void>>();

function storagePathKey(path: string, seen = new Set<string>()): string {
  const absolutePath = resolve(path);
  if (seen.has(absolutePath)) throw new Error("content-store-symlink-cycle");
  seen.add(absolutePath);
  try {
    return realpathSync(absolutePath);
  } catch {
    try {
      if (lstatSync(absolutePath).isSymbolicLink()) {
        const target = readlinkSync(absolutePath);
        return storagePathKey(resolve(dirname(absolutePath), target), seen);
      }
    } catch {
      // The storage file or one of its parents does not exist yet.
    }
    const parent = dirname(absolutePath);
    const canonicalParent = parent === absolutePath ? parent : storagePathKey(parent, seen);
    return resolve(canonicalParent, basename(absolutePath));
  }
}

function reservationsForPath(path: string): Map<string, DispatchReservationState> {
  const canonicalPath = storagePathKey(path);
  let reservations = sharedDispatchReservations.get(canonicalPath);
  if (!reservations) {
    reservations = new Map();
    sharedDispatchReservations.set(canonicalPath, reservations);
  }
  return reservations;
}

function deleteReservationMapIfCurrentAndEmpty(
  path: string,
  reservations: Map<string, DispatchReservationState>,
): void {
  const canonicalPath = storagePathKey(path);
  if (reservations.size === 0 && sharedDispatchReservations.get(canonicalPath) === reservations) {
    sharedDispatchReservations.delete(canonicalPath);
  }
}

function enqueuePathOperation<T>(path: string, callback: () => Promise<T>): Promise<T> {
  const canonicalPath = storagePathKey(path);
  const previous = sharedPathOperations.get(canonicalPath) ?? Promise.resolve();
  const operation = previous.then(callback);
  const settled = operation.then(() => undefined, () => undefined);
  sharedPathOperations.set(canonicalPath, settled);
  void settled.then(() => {
    if (sharedPathOperations.get(canonicalPath) === settled) sharedPathOperations.delete(canonicalPath);
  });
  return operation;
}

function enqueueCoordinatorOperation<T>(path: string, callback: () => Promise<T>): Promise<T> {
  const canonicalPath = storagePathKey(path);
  const previous = sharedCoordinatorOperations.get(canonicalPath) ?? Promise.resolve();
  const operation = previous.then(callback);
  const settled = operation.then(() => undefined, () => undefined);
  sharedCoordinatorOperations.set(canonicalPath, settled);
  void settled.then(() => {
    if (sharedCoordinatorOperations.get(canonicalPath) === settled) {
      sharedCoordinatorOperations.delete(canonicalPath);
    }
  });
  return operation;
}

export function getJsonContentStoreCoordinatorStateForTests(path: string): Readonly<{
  reservations: number;
  admittedWriters: number;
  coordinatorOperation: boolean;
  pathOperation: boolean;
}> {
  const canonicalPath = storagePathKey(path);
  return {
    reservations: sharedDispatchReservations.get(canonicalPath)?.size ?? 0,
    admittedWriters: sharedAdmittedWriters.get(canonicalPath)?.size ?? 0,
    coordinatorOperation: sharedCoordinatorOperations.has(canonicalPath),
    pathOperation: sharedPathOperations.has(canonicalPath),
  };
}

function admittedWritersForPath(path: string): Set<AdmittedWriterState> {
  const canonicalPath = storagePathKey(path);
  let writers = sharedAdmittedWriters.get(canonicalPath);
  if (!writers) {
    writers = new Set();
    sharedAdmittedWriters.set(canonicalPath, writers);
  }
  return writers;
}

export class JsonContentStore implements ContentLibrary {
  private operationQueue: Promise<void> = Promise.resolve();
  private stale = false;

  private constructor(
    private readonly path: string,
    private snapshot: LibrarySnapshot,
    private persistedFingerprint: string | null,
  ) {}

  static async open(path: string): Promise<JsonContentStore> {
    const canonicalPath = storagePathKey(path);
    try {
      const serialized = await readFile(canonicalPath, "utf8");
      return new JsonContentStore(
        canonicalPath,
        LibrarySnapshotSchema.parse(JSON.parse(serialized)),
        fingerprint(serialized),
      );
    } catch (error) {
      if (isMissingFile(error)) {
        return new JsonContentStore(canonicalPath, emptySnapshot(), null);
      }
      throw error;
    }
  }

  async acquireCreativeDispatchReservation(sessionId: string): Promise<CreativeDispatchReservation> {
    while (true) {
      const admitted = await enqueueCoordinatorOperation(this.path, async () => {
        const reservations = sharedDispatchReservations.get(this.path);
        const existing = reservations?.get(sessionId);
        if (existing) return { waitFor: existing.finished } as const;
        // A reservation needs a canonical-file freshness point, so writers that
        // were admitted first finish before that read. Once the reservation is
        // held, later writers remain scoped by session in admitWriter().
        const writers = [...(sharedAdmittedWriters.get(this.path) ?? [])];
        if (writers.length > 0) {
          return { waitFor: Promise.all(writers.map((writer) => writer.finished)).then(() => undefined) } as const;
        }
        await this.assertFresh();
        const state = createDispatchReservationState();
        reservationsForPath(this.path).set(sessionId, state);
        return { state } as const;
      });
      if (admitted.state !== undefined) {
        const state = admitted.state;
        return {
          id: state.id,
          isInterrupted: () => state.interrupted,
          tryFinalize: () => enqueueCoordinatorOperation(this.path, async () => {
            const reservations = sharedDispatchReservations.get(this.path);
            if (reservations?.get(sessionId) !== state || state.interrupted) return false;
            state.finalizing = true;
            return true;
          }),
          release: () => {
            void enqueueCoordinatorOperation(this.path, async () => {
              const reservations = sharedDispatchReservations.get(this.path);
              if (reservations?.get(sessionId) !== state) return;
              reservations.delete(sessionId);
              deleteReservationMapIfCurrentAndEmpty(this.path, reservations);
              state.resolve();
            });
          },
        };
      }
      await admitted.waitFor;
    }
  }

  async assertFresh(): Promise<void> {
    await this.operationQueue;
    await this.assertSnapshotFresh();
  }

  async projectCreativeMaterialState<T>(project: (state: CreativeMaterialState) => T | Promise<T>): Promise<T> {
    // Share the canonical-file queue with commits without admitting a writer or interrupting dispatch.
    // Projection consumes only this detached snapshot; it must not call back into the store.
    return enqueuePathOperation(this.path, () => this.enqueueOperation(async () => {
      await this.refreshLatestSnapshotForReservedMerge();
      const snapshot = this.snapshot;
      const result = await project(structuredClone({ sessions: snapshot.creativeSessions,
        sets: snapshot.materialSuggestionSets, decisions: snapshot.materialUseDecisions,
        segments: snapshot.segments, assets: snapshot.assets,
        editorial: { stories: snapshot.stories, tasks: snapshot.creativeProcessingTasks, revisions: snapshot.revisions,
          artifacts: snapshot.artifacts, approvals: snapshot.approvals, exports: snapshot.exportPackages,
          deliveries: snapshot.deliveries,
          revisionHeads: snapshot.creativeRevisionHeads,
          revisionBindings: snapshot.creativeRenderBindings.map(({ id, creativeSessionId, taskId, outputRoot }) =>
            ({ revisionId: id, creativeSessionId, taskId, outputRoot })) } }));
      await this.assertSnapshotFresh();
      return result;
    }));
  }

  private async assertSnapshotFresh(): Promise<void> {
    if (this.stale) throw new Error("stale-content-store");
    const current = await readCurrentFile(this.path);
    const currentFingerprint = current === null ? null : fingerprint(current);
    if (currentFingerprint !== this.persistedFingerprint) {
      this.stale = true;
      throw new Error("stale-content-store");
    }
  }

  async putAsset(asset: LibraryAsset): Promise<LibraryAsset> {
    return this.mutateSnapshot((before) => {
      const checked = LibraryAssetSchema.parse(asset);
      const merged = mergeAsset(before.assets, checked);
      return {
        next: merged.assets === before.assets ? before : { ...before, assets: merged.assets },
        result: merged.asset,
      };
    }, undefined, asset.captureContext?.creativeSessionId);
  }

  async listAssets(): Promise<LibraryAsset[]> {
    return this.readSnapshot((snapshot) => LibraryAssetSchema.array().parse(snapshot.assets));
  }

  async putUntypedSegments(segments: UntypedSegment[]): Promise<void> {
    return this.mutateSnapshot((before) => ({
      next: {
        ...before,
        untypedSegments: upsertMany(
          before.untypedSegments,
          UntypedSegmentSchema.array().parse(segments),
        ),
      },
      result: undefined,
    }));
  }

  async listUntypedSegments(): Promise<UntypedSegment[]> {
    return this.readSnapshot(
      (snapshot) => UntypedSegmentSchema.array().parse(snapshot.untypedSegments),
    );
  }

  async putSegments(segments: ContentSegment[]): Promise<void> {
    return this.mutateSnapshot((before) => {
      const checked = ContentSegmentSchema.array().parse(segments);
      for (const segment of checked) {
        const prior = before.segments.find((item) => item.id === segment.id);
        if (prior?.permissionState === "withdrawn" && segment.permissionState !== "withdrawn") {
          throw new Error("withdrawn-segment-cannot-be-reactivated");
        }
      }
      return {
        next: {
          ...before,
          segments: upsertMany(before.segments, checked),
        },
        result: undefined,
      };
    });
  }

  async listSegments(): Promise<ContentSegment[]> {
    return this.readSnapshot((snapshot) => ContentSegmentSchema.array().parse(snapshot.segments));
  }

  async putStories(stories: StoryCandidate[]): Promise<void> {
    return this.mutateSnapshot((before) => ({
      next: {
        ...before,
        stories: upsertMany(before.stories, StoryCandidateSchema.array().parse(stories)),
      },
      result: undefined,
    }));
  }

  async validateStoryAuthorization(input: StoryAuthorizationInput) {
    return this.mutateSnapshot((before) => ({ next: before,
      result: resolveStoryAuthorization(input, before),
    }));
  }

  async commitStoryCandidates(input: StoryAuthorizationInput & Readonly<{ stories: StoryCandidate[] }>): Promise<void> {
    return this.mutateSnapshot((before) => {
      const { authorization } = resolveStoryAuthorization(input, before);
      const stories = StoryCandidateSchema.array().parse(input.stories);
      if (stories.some((story) => story.batchId !== input.batchId || story.historicalSegmentIds.some((id) =>
        !(authorization.selectedSegmentIds as readonly string[]).includes(id)))) throw new Error("historical-authorization-invalid");
      return { next: { ...before, stories: upsertMany(before.stories, stories) }, result: undefined };
    });
  }

  async listStories(): Promise<StoryCandidate[]> {
    return this.readSnapshot((snapshot) => StoryCandidateSchema.array().parse(snapshot.stories));
  }

  async putRevision(revision: EditRevision): Promise<void> {
    return this.mutateSnapshot((before) => ({
      next: {
        ...before,
        revisions: insertImmutable(
          before.revisions,
          EditRevisionSchema.parse(revision),
          "immutable-revision-conflict",
        ),
      },
      result: undefined,
    }));
  }

  async listRevisions(storyId: string): Promise<EditRevision[]> {
    return this.readSnapshot((snapshot) => EditRevisionSchema.array().parse(
      snapshot.revisions.filter((item) => item.storyId === storyId),
    ));
  }

  async listCreativeRevisionHeads(): Promise<CreativeRevisionHead[]> {
    return this.readSnapshot((snapshot) => structuredClone(snapshot.creativeRevisionHeads));
  }

  async putArtifact(artifact: VariantArtifact): Promise<void> {
    return this.mutateSnapshot((before) => ({
      next: {
        ...before,
        artifacts: upsertStatusTransition(
          before.artifacts,
          VariantArtifactSchema.parse(artifact),
          "immutable-artifact-conflict",
        ),
      },
      result: undefined,
    }));
  }

  async listArtifacts(revisionId: string): Promise<VariantArtifact[]> {
    return this.readSnapshot((snapshot) => VariantArtifactSchema.array().parse(
      snapshot.artifacts.filter((item) => item.revisionId === revisionId),
    ));
  }

  async listAllArtifacts(): Promise<VariantArtifact[]> {
    return this.readSnapshot(
      (snapshot) => VariantArtifactSchema.array().parse(snapshot.artifacts),
    );
  }

  async putApproval(approval: ApprovalInvalidation): Promise<void> {
    return this.mutateSnapshot((before) => {
      const checked = ApprovalSchema.parse(approval);
      if (checked.status === "active") throw new Error("atomic-approval-commit-required");
      const approvals = upsertStatusTransition(
        before.approvals,
        checked,
        "immutable-approval-conflict",
      );
      return {
        next: {
          ...before,
          approvals,
          exportPackages: checked.status === "invalidated"
            ? invalidateExportPackagesForApprovals(before.exportPackages, new Set([checked.id]))
            : before.exportPackages,
        },
        result: undefined,
      };
    }, undefined, undefined, true);
  }

  async commitApproval(approval: Approval): Promise<Approval> {
    return this.mutateWithTimelineGate(
      () => approval.revisionId,
      async (before) => {
        const checked = ApprovalSchema.parse(approval);
        const prior = before.approvals.find((item) => item.id === checked.id);
        if (prior && (prior.artifactId !== checked.artifactId ||
          prior.revisionId !== checked.revisionId || prior.variant !== checked.variant)) {
          throw new Error("approval-id-conflict");
        }
        const sourceSegments = await validateApprovalWrite(checked, before);
        if (prior) return { next: before, result: prior };
        const sourceIds = new Set(sourceSegments.map((segment) => segment.id));
        const segments = before.segments.map((segment) => sourceIds.has(segment.id) ? {
          ...segment,
          tags: {
            ...segment.tags,
            usage: {
              storyIds: [...new Set([...segment.tags.usage.storyIds, checked.storyId])],
              variants: [...new Set([...segment.tags.usage.variants, checked.variant])],
              useCount: segment.tags.usage.useCount + 1,
              lastUsedAt: checked.approvedAt,
            },
          },
        } : segment);
        const auditEvent = AuditEventSchema.parse({
          id: `audit_${randomUUID()}`,
          entityType: "approval",
          entityId: checked.id,
          actorId: checked.reviewerId,
          action: "variant-approved",
          reason: null,
          before: null,
          after: checked,
          createdAt: checked.approvedAt,
        });
        return {
          next: {
            ...before,
            segments,
            approvals: [...before.approvals, checked],
            auditEvents: insertImmutable(
              before.auditEvents,
              auditEvent,
              "immutable-audit-conflict",
            ),
          },
          result: checked,
        };
      },
      undefined,
      approval,
    );
  }

  async listApprovals(): Promise<Approval[]> {
    return this.readSnapshot((snapshot) => ApprovalSchema.array().parse(snapshot.approvals));
  }

  async putAuditEvent(event: AuditEvent): Promise<void> {
    return this.mutateSnapshot((before) => ({
      next: {
        ...before,
        auditEvents: insertImmutable(
          before.auditEvents,
          AuditEventSchema.parse(event),
          "immutable-audit-conflict",
        ),
      },
      result: undefined,
    }), undefined, event.entityType === "creative-session" ? event.entityId : undefined);
  }

  async listAuditEvents(entityId?: string): Promise<AuditEvent[]> {
    return this.readSnapshot((snapshot) => {
      const events = entityId
        ? snapshot.auditEvents.filter((item) => item.entityId === entityId)
        : snapshot.auditEvents;
      return AuditEventSchema.array().parse(events);
    });
  }

  async commitEvidenceDerivation(value: EvidenceDerivationCommit): Promise<void> {
    return this.mutateSnapshot((before) => {
      const cards = EvidenceCardSchema.array().parse(value.cards);
      const rejectedSuggestions = RejectedEvidenceSuggestionSchema.array().parse(
        value.rejectedSuggestions,
      );
      assertUniqueIds(cards, "duplicate-evidence-card-id");
      assertUniqueIds(rejectedSuggestions, "duplicate-rejected-evidence-suggestion-id");
      if (cards.some((card) => card.reviewStatus !== "pending")) {
        throw new Error("derived-evidence-card-must-be-pending");
      }

      let evidenceCards = before.evidenceCards;
      for (const card of cards) {
        const prior = evidenceCards.find((item) => item.id === card.id);
        if (!prior) {
          evidenceCards = [...evidenceCards, card];
          continue;
        }
        const original = originalEvidenceCard(prior, before.auditEvents);
        if (JSON.stringify(original) !== JSON.stringify(card)) {
          throw new Error("immutable-evidence-card-conflict");
        }
      }

      let storedRejected = before.rejectedEvidenceSuggestions;
      for (const suggestion of rejectedSuggestions) {
        storedRejected = insertImmutable(
          storedRejected,
          suggestion,
          "immutable-rejected-evidence-conflict",
        );
      }
      if (
        evidenceCards === before.evidenceCards
        && storedRejected === before.rejectedEvidenceSuggestions
      ) {
        return { next: before, result: undefined };
      }
      return {
        next: {
          ...before,
          evidenceCards,
          rejectedEvidenceSuggestions: storedRejected,
        },
        result: undefined,
      };
    });
  }

  async listEvidenceCards(creativeSessionId?: string): Promise<EvidenceCard[]> {
    return this.readSnapshot((snapshot) => EvidenceCardSchema.array().parse(
      creativeSessionId === undefined
        ? snapshot.evidenceCards
        : snapshot.evidenceCards.filter((item) =>
          item.creativeSessionId === creativeSessionId),
    ));
  }

  async listRejectedEvidenceSuggestions(
    creativeSessionId?: string,
  ): Promise<RejectedEvidenceSuggestion[]> {
    return this.readSnapshot((snapshot) => RejectedEvidenceSuggestionSchema.array().parse(
      creativeSessionId === undefined
        ? snapshot.rejectedEvidenceSuggestions
        : snapshot.rejectedEvidenceSuggestions.filter((item) =>
          item.creativeSessionId === creativeSessionId),
    ));
  }

  async commitEvidenceReview(
    decisionInput: EvidenceDecision,
  ): Promise<EvidenceReviewCommitResult> {
    return this.mutateSnapshot((before) => {
      const decision = EvidenceDecisionSchema.parse(decisionInput);
      const priorDecision = before.evidenceDecisions.find((item) =>
        item.operationKey === decision.operationKey);
      if (priorDecision) {
        if (JSON.stringify(priorDecision) !== JSON.stringify(decision)) {
          throw new Error("evidence-review-operation-conflict");
        }
        const priorAudit = before.auditEvents.find((item) =>
          item.id === `audit_${priorDecision.id}`);
        const priorBefore = EvidenceCardSchema.safeParse(priorAudit?.before);
        const priorAfter = EvidenceCardSchema.safeParse(priorAudit?.after);
        const expectedAfter = priorBefore.success
          ? evidenceCardAfterDecision(priorBefore.data, priorDecision)
          : null;
        if (
          !priorAudit
          || !priorBefore.success
          || !priorAfter.success
          || priorAudit.entityType !== "evidence-card"
          || priorAudit.entityId !== priorDecision.cardId
          || priorAudit.actorId !== priorDecision.reviewerId
          || priorAudit.action !== `evidence-${priorDecision.action}`
          || priorAudit.reason !== priorDecision.correctionReason
          || priorAudit.createdAt !== priorDecision.reviewedAt
          || priorBefore.data.id !== priorDecision.cardId
          || priorBefore.data.customerId !== priorDecision.customerId
          || priorBefore.data.creativeSessionId !== priorDecision.creativeSessionId
          || priorBefore.data.sourceDigest !== priorDecision.sourceDigest
          || priorBefore.data.modelReceiptId !== priorDecision.modelReceiptId
          || JSON.stringify(priorAfter.data) !== JSON.stringify(expectedAfter)
        ) {
          throw new Error("evidence-review-atomic-state-corrupt");
        }
        return {
          next: before,
          result: {
            card: priorAfter.data,
            decision: priorDecision,
            auditEvent: priorAudit,
          },
        };
      }

      const current = before.evidenceCards.find((item) => item.id === decision.cardId);
      if (!current) throw new Error("evidence-card-not-found");
      if (
        current.customerId !== decision.customerId
        || current.creativeSessionId !== decision.creativeSessionId
      ) {
        throw new Error("evidence-card-scope-mismatch");
      }
      if (current.sourceDigest !== decision.sourceDigest) {
        throw new Error("evidence-source-binding-mismatch");
      }
      if (current.modelReceiptId !== decision.modelReceiptId) {
        throw new Error("evidence-receipt-binding-mismatch");
      }
      if (current.reviewStatus === "rejected") {
        throw new Error("rejected-evidence-cannot-be-reactivated");
      }

      const card = evidenceCardAfterDecision(current, decision);
      const auditEvent = AuditEventSchema.parse({
        id: `audit_${decision.id}`,
        entityType: "evidence-card",
        entityId: card.id,
        actorId: decision.reviewerId,
        action: `evidence-${decision.action}`,
        reason: decision.correctionReason,
        before: current,
        after: card,
        createdAt: decision.reviewedAt,
      });
      return {
        next: {
          ...before,
          evidenceCards: before.evidenceCards.map((item) =>
            item.id === card.id ? card : item),
          evidenceDecisions: [...before.evidenceDecisions, decision],
          auditEvents: insertImmutable(
            before.auditEvents,
            auditEvent,
            "immutable-audit-conflict",
          ),
        },
        result: { card, decision, auditEvent },
      };
    });
  }

  async listEvidenceDecisions(creativeSessionId?: string): Promise<EvidenceDecision[]> {
    return this.readSnapshot((snapshot) => EvidenceDecisionSchema.array().parse(
      creativeSessionId === undefined
        ? snapshot.evidenceDecisions
        : snapshot.evidenceDecisions.filter((item) =>
          item.creativeSessionId === creativeSessionId),
    ));
  }

  async putExportPackage(value: ExportPackage): Promise<void> {
    return this.mutateSnapshot((before) => ({
      next: {
        ...before,
        exportPackages: upsertStatusTransition(
          before.exportPackages,
          ExportPackageSchema.parse(value),
          "immutable-export-conflict",
        ),
      },
      result: undefined,
    }));
  }

  async listExportPackages(): Promise<ExportPackage[]> {
    return this.readSnapshot(
      (snapshot) => ExportPackageSchema.array().parse(snapshot.exportPackages),
    );
  }

  async commitExportPackage(value: ExportPackage, creativeSessionId?: string): Promise<ExportPackage> {
    return this.mutateWithTimelineGate((before) => before.approvals.find((item) => item.id === value.approvalId)?.revisionId, async (before) => {
      const checked = ExportPackageSchema.parse(value);
      const existing = before.exportPackages.find((item) => item.id === checked.id);
      if (existing) {
        if (JSON.stringify(exportPackageIdentity(existing)) !== JSON.stringify(exportPackageIdentity(checked))) {
          throw new Error("immutable-export-conflict");
        }
        await validateActiveExportPackage(existing, before, creativeSessionId);
        return { next: before, result: existing };
      }
      await validateActiveExportPackage(checked, before, creativeSessionId);
      return {
        next: { ...before, exportPackages: [...before.exportPackages, checked] },
        result: checked,
      };
    }, creativeSessionId);
  }

  async validateExportPackageForDelivery(packageId: string, creativeSessionId?: string): Promise<ExportPackage> {
    return this.mutateWithTimelineGate((before) => before.approvals.find((item) =>
      item.id === before.exportPackages.find((value) => value.id === packageId)?.approvalId)?.revisionId, async (before) => {
      const value = before.exportPackages.find((item) => item.id === packageId);
      if (!value) throw new Error(`export-package-not-found:${packageId}`);
      await validateActiveExportPackage(value, before, creativeSessionId);
      return { next: before, result: value };
    }, creativeSessionId);
  }

  async commitExportDelivery(input: Readonly<{
    packageId: string;
    expectedPackage: ExportPackage;
    delivery: ExportDelivery;
    packageStatus: "delivered" | "pending-retry";
    creativeSessionId?: string;
  }>): Promise<ExportDelivery> {
    return this.mutateWithTimelineGate((before) => before.approvals.find((item) =>
      item.id === before.exportPackages.find((value) => value.id === input.packageId)?.approvalId)?.revisionId, async (before) => {
      const value = before.exportPackages.find((item) => item.id === input.packageId);
      if (!value || JSON.stringify(exportPackageIdentity(value)) !==
        JSON.stringify(exportPackageIdentity(input.expectedPackage))) {
        throw new Error("active-export-package-required");
      }
      await validateActiveExportPackage(value, before, input.creativeSessionId);
      const delivery = ExportDeliverySchema.parse(input.delivery);
      if (delivery.packageId !== value.id) throw new Error("export-delivery-package-mismatch");
      const prior = before.deliveries.find((item) => item.packageId === value.id);
      if (prior?.status === "received") {
        if (JSON.stringify(prior) !== JSON.stringify(delivery)) {
          throw new Error("export-delivery-receipt-conflict");
        }
        return { next: before, result: prior };
      }
      const deliveries = before.deliveries.filter((item) => item.packageId !== value.id);
      deliveries.push(delivery);
      return {
        next: {
          ...before,
          deliveries,
          exportPackages: upsertStatusTransition(before.exportPackages,
            { ...value, status: input.packageStatus }, "immutable-export-conflict"),
        },
        result: delivery,
      };
    }, input.creativeSessionId);
  }

  async putDelivery(value: ExportDelivery): Promise<void> {
    return this.mutateSnapshot((before) => {
      const checked = ExportDeliverySchema.parse(value);
      const index = before.deliveries.findIndex((item) => item.packageId === checked.packageId);
      const deliveries = [...before.deliveries];
      if (index === -1) deliveries.push(checked);
      else deliveries[index] = checked;
      return { next: { ...before, deliveries }, result: undefined };
    });
  }

  async listDeliveries(): Promise<ExportDelivery[]> {
    return this.readSnapshot(
      (snapshot) => ExportDeliverySchema.array().parse(snapshot.deliveries),
    );
  }

  async putCreativeSession(value: CreativeSession): Promise<void> {
    return this.mutateSnapshot((before) => ({
      next: {
        ...before,
        creativeSessions: insertImmutable(
          before.creativeSessions,
          CreativeSessionSchema.parse(value),
          "immutable-creative-object-conflict",
        ),
      },
      result: undefined,
    }), undefined, value.id);
  }

  async listCreativeSessions(): Promise<CreativeSession[]> {
    return this.readSnapshot(
      (snapshot) => CreativeSessionSchema.array().parse(snapshot.creativeSessions),
    );
  }

  async putCreativeIntent(value: CreativeIntent): Promise<void> {
    return this.mutateSnapshot((before) => ({
      next: {
        ...before,
        creativeIntents: insertImmutable(
          before.creativeIntents,
          CreativeIntentSchema.parse(value),
          "immutable-creative-object-conflict",
        ),
      },
      result: undefined,
    }), undefined, value.sessionId);
  }

  async listCreativeIntents(): Promise<CreativeIntent[]> {
    return this.readSnapshot(
      (snapshot) => CreativeIntentSchema.array().parse(snapshot.creativeIntents),
    );
  }

  async putCreativeIntentAnswerOperation(value: CreativeIntentAnswerOperation): Promise<void> {
    return this.mutateSnapshot((before) => {
      const operation = CreativeIntentAnswerOperationSchema.parse(value);
      validateCreativeIntentAnswerOperationReferences(
        operation, before.creativeSessions, before.creativeIntents,
        before.materialSuggestionSets, before.segments, before.assets,
      );
      return {
        next: {
          ...before,
          creativeIntentAnswerOperations: upsertCreativeIntentAnswerOperation(
            before.creativeIntentAnswerOperations,
            operation,
          ),
        },
        result: undefined,
      };
    }, undefined, value.creativeSessionId);
  }

  async listCreativeIntentAnswerOperations(): Promise<CreativeIntentAnswerOperation[]> {
    return this.readSnapshot((snapshot) => CreativeIntentAnswerOperationSchema.array().parse(
      snapshot.creativeIntentAnswerOperations,
    ));
  }

  async putMaterialSuggestionSet(
    value: MaterialSuggestionSet,
    precondition: MaterialSuggestionCatalogPrecondition,
  ): Promise<void> {
    return this.mutateSnapshot((before) => {
      const suggestionSet = MaterialSuggestionSetSchema.parse(value);
      validateMaterialSuggestionSetWrites(
        [suggestionSet],
        precondition === undefined ? [] : [precondition],
        before.creativeSessions,
        before,
      );
      return {
        next: {
          ...before,
          materialSuggestionSets: insertImmutable(
            before.materialSuggestionSets,
            suggestionSet,
            "immutable-creative-object-conflict",
          ),
        },
        result: undefined,
      };
    }, undefined, value.sessionId, true);
  }

  async listMaterialSuggestionSets(): Promise<MaterialSuggestionSet[]> {
    return this.readSnapshot(
      (snapshot) => MaterialSuggestionSetSchema.array().parse(snapshot.materialSuggestionSets),
    );
  }

  async putMaterialUseDecision(value: MaterialUseDecision): Promise<void> {
    return this.mutateSnapshot((before) => ({
      next: {
        ...before,
        materialUseDecisions: insertImmutable(
          before.materialUseDecisions,
          MaterialUseDecisionSchema.parse(value),
          "immutable-creative-object-conflict",
        ),
      },
      result: undefined,
    }), undefined, value.sessionId);
  }

  async listMaterialUseDecisions(): Promise<MaterialUseDecision[]> {
    return this.readSnapshot(
      (snapshot) => MaterialUseDecisionSchema.array().parse(snapshot.materialUseDecisions),
    );
  }

  async putCreativeBrief(value: CreativeBrief): Promise<void> {
    return this.mutateSnapshot((before) => ({
      next: {
        ...before,
        creativeBriefs: insertImmutable(
          before.creativeBriefs,
          CreativeBriefSchema.parse(value),
          "immutable-creative-object-conflict",
        ),
      },
      result: undefined,
    }), undefined, value.sessionId);
  }

  async listCreativeBriefs(): Promise<CreativeBrief[]> {
    return this.readSnapshot(
      (snapshot) => CreativeBriefSchema.array().parse(snapshot.creativeBriefs),
    );
  }

  async putScriptPackage(value: ScriptPackage): Promise<void> {
    return this.mutateSnapshot((before) => {
      const script = ScriptPackageSchema.parse(value);
      validateUniqueScriptVersions(before.scriptPackages, [script]);
      return {
        next: {
          ...before,
          scriptPackages: upsertScriptTransition(
            before.scriptPackages,
            script,
            "immutable-creative-object-conflict",
          ),
        },
        result: undefined,
      };
    }, undefined, value.sessionId);
  }

  async listScriptPackages(): Promise<ScriptPackage[]> {
    return this.readSnapshot(
      (snapshot) => ScriptPackageSchema.array().parse(snapshot.scriptPackages),
    );
  }

  async putShotPlan(value: ShotPlan): Promise<void> {
    return this.mutateSnapshot((before) => {
      const plan = normalizeTaskForSnapshot(ShotPlanSchema.parse(value), before);
      return {
        next: {
          ...before,
          shotPlans: upsertStatusTransition(
            before.shotPlans,
            plan,
            "immutable-creative-object-conflict",
          ),
        },
        result: undefined,
      };
    }, undefined, value.sessionId);
  }

  async listShotPlans(): Promise<ShotPlan[]> {
    return this.readSnapshot((snapshot) => ShotPlanSchema.array().parse(snapshot.shotPlans));
  }

  async putDirectorCard(value: DirectorCard): Promise<void> {
    return this.mutateSnapshot((before) => {
      const card = normalizeTaskForSnapshot(DirectorCardSchema.parse(value), before);
      return {
        next: {
          ...before,
          directorCards: upsertStatusTransition(
            before.directorCards,
            card,
            "immutable-creative-object-conflict",
          ),
        },
        result: undefined,
      };
    }, undefined, value.sessionId);
  }

  async listDirectorCards(): Promise<DirectorCard[]> {
    return this.readSnapshot(
      (snapshot) => DirectorCardSchema.array().parse(snapshot.directorCards),
    );
  }

  async putDeviceBrief(value: DeviceBrief): Promise<void> {
    return this.mutateSnapshot((before) => {
      const brief = normalizeTaskForSnapshot(DeviceBriefSchema.parse(value), before);
      return {
        next: {
          ...before,
          deviceBriefs: upsertStatusTransition(
            before.deviceBriefs,
            brief,
            "immutable-creative-object-conflict",
          ),
        },
        result: undefined,
      };
    }, undefined, value.sessionId);
  }

  async listDeviceBriefs(): Promise<DeviceBrief[]> {
    return this.readSnapshot(
      (snapshot) => DeviceBriefSchema.array().parse(snapshot.deviceBriefs),
    );
  }

  async putDispatchReceipt(value: DispatchReceipt): Promise<void> {
    return this.mutateSnapshot((before) => ({
      next: {
        ...before,
        dispatchReceipts: upsertStatusTransition(
          before.dispatchReceipts,
          DispatchReceiptSchema.parse(value),
          "immutable-creative-object-conflict",
        ),
      },
      result: undefined,
    }), undefined, value.sessionId);
  }

  async listDispatchReceipts(): Promise<DispatchReceipt[]> {
    return this.readSnapshot(
      (snapshot) => DispatchReceiptSchema.array().parse(snapshot.dispatchReceipts),
    );
  }

  async putCreativeSessionEvent(value: CreativeSessionEvent): Promise<void> {
    return this.mutateSnapshot((before) => {
      const event = CreativeSessionEventSchema.parse(value);
      if (isCaptureCompletionEvent(event)) {
        validateCurrentCaptureEvents(before, before.creativeSessions.find((item) =>
          item.id === event.sessionId), [event]);
      }
      validateCreativeEventIdempotency(before.creativeSessionEvents, [event]);
      return {
        next: {
          ...before,
          creativeSessionEvents: mergeCreativeSessionEvents(
            before.creativeSessionEvents,
            [event],
          ),
        },
        result: undefined,
      };
    }, undefined, value.sessionId, true);
  }

  async listCreativeSessionEvents(): Promise<CreativeSessionEvent[]> {
    return this.readSnapshot(
      (snapshot) => CreativeSessionEventSchema.array().parse(snapshot.creativeSessionEvents),
    );
  }

  async putCreativeProcessingTask(value: CreativeProcessingTask): Promise<void> {
    return this.mutateSnapshot(async (before) => {
      const task = await validateProcessingTaskWrite(value, before);
      return {
        next: {
          ...before,
          creativeProcessingTasks: upsertById(before.creativeProcessingTasks, task),
        },
        result: undefined,
      };
    }, undefined, value.creativeSessionId);
  }

  async listCreativeProcessingTasks(): Promise<CreativeProcessingTask[]> {
    return this.readSnapshot((snapshot) =>
      CreativeProcessingTaskSchema.array().parse(snapshot.creativeProcessingTasks));
  }

  async migrateLegacyCreativeEditorialTask(taskId: string, now: string): Promise<CreativeProcessingTask> {
    return this.mutateSnapshot((before) => {
      const persisted = before.creativeProcessingTasks.find((task) => task.id === taskId);
      if (!persisted?.checkpoint) throw new Error("creative-editorial-checkpoint-migration-required");
      if (persisted.checkpoint.historicalCandidates && persisted.checkpoint.historicalCandidateDigest &&
        persisted.checkpoint.historicalEvidenceDigest) {
        return { next: before, result: CreativeProcessingTaskSchema.parse(persisted) };
      }
      const checkpoint = persisted.checkpoint;
      let migrated: CreativeProcessingTask | null = null;
      try {
        if (persisted.status === "reviewable" || checkpoint.stories.length || checkpoint.revisions.length ||
          checkpoint.artifacts.length) throw new Error("legacy-checkpoint-already-produced-content");
        const { authorization, historicalSegments } = resolveStoryAuthorization({
          batchId: persisted.editorialBatchId,
          authorization: checkpoint.authorization,
        }, before);
        const selectedIds = [...authorization.selectedSegmentIds].sort();
        const candidates = [...historicalSegments].sort((left, right) => left.id.localeCompare(right.id));
        if (JSON.stringify(candidates.map((item) => item.id)) !== JSON.stringify(selectedIds) ||
          candidates.some((segment) => !before.assets.some((asset) => asset.id === segment.assetId))) {
          throw new Error("legacy-checkpoint-source-not-unique");
        }
        const frozenDigest = frozenHistoricalCandidateDigest(candidates);
        const historicalEvidenceDigest = frozenHistoricalEvidenceDigest(candidates, before.assets);
        const captureAssets = checkpoint.assetIds.map((id) => before.assets.find((asset) => asset.id === id));
        if (captureAssets.some((asset) => !asset)) throw new Error("legacy-checkpoint-capture-missing");
        migrated = CreativeProcessingTaskSchema.parse({
          ...persisted,
          inputDigest: creativeEditorialInputDigest({
            batchId: persisted.editorialBatchId,
            authorization,
            historicalCandidateDigest: frozenDigest,
            assets: captureAssets.filter((asset): asset is LibraryAsset => asset !== undefined),
          }),
          updatedAt: now,
          checkpoint: {
            ...checkpoint,
            authorization,
            historicalCandidates: candidates,
            historicalCandidateDigest: frozenDigest,
            historicalEvidenceDigest,
          },
        });
      } catch {
        migrated = CreativeProcessingTaskSchema.parse({
          ...persisted,
          status: "failed-manual-review",
          errorCode: "creative-editorial-checkpoint-migration-required",
          updatedAt: now,
        });
      }
      const action = migrated.errorCode === "creative-editorial-checkpoint-migration-required"
        ? "creative-editorial-checkpoint-migration-required"
        : "creative-editorial-checkpoint-migrated";
      const audit = AuditEventSchema.parse({
        id: `audit_${persisted.id}_${action}`,
        entityType: "creative-session",
        entityId: persisted.creativeSessionId,
        actorId: "system",
        action,
        reason: migrated.errorCode,
        before: { taskId: persisted.id, inputDigest: persisted.inputDigest },
        after: { taskId: persisted.id, inputDigest: migrated.inputDigest },
        createdAt: now,
      });
      return {
        next: {
          ...before,
          creativeProcessingTasks: upsertById(before.creativeProcessingTasks, migrated),
          auditEvents: insertImmutable(before.auditEvents, audit, "immutable-audit-conflict"),
        },
        result: migrated,
      };
    }, undefined, undefined);
  }

  async reserveCreativeEditorialRestart(
    value: Omit<CreativeEditorialRestartAuthorization,
      "status" | "restartedTaskId" | "consumedAt">,
  ): Promise<CreativeEditorialRestartAuthorization> {
    return this.mutateSnapshot((before) => {
      const authorization = CreativeEditorialRestartAuthorizationSchema.parse({
        ...value,
        status: "authorized",
        restartedTaskId: null,
        consumedAt: null,
      });
      const existing = before.creativeEditorialRestartAuthorizations.find((item) => item.id === authorization.id);
      if (existing) {
        if (JSON.stringify(manualRestartIdentity(existing)) !==
          JSON.stringify(manualRestartIdentity(authorization))) {
          throw new Error("creative-editorial-manual-authorization-conflict");
        }
        return { next: before, result: existing };
      }
      const current = currentCreativeProcessingTaskFromSnapshot(authorization.creativeSessionId, before);
      if (!current || current.id !== authorization.failedTaskId || current.status !== "failed-manual-review") {
        throw new Error("creative-editorial-manual-task-not-current");
      }
      if (before.creativeEditorialRestartAuthorizations.some((item) =>
        item.creativeSessionId === authorization.creativeSessionId && (
          item.failedTaskId === authorization.failedTaskId ||
          item.processOperationKey === authorization.processOperationKey
        ))) {
        throw new Error("creative-editorial-manual-authorization-conflict");
      }
      const audit = AuditEventSchema.parse({
        id: `audit_${authorization.id}_authorized`, entityType: "creative-session",
        entityId: authorization.creativeSessionId, actorId: authorization.confirmedBy,
        action: "creative-editorial-manual-restart-authorized",
        reason: authorization.confirmationText,
        before: { failedTaskId: authorization.failedTaskId },
        after: { failedTaskId: authorization.failedTaskId,
          processOperationKey: authorization.processOperationKey,
          confirmationCode: authorization.confirmationCode },
        createdAt: authorization.confirmedAt,
      });
      return { next: {
        ...before,
        creativeEditorialRestartAuthorizations: [...before.creativeEditorialRestartAuthorizations, authorization],
        auditEvents: insertImmutable(before.auditEvents, audit, "immutable-audit-conflict"),
      }, result: authorization };
    }, undefined, value.creativeSessionId, true);
  }

  async listCreativeEditorialRestartAuthorizations(): Promise<CreativeEditorialRestartAuthorization[]> {
    return this.readSnapshot((snapshot) => CreativeEditorialRestartAuthorizationSchema.array()
      .parse(snapshot.creativeEditorialRestartAuthorizations));
  }

  async beginCreativeEditorialRestart(input: Readonly<{
    authorizationId: string; creativeSessionId: string; processOperationKey: string;
    replacementTask: CreativeProcessingTask; consumedAt: string;
  }>): Promise<Readonly<{
    authorization: CreativeEditorialRestartAuthorization;
    processingTask: CreativeProcessingTask;
  }>> {
    return this.mutateSnapshot(async (before) => {
      const index = before.creativeEditorialRestartAuthorizations.findIndex((item) => item.id === input.authorizationId);
      const current = before.creativeEditorialRestartAuthorizations[index];
      if (!current || current.creativeSessionId !== input.creativeSessionId ||
        current.processOperationKey !== input.processOperationKey) {
        throw new Error("creative-editorial-manual-authorization-invalid");
      }
      if (current.status === "consumed") {
        if (current.restartedTaskId !== input.replacementTask.id) {
          throw new Error("creative-editorial-manual-authorization-conflict");
        }
        const persisted = before.creativeProcessingTasks.find((task) => task.id === current.restartedTaskId);
        if (!persisted || persisted.operationKey !== input.processOperationKey ||
          persisted.creativeSessionId !== input.creativeSessionId) {
          throw new Error("creative-editorial-manual-authorization-invalid");
        }
        return { next: before, result: { authorization: current, processingTask: persisted } };
      }
      const replacement = await validateProcessingTaskWrite(input.replacementTask, before);
      if (replacement.creativeSessionId !== input.creativeSessionId ||
        replacement.operationKey !== input.processOperationKey ||
        replacement.status === "reviewable" || replacement.supersededByTaskId) {
        throw new Error("creative-editorial-manual-replacement-invalid");
      }
      const failed = before.creativeProcessingTasks.find((task) => task.id === current.failedTaskId);
      const active = currentCreativeProcessingTaskFromSnapshot(input.creativeSessionId, before);
      if (!failed || active?.id !== failed.id || failed.status !== "failed-manual-review" ||
        failed.supersededByTaskId || before.creativeProcessingTasks.some((task) => task.id === replacement.id)) {
        throw new Error("creative-editorial-manual-authorization-invalid");
      }
      const consumed = CreativeEditorialRestartAuthorizationSchema.parse({
        ...current, status: "consumed", restartedTaskId: replacement.id, consumedAt: input.consumedAt,
      });
      const authorizations = [...before.creativeEditorialRestartAuthorizations];
      authorizations[index] = consumed;
      const audit = AuditEventSchema.parse({
        id: `audit_${current.id}_consumed`, entityType: "creative-session",
        entityId: current.creativeSessionId, actorId: current.confirmedBy,
        action: "creative-editorial-manual-restart-consumed", reason: null,
        before: { failedTaskId: current.failedTaskId, status: current.status },
        after: { failedTaskId: current.failedTaskId, restartedTaskId: replacement.id,
          processOperationKey: current.processOperationKey, status: consumed.status },
        createdAt: input.consumedAt,
      });
      return { next: {
        ...before,
        creativeEditorialRestartAuthorizations: authorizations,
        creativeProcessingTasks: [
          ...before.creativeProcessingTasks.map((task) =>
            task.id === failed.id ? { ...task, supersededByTaskId: replacement.id } : task),
          replacement,
        ],
        auditEvents: insertImmutable(before.auditEvents, audit, "immutable-audit-conflict"),
      }, result: { authorization: consumed, processingTask: replacement } };
    }, undefined, input.creativeSessionId, true);
  }

  async commitCreativeCaptureBundle(value: CreativeCaptureBundleCommit): Promise<void> {
    await this.commitCreativeBundle({
      session: value.session,
      expectedSession: { ...value.expectedSession, currentIntentVersion: value.session.currentIntentVersion },
      assets: value.assets,
      events: value.events,
      processingTask: value.processingTask,
      auditEvents: value.auditEvents,
    });
  }

  async commitCreativeEditorialBundle(value: CreativeEditorialBundleCommit): Promise<void> {
    const stories = StoryCandidateSchema.array().parse(value.stories);
    const revisions = EditRevisionSchema.array().parse(value.revisions);
    const artifacts = VariantArtifactSchema.array().parse(value.artifacts);
    return this.mutateSnapshot(async (before) => {
      const bound = advanceCreativeRevisionHeads(value.processingTask, revisions,
        await bindCreativeRenderRoots(value.processingTask, revisions, value.renderOutputRoot, before));
      const task = await validateProcessingTaskWrite(value.processingTask, bound, { stories, revisions, artifacts });
      if (task.status !== "reviewable") throw new Error("creative-editorial-bundle-invalid");
      return { next: {
        ...bound,
        stories: upsertMany(before.stories.map((story) => story.batchId === task.editorialBatchId &&
          !stories.some((current) => current.id === story.id) ? { ...story, status: "catalog-only" as const } : story), stories),
        revisions: revisions.reduce((items, item) => insertImmutable(items, item, "immutable-revision-conflict"), before.revisions),
        artifacts: artifacts.reduce((items, item) => upsertStatusTransition(items, item, "immutable-artifact-conflict"), before.artifacts),
        creativeProcessingTasks: upsertById(before.creativeProcessingTasks.map((item) =>
          item.id !== task.id && item.creativeSessionId === task.creativeSessionId && !item.supersededByTaskId
            ? { ...item, supersededByTaskId: task.id } : item), task),
        auditEvents: AuditEventSchema.array().parse(value.auditEvents).reduce((items, item) =>
          insertImmutable(items, item, "immutable-audit-conflict"), before.auditEvents),
      }, result: undefined };
    }, undefined, value.processingTask.creativeSessionId);
  }

  async readCreativeEditorialBundle(taskId: string, options?: { currentRevisions: true }): Promise<CreativeEditorialResult> {
    return this.mutateSnapshot<CreativeEditorialResult>(async (before) => {
      const persisted = before.creativeProcessingTasks.find((task) => task.id === taskId);
      if (!persisted || persisted.status !== "reviewable" || !persisted.checkpoint) throw new Error("creative-editorial-task-not-reviewable");
      if (options?.currentRevisions) {
        return { next: before, result: await validateCurrentCreativeEditorial(persisted, before) };
      }
      const stories = StoryCandidateSchema.array().parse(persisted.checkpoint.stories.map((item) =>
        before.stories.find((story) => story.id === item.id)));
      const revisions = EditRevisionSchema.array().parse(persisted.checkpoint.revisions.map((item) =>
        before.revisions.find((revision) => revision.id === item.id)));
      const artifacts = VariantArtifactSchema.array().parse(persisted.checkpoint.artifacts.map((item) =>
        before.artifacts.find((artifact) => artifact.id === item.id)));
      const task = await validateProcessingTaskWrite(persisted, before, { stories, revisions, artifacts });
      return { next: before, result: { task, stories, revisions, artifacts } };
    }, undefined, undefined, options?.currentRevisions === true);
  }

  async commitPrivacyInvalidation(
    value: PrivacyInvalidationCommit,
  ): Promise<PrivacyInvalidationOutcome> {
    return this.mutateSnapshot((before) => {
      if (value.reason.trim().length === 0) throw new Error("withdrawal-reason-required");
      if (value.actorId.trim().length === 0) throw new Error("withdrawal-actor-required");
      const segment = before.segments.find((item) => item.id === value.segmentId);
      if (!segment) throw new Error(`segment-not-found:${value.segmentId}`);

      const affectedArtifacts = before.artifacts
        .filter((artifact) => artifact.status === "active" && (
          artifact.sourceSegmentIds.includes(value.segmentId) ||
          artifact.visualSourceSegmentIds?.includes(value.segmentId) === true
        ))
        .sort((left, right) => left.id.localeCompare(right.id));
      const artifactIds = new Set(affectedArtifacts.map((item) => item.id));
      const affectedApprovals = before.approvals
        .filter((approval) => approval.status === "active" && artifactIds.has(approval.artifactId))
        .sort((left, right) => left.id.localeCompare(right.id));
      const approvalIds = new Set(affectedApprovals.map((item) => item.id));
      const affectedPackages = before.exportPackages
        .filter((item) => item.status !== "invalidated" && (
          artifactIds.has(item.artifactId) || approvalIds.has(item.approvalId)
        ))
        .sort((left, right) => left.id.localeCompare(right.id));
      const affectedCreativeBriefs = before.creativeBriefs
        .filter((brief) => brief.status === "active" &&
          brief.confirmedSegmentIds.includes(value.segmentId))
        .sort((left, right) => left.id.localeCompare(right.id));
      const affectedScripts = before.scriptPackages
        .filter((script) => script.status === "active" && script.sections.some(
          (section) => section.referencedSegmentIds.includes(value.segmentId),
        ))
        .sort((left, right) => left.id.localeCompare(right.id));
      const affectedLineage = new Set(affectedScripts.map(scriptLineageKey));
      const affectedShotPlans = before.shotPlans
        .filter((item) => item.status === "active" && affectedLineage.has(scriptLineageKey(item)))
        .sort((left, right) => left.id.localeCompare(right.id));
      const affectedDirectorCards = before.directorCards
        .filter((item) => item.status === "active" && affectedLineage.has(scriptLineageKey(item)))
        .sort((left, right) => left.id.localeCompare(right.id));
      const affectedDeviceBriefs = before.deviceBriefs
        .filter((item) => item.status === "active" && affectedLineage.has(scriptLineageKey(item)))
        .sort((left, right) => left.id.localeCompare(right.id));
      const outcome: PrivacyInvalidationOutcome = {
        invalidatedCreativeBriefIds: affectedCreativeBriefs.map((item) => item.id),
        invalidatedArtifactIds: affectedArtifacts.map((item) => item.id),
        invalidatedApprovalIds: affectedApprovals.map((item) => item.id),
        invalidatedExportPackageIds: affectedPackages.map((item) => item.id),
        invalidatedScriptPackageIds: affectedScripts.map((item) => item.id),
        invalidatedShotPlanIds: affectedShotPlans.map((item) => item.id),
        invalidatedDirectorCardIds: affectedDirectorCards.map((item) => item.id),
        invalidatedDeviceBriefIds: affectedDeviceBriefs.map((item) => item.id),
      };
      const withdrawnSegment = ContentSegmentSchema.parse({
        ...segment,
        permissionState: "withdrawn",
      });
      const auditEvent = AuditEventSchema.parse({
        id: `audit_${randomUUID()}`,
        entityType: "segment",
        entityId: segment.id,
        actorId: value.actorId,
        action: "segment-permission-withdrawn",
        reason: value.reason,
        before: segment,
        after: { segment: withdrawnSegment, ...outcome },
        createdAt: new Date().toISOString(),
      });
      const next: LibrarySnapshot = {
        ...before,
        segments: upsertById(before.segments, withdrawnSegment),
        artifacts: affectedArtifacts.reduce(
          (items, item) => upsertStatusTransition(
            items,
            { ...item, status: "invalidated" },
            "immutable-artifact-conflict",
          ),
          before.artifacts,
        ),
        approvals: affectedApprovals.reduce(
          (items, item) => upsertStatusTransition(
            items,
            { ...item, status: "invalidated" },
            "immutable-approval-conflict",
          ),
          before.approvals,
        ),
        exportPackages: affectedPackages.reduce(
          (items, item) => upsertStatusTransition(
            items,
            { ...item, status: "invalidated" },
            "immutable-export-conflict",
          ),
          before.exportPackages,
        ),
        creativeBriefs: affectedCreativeBriefs.reduce(
          (items, item) => upsertStatusTransition(
            items,
            { ...item, status: "invalidated" },
            "immutable-creative-object-conflict",
          ),
          before.creativeBriefs,
        ),
        scriptPackages: affectedScripts.reduce(
          (items, item) => upsertScriptTransition(
            items,
            { ...item, status: "invalidated" },
            "immutable-creative-object-conflict",
          ),
          before.scriptPackages,
        ),
        shotPlans: invalidateStatusItems(before.shotPlans, new Set(
          affectedShotPlans.map((item) => item.id),
        )),
        directorCards: invalidateStatusItems(before.directorCards, new Set(
          affectedDirectorCards.map((item) => item.id),
        )),
        deviceBriefs: invalidateStatusItems(before.deviceBriefs, new Set(
          affectedDeviceBriefs.map((item) => item.id),
        )),
        auditEvents: insertImmutable(
          before.auditEvents,
          auditEvent,
          "immutable-audit-conflict",
        ),
      };
      return { next, result: outcome };
    });
  }

  async commitRevisionBundle(value: RevisionBundleCommit): Promise<void> {
    return this.mutateSnapshot(async (before) => {
      let bound = before;
      const revisions = EditRevisionSchema.array().parse(value.revisions);
      const artifacts = VariantArtifactSchema.array().parse(value.artifacts);
      for (const revision of revisions) assertDifferentTimelines(revision.variants);
      // Recheck the same frozen source authority under the write coordinator, after
      // model analysis/rendering and before publishing any head, binding or artifact.
      for (const revision of revisions.filter((item) => item.id === value.auditEvent.entityId ||
        artifacts.some((artifact) => artifact.revisionId === item.id) ||
        !before.revisions.some((stored) => stored.id === item.id))) {
        const story = before.stories.find((item) => item.id === revision.storyId);
        if (!story || promptStorySessionId(story, before) === undefined) continue;
        const previous = revisions.find((item) => item.id === revision.parentRevisionId);
        if (!previous || revision.storyId !== previous.storyId || revision.revision !== previous.revision + 1) {
          throw new Error("creative-current-revision-conflict");
        }
        const stored = before.revisions.find((item) => item.id === revision.id);
        if (stored && JSON.stringify(stored) !== JSON.stringify(revision)) throw new Error("immutable-revision-conflict");
        const { story: authorizedStory, segments } = await resolvePromptRevisionSources(stored ?? previous, [], before);
        const lineage = new Set(segments.map((segment) => segment.id));
        const references = [...revision.variants.flatMap((variant) => [variant.cover.segmentId,
          ...variant.timeline.map((item) => item.segmentId)]), ...revision.selectedStills.map((still) => still.segmentId)];
        if (references.some((id) => !lineage.has(id)) || revision.variants.some((variant) =>
          !variant.timeline.some((item) => authorizedStory.anchorSegmentIds.includes(item.segmentId)))) {
          throw new Error("requested-material-not-in-authorized-library");
        }
      }
      for (const revision of revisions.filter((item) => artifacts.some((artifact) => artifact.revisionId === item.id))) {
        const task = before.creativeProcessingTasks.find((item) => !item.supersededByTaskId &&
          item.checkpoint?.stories.some((story) => story.id === revision.storyId));
        if (!task) continue;
        bound = advanceCreativeRevisionHeads(task, [revision],
          await bindCreativeRenderRoots(task, [revision], value.renderOutputRoot, bound));
        await verifyRenderedRevision(revision, artifacts.filter((artifact) => artifact.revisionId === revision.id),
          before.segments, before.assets, creativeRenderRoot(revision, task, bound));
      }
      const approvals = ApprovalSchema.array().parse(value.approvals);
      if (approvals.some((approval) => approval.status === "active")) {
        throw new Error("atomic-approval-commit-required");
      }
      const invalidatedApprovalIds = new Set(approvals
        .filter((approval) => approval.status === "invalidated")
        .map((approval) => approval.id));
      const requestedPackages = ExportPackageSchema.array().parse(value.exportPackages);
      const exportPackages = requestedPackages.reduce(
        (items, item) => upsertStatusTransition(items, item, "immutable-export-conflict"),
        invalidateExportPackagesForApprovals(before.exportPackages, invalidatedApprovalIds),
      );
      return { next: {
        ...bound,
        revisions: EditRevisionSchema.array().parse(value.revisions).reduce(
          (items, item) => insertImmutable(items, item, "immutable-revision-conflict"),
          before.revisions,
        ),
        artifacts: VariantArtifactSchema.array().parse(value.artifacts).reduce(
          (items, item) => upsertStatusTransition(items, item, "immutable-artifact-conflict"),
          before.artifacts,
        ),
        approvals: approvals.reduce(
          (items, item) => upsertStatusTransition(items, item, "immutable-approval-conflict"),
          before.approvals,
        ),
        exportPackages,
        auditEvents: insertImmutable(
          before.auditEvents,
          AuditEventSchema.parse(value.auditEvent),
          "immutable-audit-conflict",
        ),
      },
      result: undefined,
    }; }, undefined, undefined, true);
  }

  async readPromptRevisionSources(revision: EditRevision, legacySegments: ContentSegment[]) {
    return this.mutateSnapshot(async (before) => ({ next: before,
      result: await resolvePromptRevisionSources(revision, legacySegments, before),
    }), undefined, undefined, true);
  }

  async validateRevisionTimelines(revisionId: string, scope?: Readonly<Pick<EditRevision, "storyId" | "revision"> & {
    creativeSessionId?: string;
  }>): Promise<void> {
    return this.mutateWithTimelineGate(() => revisionId, (before) => ({ next: before, result: undefined }),
      scope?.creativeSessionId, scope);
  }

  async commitCreativeBundle(
    value: CreativeBundleCommit,
  ): Promise<void | CreativeActionCommitResult> {
    return this.mutateSnapshot(async (before) => {
      const session = CreativeSessionSchema.parse(value.session);
      const assets = LibraryAssetSchema.array().parse(value.assets ?? []);
      const events = CreativeSessionEventSchema.array().parse(value.events ?? []);
      const currentSession = before.creativeSessions.find((item) => item.id === session.id);
      if (assets.some((asset) => asset.captureContext?.creativeSessionId !== session.id)) {
        throw new Error("creative-bundle-asset-session-mismatch");
      }
      const enforcesCaptureIdentity = isCaptureCompletionWrite(
        currentSession, session, assets, events, value.processingTask,
      );
      if (enforcesCaptureIdentity) {
        validateUniqueAcceptedCaptureIdentity(
          before.assets,
          assets,
          before.creativeSessionEvents,
          events,
          currentSession,
        );
      }
      const intents = CreativeIntentSchema.array().parse(value.intents ?? []);
      const intentAnswerOperation = value.intentAnswerOperation === undefined
        ? undefined
        : CreativeIntentAnswerOperationSchema.parse(value.intentAnswerOperation);
      const materialSuggestionSets = MaterialSuggestionSetSchema.array().parse(
        value.materialSuggestionSets ?? [],
      );
      const decisions = MaterialUseDecisionSchema.array().parse(value.decisions ?? []);
      const creativeBriefs = CreativeBriefSchema.array().parse(value.creativeBriefs ?? []);
      const scripts = ScriptPackageSchema.array().parse(value.scripts ?? []);
      const shotPlans = ShotPlanSchema.array().parse(value.shotPlans ?? []);
      const directorCards = DirectorCardSchema.array().parse(value.directorCards ?? []);
      const deviceBriefs = DeviceBriefSchema.array().parse(value.deviceBriefs ?? []);
      const receipts = DispatchReceiptSchema.array().parse(value.receipts ?? []);
      const processingTask = value.processingTask === undefined
        ? undefined
        : await validateProcessingTaskWrite(value.processingTask, before);
      const auditEvents = AuditEventSchema.array().parse(value.auditEvents);
      const actionEvent = value.returnActionResult === true
        ? requireSingleCreativeActionEvent(events)
        : null;
      const actionResultingScriptVersion = actionEvent
        ? resultingScriptVersionForCreativeFieldEvent(actionEvent)
        : null;
      if (actionEvent) {
        const existing = eventsByIdempotencyKey(
          before.creativeSessionEvents,
          actionEvent.idempotencyKey,
        );
        if (existing.length > 1) throw new Error("creative-event-idempotency-corrupt");
        if (existing[0]) {
          if (!isSameCreativeEventIdentity(existing[0], actionEvent)) {
            throw new Error("creative-event-operation-conflict");
          }
          return {
            next: before,
            result: {
              event: existing[0],
              resultingScriptVersion: resultingScriptVersionForCreativeFieldEvent(existing[0]),
            },
          };
        }
        validateCreativeActionTake(before.creativeSessionEvents, actionEvent);
      }
      validateCreativeEventIdempotency(before.creativeSessionEvents, events);
      const sessionId = session.id;
      const children = [
        ...intents,
        ...materialSuggestionSets,
        ...decisions,
        ...creativeBriefs,
        ...scripts,
        ...shotPlans,
        ...directorCards,
        ...deviceBriefs,
        ...receipts,
        ...events,
      ];
      if (children.some((item) => item.sessionId !== sessionId)) {
        throw new Error("creative-bundle-session-mismatch");
      }
      if (processingTask && processingTask.creativeSessionId !== sessionId) {
        throw new Error("creative-processing-task-session-mismatch");
      }
      if (intentAnswerOperation && intentAnswerOperation.creativeSessionId !== sessionId) {
        throw new Error("creative-intent-answer-operation-session-mismatch");
      }

      validateExpectedCreativeSession(
        before.creativeSessions,
        value.expectedSession,
        value.expectedSessionSnapshot,
        session.id,
      );
      validateExpectedScript(
        before.scriptPackages,
        value.expectedScript,
        currentSession,
      );
      validateTaskLineageAuthorization(
        value.invalidateTaskLineage ?? [],
        session.id,
        value.expectedScript,
      );
      validateUniqueScriptVersions(before.scriptPackages, scripts);
      validateCreativeActionResultVersion(
        actionEvent,
        actionResultingScriptVersion,
        session,
        scripts,
      );
      validateRequiredEligibleHistoricalSegments(
        before.segments,
        before.assets,
        value.requiredEligibleHistoricalSegmentIds ?? [],
      );
      validateCreativeSessionBundleUpdate(
        currentSession,
        session,
        auditEvents,
        new Set(before.auditEvents.map((event) => event.id)),
      );
      const effectiveCreativeSessions = upsertAuditedCreativeSession(
        before.creativeSessions,
        session,
        "immutable-creative-object-conflict",
      );
      const effectiveScriptPackages = scripts.reduce(
        (items, item) => upsertScriptTransition(
          items,
          item,
          "immutable-creative-object-conflict",
        ),
        before.scriptPackages,
      );
      const effectiveSnapshot: LibrarySnapshot = {
        ...before,
        creativeSessions: effectiveCreativeSessions,
        scriptPackages: effectiveScriptPackages,
      };
      const effectiveIntents = intents.reduce(
        (items, item) => insertImmutable(items, item, "immutable-creative-object-conflict"),
        before.creativeIntents,
      );
      validateMaterialSuggestionSetWrites(
        materialSuggestionSets,
        value.materialSuggestionPreconditions ?? [],
        effectiveCreativeSessions,
        before,
      );
      const effectiveMaterialSuggestionSets = materialSuggestionSets.reduce(
        (items, item) => insertImmutable(items, item, "immutable-creative-object-conflict"),
        before.materialSuggestionSets,
      );
      if (intentAnswerOperation) {
        validateCreativeIntentAnswerOperationReferences(
          intentAnswerOperation,
          effectiveCreativeSessions,
          effectiveIntents,
          effectiveMaterialSuggestionSets,
          before.segments,
          before.assets,
        );
      }
      const taskLineage = new Set((value.invalidateTaskLineage ?? []).map(scriptLineageKey));
      const mergedShotPlans = shotPlans.map(
        (item) => normalizeTaskForSnapshot(item, effectiveSnapshot),
      ).reduce(
        (items, item) => upsertStatusTransition(items, item, "immutable-creative-object-conflict"),
        before.shotPlans,
      );
      const mergedDirectorCards = directorCards.map(
        (item) => normalizeTaskForSnapshot(item, effectiveSnapshot),
      ).reduce(
        (items, item) => upsertStatusTransition(items, item, "immutable-creative-object-conflict"),
        before.directorCards,
      );
      const mergedDeviceBriefs = deviceBriefs.map(
        (item) => normalizeTaskForSnapshot(item, effectiveSnapshot),
      ).reduce(
        (items, item) => upsertStatusTransition(items, item, "immutable-creative-object-conflict"),
        before.deviceBriefs,
      );
      const next: LibrarySnapshot = {
        ...before,
        assets: mergeAssets(before.assets, assets),
        creativeSessions: effectiveCreativeSessions,
        creativeIntents: effectiveIntents,
        creativeIntentAnswerOperations: intentAnswerOperation === undefined
          ? before.creativeIntentAnswerOperations
          : upsertCreativeIntentAnswerOperation(
              before.creativeIntentAnswerOperations,
              intentAnswerOperation,
            ),
        materialSuggestionSets: effectiveMaterialSuggestionSets,
        materialUseDecisions: decisions.reduce(
          (items, item) => insertImmutable(items, item, "immutable-creative-object-conflict"),
          before.materialUseDecisions,
        ),
        creativeBriefs: creativeBriefs.reduce(
          (items, item) => insertImmutable(items, item, "immutable-creative-object-conflict"),
          before.creativeBriefs,
        ),
        scriptPackages: effectiveScriptPackages,
        shotPlans: invalidateTaskLineage(mergedShotPlans, taskLineage),
        directorCards: invalidateTaskLineage(mergedDirectorCards, taskLineage),
        deviceBriefs: invalidateTaskLineage(mergedDeviceBriefs, taskLineage),
        dispatchReceipts: receipts.reduce(
          (items, item) => upsertStatusTransition(
            items,
            item,
            "immutable-creative-object-conflict",
          ),
          before.dispatchReceipts,
        ),
        creativeSessionEvents: mergeCreativeSessionEvents(before.creativeSessionEvents, events),
        creativeProcessingTasks: processingTask === undefined
          ? before.creativeProcessingTasks
          : upsertById(before.creativeProcessingTasks, processingTask),
        auditEvents: auditEvents.reduce(
          (items, item) => insertImmutable(items, item, "immutable-audit-conflict"),
          before.auditEvents,
        ),
      };
      if (enforcesCaptureIdentity) {
        // A first-take session may create its plan and device tasks atomically with
        // the completion event. Existing sessions must still use only prior durable
        // authority, so a caller cannot smuggle a stale take into the same write.
        const captureAuthority = currentSession ? before : {
          ...next,
          assets: before.assets,
          creativeSessionEvents: before.creativeSessionEvents,
        };
        validateCurrentCaptureSettlement(
          captureAuthority,
          currentSession ?? session,
          session,
          assets,
          events,
          processingTask,
        );
      }
      if (next.creativeSessions.filter(
        (item) => item.stage === "shooting" && item.status !== "invalidated",
      ).length > 1) {
        throw new Error("active-shooting-session-exists");
      }
      return {
        next,
        result: actionEvent
          ? { event: actionEvent, resultingScriptVersion: actionResultingScriptVersion! }
          : undefined,
      };
    }, value.dispatchReservationId, value.session.id,
    value.dispatchReservationId !== undefined || value.returnActionResult === true ||
      (value.materialSuggestionSets?.length ?? 0) > 0);
  }

  private async admitWriter(
    reservationId?: string,
    sessionId?: string,
  ): Promise<() => Promise<void>> {
    const writer = createAdmittedWriterState(sessionId);
    const blockers = await enqueueCoordinatorOperation(this.path, async () => {
      const reservations = sharedDispatchReservations.get(this.path);
      const ownReservation = sessionId === undefined ? undefined : reservations?.get(sessionId);
      if (reservationId !== undefined && ownReservation?.id !== reservationId) {
        if (reservations) deleteReservationMapIfCurrentAndEmpty(this.path, reservations);
        throw new Error("creative-dispatch-reservation-required");
      }
      const candidates = sessionId === undefined
        ? [...(reservations?.values() ?? [])]
        : [ownReservation].filter((item): item is DispatchReservationState => item !== undefined);
      const found = candidates.filter((item) => item.id !== reservationId);
      for (const reservation of found) {
        if (!reservation.finalizing) reservation.interrupted = true;
      }
      admittedWritersForPath(this.path).add(writer);
      return found;
    });
    await Promise.all(blockers.map((item) => item.finished));
    return async () => enqueueCoordinatorOperation(this.path, async () => {
      const writers = sharedAdmittedWriters.get(this.path);
      writers?.delete(writer);
      if (writers?.size === 0 && sharedAdmittedWriters.get(this.path) === writers) {
        sharedAdmittedWriters.delete(this.path);
      }
      const reservations = sharedDispatchReservations.get(this.path);
      if (reservations) deleteReservationMapIfCurrentAndEmpty(this.path, reservations);
      writer.resolve();
    });
  }

  private enqueueOperation<T>(callback: () => Promise<T>): Promise<T> {
    const operation = this.operationQueue.then(callback);
    this.operationQueue = operation.then(() => undefined, () => undefined);
    return operation;
  }

  private async mutateSnapshot<T>(
    build: (before: LibrarySnapshot) => { next: LibrarySnapshot; result: T } | Promise<{ next: LibrarySnapshot; result: T }>,
    reservationId?: string,
    sessionId?: string,
    mergeLatestForReservedSession = false,
  ): Promise<T> {
    const outcome = await this.runMutation<{ value: T } | { error: CurrentCreativeOutputError }>(async (before) => {
      try {
        const mutation = await build(before);
        return { next: mutation.next, result: { value: mutation.result } };
      } catch (error) {
        if (!(error instanceof CurrentCreativeOutputError)) throw error;
        const task = before.creativeProcessingTasks.find((item) => item.id === error.taskId);
        if (!task || task.status !== "reviewable" || task.supersededByTaskId) throw error;
        const failed = CreativeProcessingTaskSchema.parse({ ...task, status: "failed-manual-review",
          errorCode: error.message, publishedClaimed: false, updatedAt: new Date().toISOString() });
        return { next: { ...before, creativeProcessingTasks: upsertById(before.creativeProcessingTasks, failed) },
          result: { error } };
      }
    }, reservationId, sessionId, mergeLatestForReservedSession);
    // Throw only after the coordinated write and its freshness barrier succeed.
    if ("error" in outcome) throw outcome.error;
    return outcome.value;
  }

  /** Persist a current edit-integrity failure before rejecting approval/export/delivery. */
  private async mutateWithTimelineGate<T>(
    revisionId: (before: LibrarySnapshot) => string | undefined,
    build: (before: LibrarySnapshot) => { next: LibrarySnapshot; result: T } | Promise<{ next: LibrarySnapshot; result: T }>,
    sessionId?: string,
    expectedRevision?: Pick<EditRevision, "storyId" | "revision">,
  ): Promise<T> {
    return this.mutateSnapshot<T>(async (before) => {
      const id = revisionId(before);
      const revision = before.revisions.find((item) => item.id === id);
      if (revision && expectedRevision && (revision.storyId !== expectedRevision.storyId ||
        revision.revision !== expectedRevision.revision)) return build(before);
      // The current head survives loss of the revision row. Resolve its exact
      // owner before requiring output records; never select a surviving older cut.
      const heads = before.creativeRevisionHeads.filter((head) => head.revisionId === id &&
        (!expectedRevision || (head.storyId === expectedRevision.storyId && head.revision === expectedRevision.revision)) &&
        (sessionId === undefined || head.creativeSessionId === sessionId));
      const owners = before.creativeProcessingTasks.filter((task) => !task.supersededByTaskId &&
        heads.some((head) => head.taskId === task.id && head.creativeSessionId === task.creativeSessionId &&
          task.checkpoint?.stories.some((story) => story.id === head.storyId)));
      const exactOwner = owners.length === 1 ? owners[0] : undefined;
      if (exactOwner?.status === "failed-manual-review" &&
        exactOwner.errorCode === "creative-editorial-current-output-invalid") {
        throw new Error(exactOwner.errorCode);
      }
      const task = owners.length === 1 ? owners[0] : revision && before.creativeProcessingTasks.find((item) =>
        !item.supersededByTaskId && item.checkpoint?.stories.some((story) => story.id === revision.storyId));
      if (task && task.status !== "reviewable") throw new Error("creative-editorial-task-not-reviewable");
      if (task?.status === "reviewable" && task.checkpoint) {
        try {
          await validateCurrentCreativeEditorial(task, before);
        } catch (error) {
          if (error instanceof CurrentCreativeOutputError) throw error;
          // Preserve each operation's existing authority/business error order.
          // Its build callback still validates those rules before any write.
        }
      }
      if (revision) assertDifferentTimelines(revision.variants);
      return build(before);
    }, undefined, sessionId, true);
  }

  private async runMutation<T>(
    build: (before: LibrarySnapshot) => { next: LibrarySnapshot; result: T } | Promise<{ next: LibrarySnapshot; result: T }>,
    reservationId?: string,
    sessionId?: string,
    mergeLatestForReservedSession = false,
  ): Promise<T> {
    const releaseAdmission = await this.admitWriter(reservationId, sessionId);
    try {
      return await enqueuePathOperation(this.path, () => this.enqueueOperation(async () => {
        if (mergeLatestForReservedSession) await this.refreshLatestSnapshotForReservedMerge();
        else if (this.stale) throw new Error("stale-content-store");
        const before = this.snapshot;
        const mutation = await build(before);
        if (mutation.next === before) {
          // Validated read-only returns also need the same disk freshness barrier as writes.
          await this.assertSnapshotFresh();
          return mutation.result;
        }
        this.snapshot = mutation.next;
        try {
          await this.persist();
          return mutation.result;
        } catch (error) {
          if (!this.stale) this.snapshot = before;
          throw error;
        }
      }));
    } finally {
      await releaseAdmission();
    }
  }

  private async readSnapshot<T>(read: (snapshot: LibrarySnapshot) => T): Promise<T> {
    await this.operationQueue;
    return read(this.snapshot);
  }

  private async refreshLatestSnapshotForReservedMerge(): Promise<void> {
    const current = await readCurrentFile(this.path);
    const currentFingerprint = current === null ? null : fingerprint(current);
    if (currentFingerprint === this.persistedFingerprint) return;
    this.snapshot = current === null
      ? emptySnapshot()
      : LibrarySnapshotSchema.parse(JSON.parse(current));
    this.persistedFingerprint = currentFingerprint;
    this.stale = false;
  }

  private persist(): Promise<void> {
    if (this.stale) return Promise.reject(new Error("stale-content-store"));
    const serialized = `${JSON.stringify(LibrarySnapshotSchema.parse(this.snapshot), null, 2)}\n`;
    return this.persistSerialized(serialized);
  }

  private async persistSerialized(serialized: string): Promise<void> {
    if (this.stale) throw new Error("stale-content-store");
    await mkdir(dirname(this.path), { recursive: true });
    const release = await acquireFileLock(`${this.path}.lock`);
    try {
      const current = await readCurrentFile(this.path);
      const currentFingerprint = current === null ? null : fingerprint(current);
      if (currentFingerprint !== this.persistedFingerprint) {
        this.snapshot = current === null
          ? emptySnapshot()
          : LibrarySnapshotSchema.parse(JSON.parse(current));
        this.stale = true;
        throw new Error("stale-content-store");
      }
      const nextPath = `${this.path}.next-${randomUUID()}`;
      await writeFile(nextPath, serialized, "utf8");
      const handle = await openFile(nextPath, "r+");
      try {
        await handle.sync();
      } finally {
        await handle.close();
      }
      await rename(nextPath, this.path);
      this.persistedFingerprint = fingerprint(serialized);
    } finally {
      await release();
    }
  }
}

function resolveStoryAuthorization(input: StoryAuthorizationInput, before: LibrarySnapshot): StoryAuthorizationResult {
  const authorization = validateHistoricalMaterialAuthorization(input.authorization, input.batchId, {
    sessions: before.creativeSessions, sets: before.materialSuggestionSets, decisions: before.materialUseDecisions,
  });
  const historicalSegments = authorization.selectedSegmentIds.map((id) => {
    const segment = before.segments.find((item) => item.id === id);
    const asset = before.assets.find((item) => item.id === segment?.assetId);
    if (!isEligibleHistoricalSource(segment, asset) || segment.batchId === input.batchId) {
      throw new Error("historical-source-permission-invalid");
    }
    return segment;
  });
  return { authorization, historicalSegments: ContentSegmentSchema.array().parse(historicalSegments) };
}

function currentCreativeProcessingTaskFromSnapshot(
  sessionId: string,
  before: LibrarySnapshot,
): CreativeProcessingTask | undefined {
  return [...before.creativeProcessingTasks]
    .filter((task) => task.creativeSessionId === sessionId && !task.supersededByTaskId)
    .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt) ||
      Number(Boolean(right.operationKey)) - Number(Boolean(left.operationKey)) ||
      right.id.localeCompare(left.id))[0];
}

function invalidateExportPackagesForApprovals(
  packages: ExportPackage[],
  approvalIds: Set<string>,
): ExportPackage[] {
  if (approvalIds.size === 0) return packages;
  return packages.map((value) => approvalIds.has(value.approvalId) && value.status !== "invalidated"
    ? { ...value, status: "invalidated" as const }
    : value);
}

function exportPackageIdentity(value: ExportPackage) {
  const { status: _status, createdAt: _createdAt, ...identity } = value;
  return identity;
}

async function validateApprovalWrite(
  approval: Approval,
  before: LibrarySnapshot,
): Promise<ContentSegment[]> {
  const revision = before.revisions.find((item) =>
    item.id === approval.revisionId && item.storyId === approval.storyId &&
    item.revision === approval.revision);
  if (!revision) throw new Error("exact-revision-required");
  const variant = revision.variants.find((item) => item.kind === approval.variant);
  if (!variant) throw new Error("exact-variant-required");
  const artifact = before.artifacts.find((item) => item.id === approval.artifactId &&
    item.revisionId === revision.id && item.kind === approval.variant && item.status === "active");
  if (!artifact) throw new Error("exact-active-artifact-required");
  const expectedVideoSources = [...new Set(variant.timeline.map((item) => item.segmentId))];
  const expectedVisualSources = [...new Set([
    variant.cover.segmentId,
    ...revision.selectedStills.map((still) => still.segmentId),
  ])];
  if (!sameIdentitySet(artifact.sourceSegmentIds, expectedVideoSources) ||
    !sameIdentitySet(artifact.visualSourceSegmentIds, expectedVisualSources)) {
    throw new Error("artifact-lineage-mismatch");
  }
  if (approval.artifactDigest !== artifact.digest || approval.coverDigest !== artifact.coverDigest ||
    approval.renderInputDigest !== artifact.renderInputDigest ||
    JSON.stringify(approval.stillDigests) !== JSON.stringify(artifact.stillDigests)) {
    throw new Error("approved-artifact-digest-mismatch");
  }

  const sourceIds = [...new Set([
    ...artifact.sourceSegmentIds,
    ...artifact.visualSourceSegmentIds,
  ])];
  const sourceSegments = sourceIds.map((id) => {
    const segment = before.segments.find((item) => item.id === id);
    const asset = before.assets.find((item) => item.id === segment?.assetId);
    if (!segment || segment.permissionState !== "allowed" ||
      !asset || asset.permissionState !== "allowed") {
      throw new Error("all-source-segments-must-be-allowed");
    }
    return segment;
  });

  const story = before.stories.find((item) => item.id === revision.storyId);
  const task = before.creativeProcessingTasks.find((item) => !item.supersededByTaskId &&
    item.checkpoint?.stories.some((candidate) => candidate.id === revision.storyId));
  const captureOwners = new Set(sourceSegments.flatMap((segment) => {
    const asset = before.assets.find((item) => item.id === segment.assetId);
    return asset?.captureContext?.creativeSessionId ? [asset.captureContext.creativeSessionId] : [];
  }));
  if (task || captureOwners.size > 0) {
    if (!story || !task || !task.checkpoint || captureOwners.size !== 1 ||
      !captureOwners.has(task.creativeSessionId)) {
      throw new Error("creative-current-revision-conflict");
    }
    const currentRevision = currentCreativeRevision(story.id, task, before);
    await validateProcessingTaskWrite(task, before, task.checkpoint, false,
      currentCreativeRevisionSourceIds(task, before));
    const currentArtifacts = before.artifacts.filter((item) =>
      item.revisionId === currentRevision.id && item.status === "active");
    if (currentRevision.id !== revision.id || currentArtifacts.length !== 4 ||
      !currentArtifacts.some((item) => item.id === artifact.id)) {
      throw new Error("exact-active-artifact-required");
    }
    const authorized = await resolvePromptRevisionSources(currentRevision, [], before);
    const authorizedIds = new Set(authorized.segments.map((segment) => segment.id));
    if (sourceIds.some((id) => !authorizedIds.has(id))) {
      throw new Error("all-source-segments-must-be-allowed");
    }
    await verifyRenderedRevision(currentRevision, currentArtifacts, before.segments, before.assets,
      creativeRenderRoot(currentRevision, task, before));
  }
  return sourceSegments;
}

function sameIdentitySet(left: readonly string[], right: readonly string[]): boolean {
  return new Set(left).size === left.length && new Set(right).size === right.length &&
    left.length === right.length && left.every((value) => right.includes(value));
}

async function validateActiveExportPackage(
  value: ExportPackage,
  before: LibrarySnapshot,
  creativeSessionId?: string,
): Promise<void> {
  if (value.status === "invalidated") throw new Error("active-export-package-required");
  const approval = before.approvals.find((item) => item.id === value.approvalId);
  if (!approval || approval.status !== "active") throw new Error("active-approval-required");
  const artifact = before.artifacts.find((item) => item.id === approval.artifactId);
  const revision = before.revisions.find((item) => item.id === approval.revisionId);
  const story = before.stories.find((item) => item.id === approval.storyId);
  if (!artifact || artifact.status !== "active" || !revision || !story ||
    revision.storyId !== story.id || artifact.revisionId !== revision.id ||
    approval.revision !== revision.revision || approval.artifactId !== value.artifactId ||
    approval.variant !== artifact.kind || value.variant !== artifact.kind) {
    throw new Error("active-approved-artifact-required");
  }
  assertDifferentTimelines(revision.variants);
  const taskOwner = before.creativeProcessingTasks.find((task) =>
    task.checkpoint?.stories.some((item) => item.id === story.id));
  const captureOwnerIds = new Set(story.anchorSegmentIds.flatMap((segmentId) => {
    const segment = before.segments.find((item) => item.id === segmentId);
    const asset = before.assets.find((item) => item.id === segment?.assetId);
    return asset?.captureContext?.creativeSessionId ? [asset.captureContext.creativeSessionId] : [];
  }));
  const creativeOwnerId = taskOwner?.creativeSessionId ??
    (captureOwnerIds.size === 1 ? [...captureOwnerIds][0] : undefined);
  if (taskOwner && captureOwnerIds.size > 0 && !captureOwnerIds.has(taskOwner.creativeSessionId)) {
    throw new Error("legacy-export-creative-forbidden");
  }
  if (captureOwnerIds.size > 1) throw new Error("legacy-export-creative-forbidden");
  if (creativeOwnerId) {
    const creativeSession = before.creativeSessions.find((session) => session.id === creativeOwnerId);
    const currentTask = creativeSession
      ? currentCreativeProcessingTaskFromSnapshot(creativeSession.id, before)
      : undefined;
    if (!creativeSession || creativeSession.editorialBatchId !== story.batchId ||
      creativeSession.id !== creativeSessionId || creativeSession.status !== "active" ||
      creativeSession.stage !== "editorial_review" || creativeSession.privacyMode === "paused" ||
      !currentTask || currentTask.status !== "reviewable" || !currentTask.checkpoint) {
      throw new Error("legacy-export-creative-forbidden");
    }
    await validateProcessingTaskWrite(currentTask, before, currentTask.checkpoint, false,
      currentCreativeRevisionSourceIds(currentTask, before));
    if (!currentTask.checkpoint.stories.some((item) => item.id === story.id)) {
      throw new Error("legacy-export-creative-forbidden");
    }
    const latestRevision = currentCreativeRevision(story.id, currentTask, before);
    const latestArtifacts = latestRevision
      ? before.artifacts.filter((item) => item.revisionId === latestRevision.id && item.status === "active")
      : [];
    if (!latestRevision || revision.id !== latestRevision.id || latestArtifacts.length !== 4 ||
      !latestArtifacts.some((item) => item.id === artifact.id)) {
      throw new Error("legacy-export-creative-forbidden");
    }
    await verifyRenderedRevision(latestRevision, latestArtifacts, before.segments, before.assets,
      creativeRenderRoot(latestRevision, currentTask, before));
  } else if (creativeSessionId !== undefined) {
    const scope = before.creativeSessions.find((session) => session.id === creativeSessionId);
    if (!scope || scope.editorialBatchId !== story.batchId) {
      throw new Error("creative-export-session-mismatch");
    }
  }
  const variant = revision.variants.find((item) => item.kind === approval.variant);
  if (!variant || value.title !== variant.title || value.caption !== variant.caption ||
    value.videoPath !== artifact.videoPath || value.coverPath !== artifact.coverPath ||
    JSON.stringify(value.stillPaths) !== JSON.stringify(artifact.stillPaths) ||
    value.videoDigest !== artifact.digest || value.coverDigest !== artifact.coverDigest ||
    JSON.stringify(value.stillDigests) !== JSON.stringify(artifact.stillDigests) ||
    value.renderInputDigest !== artifact.renderInputDigest ||
    approval.artifactDigest !== artifact.digest || approval.coverDigest !== artifact.coverDigest ||
    JSON.stringify(approval.stillDigests) !== JSON.stringify(artifact.stillDigests) ||
    approval.renderInputDigest !== artifact.renderInputDigest ||
    value.idempotencyKey !== `${approval.id}:${artifact.digest}`) {
    throw new Error("approved-artifact-digest-mismatch");
  }
  if (await sha256File(artifact.videoPath) !== artifact.digest ||
    await sha256File(artifact.coverPath) !== artifact.coverDigest) {
    throw new Error("approved-artifact-bytes-changed");
  }
  for (const [index, path] of artifact.stillPaths.entries()) {
    if (await sha256File(path) !== artifact.stillDigests[index]) {
      throw new Error("approved-artifact-bytes-changed");
    }
  }
}

async function sha256File(path: string): Promise<string> {
  return createHash("sha256").update(await readFile(path)).digest("hex");
}

function manualRestartIdentity(value: CreativeEditorialRestartAuthorization) {
  const { status: _status, restartedTaskId: _restartedTaskId, consumedAt: _consumedAt,
    confirmedAt: _confirmedAt, ...identity } = value;
  return identity;
}

/** The only gate for public processing-task writes and reviewable reads. */
async function validateProcessingTaskWrite(
  value: CreativeProcessingTask,
  before: LibrarySnapshot,
  bundle?: Pick<CreativeEditorialBundleCommit, "stories" | "revisions" | "artifacts">,
  verifyOutputs = true,
  projectedCurrentSourceIds?: ReadonlySet<string>,
): Promise<CreativeProcessingTask> {
  const task = CreativeProcessingTaskSchema.parse(value);
  if (task.status !== "reviewable") return task;
  if (!bundle) throw new Error("creative-editorial-bundle-required");
  const { stories, revisions, artifacts } = bundle;
  const session = before.creativeSessions.find((item) => item.id === task.creativeSessionId);
  const pending = before.creativeProcessingTasks.find((item) => item.id === task.id);
  const checkpoint = task.checkpoint;
  if (!session || session.status !== "active" || session.stage !== "editorial_review" || session.privacyMode === "paused" ||
    session.editorialBatchId !== task.editorialBatchId || !checkpoint || !task.operationKey || !task.inputDigest ||
    task.phase !== "complete" || task.publishedClaimed !== false || task.supersededByTaskId ||
    !pending || pending.supersededByTaskId || pending.inputDigest !== task.inputDigest || pending.operationKey !== task.operationKey ||
    JSON.stringify(checkpoint) !== JSON.stringify(pending.checkpoint)) {
    throw new Error("creative-editorial-bundle-invalid");
  }
  const consentRecords = {
    sessions: before.creativeSessions, sets: before.materialSuggestionSets, decisions: before.materialUseDecisions,
  };
  const consent = validateHistoricalMaterialAuthorization(checkpoint.authorization, task.editorialBatchId, consentRecords);
  const currentConsent = resolveHistoricalMaterialAuthorizationFromRecords(session.id, consentRecords);
  if (JSON.stringify(consent) !== JSON.stringify(currentConsent)) throw new Error("historical-authorization-invalid");
  const frozenCandidates = checkpoint.historicalCandidates;
  const frozenCandidateDigest = checkpoint.historicalCandidateDigest;
  const frozenEvidenceDigest = checkpoint.historicalEvidenceDigest;
  const selectedHistoricalIds = [...consent.selectedSegmentIds].sort();
  if (!frozenCandidates || !frozenCandidateDigest || !frozenEvidenceDigest ||
    frozenCandidateDigest !== frozenHistoricalCandidateDigest(frozenCandidates) ||
    JSON.stringify(frozenCandidates.map((segment) => segment.id).sort()) !== JSON.stringify(selectedHistoricalIds)) {
    throw new Error("creative-editorial-historical-evidence-changed");
  }
  const currentHistoricalCandidates = selectedHistoricalIds.map((id) =>
    before.segments.find((segment) => segment.id === id));
  if (currentHistoricalCandidates.some((segment) => !segment) ||
    frozenEvidenceDigest !== frozenHistoricalEvidenceDigest(frozenCandidates, before.assets) ||
    frozenEvidenceDigest !== frozenHistoricalEvidenceDigest(
      currentHistoricalCandidates.filter((segment): segment is ContentSegment => segment !== undefined),
      before.assets,
    )) {
    throw new Error("creative-editorial-historical-evidence-changed");
  }
  const plans = before.shotPlans.filter((plan) => plan.sessionId === session.id && plan.status === "active" &&
    plan.briefVersion === session.currentBriefVersion && plan.scriptVersion === session.currentScriptVersion &&
    plan.shotPlanVersion === session.currentShotPlanVersion);
  if (plans.length !== 1) throw new Error("creative-editorial-plan-not-current");
  const plan = plans[0]!;
  const steps = resolveCreativeSteps(plan, before.creativeSessionEvents, before.assets);
  if (steps.some((step) => step.state === "pending" || step.state === "retake-pending")) {
    throw new Error("creative-editorial-steps-not-settled");
  }
  const currentAssetIds = before.assets.filter((asset) => {
    const context = asset.captureContext;
    return asset.batchId === task.editorialBatchId && context?.creativeSessionId === session.id &&
      context.scriptVersion === plan.scriptVersion && context.shotPlanVersion === plan.shotPlanVersion &&
      context.stepOutcome === "completed" && steps.some((step) => step.stepId === context.stepId &&
        step.state === "completed-returned" && step.acceptedTake === context.take);
  }).map((asset) => asset.id).sort();
  if (!currentAssetIds.length || JSON.stringify([...checkpoint.assetIds].sort()) !== JSON.stringify(currentAssetIds)) {
    throw new Error("creative-editorial-captures-changed");
  }
  if (!projectedCurrentSourceIds && checkpoint.segmentDigest !== createHash("sha256").update(JSON.stringify(
    checkpoint.segmentIds.map((id) => editorialEvidenceSegment(
      before.segments.find((segment) => segment.id === id),
    )),
  )).digest("hex")) throw new Error("creative-editorial-evidence-changed");
  if (projectedCurrentSourceIds && [...projectedCurrentSourceIds].some((id) => {
    const segment = before.segments.find((item) => item.id === id);
    const asset = before.assets.find((item) => item.id === segment?.assetId);
    return !segment || !asset || segment.permissionState !== "allowed" || asset.permissionState !== "allowed";
  })) throw new Error("creative-editorial-evidence-changed");
  if (projectedCurrentSourceIds) {
    validateCurrentRevisionEvidence(task, before, projectedCurrentSourceIds);
  }
  if (!stories.length || revisions.length !== stories.length || artifacts.length !== revisions.length * 4 ||
    new Set(stories.map((story) => story.id)).size !== stories.length ||
    new Set(revisions.map((revision) => revision.storyId)).size !== stories.length ||
    JSON.stringify(stories) !== JSON.stringify(checkpoint.stories) ||
    JSON.stringify(revisions) !== JSON.stringify(checkpoint.revisions) ||
    JSON.stringify(artifacts) !== JSON.stringify(checkpoint.artifacts)) {
    throw new Error("creative-editorial-bundle-incomplete");
  }
  for (const story of stories) {
    const revision = revisions.find((item) => item.storyId === story.id);
    const lineage = new Set([...story.anchorSegmentIds, ...story.historicalSegmentIds]);
    if (story.batchId !== task.editorialBatchId || !revision ||
      story.historicalSegmentIds.some((id) => !(consent.selectedSegmentIds as readonly string[]).includes(id)) ||
      story.anchorSegmentIds.some((id) => {
        const segment = before.segments.find((item) => item.id === id);
        const asset = before.assets.find((item) => item.id === segment?.assetId);
        return !segment || segment.batchId !== task.editorialBatchId || !asset ||
          !currentAssetIds.includes(asset.id) || asset.captureContext?.creativeSessionId !== session.id;
      }) || [...(projectedCurrentSourceIds ?? lineage)].some((id) =>
        lineage.has(id) && before.segments.find((segment) => segment.id === id)?.permissionState !== "allowed")) {
      throw new Error("creative-editorial-lineage-invalid");
    }
    const references = [...revision.variants.flatMap((variant) => [variant.cover.segmentId,
      ...variant.timeline.map((item) => item.segmentId)]), ...revision.selectedStills.map((item) => item.segmentId)];
    if (references.some((id) => !lineage.has(id))) throw new Error("creative-editorial-variants-invalid");
    // During current reads, base checkpoint edits are historical evidence. Inspect
    // distinctness on the current heads inside the persistent recovery boundary.
    if (verifyOutputs) assertDifferentTimelines(revision.variants);
    if (verifyOutputs) await verifyRenderedRevision(revision, artifacts.filter((artifact) => artifact.revisionId === revision.id),
      before.segments, before.assets, creativeRenderRoot(revision, task, before));
  }
  return task;
}

/** Validate authority first, then classify only the persisted current output. */
async function validateCurrentCreativeEditorial(
  task: CreativeProcessingTask,
  before: LibrarySnapshot,
): Promise<CreativeEditorialResult> {
  if (task.status !== "reviewable" || !task.checkpoint) throw new Error("creative-editorial-task-not-reviewable");
  let revisions: EditRevision[] | undefined;
  try {
    revisions = task.checkpoint.stories.map((story) => currentCreativeRevision(story.id, task, before));
  } catch {
    // Missing heads cannot supply a source projection. Keep the established
    // consent/capture/checkpoint gate before classifying that output loss.
  }
  const sources = revisions ? new Set(revisions.flatMap(revisionSourceIds)) : undefined;
  await validateProcessingTaskWrite(task, before, task.checkpoint, false, sources);
  if (!revisions) throw new CurrentCreativeOutputError(task.id);
  const currentRevisionIds = new Set(revisions.map((revision) => revision.id));
  const artifacts = before.artifacts.filter((artifact) => artifact.status === "active" && currentRevisionIds.has(artifact.revisionId));
  const stories = task.checkpoint.stories.map((item) => before.stories.find((story) => story.id === item.id));
  if (JSON.stringify(stories) !== JSON.stringify(task.checkpoint.stories)) throw new CurrentCreativeOutputError(task.id);
  for (const [index, revision] of revisions.entries()) {
    const story = stories[index]!;
    const lineage = new Set([...story.anchorSegmentIds, ...story.historicalSegmentIds]);
    if (revisionSourceIds(revision).some((id) => !lineage.has(id))) throw new CurrentCreativeOutputError(task.id);
    try {
      await verifyRenderedRevision(revision, artifacts.filter((artifact) => artifact.revisionId === revision.id),
        before.segments, before.assets, creativeRenderRoot(revision, task, before));
    } catch (error) {
      // Source-media/permission failures are authority failures, not broken
      // current output. Their existing caller-specific error semantics remain.
      if (error instanceof Error && error.message.startsWith("source-")) throw error;
      throw new CurrentCreativeOutputError(task.id);
    }
  }
  return { task, stories: stories as StoryCandidate[], revisions, artifacts };
}

/**
 * Current operations bind only the sources used by the current revision heads.
 * Each current head carries the immutable full-record digest captured at render time;
 * obsolete checkpoint sources therefore cannot invalidate an unrelated new cut.
 */
function validateCurrentRevisionEvidence(
  task: CreativeProcessingTask,
  before: LibrarySnapshot,
  projectedCurrentSourceIds: ReadonlySet<string>,
): void {
  if (!task.checkpoint) throw new Error("creative-current-revision-invalid");
  const revisions = task.checkpoint.stories.map((story) =>
    currentCreativeRevision(story.id, task, before));
  const actualSourceIds = new Set(revisions.flatMap(revisionSourceIds));
  if (!sameIdentitySet([...projectedCurrentSourceIds], [...actualSourceIds])) {
    throw new Error("creative-editorial-evidence-changed");
  }
  const expectedDigests = calculateRenderInputDigests(revisions, before.segments, before.assets);
  const expectedSourceEvidenceDigests = calculateRevisionSourceEvidenceDigests(
    revisions, before.segments, before.assets,
  );
  const headsByRevisionId = new Map(before.creativeRevisionHeads.filter((head) =>
    head.creativeSessionId === task.creativeSessionId && head.taskId === task.id)
    .map((head) => [head.revisionId, head]));
  const artifactsById = new Map<string, VariantArtifact[]>();
  for (const artifact of before.artifacts) {
    const current = artifactsById.get(artifact.id) ?? [];
    current.push(artifact);
    artifactsById.set(artifact.id, current);
  }
  for (const revision of revisions) {
    const head = headsByRevisionId.get(revision.id);
    if (head?.sourceEvidenceDigest && head.sourceEvidenceDigest !== expectedSourceEvidenceDigests.get(revision.id)) {
      throw new Error("creative-editorial-evidence-changed");
    }
  }
  // Check every current source witness before any output failure can mask a
  // still-used sibling source change, even when its artifacts are unavailable.
  for (const revision of revisions) {
    const head = headsByRevisionId.get(revision.id);
    if (!head?.sourceEvidenceDigest || !head.renderInputDigest || head.renderInputDigest !== expectedDigests.get(revision.id)) {
      throw new CurrentCreativeOutputError(task.id);
    }
    try {
      creativeRenderRoot(revision, task, before);
    } catch {
      throw new CurrentCreativeOutputError(task.id);
    }
    const artifactGroups = revision.variants.map((variant) =>
      artifactsById.get(`artifact_${revision.id}_${variant.kind}`) ?? []);
    if (artifactGroups.some((group) => group.length !== 1)) {
      throw new CurrentCreativeOutputError(task.id);
    }
    const artifacts = artifactGroups.flat();
    if (artifacts.some((artifact, index) =>
      artifact.revisionId !== revision.id || artifact.kind !== revision.variants[index]?.kind ||
      artifact.renderInputDigest !== head.renderInputDigest || artifact.status !== "active")) {
      throw new CurrentCreativeOutputError(task.id);
    }
  }
}

function revisionSourceIds(revision: EditRevision): string[] {
  return [
    ...revision.variants.flatMap((variant) => [
      variant.cover.segmentId,
      ...variant.timeline.map((item) => item.segmentId),
    ]),
    ...revision.selectedStills.map((item) => item.segmentId),
  ];
}

function calculateRevisionSourceEvidenceDigests(
  revisions: readonly EditRevision[],
  segments: readonly ContentSegment[],
  assets: readonly LibraryAsset[],
): ReadonlyMap<string, string> {
  const segmentsById = new Map(segments.map((segment) => [segment.id, segment]));
  const assetsById = new Map(assets.map((asset) => [asset.id, asset]));
  return new Map(revisions.map((revision) => [revision.id, createHash("sha256").update(JSON.stringify(
    [...new Set(revisionSourceIds(revision))].sort().map((segmentId) => {
      const segment = segmentsById.get(segmentId);
      return { segment: editorialEvidenceSegment(segment), parentAsset: segment ? assetsById.get(segment.assetId) ?? null : null };
    }),
  )).digest("hex")]));
}

async function resolvePromptRevisionSources(
  revision: EditRevision,
  legacySegments: ContentSegment[],
  before: LibrarySnapshot,
): Promise<Readonly<{ story: StoryCandidate; segments: ContentSegment[] }>> {
  const story = before.stories.find((item) => item.id === revision.storyId);
  if (!story) throw new Error("story-lineage-required");
  const lineage = new Set([...story.anchorSegmentIds, ...story.historicalSegmentIds]);
  const sessionId = promptStorySessionId(story, before);
  if (sessionId === undefined) return { story: StoryCandidateSchema.parse(story),
    segments: ContentSegmentSchema.array().parse(legacySegments.filter((segment) =>
      lineage.has(segment.id) && segment.permissionState === "allowed")),
  };
  const session = before.creativeSessions.find((item) => item.id === sessionId);
  const task = session && currentCreativeProcessingTaskFromSnapshot(session.id, before);
  if (!session || !task || task.status !== "reviewable" || !task.checkpoint ||
    !task.checkpoint.stories.some((item) => JSON.stringify(item) === JSON.stringify(story))) {
    throw new Error("creative-current-revision-conflict");
  }
  const current = await validateCurrentCreativeEditorial(task, before);
  if (!current.revisions.some((item) => JSON.stringify(item) === JSON.stringify(revision))) {
    throw new Error("creative-current-revision-conflict");
  }
  const currentSourceIds = currentCreativeRevisionSourceIds(task, before);
  const authorization = task.checkpoint.authorization;
  if (story.historicalSegmentIds.length > 0) {
    const set = "suggestionSetId" in authorization
      ? before.materialSuggestionSets.find((item) => item.id === authorization.suggestionSetId) : undefined;
    if (!set || currentMaterialViewingDecision(session, set, before.materialUseDecisions)?.action !== "accept-view") {
      throw new Error("historical-authorization-invalid");
    }
  }
  const segments = [...lineage].flatMap((id) => {
    const segment = before.segments.find((item) => item.id === id);
    const asset = before.assets.find((item) => item.id === segment?.assetId);
    if (!segment || !asset) {
      if (currentSourceIds.has(id)) throw new Error("requested-material-not-in-authorized-library");
      return [];
    }
    const authorized = segment.permissionState === "allowed" && asset.permissionState === "allowed" &&
      (story.anchorSegmentIds.includes(id)
        ? segment.batchId === story.batchId && asset.batchId === story.batchId &&
          asset.captureContext?.creativeSessionId === session.id && task.checkpoint!.assetIds.includes(asset.id)
        : (authorization.selectedSegmentIds as readonly string[]).includes(id) && isEligibleHistoricalSource(segment, asset));
    if (!authorized) {
      if (currentSourceIds.has(id)) {
        throw new Error("requested-material-not-in-authorized-library");
      }
      return [];
    }
    return [segment];
  });
  return { story: StoryCandidateSchema.parse(story), segments: ContentSegmentSchema.array().parse(segments) };
}

/** A shared batch label alone does not establish ownership of a legacy story. */
function promptStorySessionId(story: StoryCandidate, before: LibrarySnapshot): string | undefined {
  const owners = new Set(before.creativeProcessingTasks.filter((task) =>
    task.checkpoint?.stories.some((item) => item.id === story.id)).map((task) => task.creativeSessionId));
  for (const id of story.anchorSegmentIds) {
    const segment = before.segments.find((item) => item.id === id);
    const sessionId = before.assets.find((item) => item.id === segment?.assetId)?.captureContext?.creativeSessionId;
    if (sessionId) owners.add(sessionId);
  }
  if (owners.size > 1) throw new Error("creative-current-revision-conflict");
  return [...owners][0];
}

function advanceCreativeRevisionHeads(
  task: CreativeProcessingTask,
  revisions: readonly EditRevision[],
  before: LibrarySnapshot,
): LibrarySnapshot {
  const heads = [...before.creativeRevisionHeads];
  const renderInputDigests = calculateRenderInputDigests(revisions, before.segments, before.assets);
  const sourceEvidenceDigests = calculateRevisionSourceEvidenceDigests(revisions, before.segments, before.assets);
  for (const revision of revisions) {
    const renderInputDigest = renderInputDigests.get(revision.id);
    const sourceEvidenceDigest = sourceEvidenceDigests.get(revision.id);
    if (!renderInputDigest || !sourceEvidenceDigest) throw new Error("creative-current-revision-invalid");
    const next = { storyId: revision.storyId, creativeSessionId: task.creativeSessionId,
      taskId: task.id, revisionId: revision.id, revision: revision.revision,
      renderInputDigest, sourceEvidenceDigest };
    const index = heads.findIndex((item) => item.storyId === revision.storyId);
    const prior = heads[index];
    if (prior && (prior.creativeSessionId !== task.creativeSessionId ||
      (prior.revision >= next.revision && JSON.stringify(prior) !== JSON.stringify(next)))) {
      throw new Error("creative-current-revision-conflict");
    }
    if (index < 0) heads.push(next);
    else heads[index] = next;
  }
  return { ...before, creativeRevisionHeads: heads };
}

function currentCreativeRevision(storyId: string, task: CreativeProcessingTask, before: LibrarySnapshot): EditRevision {
  const heads = before.creativeRevisionHeads.filter((item) => item.storyId === storyId);
  const head = heads[0];
  const revision = head && before.revisions.find((item) => item.id === head.revisionId);
  if (heads.length !== 1 || !head || head.creativeSessionId !== task.creativeSessionId || head.taskId !== task.id ||
    !revision || revision.storyId !== storyId || revision.revision !== head.revision) {
    throw new Error("creative-current-revision-invalid");
  }
  return revision;
}

function currentCreativeRevisionSourceIds(
  task: CreativeProcessingTask,
  before: LibrarySnapshot,
): Set<string> {
  if (!task.checkpoint) throw new Error("creative-current-revision-invalid");
  return new Set(task.checkpoint.stories.flatMap((story) => {
    const revision = currentCreativeRevision(story.id, task, before);
    return [
      ...revision.variants.flatMap((variant) => [
        variant.cover.segmentId,
        ...variant.timeline.map((item) => item.segmentId),
      ]),
      ...revision.selectedStills.map((item) => item.segmentId),
    ];
  }));
}

async function bindCreativeRenderRoots(
  task: CreativeProcessingTask,
  revisions: readonly EditRevision[],
  outputRoot: string | undefined,
  before: LibrarySnapshot,
): Promise<LibrarySnapshot> {
  if (!outputRoot) throw new Error("creative-render-root-required");
  const canonicalRoot = await realpath(outputRoot);
  const bindings = revisions.reduce((items, revision) => insertImmutable(items, {
    id: revision.id, creativeSessionId: task.creativeSessionId, taskId: task.id, outputRoot: canonicalRoot,
  }, "immutable-render-root-conflict"), before.creativeRenderBindings);
  return { ...before, creativeRenderBindings: bindings };
}

function creativeRenderRoot(revision: EditRevision, task: CreativeProcessingTask, before: LibrarySnapshot): string {
  const bindings = before.creativeRenderBindings.filter((item) => item.id === revision.id);
  const binding = bindings[0];
  if (bindings.length !== 1 || !binding || binding.taskId !== task.id ||
    binding.creativeSessionId !== task.creativeSessionId) throw new Error("creative-render-root-invalid");
  return binding.outputRoot;
}

function editorialEvidenceSegment(segment: ContentSegment | undefined) {
  if (!segment) return undefined;
  const { usage: _mutableUsage, ...evidenceTags } = segment.tags;
  return { ...segment, tags: evidenceTags };
}

async function acquireFileLock(path: string): Promise<() => Promise<void>> {
  const token = randomUUID();
  const candidatePath = `${path}.candidate-${token}`;
  const candidate = await openFile(candidatePath, "wx");
  try {
    await candidate.writeFile(JSON.stringify({ token, pid: process.pid, createdAt: Date.now() }), "utf8");
    await candidate.sync();
  } finally {
    await candidate.close();
  }
  for (let attempt = 0; attempt < 50; attempt += 1) {
    try {
      await link(candidatePath, path);
      await unlink(candidatePath).catch(() => undefined);
      return async () => {
        const owner = await readCurrentFile(path);
        if (owner?.includes(token)) await unlink(path).catch(() => undefined);
      };
    } catch (error) {
      if (!isAlreadyExists(error)) {
        await unlink(candidatePath).catch(() => undefined);
        throw error;
      }
      if (await isOrphanedLock(path)) {
        await unlink(candidatePath).catch(() => undefined);
        throw new Error("orphaned-content-store-lock");
      }
      await new Promise((resolve) => setTimeout(resolve, 15));
    }
  }
  await unlink(candidatePath).catch(() => undefined);
  throw new Error("content-store-lock-timeout");
}

async function isOrphanedLock(path: string): Promise<boolean> {
  const raw = await readCurrentFile(path);
  if (raw === null) return false;
  try {
    const value = JSON.parse(raw) as { pid?: unknown; createdAt?: unknown };
    if (typeof value.pid !== "number" || typeof value.createdAt !== "number") return true;
    try {
      process.kill(value.pid, 0);
      return false;
    } catch {
      return true;
    }
  } catch {
    return true;
  }
}

async function readCurrentFile(path: string): Promise<string | null> {
  try {
    return await readFile(path, "utf8");
  } catch (error) {
    if (isMissingFile(error)) return null;
    throw error;
  }
}

function fingerprint(value: string): string {
  return value;
}

function isAlreadyExists(error: unknown): boolean {
  return typeof error === "object" && error !== null && "code" in error && error.code === "EEXIST";
}

function upsertMany<T extends { id: string }>(existing: T[], additions: T[]): T[] {
  return additions.reduce((items, item) => upsertById(items, item), existing);
}

function sameAssetIdentity(left: LibraryAsset, right: LibraryAsset): boolean {
  return left.id === right.id &&
    left.batchId === right.batchId &&
    left.sessionId === right.sessionId &&
    left.digest === right.digest &&
    left.localPath === right.localPath &&
    left.mediaKind === right.mediaKind &&
    left.importedAt === right.importedAt &&
    left.origin === right.origin &&
    canonicalCaptureContextKey(left.captureContext) === canonicalCaptureContextKey(right.captureContext) &&
    left.derivedFromAssetId === right.derivedFromAssetId &&
    JSON.stringify(left.privacyActions ?? []) === JSON.stringify(right.privacyActions ?? []) &&
    JSON.stringify(left.privacyEvidence ?? null) === JSON.stringify(right.privacyEvidence ?? null);
}

function mergeAssets(existing: LibraryAsset[], additions: LibraryAsset[]): LibraryAsset[] {
  return additions.reduce((items, asset) => mergeAsset(items, asset).assets, existing);
}

function validateUniqueAcceptedCaptureIdentity(
  existingAssets: LibraryAsset[],
  newAssets: LibraryAsset[],
  existingEvents: CreativeSessionEvent[],
  incomingEvents: CreativeSessionEvent[],
  currentSession: CreativeSession | undefined,
): void {
  if (newAssets.length === 0) return;
  const returnedEvents = incomingEvents.filter((event) => event.action === "media-returned");
  if (returnedEvents.length === 0) throw new Error("creative-capture-acceptance-event-required");
  if (returnedEvents.length !== 1) throw new Error("creative-capture-acceptance-event-ambiguous");
  const acceptance = returnedEvents[0]!;
  // A return event authorizes exactly one atomic asset group. Handoff replay
  // reads that stored group; it must not reopen it through another bundle.
  if (existingEvents.some((event) => event.idempotencyKey === acceptance.idempotencyKey)) {
    throw new Error("creative-capture-acceptance-event-already-used");
  }
  if (!currentSession || acceptance.sessionId !== currentSession.id ||
    acceptance.scriptVersion !== currentSession.currentScriptVersion ||
    acceptance.shotPlanVersion !== currentSession.currentShotPlanVersion ||
    !newAssets.every((asset) => captureMatchesAcceptance(asset, acceptance))) {
    throw new Error("creative-capture-acceptance-provenance-mismatch");
  }
  const newKeys = new Set(newAssets.map(captureBusinessKey).filter((key): key is string => key !== null));
  if (existingAssets.some((asset) => {
    const key = captureBusinessKey(asset);
    return key !== null && newKeys.has(key);
  })) {
    throw new Error("creative-capture-take-already-returned");
  }
}

/** Runs inside the coordinator: file preparation never reserves a completed take. */
function validateCurrentCaptureSettlement(
  before: LibrarySnapshot,
  session: CreativeSession | undefined,
  nextSession: CreativeSession,
  assets: LibraryAsset[],
  events: CreativeSessionEvent[],
  task: CreativeProcessingTask | undefined,
): void {
  if (!session) throw new Error("creative-capture-session-not-found");
  validateCurrentCaptureEvents(before, session, events);
  const plan = before.shotPlans.find((item) => item.sessionId === session.id && item.status === "active" &&
    item.briefVersion === session.currentBriefVersion && item.scriptVersion === session.currentScriptVersion &&
    item.shotPlanVersion === session.currentShotPlanVersion);
  if (!plan) throw new Error("creative-capture-shot-plan-not-found");
  const combinedAssets = [...before.assets, ...assets];
  const steps = resolveCreativeSteps(plan, [...before.creativeSessionEvents, ...events], combinedAssets);
  const status = steps.some((step) => step.state === "pending" || step.state === "retake-pending")
    ? "collecting" : combinedAssets.some((asset) => asset.captureContext?.creativeSessionId === session.id)
      ? "ready-for-editorial" : "no-assets";
  if (task && task.status !== status) throw new Error("creative-capture-settlement-changed");
  if ((status === "collecting" && nextSession.stage === "editorial_review") ||
    (status !== "collecting" && nextSession.stage === "shooting")) {
    throw new Error("creative-capture-settlement-changed");
  }
}

function validateCurrentCaptureEvents(
  before: LibrarySnapshot,
  session: CreativeSession | undefined,
  events: readonly CreativeSessionEvent[],
): void {
  if (!events.some(isCaptureCompletionEvent)) return;
  if (!session) throw new Error("creative-capture-session-not-found");
  const plan = before.shotPlans.find((item) => item.sessionId === session.id && item.status === "active" &&
    item.briefVersion === session.currentBriefVersion && item.scriptVersion === session.currentScriptVersion &&
    item.shotPlanVersion === session.currentShotPlanVersion);
  if (!plan) throw new Error("creative-capture-shot-plan-not-found");
  let projectedEvents = before.creativeSessionEvents;
  for (const event of events) {
    // An exact operation replay is already represented in the durable projection.
    // Otherwise every event, including a retake, becomes authority for the next
    // event in this same atomic bundle before a completion is evaluated.
    if (projectedEvents.some((item) => item.idempotencyKey === event.idempotencyKey)) continue;
    if (!isCaptureCompletionEvent(event)) {
      projectedEvents = [...projectedEvents, event];
      continue;
    }
    const currentSteps = resolveCreativeSteps(plan, projectedEvents, before.assets);
    const step = currentSteps.find((item) => item.stepId === event.stepId);
    if (event.briefVersion !== plan.briefVersion || event.scriptVersion !== plan.scriptVersion ||
      event.shotPlanVersion !== plan.shotPlanVersion || !plan.steps.some((item) => item.id === event.stepId) ||
      !event.deviceId || !event.deviceRole || !session.deviceBindings.some((binding) =>
        binding.deviceId === event.deviceId && binding.deviceRole === event.deviceRole)) {
      throw new Error("creative-capture-return-not-current");
    }
    if (event.action === "completed") {
      const relevant = projectedEvents.filter((item) =>
        item.sessionId === session.id && item.stepId === event.stepId &&
        item.briefVersion === plan.briefVersion && item.scriptVersion === plan.scriptVersion &&
        item.shotPlanVersion === plan.shotPlanVersion);
      const currentTake = Math.max(1, ...relevant.map((item) =>
        item.action === "retake" ? (item.take ?? 1) + 1 : item.take ?? 1));
      const hasCurrentTask = event.deviceRole === "watch-director"
        ? before.directorCards.some((item) => item.sessionId === session.id && item.stepId === event.stepId &&
          item.briefVersion === plan.briefVersion && item.scriptVersion === plan.scriptVersion &&
          item.shotPlanVersion === plan.shotPlanVersion && item.status === "active")
        : before.deviceBriefs.some((item) => item.sessionId === session.id && item.stepId === event.stepId &&
          item.briefVersion === plan.briefVersion && item.scriptVersion === plan.scriptVersion &&
          item.shotPlanVersion === plan.shotPlanVersion && item.deviceId === event.deviceId &&
          item.deviceRole === event.deviceRole && item.status === "active");
      if (!hasCurrentTask || !step || event.take !== currentTake) {
        throw new Error("creative-capture-return-not-current");
      }
      projectedEvents = [...projectedEvents, event];
      continue;
    }
    if (step?.state !== "pending" || step.returnableTake !== event.take) {
      throw new Error("creative-capture-return-not-current");
    }
    const recorded = [...projectedEvents].reverse().find((item) =>
      item.sessionId === session.id && item.stepId === event.stepId &&
      item.briefVersion === plan.briefVersion && item.scriptVersion === plan.scriptVersion &&
      item.shotPlanVersion === plan.shotPlanVersion && item.deviceId === event.deviceId &&
      item.deviceRole === event.deviceRole && ["completed", "retake", "skipped"].includes(item.action));
    if (!recorded || recorded.take !== event.take || recorded.action !== "completed") {
      throw new Error("creative-capture-return-not-current");
    }
    projectedEvents = [...projectedEvents, event];
  }
}

function isCaptureCompletionEvent(event: CreativeSessionEvent): boolean {
  return event.action === "completed" || event.action === "media-returned";
}

function isCaptureCompletionWrite(
  current: CreativeSession | undefined,
  next: CreativeSession,
  assets: readonly LibraryAsset[],
  events: readonly CreativeSessionEvent[],
  task: CreativeProcessingTask | undefined,
): boolean {
  return assets.some((asset) => asset.captureContext !== undefined) ||
    events.some(isCaptureCompletionEvent) || task?.phase === "capture" ||
    task?.status === "ready-for-editorial" || task?.status === "no-assets" ||
    (current !== undefined && current.stage !== next.stage &&
      (current.stage === "shooting" || current.stage === "ingesting") &&
      (next.stage === "ingesting" || next.stage === "editorial_review"));
}

function captureMatchesAcceptance(asset: LibraryAsset, event: CreativeSessionEvent): boolean {
  const context = asset.captureContext;
  return context?.stepOutcome === "completed" &&
    context.creativeSessionId === event.sessionId && context.stepId === event.stepId &&
    context.deviceId === event.deviceId && context.deviceRole === event.deviceRole &&
    context.take === event.take && context.scriptVersion === event.scriptVersion &&
    context.shotPlanVersion === event.shotPlanVersion;
}

function captureBusinessKey(asset: LibraryAsset): string | null {
  const context = asset.captureContext;
  if (!context || context.stepOutcome !== "completed") return null;
  return [
    context.creativeSessionId,
    context.stepId,
    String(context.take),
    String(context.scriptVersion),
    String(context.shotPlanVersion),
  ].map(encodeURIComponent).join("|");
}

function mergeAsset(
  existing: LibraryAsset[],
  checked: LibraryAsset,
): { assets: LibraryAsset[]; asset: LibraryAsset } {
  const sameAsset = existing.find((item) => item.id === checked.id);
  if (sameAsset) {
    if (!sameAssetIdentity(sameAsset, checked) ||
      sameAsset.permissionState !== checked.permissionState ||
      !sameAsset.technicalFlags.every((flag) => checked.technicalFlags.includes(flag))) {
      throw new Error("immutable-asset-conflict");
    }
    return { assets: upsertById(existing, checked), asset: checked };
  }
  const duplicate = existing.find((item) =>
    item.digest === checked.digest &&
    item.batchId === checked.batchId &&
    item.sessionId === checked.sessionId &&
    item.permissionState === checked.permissionState &&
    canonicalCaptureContextKey(item.captureContext) === canonicalCaptureContextKey(checked.captureContext));
  if (duplicate) return { assets: existing, asset: duplicate };
  return { assets: upsertById(existing, checked), asset: checked };
}

function upsertById<T extends { id: string }>(existing: T[], value: T): T[] {
  const index = existing.findIndex((item) => item.id === value.id);
  if (index === -1) return [...existing, value];
  const updated = [...existing];
  updated[index] = value;
  return updated;
}

function upsertCreativeIntentAnswerOperation(
  existing: CreativeIntentAnswerOperation[],
  value: CreativeIntentAnswerOperation,
): CreativeIntentAnswerOperation[] {
  const owned = existing.find((item) =>
    item.clientInstanceId === value.clientInstanceId && item.operationKey === value.operationKey,
  );
  if (owned && owned.id !== value.id) throw new Error("creative-intent-answer-operation-conflict");
  const prior = existing.find((item) => item.id === value.id);
  if (!prior) {
    if (value.phase !== "answer-pending" || value.intentVersion !== null || value.suggestionSetId !== null) {
      throw new Error("creative-intent-answer-operation-phase-invalid");
    }
    return [...existing, value];
  }
  const identityFields = ["operationKey", "route", "bodyDigest", "customerId",
    "clientInstanceId", "creativeSessionId"] as const;
  if (identityFields.some((field) => prior[field] !== value[field])) {
    throw new Error("creative-intent-answer-operation-conflict");
  }
  if (prior.intentVersion !== null && value.intentVersion !== prior.intentVersion) {
    throw new Error("creative-intent-answer-operation-conflict");
  }
  if (prior.suggestionSetId !== null && value.suggestionSetId !== prior.suggestionSetId) {
    throw new Error("creative-intent-answer-operation-conflict");
  }
  const phases = ["answer-pending", "answer-stored", "suggestion-stored", "complete"] as const;
  const priorPhase = phases.indexOf(prior.phase);
  const nextPhase = phases.indexOf(value.phase);
  const completesWithoutSuggestion = prior.phase === "answer-stored" && value.phase === "complete" &&
    value.suggestionSetId === null;
  if (nextPhase < priorPhase || (nextPhase > priorPhase + 1 && !completesWithoutSuggestion) ||
    (nextPhase === priorPhase && JSON.stringify(prior) !== JSON.stringify(value)) ||
    (nextPhase >= 1 && value.intentVersion === null) ||
    (nextPhase >= 2 && value.suggestionSetId === null && !completesWithoutSuggestion)) {
    throw new Error("creative-intent-answer-operation-phase-invalid");
  }
  if (JSON.stringify(prior) === JSON.stringify(value)) return existing;
  return upsertById(existing, value);
}

function validateCreativeIntentAnswerOperationReferences(
  operation: CreativeIntentAnswerOperation,
  sessions: CreativeSession[],
  intents: CreativeIntent[],
  suggestionSets: MaterialSuggestionSet[],
  segments: ContentSegment[],
  assets: LibraryAsset[],
): void {
  const session = sessions.find((item) => item.id === operation.creativeSessionId);
  if (!session || session.customerId !== operation.customerId || session.status !== "active") {
    throw new Error("creative-intent-answer-operation-session-invalid");
  }
  if (operation.phase === "answer-pending") {
    if (session.stage !== "draft_intent" || operation.intentVersion !== null ||
      operation.suggestionSetId !== null) {
      throw new Error("creative-intent-answer-operation-phase-invalid");
    }
    return;
  }
  const intent = intents.find((item) => item.sessionId === session.id &&
    item.version === operation.intentVersion);
  if (!intent || session.currentIntentVersion !== intent.version) {
    throw new Error("creative-intent-answer-operation-state-invalid");
  }
  if (operation.phase === "answer-stored") {
    if (session.stage !== "draft_intent" || operation.suggestionSetId !== null) {
      throw new Error("creative-intent-answer-operation-phase-invalid");
    }
    return;
  }
  if (operation.suggestionSetId === null) {
    if (operation.phase === "complete" && session.stage === "draft_intent") return;
    throw new Error("creative-intent-answer-operation-phase-invalid");
  }
  const suggestionSet = suggestionSets.find((item) => item.id === operation.suggestionSetId);
  if (!suggestionSet || suggestionSet.sessionId !== session.id) {
    throw new Error("creative-intent-answer-operation-state-invalid");
  }
  const latestPreShootVersion = Math.max(...suggestionSets.filter((item) =>
    item.sessionId === session.id && item.purpose !== "post-shoot-edit").map((item) => item.version));
  const assetsById = new Map(assets.map((asset) => [asset.id, asset]));
  if (suggestionSet.version !== latestPreShootVersion || suggestionSet.suggestions.some((suggestion) => {
    const segment = segments.find((item) => item.id === suggestion.segmentId);
    return !isEligibleHistoricalSource(segment, assetsById.get(segment?.assetId ?? ""));
  })) {
    throw new Error("creative-intent-answer-operation-state-invalid");
  }
  if (!isIntentSuggestionCompletionStage(session.stage)) {
    throw new Error("creative-intent-answer-operation-phase-invalid");
  }
}

function isIntentSuggestionCompletionStage(stage: CreativeSession["stage"]): boolean {
  return stage === "awaiting_material_consent" || stage === "reviewing_materials" ||
    stage === "materials_declined" || stage === "awaiting_script_confirmation" ||
    stage === "ready_to_dispatch" || stage === "shooting" || stage === "ingesting" ||
    stage === "editorial_review" || stage === "completed";
}

function originalEvidenceCard(
  current: EvidenceCard,
  auditEvents: AuditEvent[],
): EvidenceCard {
  if (current.reviewStatus === "pending") return current;
  const firstReview = auditEvents.find((event) =>
    event.entityType === "evidence-card"
    && event.entityId === current.id
    && event.action.startsWith("evidence-"));
  const parsed = EvidenceCardSchema.safeParse(firstReview?.before);
  if (!parsed.success || parsed.data.reviewStatus !== "pending") {
    throw new Error("evidence-review-atomic-state-corrupt");
  }
  return parsed.data;
}

function evidenceCardAfterDecision(
  current: EvidenceCard,
  decision: EvidenceDecision,
): EvidenceCard {
  return EvidenceCardSchema.parse({
    ...current,
    value: decision.action === "corrected"
      ? decision.correctedValue
      : current.value,
    reviewStatus: decision.action,
    reviewedBy: decision.reviewerId,
    reviewedAt: decision.reviewedAt,
    correctionReason: decision.action === "accepted"
      ? null
      : decision.correctionReason,
  });
}

function assertUniqueIds(
  values: readonly { id: string }[],
  errorCode: string,
): void {
  if (new Set(values.map((value) => value.id)).size !== values.length) {
    throw new Error(errorCode);
  }
}

function insertImmutable<T extends { id: string }>(
  existing: T[],
  value: T,
  conflictCode: string,
): T[] {
  const prior = existing.find((item) => item.id === value.id);
  if (!prior) return [...existing, value];
  if (JSON.stringify(prior) !== JSON.stringify(value)) throw new Error(conflictCode);
  return existing;
}

function upsertStatusTransition<T extends { id: string; status: string }>(
  existing: T[],
  value: T,
  conflictCode: string,
): T[] {
  const prior = existing.find((item) => item.id === value.id);
  if (!prior) return [...existing, value];
  const priorWithoutStatus = { ...prior, status: undefined };
  const nextWithoutStatus = { ...value, status: undefined };
  if (JSON.stringify(priorWithoutStatus) !== JSON.stringify(nextWithoutStatus)) {
    throw new Error(conflictCode);
  }
  if (prior.status === "invalidated" && value.status !== "invalidated") {
    throw new Error(conflictCode);
  }
  return upsertById(existing, value);
}

function upsertScriptTransition(
  existing: ScriptPackage[],
  value: ScriptPackage,
  conflictCode: string,
): ScriptPackage[] {
  const prior = existing.find((item) => item.id === value.id);
  if (!prior) return [...existing, value];
  const priorImmutable = { ...prior, status: undefined, confirmedAt: undefined };
  const nextImmutable = { ...value, status: undefined, confirmedAt: undefined };
  if (JSON.stringify(priorImmutable) !== JSON.stringify(nextImmutable)) {
    throw new Error(conflictCode);
  }
  const confirmationChanged = prior.confirmedAt !== value.confirmedAt;
  const statusChanged = prior.status !== value.status;
  if (confirmationChanged && statusChanged) throw new Error(conflictCode);
  if (confirmationChanged && !(
    prior.status === "active" && value.status === "active" &&
    prior.confirmedAt === null && value.confirmedAt !== null
  )) throw new Error(conflictCode);
  if (statusChanged && !(
    prior.status === "active" && value.status === "invalidated" &&
    prior.confirmedAt === value.confirmedAt
  )) throw new Error(conflictCode);
  return upsertById(existing, value);
}

function validateExpectedCreativeSession(
  sessions: CreativeSession[],
  expected: CreativeBundleCommit["expectedSession"],
  expectedSnapshot: CreativeBundleCommit["expectedSessionSnapshot"],
  sessionId: string,
): void {
  if (expectedSnapshot) {
    const actual = sessions.find((item) => item.id === expectedSnapshot.id);
    if (expectedSnapshot.id !== sessionId || !actual ||
      canonicalCreativeSession(actual) !== canonicalCreativeSession(expectedSnapshot)) {
      throw new Error("creative-session-precondition-failed");
    }
  }
  if (!expected) return;
  const actual = sessions.find((item) => item.id === expected.id);
  if (expected.id !== sessionId || !actual ||
    actual.stage !== expected.stage ||
    actual.currentIntentVersion !== expected.currentIntentVersion ||
    actual.currentBriefVersion !== expected.currentBriefVersion ||
    actual.currentScriptVersion !== expected.currentScriptVersion ||
    actual.currentShotPlanVersion !== expected.currentShotPlanVersion) {
    throw new Error("creative-session-precondition-failed");
  }
}

function canonicalCreativeSession(session: CreativeSession): string {
  return JSON.stringify({
    id: session.id,
    customerId: session.customerId,
    title: session.title,
    stage: session.stage,
    currentIntentVersion: session.currentIntentVersion,
    currentBriefVersion: session.currentBriefVersion,
    currentScriptVersion: session.currentScriptVersion,
    currentShotPlanVersion: session.currentShotPlanVersion,
    materialViewDecisionIds: session.materialViewDecisionIds,
    materialUseDecisionIds: session.materialUseDecisionIds,
    deviceBindings: session.deviceBindings,
    captureBatchIds: session.captureBatchIds,
    editorialBatchId: session.editorialBatchId,
    privacyMode: session.privacyMode,
    prohibitedAreas: session.prohibitedAreas,
    status: session.status,
    createdAt: session.createdAt,
    updatedAt: session.updatedAt,
  });
}

function validateExpectedScript(
  scripts: ScriptPackage[],
  expected: CreativeBundleCommit["expectedScript"],
  session: CreativeSession | undefined,
): void {
  if (!expected) return;
  const actual = scripts.find((item) => item.id === expected.id);
  if (!session ||
    expected.sessionId !== session.id ||
    expected.briefVersion !== session.currentBriefVersion ||
    expected.scriptVersion !== session.currentScriptVersion ||
    !actual ||
    actual.sessionId !== expected.sessionId ||
    actual.briefVersion !== expected.briefVersion ||
    actual.scriptVersion !== expected.scriptVersion ||
    actual.status !== expected.status ||
    actual.confirmedAt !== expected.confirmedAt) {
    throw new Error("script-precondition-failed");
  }
}

function validateTaskLineageAuthorization(
  lineage: NonNullable<CreativeBundleCommit["invalidateTaskLineage"]>,
  sessionId: string,
  expected: CreativeBundleCommit["expectedScript"],
): void {
  if (lineage.length === 0) return;
  if (!expected || expected.status !== "active" || expected.confirmedAt === null ||
    expected.sessionId !== sessionId || lineage.some((item) =>
      item.sessionId !== sessionId ||
      item.sessionId !== expected.sessionId ||
      item.briefVersion !== expected.briefVersion ||
      item.scriptVersion !== expected.scriptVersion)) {
    throw new Error("task-lineage-invalidation-not-authorized");
  }
}

function validateUniqueScriptVersions(
  existing: ScriptPackage[],
  incoming: ScriptPackage[],
): void {
  const candidates = [...existing, ...incoming];
  for (const script of incoming) {
    if (candidates.some((other) => other.id !== script.id &&
      other.sessionId === script.sessionId &&
      other.briefVersion === script.briefVersion &&
      other.scriptVersion === script.scriptVersion)) {
      throw new Error("duplicate-script-version");
    }
  }
}

function requireSingleCreativeActionEvent(
  events: CreativeSessionEvent[],
): CreativeSessionEvent {
  if (events.length !== 1) throw new Error("creative-action-event-required");
  return events[0]!;
}

function eventsByIdempotencyKey(
  events: CreativeSessionEvent[],
  idempotencyKey: string,
): CreativeSessionEvent[] {
  return events.filter((event) => event.idempotencyKey === idempotencyKey);
}

function validateCreativeEventIdempotency(
  existing: CreativeSessionEvent[],
  incoming: CreativeSessionEvent[],
): void {
  const seen = new Map<string, CreativeSessionEvent>();
  for (const event of existing) {
    const prior = seen.get(event.idempotencyKey);
    if (prior) throw new Error("creative-event-idempotency-corrupt");
    seen.set(event.idempotencyKey, event);
  }
  for (const event of incoming) {
    const prior = seen.get(event.idempotencyKey);
    if (prior && !isSameCreativeEventIdentity(prior, event)) {
      throw new Error("creative-event-operation-conflict");
    }
    if (!prior) seen.set(event.idempotencyKey, event);
  }
}

function validateCreativeActionTake(
  existing: CreativeSessionEvent[],
  event: CreativeSessionEvent,
): void {
  const currentTake = Math.max(
    1,
    ...existing
      .filter((item) =>
        item.sessionId === event.sessionId && item.stepId === event.stepId &&
        item.briefVersion === event.briefVersion && item.scriptVersion === event.scriptVersion &&
        item.shotPlanVersion === event.shotPlanVersion,
      )
      .map((item) => item.action === "retake" ? (item.take ?? 1) + 1 : item.take ?? 1),
  );
  const expected = currentTake;
  if (event.take !== expected) throw new Error("creative-event-take-not-current");
}

function validateCreativeActionResultVersion(
  event: CreativeSessionEvent | null,
  resultingScriptVersion: number | null,
  session: CreativeSession,
  scripts: ScriptPackage[],
): void {
  if (!event) return;
  if (resultingScriptVersion === null || session.currentScriptVersion !== resultingScriptVersion) {
    throw new Error("creative-action-result-version-mismatch");
  }
  if (event.action === "voice-rewrite" && !scripts.some((script) =>
    script.sessionId === event.sessionId && script.briefVersion === event.briefVersion &&
    script.scriptVersion === resultingScriptVersion,
  )) {
    throw new Error("creative-action-result-script-required");
  }
}

function mergeCreativeSessionEvents(
  existing: CreativeSessionEvent[],
  incoming: CreativeSessionEvent[],
): CreativeSessionEvent[] {
  return incoming.reduce((items, event) => {
    const prior = items.find((item) => item.idempotencyKey === event.idempotencyKey);
    if (prior) return items;
    return insertImmutable(items, event, "immutable-creative-object-conflict");
  }, existing);
}

function scriptLineageKey(value: {
  sessionId: string;
  briefVersion: number;
  scriptVersion: number;
}): string {
  return `${value.sessionId}:${value.briefVersion}:${value.scriptVersion}`;
}

function normalizeTaskForSnapshot<T extends ShotPlan | DirectorCard | DeviceBrief>(
  task: T,
  snapshot: LibrarySnapshot,
): T {
  if (task.status === "invalidated") return task;
  const lineage = scriptLineageKey(task);
  const script = snapshot.scriptPackages.find((item) => scriptLineageKey(item) === lineage);
  const session = snapshot.creativeSessions.find((item) => item.id === task.sessionId);
  const superseded = session !== undefined && session.currentScriptVersion > 0 && (
    session.currentBriefVersion !== task.briefVersion ||
    session.currentScriptVersion !== task.scriptVersion
  );
  if (script?.status === "invalidated" || session?.status === "invalidated" || superseded) {
    return { ...task, status: "invalidated" } as T;
  }
  return task;
}

function invalidateTaskLineage<T extends ShotPlan | DirectorCard | DeviceBrief>(
  items: T[],
  lineage: Set<string>,
): T[] {
  return items.map((item) => item.status === "active" && lineage.has(scriptLineageKey(item))
    ? { ...item, status: "invalidated" } as T
    : item);
}

function invalidateStatusItems<T extends ShotPlan | DirectorCard | DeviceBrief>(
  items: T[],
  ids: Set<string>,
): T[] {
  return items.map((item) => item.status === "active" && ids.has(item.id)
    ? { ...item, status: "invalidated" } as T
    : item);
}

function upsertAuditedCreativeSession(
  existing: CreativeSession[],
  value: CreativeSession,
  conflictCode: string,
): CreativeSession[] {
  const prior = existing.find((item) => item.id === value.id);
  if (!prior) return [...existing, value];
  if (
    prior.customerId !== value.customerId ||
    prior.title !== value.title ||
    prior.createdAt !== value.createdAt
  ) throw new Error(conflictCode);
  if (prior.status === "invalidated" && value.status !== "invalidated") {
    throw new Error(conflictCode);
  }
  return upsertById(existing, value);
}

function validateCreativeSessionBundleUpdate(
  prior: CreativeSession | undefined,
  next: CreativeSession,
  auditEvents: AuditEvent[],
  persistedAuditIds: Set<string>,
): void {
  if (!hasConsistentCreativeSessionStatus(next)) {
    throw new Error("creative-session-status-stage-inconsistent");
  }
  if (!prior) return;
  if (
    prior.customerId !== next.customerId ||
    prior.title !== next.title ||
    prior.createdAt !== next.createdAt
  ) throw new Error("immutable-creative-object-conflict");

  if (prior.stage !== next.stage) {
    if (!isCreativeSessionTransitionAllowed(prior.stage, next.stage)) {
      throw new Error("creative-session-transition-not-allowed");
    }
    if (!auditEvents.some(
      (event) => !persistedAuditIds.has(event.id) && isMatchingTransitionAudit(event, prior, next),
    )) {
      throw new Error("creative-session-audit-required");
    }
    return;
  }
  if (JSON.stringify(prior) !== JSON.stringify(next) && !auditEvents.some(
    (event) => !persistedAuditIds.has(event.id) && isCreativeSessionAuditFor(event, next.id),
  )) {
    throw new Error("creative-session-audit-required");
  }
}

function validateMaterialSuggestionSetWrites(
  suggestionSets: MaterialSuggestionSet[],
  preconditions: readonly MaterialSuggestionCatalogPrecondition[],
  sessions: CreativeSession[],
  snapshot: LibrarySnapshot,
): void {
  if (suggestionSets.length === 0) {
    if (preconditions.length > 0) throw new Error("material-suggestion-precondition-orphaned");
    return;
  }
  if (preconditions.length !== suggestionSets.length ||
    new Set(preconditions.map((item) => item.suggestionSetId)).size !== preconditions.length) {
    throw new Error("material-suggestion-precondition-required");
  }
  for (const suggestionSet of suggestionSets) {
    const precondition = preconditions.find((item) => item.suggestionSetId === suggestionSet.id);
    if (!precondition) throw new Error("material-suggestion-precondition-required");
    validateMaterialSuggestionSetWrite(suggestionSet, precondition, sessions, snapshot);
  }
}

function validateMaterialSuggestionSetWrite(
  suggestionSet: MaterialSuggestionSet,
  precondition: MaterialSuggestionCatalogPrecondition,
  sessions: CreativeSession[],
  snapshot: LibrarySnapshot,
): void {
  const session = sessions.find((item) => item.id === suggestionSet.sessionId);
  if (!session || session.status !== "active") throw new Error("material-suggestion-session-invalid");
  const purpose = suggestionSet.purpose ?? "pre-shoot-script";
  if (precondition.purpose !== purpose) throw new Error("material-suggestion-purpose-invalid");
  if (purpose === "pre-shoot-script") {
    if (session.stage !== "awaiting_material_consent") {
      throw new Error("material-suggestion-stage-invalid");
    }
  } else if (session.stage !== "ingesting" && session.stage !== "editorial_review") {
    throw new Error("material-suggestion-stage-invalid");
  }

  const existing = snapshot.materialSuggestionSets.find((item) => item.id === suggestionSet.id);
  const exactReplay = existing !== undefined && JSON.stringify(existing) === JSON.stringify(suggestionSet);
  if (!exactReplay) {
    const expectedVersion = Math.max(0, ...snapshot.materialSuggestionSets
      .filter((item) => item.sessionId === session.id)
      .map((item) => item.version)) + 1;
    if (suggestionSet.version !== expectedVersion) throw new Error("material-suggestion-version-invalid");
  }

  const catalog = resolveHistoricalMaterialCatalog(
    session,
    snapshot.segments,
    snapshot.assets,
    sessions,
  );
  if (canonicalHistoricalMaterialSources(precondition.sources) !==
    canonicalHistoricalMaterialSources(catalog.sources)) {
    throw createMaterialCatalogRaceError();
  }
  const sourcesBySegmentId = new Map(catalog.sources.map((source) => [source.segmentId, source]));
  const segmentsById = new Map(catalog.segments.map((segment) => [segment.id, segment]));
  const assetsById = new Map(snapshot.assets.map((asset) => [asset.id, asset]));
  if (new Set(suggestionSet.suggestions.map((item) => item.segmentId)).size !==
    suggestionSet.suggestions.length) {
    throw new Error("material-suggestion-segment-duplicate");
  }
  if (suggestionSet.suggestions.some((suggestion) => {
    const source = sourcesBySegmentId.get(suggestion.segmentId);
    const segment = segmentsById.get(suggestion.segmentId);
    const asset = source ? assetsById.get(source.assetId) : undefined;
    return !source || !segment || !asset ||
      suggestion.thumbnailPath !== asset.localPath ||
      suggestion.capturedAt !== asset.importedAt ||
      suggestion.productId !== (segment.tags.products[0]?.id ?? null) ||
      suggestion.processStage !== (segment.tags.processStages[0] ?? null) ||
      suggestion.source !== asset.origin ||
      suggestion.permissionState !== segment.permissionState ||
      suggestion.lastUsedAt !== segment.tags.usage.lastUsedAt ||
      suggestion.technicallyAvailable !== true;
  })) {
    throw new Error("material-suggestion-source-invalid");
  }
}

function validateRequiredEligibleHistoricalSegments(
  segments: ContentSegment[],
  assets: LibraryAsset[],
  requiredIds: string[],
): void {
  if (new Set(requiredIds).size !== requiredIds.length) {
    throw new Error("duplicate-material-precondition-id");
  }
  const segmentsById = new Map(segments.map((segment) => [segment.id, segment]));
  const assetsById = new Map(assets.map((asset) => [asset.id, asset]));
  if (requiredIds.some((id) => {
    const segment = segmentsById.get(id);
    return !isEligibleHistoricalSource(segment, assetsById.get(segment?.assetId ?? ""));
  })) {
    throw new Error("material-no-longer-eligible");
  }
}

function isMatchingTransitionAudit(
  event: AuditEvent,
  prior: CreativeSession,
  next: CreativeSession,
): boolean {
  return isCreativeSessionAuditFor(event, next.id) &&
    event.action === "creative-session-transitioned" &&
    (event.before as { stage?: string } | null)?.stage === prior.stage &&
    (event.after as { stage?: string } | null)?.stage === next.stage;
}

function isCreativeSessionAuditFor(event: AuditEvent, sessionId: string): boolean {
  return event.entityType === "creative-session" &&
    event.entityId === sessionId &&
    event.actorId.trim().length > 0 &&
    event.reason !== null &&
    event.reason.trim().length > 0;
}

function isMissingFile(error: unknown): error is NodeJS.ErrnoException {
  return error instanceof Error && "code" in error && error.code === "ENOENT";
}
