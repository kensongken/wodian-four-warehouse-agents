import {
  createD1ActiveMembershipLookup,
  getCurrentUser,
  type ActiveMembershipLookup,
  type CurrentUser,
} from "./current-user";
import type { D1DatabaseLike } from "@/server/supervision/d1-repository";

export class AuthorizationError extends Error {
  readonly status = 403;

  constructor() {
    super("Insufficient role for this operation.");
    this.name = "AuthorizationError";
  }
}

/** Resolves an active owner or employee for employee-facing supervision flows. */
export async function requireEmployee(
  headers: Headers,
  memberships: ActiveMembershipLookup,
): Promise<CurrentUser> {
  return getCurrentUser(headers, memberships);
}

/** Resolves an active owner for supervision flows restricted to shop owners. */
export async function requireOwner(
  headers: Headers,
  memberships: ActiveMembershipLookup,
): Promise<CurrentUser> {
  const currentUser = await getCurrentUser(headers, memberships);
  if (currentUser.role !== "owner") throw new AuthorizationError();
  return currentUser;
}

/** Production adapter for employee-facing routes with an explicit D1 binding. */
export async function requireEmployeeFromD1(
  headers: Headers,
  d1: D1DatabaseLike,
): Promise<CurrentUser> {
  return requireEmployee(headers, createD1ActiveMembershipLookup(d1));
}

/** Production adapter for owner-only routes with an explicit D1 binding. */
export async function requireOwnerFromD1(
  headers: Headers,
  d1: D1DatabaseLike,
): Promise<CurrentUser> {
  return requireOwner(headers, createD1ActiveMembershipLookup(d1));
}
