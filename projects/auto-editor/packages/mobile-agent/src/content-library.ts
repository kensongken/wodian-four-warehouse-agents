import type {
  Approval,
  AuditEvent,
  ContentSegment,
  CreativeBrief,
  CreativeIntent,
  CreativeIntentAnswerOperation,
  CreativeEditorialRestartAuthorization,
  CreativeSession,
  CreativeSessionEvent,
  CreativeProcessingTask,
  HistoricalMaterialConsent,
  DeviceBrief,
  DirectorCard,
  DispatchReceipt,
  EditRevision,
  EvidenceCard,
  EvidenceDecision,
  ExportDelivery,
  ExportPackage,
  LibraryAsset,
  MaterialSuggestionSet,
  MaterialUseDecision,
  RejectedEvidenceSuggestion,
  ScriptPackage,
  ShotPlan,
  StoryCandidate,
  UntypedSegment,
  VariantArtifact,
} from "@wearable/contracts";
import type { HistoricalMaterialSourceSnapshot } from "./historical-source";

export interface ContentLibrary {
  acquireCreativeDispatchReservation(sessionId: string): Promise<CreativeDispatchReservation>;
  assertFresh(): Promise<void>;
  projectCreativeMaterialState<T>(project: (state: CreativeMaterialState) => T | Promise<T>): Promise<T>;
  putAsset(asset: LibraryAsset): Promise<LibraryAsset>;
  listAssets(): Promise<LibraryAsset[]>;
  putUntypedSegments(segments: UntypedSegment[]): Promise<void>;
  listUntypedSegments(): Promise<UntypedSegment[]>;
  putSegments(segments: ContentSegment[]): Promise<void>;
  listSegments(): Promise<ContentSegment[]>;
  putStories(stories: StoryCandidate[]): Promise<void>;
  validateStoryAuthorization(input: StoryAuthorizationInput): Promise<StoryAuthorizationResult>;
  commitStoryCandidates(input: StoryAuthorizationInput & Readonly<{ stories: StoryCandidate[] }>): Promise<void>;
  listStories(): Promise<StoryCandidate[]>;
  readPromptRevisionSources(revision: EditRevision, legacySegments: ContentSegment[]): Promise<Readonly<{
    story: StoryCandidate;
    segments: ContentSegment[];
  }>>;
  putRevision(revision: EditRevision): Promise<void>;
  listRevisions(storyId: string): Promise<EditRevision[]>;
  validateRevisionTimelines(revisionId: string, scope?: Readonly<Pick<EditRevision, "storyId" | "revision"> & {
    creativeSessionId?: string;
  }>): Promise<void>;
  listCreativeRevisionHeads(): Promise<CreativeRevisionHead[]>;
  putArtifact(artifact: VariantArtifact): Promise<void>;
  listArtifacts(revisionId: string): Promise<VariantArtifact[]>;
  listAllArtifacts(): Promise<VariantArtifact[]>;
  putApproval(approval: ApprovalInvalidation): Promise<void>;
  commitApproval(approval: Approval): Promise<Approval>;
  listApprovals(): Promise<Approval[]>;
  putAuditEvent(event: AuditEvent): Promise<void>;
  listAuditEvents(entityId?: string): Promise<AuditEvent[]>;
  commitEvidenceDerivation(value: EvidenceDerivationCommit): Promise<void>;
  listEvidenceCards(creativeSessionId?: string): Promise<EvidenceCard[]>;
  listRejectedEvidenceSuggestions(
    creativeSessionId?: string,
  ): Promise<RejectedEvidenceSuggestion[]>;
  commitEvidenceReview(decision: EvidenceDecision): Promise<EvidenceReviewCommitResult>;
  listEvidenceDecisions(creativeSessionId?: string): Promise<EvidenceDecision[]>;
  putExportPackage(value: ExportPackage): Promise<void>;
  listExportPackages(): Promise<ExportPackage[]>;
  putDelivery(value: ExportDelivery): Promise<void>;
  listDeliveries(): Promise<ExportDelivery[]>;
  putCreativeSession(value: CreativeSession): Promise<void>;
  listCreativeSessions(): Promise<CreativeSession[]>;
  putCreativeIntent(value: CreativeIntent): Promise<void>;
  listCreativeIntents(): Promise<CreativeIntent[]>;
  putCreativeIntentAnswerOperation(value: CreativeIntentAnswerOperation): Promise<void>;
  listCreativeIntentAnswerOperations(): Promise<CreativeIntentAnswerOperation[]>;
  putMaterialSuggestionSet(
    value: MaterialSuggestionSet,
    precondition: MaterialSuggestionCatalogPrecondition,
  ): Promise<void>;
  listMaterialSuggestionSets(): Promise<MaterialSuggestionSet[]>;
  putMaterialUseDecision(value: MaterialUseDecision): Promise<void>;
  listMaterialUseDecisions(): Promise<MaterialUseDecision[]>;
  putCreativeBrief(value: CreativeBrief): Promise<void>;
  listCreativeBriefs(): Promise<CreativeBrief[]>;
  putScriptPackage(value: ScriptPackage): Promise<void>;
  listScriptPackages(): Promise<ScriptPackage[]>;
  putShotPlan(value: ShotPlan): Promise<void>;
  listShotPlans(): Promise<ShotPlan[]>;
  putDirectorCard(value: DirectorCard): Promise<void>;
  listDirectorCards(): Promise<DirectorCard[]>;
  putDeviceBrief(value: DeviceBrief): Promise<void>;
  listDeviceBriefs(): Promise<DeviceBrief[]>;
  putDispatchReceipt(value: DispatchReceipt): Promise<void>;
  listDispatchReceipts(): Promise<DispatchReceipt[]>;
  putCreativeSessionEvent(value: CreativeSessionEvent): Promise<void>;
  listCreativeSessionEvents(): Promise<CreativeSessionEvent[]>;
  putCreativeProcessingTask(value: CreativeProcessingTask): Promise<void>;
  listCreativeProcessingTasks(): Promise<CreativeProcessingTask[]>;
  migrateLegacyCreativeEditorialTask(taskId: string, now: string): Promise<CreativeProcessingTask>;
  reserveCreativeEditorialRestart(
    value: Omit<CreativeEditorialRestartAuthorization,
      "status" | "restartedTaskId" | "consumedAt">,
  ): Promise<CreativeEditorialRestartAuthorization>;
  listCreativeEditorialRestartAuthorizations(): Promise<CreativeEditorialRestartAuthorization[]>;
  beginCreativeEditorialRestart(input: Readonly<{
    authorizationId: string;
    creativeSessionId: string;
    processOperationKey: string;
    replacementTask: CreativeProcessingTask;
    consumedAt: string;
  }>): Promise<Readonly<{
    authorization: CreativeEditorialRestartAuthorization;
    processingTask: CreativeProcessingTask;
  }>>;
  commitExportPackage(value: ExportPackage, creativeSessionId?: string): Promise<ExportPackage>;
  validateExportPackageForDelivery(packageId: string, creativeSessionId?: string): Promise<ExportPackage>;
  commitExportDelivery(input: Readonly<{
    packageId: string;
    expectedPackage: ExportPackage;
    delivery: ExportDelivery;
    packageStatus: "delivered" | "pending-retry";
    creativeSessionId?: string;
  }>): Promise<ExportDelivery>;
  commitCreativeCaptureBundle(value: CreativeCaptureBundleCommit): Promise<void>;
  commitCreativeEditorialBundle(value: CreativeEditorialBundleCommit): Promise<void>;
  readCreativeEditorialBundle(taskId: string, options?: { currentRevisions: true }): Promise<CreativeEditorialResult>;
  commitPrivacyInvalidation(value: PrivacyInvalidationCommit): Promise<PrivacyInvalidationOutcome>;
  commitRevisionBundle(value: RevisionBundleCommit): Promise<void>;
  commitCreativeBundle(
    value: CreativeBundleCommit,
  ): Promise<void | CreativeActionCommitResult>;
}

export type EvidenceDerivationCommit = Readonly<{
  cards: readonly EvidenceCard[];
  rejectedSuggestions: readonly RejectedEvidenceSuggestion[];
}>;

export type EvidenceReviewCommitResult = Readonly<{
  card: EvidenceCard;
  decision: EvidenceDecision;
  auditEvent: AuditEvent;
}>;

export type CreativeMaterialState = Readonly<{
  sessions: readonly CreativeSession[];
  sets: readonly MaterialSuggestionSet[];
  decisions: readonly MaterialUseDecision[];
  segments: readonly ContentSegment[];
  assets: readonly LibraryAsset[];
  editorial: Readonly<{
    stories: readonly StoryCandidate[];
    tasks: readonly CreativeProcessingTask[];
    revisions: readonly EditRevision[];
    artifacts: readonly VariantArtifact[];
    approvals: readonly Approval[];
    exports: readonly ExportPackage[];
    deliveries: readonly ExportDelivery[];
    revisionHeads: readonly CreativeRevisionHead[];
    revisionBindings: readonly Readonly<{
      revisionId: string;
      creativeSessionId: string;
      taskId: string;
      outputRoot: string;
    }>[];
  }>;
}>;

export type PrivacyInvalidationCommit = Readonly<{
  segmentId: string;
  reason: string;
  actorId: string;
}>;

export type PrivacyInvalidationOutcome = Readonly<{
  invalidatedCreativeBriefIds: string[];
  invalidatedApprovalIds: string[];
  invalidatedArtifactIds: string[];
  invalidatedExportPackageIds: string[];
  invalidatedScriptPackageIds: string[];
  invalidatedShotPlanIds: string[];
  invalidatedDirectorCardIds: string[];
  invalidatedDeviceBriefIds: string[];
}>;

export type ApprovalInvalidation = Approval & Readonly<{ status: "invalidated" }>;

export type RevisionBundleCommit = Readonly<{
  renderOutputRoot?: string;
  revisions: EditRevision[];
  artifacts: VariantArtifact[];
  approvals: ApprovalInvalidation[];
  exportPackages: ExportPackage[];
  auditEvent: AuditEvent;
}>;

export type CreativeRevisionHead = Readonly<{
  storyId: string;
  creativeSessionId: string;
  taskId: string;
  revisionId: string;
  revision: number;
  renderInputDigest?: string;
  sourceEvidenceDigest?: string;
}>;

export type CreativeBundleCommit = Readonly<{
  session: CreativeSession;
  assets?: LibraryAsset[];
  dispatchReservationId?: string;
  expectedSession?: Pick<
    CreativeSession,
    | "id"
    | "stage"
    | "currentIntentVersion"
    | "currentBriefVersion"
    | "currentScriptVersion"
    | "currentShotPlanVersion"
  >;
  expectedSessionSnapshot?: CreativeSession;
  expectedScript?: Pick<
    ScriptPackage,
    "id" | "sessionId" | "briefVersion" | "scriptVersion" | "status" | "confirmedAt"
  >;
  invalidateTaskLineage?: Array<Readonly<{
    sessionId: string;
    briefVersion: number;
    scriptVersion: number;
  }>>;
  requiredEligibleHistoricalSegmentIds?: string[];
  intents?: CreativeIntent[];
  intentAnswerOperation?: CreativeIntentAnswerOperation;
  materialSuggestionSets?: MaterialSuggestionSet[];
  materialSuggestionPreconditions?: MaterialSuggestionCatalogPrecondition[];
  decisions?: MaterialUseDecision[];
  creativeBriefs?: CreativeBrief[];
  scripts?: ScriptPackage[];
  shotPlans?: ShotPlan[];
  directorCards?: DirectorCard[];
  deviceBriefs?: DeviceBrief[];
  receipts?: DispatchReceipt[];
  events?: CreativeSessionEvent[];
  processingTask?: CreativeProcessingTask;
  returnActionResult?: true;
  auditEvents: AuditEvent[];
}>;

export type MaterialSuggestionCatalogPrecondition = Readonly<{
  suggestionSetId: string;
  purpose: "pre-shoot-script" | "post-shoot-edit";
  sources: readonly HistoricalMaterialSourceSnapshot[];
}>;

export type CreativeActionCommitResult = Readonly<{
  event: CreativeSessionEvent;
  resultingScriptVersion: number;
}>;

export type CreativeCaptureBundleCommit = Readonly<{
  session: CreativeSession;
  expectedSession: Pick<CreativeSession,
    "id" | "stage" | "currentBriefVersion" | "currentScriptVersion" | "currentShotPlanVersion">;
  assets: LibraryAsset[];
  events: CreativeSessionEvent[];
  processingTask: CreativeProcessingTask;
  auditEvents: AuditEvent[];
}>;

export type CreativeEditorialBundleCommit = Readonly<{
  renderOutputRoot: string;
  processingTask: CreativeProcessingTask;
  stories: StoryCandidate[];
  revisions: EditRevision[];
  artifacts: VariantArtifact[];
  auditEvents: AuditEvent[];
}>;

export type StoryAuthorizationInput = Readonly<{ batchId: string; authorization: HistoricalMaterialConsent }>;
export type StoryAuthorizationResult = Readonly<{
  authorization: HistoricalMaterialConsent;
  historicalSegments: ContentSegment[];
}>;
export type CreativeEditorialResult = Readonly<{
  task: CreativeProcessingTask;
  stories: StoryCandidate[];
  revisions: EditRevision[];
  artifacts: VariantArtifact[];
}>;

export type CreativeDispatchReservation = Readonly<{
  id: string;
  isInterrupted(): boolean;
  tryFinalize(): Promise<boolean>;
  release(): void;
}>;
