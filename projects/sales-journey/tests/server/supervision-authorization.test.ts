import { describe, expect, it } from "vitest";

import type { ActiveMembershipLookup } from "@/server/auth/current-user";
import { requireEmployee, requireOwner } from "@/server/auth/authorization";

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
    if (input.accountUserId === "owner-1" && input.email === "owner@shop.test") {
      return {
        accountUserId: "owner-1",
        email: "owner@shop.test",
        tenantId: "tenant-a",
        role: "owner",
      };
    }
    return undefined;
  },
};

function headersFor(email: string): Headers {
  return new Headers({
    "oai-authenticated-user-id": email.startsWith("owner") ? "owner-1" : "employee-1",
    "oai-authenticated-user-email": email,
  });
}

describe("supervision authorization", () => {
  it("accepts an active employee for employee flows", async () => {
    await expect(requireEmployee(headersFor("employee@shop.test"), memberships))
      .resolves.toMatchObject({ accountUserId: "employee-1", role: "employee" });
  });

  it("accepts an owner for employee flows", async () => {
    await expect(requireEmployee(headersFor("owner@shop.test"), memberships))
      .resolves.toMatchObject({ accountUserId: "owner-1", role: "owner" });
  });

  it("rejects an employee from the owner dashboard", async () => {
    await expect(requireOwner(headersFor("employee@shop.test"), memberships))
      .rejects.toMatchObject({ status: 403 });
  });

  it("accepts an owner for owner-only flows", async () => {
    await expect(requireOwner(headersFor("owner@shop.test"), memberships))
      .resolves.toMatchObject({ accountUserId: "owner-1", role: "owner" });
  });
});
