import type { SupervisionRole } from "@/domain/supervision/contracts";
import type { D1DatabaseLike } from "@/server/supervision/d1-repository";

export type PlatformAuthenticatedIdentity = Readonly<{
  accountUserId: string;
  email: string;
}>;

export type CurrentUser = PlatformAuthenticatedIdentity & Readonly<{
  tenantId: string;
  role: SupervisionRole;
}>;

/**
 * The membership directory is server-owned. Callers must never construct it
 * from cookies, query values, form fields, or JSON request bodies.
 */
export interface ActiveMembershipLookup {
  findActiveMembership(identity: PlatformAuthenticatedIdentity): Promise<CurrentUser | undefined>;
}

export class AuthenticationError extends Error {
  readonly status = 401;

  constructor() {
    super("Authentication required.");
    this.name = "AuthenticationError";
  }
}

function requiredPlatformHeader(headers: Headers, name: string): string {
  const value = headers.get(name)?.trim();
  if (!value) throw new AuthenticationError();
  return value;
}

function normalizeEmail(email: string): string {
  return email.trim().toLowerCase();
}

function isNonBlankString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isSupervisionRole(value: unknown): value is SupervisionRole {
  return value === "employee" || value === "owner";
}

function isCurrentUserForIdentity(
  membership: unknown,
  identity: PlatformAuthenticatedIdentity,
): membership is CurrentUser {
  if (!membership || typeof membership !== "object") return false;

  const candidate = membership as Record<string, unknown>;
  return isNonBlankString(candidate.accountUserId)
    && isNonBlankString(candidate.email)
    && isNonBlankString(candidate.tenantId)
    && candidate.accountUserId === identity.accountUserId
    && normalizeEmail(candidate.email) === identity.email
    && isSupervisionRole(candidate.role);
}

function mapD1Membership(
  row: Record<string, unknown>,
  identity: PlatformAuthenticatedIdentity,
): CurrentUser | undefined {
  const membership = {
    accountUserId: row.id,
    email: row.email,
    tenantId: row.tenant_id,
    role: row.role,
  };

  if (!isCurrentUserForIdentity(membership, identity)) return undefined;

  return {
    accountUserId: membership.accountUserId,
    email: normalizeEmail(membership.email),
    tenantId: membership.tenantId,
    role: membership.role,
  };
}

/** Builds an active-membership directory backed solely by Task 2's users table. */
export function createD1ActiveMembershipLookup(d1: D1DatabaseLike): ActiveMembershipLookup {
  return {
    async findActiveMembership(identity) {
      try {
        const { results } = await d1.prepare(
          `SELECT id, tenant_id, email, role
           FROM users
           WHERE id = ? AND LOWER(email) = ? AND active = 1`,
        ).bind(identity.accountUserId, identity.email).all<Record<string, unknown>>();
        if (results.length !== 1) return undefined;
        return mapD1Membership(results[0]!, identity);
      } catch {
        return undefined;
      }
    },
  };
}

/**
 * Resolves the current active membership from platform-authenticated headers
 * using an explicitly injected, server-owned membership directory.
 */
export async function getCurrentUser(
  headers: Headers,
  memberships: ActiveMembershipLookup,
): Promise<CurrentUser> {
  const identity: PlatformAuthenticatedIdentity = {
    accountUserId: requiredPlatformHeader(headers, "oai-authenticated-user-id"),
    email: normalizeEmail(requiredPlatformHeader(headers, "oai-authenticated-user-email")),
  };
  let membership: CurrentUser | undefined;
  try {
    membership = await memberships.findActiveMembership(identity);
  } catch {
    throw new AuthenticationError();
  }

  if (!membership || !isCurrentUserForIdentity(membership, identity)) {
    throw new AuthenticationError();
  }

  return {
    accountUserId: membership.accountUserId,
    email: normalizeEmail(membership.email),
    tenantId: membership.tenantId,
    role: membership.role,
  };
}

/** Production adapter for routes that receive Task 2's D1 binding directly. */
export async function getCurrentUserFromD1(
  headers: Headers,
  d1: D1DatabaseLike,
): Promise<CurrentUser> {
  return getCurrentUser(headers, createD1ActiveMembershipLookup(d1));
}
