import { z } from "zod";

import {
  ProductStrategySealSchema,
  type DecisionCard,
  type ProductStrategySeal,
} from "@/domain/decision-card/schema";
import {
  CUSTOMER_INTENTS,
  SAFETY_RULE_IDS,
  SALES_SCENARIOS,
  SALES_STAGES,
  type SalesScenario,
  type SalesStage,
} from "@/domain/decision-card/types";
import {
  EmployeeReplyFeedbackRequestSchema,
  REPLY_AGENT_VERSION,
} from "@/domain/reply-feedback/schema";
import { sanitizeFeedbackFinalReply } from "@/domain/reply-feedback/redaction";
import {
  calculateVaultDeleteAfter,
  type AuditEvent,
  type CustomerRecord,
  type CreateReplyFeedbackInput,
  type DecisionUsageRecord,
  type FollowUpRecord,
  type ReplyFeedbackSaveResult,
  type VaultAssetKind,
  type VaultAssetRecord,
} from "@/domain/supervision/contracts";
import {
  AppendCorrectionInputSchema,
  AppendFollowUpInputSchema,
  CreateCustomerInputSchema,
} from "@/domain/supervision/schemas";
import type { CurrentUser } from "@/server/auth/current-user";
import {
  ProductStrategyFreshnessUnavailableError,
  StaleProductStrategyError,
  type ProductStrategyFreshnessGuard,
} from "@/server/product-strategy/freshness";
import type { D1DatabaseLike } from "./d1-repository";
import {
  getSupervisionRepository,
  type AppendCorrectionBundleResult,
  type AppendFollowUpBundleResult,
  type CreateCustomerBundleResult,
  type SupervisionRepository,
  type TimelineEntry,
} from "./repository";
import type { PrivateTextVault } from "./vault";
import { getLocalSyntheticSupervisionRuntime } from "./local-synthetic-runtime";

export type { PrivateTextVault, PrivateTextVaultWrite } from "./vault";

const DEFAULT_USAGE_TOKEN_TTL_MILLISECONDS = 15 * 60 * 1_000;
const NonBlankRequestIdSchema = z.string().trim().min(1).max(120);
const CreateCustomerCommandSchema = CreateCustomerInputSchema.extend({
  requestId: NonBlankRequestIdSchema,
});
const AppendFollowUpCommandSchema = AppendFollowUpInputSchema.extend({
  requestId: NonBlankRequestIdSchema,
  usageToken: z.string().trim().min(1).max(8_000),
});
const AppendCorrectionCommandSchema = AppendCorrectionInputSchema.extend({
  requestId: NonBlankRequestIdSchema,
});

const UsageTokenPayloadSchema = z.object({
  version: z.literal("decision-usage.v3"),
  tenantId: z.string().trim().min(1),
  employeeUserId: z.string().trim().min(1),
  customerId: z.string().trim().min(1),
  decisionCardId: z.string().trim().min(1),
  cardInstanceId: z.string().trim().min(1).max(120),
  cardDigest: z.string().regex(/^sha256_[a-f0-9]{64}$/u),
  agentVersion: z.literal(REPLY_AGENT_VERSION),
  scenario: z.enum(SALES_SCENARIOS),
  salesStage: z.enum(SALES_STAGES),
  evidenceIds: z.array(z.string().trim().min(1).max(120)).max(20).readonly(),
  customerIntent: z.enum(CUSTOMER_INTENTS),
  deliveryMode: z.literal("copy_only"),
  providedReplyIds: z.array(z.string().trim().min(1)).min(1).readonly(),
  safetyStatus: z.enum(["safe", "reviewRequired"]),
  safetyRuleIds: z.array(z.enum(SAFETY_RULE_IDS)).readonly(),
  productStrategySeal: ProductStrategySealSchema.optional(),
  issuedAt: z.string().datetime({ offset: true }),
  expiresAt: z.string().datetime({ offset: true }),
}).strict();

const DecisionUsageJourneySnapshotSchema = z.object({
  scenario: z.enum(SALES_SCENARIOS),
  salesStage: z.enum(SALES_STAGES),
  evidenceIds: z.array(z.string().trim().min(1).max(120)).max(20)
    .refine((ids) => new Set(ids).size === ids.length, {
      message: "Approved evidence ids must be unique.",
    })
    .readonly(),
}).strict();

type AppendFollowUpCommand = z.infer<typeof AppendFollowUpCommandSchema>;
type UsageTokenPayload = z.infer<typeof UsageTokenPayloadSchema>;
export type DecisionUsageJourneySnapshot = z.infer<typeof DecisionUsageJourneySnapshotSchema>;

export type VerifiedDecisionContext = Readonly<{
  tenantId: string;
  employeeUserId: string;
  customerId: string;
  decisionCardId: string;
  cardInstanceId: string;
  cardDigest: string;
  agentVersion: typeof REPLY_AGENT_VERSION;
  scenario: SalesScenario;
  salesStage: SalesStage;
  evidenceIds: readonly string[];
}>;

export type VersionedIdempotencySecret = Readonly<{
  version: string;
  secret: string;
}>;

export type IdempotencyKeyring = readonly [
  VersionedIdempotencySecret,
  ...VersionedIdempotencySecret[],
];

export type CustomerServiceOptions = Readonly<{
  repository: SupervisionRepository;
  vault: PrivateTextVault;
  usageTokenSecret: string;
  idempotencyKeyring: IdempotencyKeyring;
  productStrategyFreshnessGuard?: ProductStrategyFreshnessGuard;
  now?: () => string;
  createId?: () => string;
  usageTokenTtlMilliseconds?: number;
}>;

export type SupervisionApiRuntimeBindings = Readonly<{
  d1: D1DatabaseLike;
  repository?: SupervisionRepository;
  vault: PrivateTextVault;
  usageTokenSecret: string;
  idempotencyKeyring: IdempotencyKeyring;
  retentionSchedulerSecret: string;
  now?: () => string;
  createId?: () => string;
}>;

export type SupervisionApiRuntime = Readonly<{
  d1: D1DatabaseLike;
  repository: SupervisionRepository;
  vault: PrivateTextVault;
  usageTokenSecret: string;
  idempotencyKeyring: IdempotencyKeyring;
  retentionSchedulerSecret: string;
  now?: () => string;
  createId?: () => string;
}>;

type RuntimeFactory = () => SupervisionApiRuntimeBindings | undefined;

export class CustomerNotFoundError extends Error {
  readonly status = 404;
  readonly code = "CUSTOMER_NOT_FOUND";

  constructor() {
    super("Customer not found.");
    this.name = "CustomerNotFoundError";
  }
}

export class FollowUpNotFoundError extends Error {
  readonly status = 404;
  readonly code = "FOLLOW_UP_NOT_FOUND";

  constructor() {
    super("Follow-up not found.");
    this.name = "FollowUpNotFoundError";
  }
}

export class UsageTokenError extends Error {
  readonly status = 400;

  constructor(readonly code: "INVALID_USAGE_TOKEN" | "EXPIRED_USAGE_TOKEN") {
    super(code === "EXPIRED_USAGE_TOKEN" ? "Usage token expired." : "Invalid usage token.");
    this.name = "UsageTokenError";
  }
}

export class FutureActualFollowUpError extends Error {
  readonly status = 400;
  readonly code = "FUTURE_ACTUAL_FOLLOW_UP";

  constructor() {
    super("Actual follow-up time cannot be in the future.");
    this.name = "FutureActualFollowUpError";
  }
}

export class IdempotencyConflictError extends Error {
  readonly status = 409;
  readonly code = "IDEMPOTENCY_CONFLICT";

  constructor() {
    super("Idempotency key was already used for a different request.");
    this.name = "IdempotencyConflictError";
  }
}

export class ReplyFeedbackProofConflictError extends Error {
  readonly status = 409;
  readonly code = "REPLY_FEEDBACK_PROOF_CONFLICT";

  constructor() {
    super("Reply feedback proof does not match server context.");
    this.name = "ReplyFeedbackProofConflictError";
  }
}

export class SupervisionRuntimeUnavailableError extends Error {
  readonly status = 503;
  readonly code = "SUPERVISION_RUNTIME_UNAVAILABLE";

  constructor() {
    super("Supervision runtime unavailable.");
    this.name = "SupervisionRuntimeUnavailableError";
  }
}

let runtimeFactory: RuntimeFactory | undefined;

function secretHasMinimumEntropy(value: unknown): value is string {
  return typeof value === "string" && new TextEncoder().encode(value).byteLength >= 32;
}

function validateIdempotencyKeyring(value: unknown): IdempotencyKeyring {
  if (!Array.isArray(value) || value.length === 0) {
    throw new SupervisionRuntimeUnavailableError();
  }
  const versions = new Set<string>();
  const secrets = new Set<string>();
  for (const candidate of value) {
    if (
      !candidate
      || typeof candidate !== "object"
      || typeof candidate.version !== "string"
      || !/^[a-z0-9][a-z0-9_-]{0,31}$/u.test(candidate.version)
      || !secretHasMinimumEntropy(candidate.secret)
      || versions.has(candidate.version)
      || secrets.has(candidate.secret)
    ) {
      throw new SupervisionRuntimeUnavailableError();
    }
    versions.add(candidate.version);
    secrets.add(candidate.secret);
  }
  return value as unknown as IdempotencyKeyring;
}

/** Hosting code supplies server-owned bindings; callers receive a scoped restore hook for tests. */
export function configureSupervisionApiRuntime(factory: RuntimeFactory | undefined): () => void {
  const previousFactory = runtimeFactory;
  runtimeFactory = factory;
  return () => {
    if (runtimeFactory === factory) runtimeFactory = previousFactory;
  };
}

/** Resolves the trusted D1 binding before a route can authenticate or touch records. */
export function getSupervisionApiRuntime(): SupervisionApiRuntime {
  let bindings: SupervisionApiRuntimeBindings | undefined;
  try {
    bindings = runtimeFactory?.() ?? getLocalSyntheticSupervisionRuntime();
  } catch {
    throw new SupervisionRuntimeUnavailableError();
  }
  if (
    !bindings
    || typeof bindings.d1?.prepare !== "function"
    || typeof bindings.vault?.putText !== "function"
    || typeof bindings.vault?.deleteText !== "function"
    || !secretHasMinimumEntropy(bindings.usageTokenSecret)
    || !secretHasMinimumEntropy(bindings.retentionSchedulerSecret)
  ) {
    throw new SupervisionRuntimeUnavailableError();
  }
  const idempotencyKeyring = validateIdempotencyKeyring(bindings.idempotencyKeyring);
  if (
    bindings.retentionSchedulerSecret === bindings.usageTokenSecret
    || idempotencyKeyring.some(({ secret }) => (
      secret === bindings.usageTokenSecret || secret === bindings.retentionSchedulerSecret
    ))
  ) {
    throw new SupervisionRuntimeUnavailableError();
  }
  return {
    ...bindings,
    idempotencyKeyring,
    repository: bindings.repository ?? getSupervisionRepository({ d1: bindings.d1 }),
  };
}

function defaultNow(): string {
  return new Date().toISOString();
}

function defaultCreateId(): string {
  return crypto.randomUUID();
}

function encodeBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/u, "");
}

function decodeBase64Url(value: string): Uint8Array<ArrayBuffer> {
  const normalized = value.replaceAll("-", "+").replaceAll("_", "/");
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

async function importUsageTokenKey(secret: string): Promise<CryptoKey> {
  if (new TextEncoder().encode(secret).byteLength < 32) {
    throw new SupervisionRuntimeUnavailableError();
  }
  return crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"],
  );
}

async function safeHmacValue(
  prefix: "idem" | "fp",
  label: string,
  value: unknown,
  secret: string,
): Promise<string> {
  const signature = await crypto.subtle.sign(
    "HMAC",
    await importUsageTokenKey(secret),
    new TextEncoder().encode(JSON.stringify([label, value])),
  );
  return `${prefix}_${[...new Uint8Array(signature)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("")}`;
}

type IdempotencyMaterial<T> = Readonly<{
  version: string;
  secret: string;
  idempotencyKey: string;
  existing?: T;
}>;

async function resolveIdempotencyMaterial<T>(input: {
  label: "customer" | "follow-up" | "correction";
  scope: Readonly<Record<string, string>>;
  keyring: IdempotencyKeyring;
  findExisting: (idempotencyKey: string) => Promise<T | undefined>;
}): Promise<IdempotencyMaterial<T>> {
  for (const candidate of input.keyring) {
    const idempotencyKey = await safeHmacValue("idem", input.label, {
      keyVersion: candidate.version,
      ...input.scope,
    }, candidate.secret);
    const existing = await input.findExisting(idempotencyKey);
    if (existing) return { ...candidate, idempotencyKey, existing };
  }
  const current = input.keyring[0];
  return {
    ...current,
    idempotencyKey: await safeHmacValue("idem", input.label, {
      keyVersion: current.version,
      ...input.scope,
    }, current.secret),
  };
}

async function requestFingerprint(
  label: "customer" | "follow-up" | "correction",
  value: unknown,
  material: Pick<IdempotencyMaterial<unknown>, "version" | "secret">,
): Promise<string> {
  return safeHmacValue("fp", label, {
    keyVersion: material.version,
    value,
  }, material.secret);
}

function opaqueId(prefix: string, createId: () => string): string {
  return `${prefix}-${createId()}`;
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value)
      .filter(([, entry]) => entry !== undefined)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, entry]) => [key, canonicalize(entry)]));
  }
  return value;
}

async function decisionCardDigest(card: DecisionCard): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(JSON.stringify(canonicalize(card))),
  );
  return `sha256_${[...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("")}`;
}

async function signUsageToken(payload: UsageTokenPayload, secret: string): Promise<string> {
  const encodedPayload = encodeBase64Url(new TextEncoder().encode(JSON.stringify(payload)));
  const signature = await crypto.subtle.sign(
    "HMAC",
    await importUsageTokenKey(secret),
    new TextEncoder().encode(encodedPayload),
  );
  return `${encodedPayload}.${encodeBase64Url(new Uint8Array(signature))}`;
}

async function verifyUsageToken(token: string, secret: string): Promise<UsageTokenPayload> {
  try {
    const parts = token.split(".");
    if (parts.length !== 2 || !parts[0] || !parts[1]) throw new Error("malformed token");
    const verified = await crypto.subtle.verify(
      "HMAC",
      await importUsageTokenKey(secret),
      decodeBase64Url(parts[1]),
      new TextEncoder().encode(parts[0]),
    );
    if (!verified) throw new Error("invalid signature");
    return UsageTokenPayloadSchema.parse(JSON.parse(
      new TextDecoder().decode(decodeBase64Url(parts[0])),
    ));
  } catch (error) {
    if (error instanceof SupervisionRuntimeUnavailableError) throw error;
    throw new UsageTokenError("INVALID_USAGE_TOKEN");
  }
}

function usageTokenIsExpired(expiresAt: string, trustedNow: string): boolean {
  const expiresAtMilliseconds = Date.parse(expiresAt);
  const nowMilliseconds = Date.parse(trustedNow);
  if (!Number.isFinite(expiresAtMilliseconds) || !Number.isFinite(nowMilliseconds)) {
    throw new SupervisionRuntimeUnavailableError();
  }
  return expiresAtMilliseconds <= nowMilliseconds;
}

function auditRecord(input: {
  id: string;
  tenantId: string;
  actorUserId: string;
  action: string;
  targetKind: string;
  targetId: string;
  now: string;
}): AuditEvent {
  return {
    id: input.id,
    tenantId: input.tenantId,
    actorUserId: input.actorUserId,
    action: input.action,
    targetKind: input.targetKind,
    targetId: input.targetId,
    occurredAt: input.now,
    version: 1,
    createdAt: input.now,
  };
}

function vaultAssetRecord(input: {
  id: string;
  tenantId: string;
  customerId: string;
  actorUserId: string;
  kind: Extract<VaultAssetKind, "customer_identity" | "chat_text">;
  vaultKey: string;
  now: string;
}): VaultAssetRecord {
  return {
    id: input.id,
    tenantId: input.tenantId,
    customerId: input.customerId,
    uploadedByUserId: input.actorUserId,
    kind: input.kind,
    vaultKey: input.vaultKey,
    uploadedAt: input.now,
    deleteAfter: calculateVaultDeleteAfter(input.now),
    version: 1,
    createdAt: input.now,
  };
}

function assertCustomerAccess(actor: CurrentUser, customer: CustomerRecord | undefined): CustomerRecord {
  if (
    !customer
    || customer.tenantId !== actor.tenantId
    || (actor.role !== "owner" && customer.employeeUserId !== actor.accountUserId)
  ) {
    throw new CustomerNotFoundError();
  }
  return customer;
}

function assertUsageSelection(command: AppendFollowUpCommand, payload: UsageTokenPayload): void {
  if (
    command.decisionCardId !== payload.decisionCardId
    || (command.cardInstanceId && command.cardInstanceId !== payload.cardInstanceId)
    || (command.cardDigest && command.cardDigest !== payload.cardDigest)
  ) {
    throw new UsageTokenError("INVALID_USAGE_TOKEN");
  }
  if (command.replyUsage === "not_used") {
    if (command.chosenReplyId || command.finalEditedText) {
      throw new UsageTokenError("INVALID_USAGE_TOKEN");
    }
    return;
  }
  if (!command.chosenReplyId || !payload.providedReplyIds.includes(command.chosenReplyId)) {
    throw new UsageTokenError("INVALID_USAGE_TOKEN");
  }
  if (command.replyUsage === "edited" && !command.finalEditedText) {
    throw new UsageTokenError("INVALID_USAGE_TOKEN");
  }
  if (command.replyUsage === "adopted" && command.finalEditedText) {
    throw new UsageTokenError("INVALID_USAGE_TOKEN");
  }
}

export function createCustomerService(options: CustomerServiceOptions) {
  const now = options.now ?? defaultNow;
  const createId = options.createId ?? defaultCreateId;
  const idempotencyKeyring = validateIdempotencyKeyring(options.idempotencyKeyring);
  const tokenTtl = options.usageTokenTtlMilliseconds ?? DEFAULT_USAGE_TOKEN_TTL_MILLISECONDS;
  if (!Number.isSafeInteger(tokenTtl) || tokenTtl <= 0) {
    throw new RangeError("usageTokenTtlMilliseconds must be a positive integer");
  }

  async function requireAccessibleCustomer(actor: CurrentUser, customerId: string) {
    return assertCustomerAccess(actor, await options.repository.getCustomer({
      tenantId: actor.tenantId,
      customerId,
    }));
  }

  async function createCustomer(
    actor: CurrentUser,
    command: unknown,
  ): Promise<CreateCustomerBundleResult> {
    const parsed = CreateCustomerCommandSchema.parse(command);
    const material = await resolveIdempotencyMaterial({
      label: "customer",
      scope: {
        tenantId: actor.tenantId,
        employeeUserId: actor.accountUserId,
        clientRequestId: parsed.requestId,
      },
      keyring: idempotencyKeyring,
      findExisting: (idempotencyKey) => options.repository
        .getCustomerBundleByIdempotencyKey({
          tenantId: actor.tenantId,
          employeeUserId: actor.accountUserId,
          idempotencyKey,
        }),
    });
    const fingerprint = await requestFingerprint("customer", {
      customerAlias: parsed.customerAlias,
      realName: parsed.realName,
      category: parsed.category,
      plannedFollowUpAt: parsed.plannedFollowUpAt,
    }, material);
    const canonicalFingerprintSecret = idempotencyKeyring.at(-1)!;
    const customerFingerprint = await safeHmacValue("fp", "customer-identity", {
      keyVersion: canonicalFingerprintSecret.version,
      customerAlias: parsed.customerAlias.normalize("NFKC").toLowerCase(),
      realName: parsed.realName?.normalize("NFKC").toLowerCase(),
    }, canonicalFingerprintSecret.secret);
    const idempotencyLookup = {
      tenantId: actor.tenantId,
      employeeUserId: actor.accountUserId,
      idempotencyKey: material.idempotencyKey,
    };
    const existing = material.existing;
    if (existing) {
      if (existing.customer.requestFingerprint !== fingerprint) {
        throw new IdempotencyConflictError();
      }
      return existing;
    }
    const createdAt = now();
    const customerId = opaqueId("customer", createId);
    const customer: CustomerRecord = {
      id: customerId,
      tenantId: actor.tenantId,
      employeeUserId: actor.accountUserId,
      idempotencyKey: material.idempotencyKey,
      requestFingerprint: fingerprint,
      customerFingerprint,
      customerNumber: `C-${customerId.slice(-10).toUpperCase()}`,
      customerAlias: parsed.customerAlias,
      category: parsed.category,
      plannedFollowUpAt: parsed.plannedFollowUpAt,
      status: "pending",
      version: 1,
      createdAt,
    };
    const audit = auditRecord({
      id: opaqueId("audit", createId),
      tenantId: actor.tenantId,
      actorUserId: actor.accountUserId,
      action: "customer.created",
      targetKind: "customer",
      targetId: customerId,
      now: customer.createdAt,
    });

    const identityAssetId = opaqueId("vault", createId);
    const identityVaultKey = `private/${actor.tenantId}/${customerId}/${identityAssetId}`;
    const vaultAsset = parsed.realName ? vaultAssetRecord({
      id: identityAssetId,
      tenantId: actor.tenantId,
      customerId,
      actorUserId: actor.accountUserId,
      kind: "customer_identity",
      vaultKey: identityVaultKey,
      now: customer.createdAt,
    }) : undefined;
    if (parsed.realName && vaultAsset) {
      await options.vault.putText({
        tenantId: actor.tenantId,
        customerId,
        uploadedByUserId: actor.accountUserId,
        kind: "customer_identity",
        vaultKey: identityVaultKey,
        content: parsed.realName,
        deleteAfter: vaultAsset.deleteAfter,
      });
    }
    try {
      return await options.repository.createCustomerBundle({ customer, vaultAsset, audit });
    } catch (error) {
      const concurrentlyCreated = await options.repository.getCustomerBundleByIdempotencyKey(
        idempotencyLookup,
      );
      if (concurrentlyCreated) {
        if (
          vaultAsset
          && concurrentlyCreated.vaultAsset?.vaultKey !== vaultAsset.vaultKey
        ) {
          await options.vault.deleteText(vaultAsset.vaultKey).catch(() => undefined);
        }
        if (concurrentlyCreated.customer.requestFingerprint !== fingerprint) {
          throw new IdempotencyConflictError();
        }
        return concurrentlyCreated;
      }
      if (vaultAsset) await options.vault.deleteText(vaultAsset.vaultKey).catch(() => undefined);
      throw error;
    }
  }

  async function listCustomers(actor: CurrentUser): Promise<CustomerRecord[]> {
    return options.repository.listEmployeeCustomers({
      tenantId: actor.tenantId,
      employeeUserId: actor.accountUserId,
    });
  }

  async function getCustomerTimeline(
    actor: CurrentUser,
    customerId: string,
  ): Promise<{ customer: CustomerRecord; timeline: TimelineEntry[] }> {
    const customer = await requireAccessibleCustomer(actor, customerId);
    const timeline = await options.repository.listTimeline({ tenantId: actor.tenantId, customerId });
    return { customer, timeline };
  }

  async function issueDecisionUsageToken(
    actor: CurrentUser,
    customerId: string,
    card: DecisionCard,
    journeySnapshot?: DecisionUsageJourneySnapshot,
    productStrategySeal?: ProductStrategySeal,
  ): Promise<{
    token: string;
    expiresAt: string;
    cardInstanceId: string;
    cardDigest: string;
  }> {
    await requireAccessibleCustomer(actor, customerId);
    const journey = journeySnapshot === undefined
      ? {
          scenario: card.scenario,
          salesStage: card.salesStage,
          evidenceIds: card.audit.evidenceIds,
        }
      : DecisionUsageJourneySnapshotSchema.parse(journeySnapshot);
    if (
      journey.scenario !== card.scenario
      || journey.salesStage !== card.salesStage
    ) {
      throw new UsageTokenError("INVALID_USAGE_TOKEN");
    }
    const cardSeal = card.productStrategySeal;
    if (
      (cardSeal === undefined) !== (productStrategySeal === undefined)
      || (cardSeal && productStrategySeal && (
        cardSeal.productCode !== productStrategySeal.productCode
        || cardSeal.strategyInputVersion !== productStrategySeal.strategyInputVersion
        || cardSeal.contentSha256 !== productStrategySeal.contentSha256
        || cardSeal.status !== productStrategySeal.status
      ))
    ) {
      throw new UsageTokenError("INVALID_USAGE_TOKEN");
    }
    const issuedAt = now();
    const expiresAt = new Date(new Date(issuedAt).getTime() + tokenTtl).toISOString();
    const cardInstanceId = opaqueId("card-instance", createId);
    const cardDigest = await decisionCardDigest(card);
    const payload: UsageTokenPayload = UsageTokenPayloadSchema.parse({
      version: "decision-usage.v3",
      tenantId: actor.tenantId,
      employeeUserId: actor.accountUserId,
      customerId,
      decisionCardId: card.cardId,
      cardInstanceId,
      cardDigest,
      agentVersion: REPLY_AGENT_VERSION,
      scenario: journey.scenario,
      salesStage: journey.salesStage,
      evidenceIds: journey.evidenceIds,
      customerIntent: card.customerIntent,
      deliveryMode: card.deliveryMode,
      providedReplyIds: card.replyOptions.map((reply) => reply.id),
      safetyStatus: card.safety.status,
      safetyRuleIds: card.safety.rules,
      productStrategySeal,
      issuedAt,
      expiresAt,
    });
    return {
      token: await signUsageToken(payload, options.usageTokenSecret),
      expiresAt,
      cardInstanceId,
      cardDigest,
    };
  }

  async function appendFollowUp(
    actor: CurrentUser,
    customerId: string,
    command: unknown,
  ): Promise<AppendFollowUpBundleResult> {
    const parsed = AppendFollowUpCommandSchema.parse(command);
    const customer = await requireAccessibleCustomer(actor, customerId);
    const material = await resolveIdempotencyMaterial({
      label: "follow-up",
      scope: {
        tenantId: actor.tenantId,
        employeeUserId: actor.accountUserId,
        clientRequestId: parsed.requestId,
      },
      keyring: idempotencyKeyring,
      findExisting: (idempotencyKey) => options.repository
        .getFollowUpBundleByIdempotencyKey({
          tenantId: actor.tenantId,
          employeeUserId: actor.accountUserId,
          idempotencyKey,
        }),
    });
    const { requestId: _requestId, ...retryCommand } = parsed;
    const fingerprint = await requestFingerprint("follow-up", {
      customerId,
      command: retryCommand,
    }, material);
    const idempotencyLookup = {
      tenantId: actor.tenantId,
      employeeUserId: actor.accountUserId,
      idempotencyKey: material.idempotencyKey,
    };
    if (material.existing) {
      if (material.existing.followUp.requestFingerprint !== fingerprint) {
        throw new IdempotencyConflictError();
      }
      return material.existing;
    }
    const payload = await verifyUsageToken(parsed.usageToken, options.usageTokenSecret);
    if (
      payload.tenantId !== actor.tenantId
      || payload.employeeUserId !== actor.accountUserId
      || payload.customerId !== customerId
    ) {
      throw new UsageTokenError("INVALID_USAGE_TOKEN");
    }
    assertUsageSelection(parsed, payload);
    const trustedNow = now();
    if (
      parsed.actualFollowUpAt
      && new Date(parsed.actualFollowUpAt).getTime() > new Date(trustedNow).getTime()
    ) {
      throw new FutureActualFollowUpError();
    }

    if (usageTokenIsExpired(payload.expiresAt, trustedNow)) {
      throw new UsageTokenError("EXPIRED_USAGE_TOKEN");
    }

    if (payload.productStrategySeal) {
      const guard = options.productStrategyFreshnessGuard;
      if (!guard) throw new ProductStrategyFreshnessUnavailableError();
      await guard.assertCurrent(actor.tenantId, payload.productStrategySeal);
    }

    const createdAt = trustedNow;
    const followUpId = opaqueId("follow-up", createId);
    const usageId = opaqueId("usage", createId);
    const vaultAssetId = opaqueId("vault", createId);
    const vaultKey = `private/${actor.tenantId}/${customerId}/${vaultAssetId}`;
    const vaultAsset = vaultAssetRecord({
      id: vaultAssetId,
      tenantId: actor.tenantId,
      customerId,
      actorUserId: actor.accountUserId,
      kind: "chat_text",
      vaultKey,
      now: createdAt,
    });
    const followUp: FollowUpRecord = {
      id: followUpId,
      tenantId: actor.tenantId,
      customerId,
      employeeUserId: actor.accountUserId,
      idempotencyKey: material.idempotencyKey,
      requestFingerprint: fingerprint,
      customerMessageVaultAssetId: vaultAsset.id,
      customerMessageSummary: `品类：${customer.category}；意图：${payload.customerIntent}。`,
      plannedFollowUpAt: parsed.plannedFollowUpAt,
      actualFollowUpAt: parsed.actualFollowUpAt,
      result: parsed.result,
      nextAction: parsed.nextAction,
      decisionCardId: payload.decisionCardId,
      cardInstanceId: payload.cardInstanceId,
      replyUsage: parsed.replyUsage,
      chosenReplyId: parsed.chosenReplyId,
      finalEditedText: parsed.finalEditedText,
      version: 1,
      createdAt,
    };
    const decisionUsage: DecisionUsageRecord = {
      id: usageId,
      tenantId: actor.tenantId,
      customerId,
      followUpId,
      employeeUserId: actor.accountUserId,
      decisionCardId: payload.decisionCardId,
      cardInstanceId: payload.cardInstanceId,
      cardDigest: payload.cardDigest,
      scenario: payload.scenario,
      deliveryMode: payload.deliveryMode,
      providedReplyIds: payload.providedReplyIds,
      replyUsage: parsed.replyUsage,
      chosenReplyId: parsed.chosenReplyId,
      finalEditedText: parsed.finalEditedText,
      copiedAt: parsed.replyUsage === "not_used" ? undefined : createdAt,
      safetyStatus: payload.safetyStatus,
      safetyRuleIds: payload.safetyRuleIds,
      version: 1,
      createdAt,
    };
    const audit = auditRecord({
      id: opaqueId("audit", createId),
      tenantId: actor.tenantId,
      actorUserId: actor.accountUserId,
      action: "follow_up.created",
      targetKind: "follow_up",
      targetId: followUpId,
      now: createdAt,
    });

    await options.vault.putText({
      tenantId: actor.tenantId,
      customerId,
      uploadedByUserId: actor.accountUserId,
      kind: "chat_text",
      vaultKey,
      content: JSON.stringify({
        customerMessage: parsed.customerMessage,
        recentContext: parsed.recentContext,
      }),
      deleteAfter: vaultAsset.deleteAfter,
    });
    try {
      return await options.repository.appendFollowUpBundle({
        followUp,
        decisionUsage,
        vaultAsset,
        audit,
      });
    } catch (error) {
      const concurrent = await options.repository.getFollowUpBundleByIdempotencyKey(
        idempotencyLookup,
      );
      if (concurrent) {
        if (concurrent.vaultAsset.vaultKey !== vaultKey) {
          await options.vault.deleteText(vaultKey).catch(() => undefined);
        }
        if (concurrent.followUp.requestFingerprint !== fingerprint) {
          throw new IdempotencyConflictError();
        }
        return concurrent;
      }
      await options.vault.deleteText(vaultKey).catch(() => undefined);
      throw error;
    }
  }

  async function verifyDecisionUsageProof(
    actor: CurrentUser,
    customerId: string,
    usageToken: string,
  ): Promise<Readonly<{
    context: VerifiedDecisionContext;
    expired: boolean;
  }>> {
    await requireAccessibleCustomer(actor, customerId);
    let payload: UsageTokenPayload;
    try {
      payload = await verifyUsageToken(usageToken, options.usageTokenSecret);
    } catch (error) {
      if (error instanceof UsageTokenError) throw new ReplyFeedbackProofConflictError();
      throw error;
    }
    if (
      payload.tenantId !== actor.tenantId
      || payload.employeeUserId !== actor.accountUserId
      || payload.customerId !== customerId
    ) {
      throw new ReplyFeedbackProofConflictError();
    }
    const context: VerifiedDecisionContext = Object.freeze({
      tenantId: payload.tenantId,
      employeeUserId: payload.employeeUserId,
      customerId: payload.customerId,
      decisionCardId: payload.decisionCardId,
      cardInstanceId: payload.cardInstanceId,
      cardDigest: payload.cardDigest,
      agentVersion: payload.agentVersion,
      scenario: payload.scenario,
      salesStage: payload.salesStage,
      evidenceIds: Object.freeze([...payload.evidenceIds]),
    });
    return { context, expired: usageTokenIsExpired(payload.expiresAt, now()) };
  }

  async function verifyDecisionUsageContext(
    actor: CurrentUser,
    customerId: string,
    usageToken: string,
  ): Promise<VerifiedDecisionContext> {
    const verified = await verifyDecisionUsageProof(actor, customerId, usageToken);
    if (verified.expired) throw new UsageTokenError("EXPIRED_USAGE_TOKEN");
    return verified.context;
  }

  async function saveReplyFeedback(
    actor: CurrentUser,
    customerId: string,
    request: unknown,
  ): Promise<ReplyFeedbackSaveResult> {
    const parsed = EmployeeReplyFeedbackRequestSchema.parse(request);
    const verified = await verifyDecisionUsageProof(actor, customerId, parsed.usageToken);
    const privacy = parsed.finalReply === undefined
      ? { privacyStatus: "safe" as const, privacyReasonCodes: [] as const }
      : sanitizeFeedbackFinalReply(parsed.finalReply);
    const input = {
      ...verified.context,
      rating: parsed.rating,
      reasonCodes: parsed.reasonCodes,
      ...privacy,
    } satisfies CreateReplyFeedbackInput;

    if (verified.expired) {
      const replacement = await options.repository.replaceQuarantinedReplyFeedback(input);
      if (!replacement) throw new ReplyFeedbackProofConflictError();
      return replacement;
    }

    if (input.privacyStatus === "safe") {
      const replacement = await options.repository.replaceQuarantinedReplyFeedback(input);
      if (replacement) return replacement;
    }

    try {
      return await options.repository.saveReplyFeedback(input);
    } catch (error) {
      if (
        input.privacyStatus === "safe"
        && error instanceof Error
        && /immutable snapshot mismatch|requires guarded replacement/u.test(error.message)
      ) {
        const replacement = await options.repository.replaceQuarantinedReplyFeedback(input);
        if (replacement) return replacement;
        throw new ReplyFeedbackProofConflictError();
      }
      if (
        error instanceof Error
        && /immutable snapshot mismatch|requires guarded replacement/u.test(error.message)
      ) {
        throw new ReplyFeedbackProofConflictError();
      }
      throw error;
    }
  }

  async function appendCorrection(
    actor: CurrentUser,
    customerId: string,
    command: unknown,
  ): Promise<AppendCorrectionBundleResult> {
    const parsed = AppendCorrectionCommandSchema.parse(command);
    await requireAccessibleCustomer(actor, customerId);
    const timeline = await options.repository.listTimeline({ tenantId: actor.tenantId, customerId });
    const followUp = timeline.find(
      (entry) => entry.kind === "follow_up" && entry.record.id === parsed.followUpId,
    );
    if (followUp?.kind !== "follow_up") throw new FollowUpNotFoundError();

    const material = await resolveIdempotencyMaterial({
      label: "correction",
      scope: {
        tenantId: actor.tenantId,
        actorUserId: actor.accountUserId,
        clientRequestId: parsed.requestId,
      },
      keyring: idempotencyKeyring,
      findExisting: (idempotencyKey) => options.repository
        .getCorrectionBundleByIdempotencyKey({
          tenantId: actor.tenantId,
          actorUserId: actor.accountUserId,
          idempotencyKey,
        }),
    });
    const fingerprint = await requestFingerprint("correction", {
      customerId,
      followUpId: parsed.followUpId,
      reason: parsed.reason,
    }, material);
    const idempotencyLookup = {
      tenantId: actor.tenantId,
      actorUserId: actor.accountUserId,
      idempotencyKey: material.idempotencyKey,
    };
    const existing = material.existing;
    if (existing) {
      if (existing.correction.requestFingerprint !== fingerprint) {
        throw new IdempotencyConflictError();
      }
      return existing;
    }
    const correctionId = opaqueId("correction", createId);
    const createdAt = now();
    const audit = auditRecord({
      id: opaqueId("audit", createId),
      tenantId: actor.tenantId,
      actorUserId: actor.accountUserId,
      action: "correction.created",
      targetKind: "correction",
      targetId: correctionId,
      now: createdAt,
    });

    try {
      return await options.repository.appendCorrectionBundle({
        correction: {
          id: correctionId,
          tenantId: actor.tenantId,
          followUpId: parsed.followUpId,
          actorUserId: actor.accountUserId,
          idempotencyKey: material.idempotencyKey,
          requestFingerprint: fingerprint,
          reason: parsed.reason,
          createdAt,
        },
        audit,
      });
    } catch (error) {
      const concurrent = await options.repository.getCorrectionBundleByIdempotencyKey(
        idempotencyLookup,
      );
      if (concurrent) {
        if (concurrent.correction.requestFingerprint !== fingerprint) {
          throw new IdempotencyConflictError();
        }
        return concurrent;
      }
      throw error;
    }
  }

  return {
    createCustomer,
    listCustomers,
    getCustomerTimeline,
    issueDecisionUsageToken,
    verifyDecisionUsageContext,
    saveReplyFeedback,
    appendFollowUp,
    appendCorrection,
  };
}

export function supervisionApiErrorResponse(error: unknown): Response {
  if (error instanceof z.ZodError) {
    return Response.json({
      error: "INVALID_SUPERVISION_REQUEST",
      details: error.flatten(),
    }, { status: 400 });
  }
  if (
    error instanceof CustomerNotFoundError
    || error instanceof FollowUpNotFoundError
    || error instanceof UsageTokenError
    || error instanceof FutureActualFollowUpError
    || error instanceof IdempotencyConflictError
    || error instanceof ReplyFeedbackProofConflictError
    || error instanceof SupervisionRuntimeUnavailableError
    || error instanceof ProductStrategyFreshnessUnavailableError
    || error instanceof StaleProductStrategyError
  ) {
    return Response.json({ error: error.code }, { status: error.status });
  }
  if (error && typeof error === "object" && "status" in error) {
    const status = (error as { status?: unknown }).status;
    if (status === 401) return Response.json({ error: "AUTHENTICATION_REQUIRED" }, { status });
    if (status === 403) return Response.json({ error: "FORBIDDEN" }, { status });
  }
  return Response.json({ error: "SUPERVISION_OPERATION_FAILED" }, { status: 500 });
}
