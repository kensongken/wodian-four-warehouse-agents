import { describe, expect, it } from "vitest";

import {
  getCurrentUser,
  type ActiveMembershipLookup,
} from "@/server/auth/current-user";

const memberships: ActiveMembershipLookup = {
  async findActiveMembership(input) {
    if (input.accountUserId === "employee-1" && input.email === "employee@shop.test") {
      return {
        accountUserId: "employee-1",
        email: "employee@shop.test",
        tenantId: "tenant-a",
        role: "employee",
      };
    }
    return undefined;
  },
};

function headersFor(accountUserId: string, email: string): Headers {
  return new Headers({
    "oai-authenticated-user-id": accountUserId,
    "oai-authenticated-user-email": email,
  });
}

describe("getCurrentUser", () => {
  it("rejects a request without a platform authenticated email", async () => {
    await expect(getCurrentUser(new Headers(), memberships)).rejects.toMatchObject({ status: 401 });
  });

  it("rejects a request without a platform authenticated user id", async () => {
    await expect(getCurrentUser(new Headers({
      "oai-authenticated-user-email": "employee@shop.test",
    }), memberships)).rejects.toMatchObject({ status: 401 });
  });

  it("resolves the active membership matching both platform identity headers", async () => {
    await expect(getCurrentUser(
      headersFor("employee-1", "employee@shop.test"),
      memberships,
    )).resolves.toEqual({
      accountUserId: "employee-1",
      email: "employee@shop.test",
      tenantId: "tenant-a",
      role: "employee",
    });
  });

  it("rejects an unknown or mismatched platform identity", async () => {
    await expect(getCurrentUser(
      headersFor("owner-1", "employee@shop.test"),
      memberships,
    )).rejects.toMatchObject({ status: 401 });
  });

  it("rejects a membership lookup result that does not match both platform headers", async () => {
    const mismatchedMemberships: ActiveMembershipLookup = {
      async findActiveMembership() {
        return {
          accountUserId: "owner-1",
          email: "owner@shop.test",
          tenantId: "tenant-a",
          role: "owner",
        };
      },
    };

    await expect(getCurrentUser(
      headersFor("employee-1", "employee@shop.test"),
      mismatchedMemberships,
    )).rejects.toMatchObject({ status: 401 });
  });

  it("rejects an inactive membership", async () => {
    const inactiveMemberships: ActiveMembershipLookup = {
      async findActiveMembership() {
        return undefined;
      },
    };

    await expect(getCurrentUser(
      headersFor("employee-1", "employee@shop.test"),
      inactiveMemberships,
    )).rejects.toMatchObject({ status: 401 });
  });

  it("turns a malformed membership result into a 401 instead of throwing a type error", async () => {
    const malformedMemberships: ActiveMembershipLookup = {
      async findActiveMembership() {
        return {
          accountUserId: "employee-1",
          email: "employee@shop.test",
          tenantId: 42,
          role: "employee",
        } as unknown as Awaited<ReturnType<ActiveMembershipLookup["findActiveMembership"]>>;
      },
    };

    await expect(getCurrentUser(
      headersFor("employee-1", "employee@shop.test"),
      malformedMemberships,
    )).rejects.toMatchObject({ status: 401, name: "AuthenticationError" });
  });

  it("does not treat cookies or forged request fields as a platform identity", async () => {
    const forgedRequestHeaders = new Headers({
      cookie: "accountUserId=employee-1; role=owner",
      "x-user-id": "employee-1",
      "x-user-email": "employee@shop.test",
      "x-user-role": "owner",
    });

    await expect(getCurrentUser(forgedRequestHeaders, memberships))
      .rejects.toMatchObject({ status: 401 });
  });
});
