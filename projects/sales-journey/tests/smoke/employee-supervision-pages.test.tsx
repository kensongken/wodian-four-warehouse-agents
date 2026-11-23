import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CurrentUser } from "@/server/auth/current-user";
import type { CustomerRecord } from "@/domain/supervision/contracts";
import type { TimelineEntry } from "@/server/supervision/repository";

const actor: CurrentUser = {
  accountUserId: "SECRET_ACTOR_ID",
  email: "staff@shop.test",
  tenantId: "SECRET_TENANT_ID",
  role: "employee",
};

const customer: CustomerRecord = {
  id: "customer-1",
  tenantId: "SECRET_TENANT_ID",
  employeeUserId: "SECRET_ACTOR_ID",
  idempotencyKey: "SECRET_CUSTOMER_IDEMPOTENCY",
  requestFingerprint: "SECRET_CUSTOMER_FINGERPRINT",
  customerFingerprint: "SECRET_CUSTOMER_IDENTITY_FINGERPRINT",
  customerNumber: "C-0000000001",
  customerAlias: "林女士",
  category: "沉香",
  plannedFollowUpAt: "2026-08-31T06:30:00.000Z",
  status: "interested",
  version: 1,
  createdAt: "2026-08-30T06:00:00.000Z",
};

const timeline: TimelineEntry[] = [{
  kind: "follow_up",
  record: {
    id: "follow-up-1",
    tenantId: "SECRET_TENANT_ID",
    customerId: "customer-1",
    employeeUserId: "SECRET_ACTOR_ID",
    idempotencyKey: "SECRET_FOLLOW_UP_IDEMPOTENCY",
    requestFingerprint: "SECRET_FOLLOW_UP_FINGERPRINT",
    customerMessageVaultAssetId: "SECRET_VAULT_ID",
    customerMessageSummary: "品类：沉香；意图：了解身份珠。",
    plannedFollowUpAt: "2026-08-31T06:30:00.000Z",
    actualFollowUpAt: "2026-08-30T06:30:00.000Z",
    result: "interested",
    nextAction: "明天确认到店",
    decisionCardId: "SECRET_CARD_ID",
    cardInstanceId: "SECRET_CARD_INSTANCE",
    replyUsage: "edited",
    chosenReplyId: "SECRET_REPLY_ID",
    finalEditedText: "SECRET_FINAL_TEXT",
    version: 9,
    createdAt: "2026-08-30T06:31:00.000Z",
  },
}];

const requireEmployeeFromD1 = vi.fn(async () => actor);
const listCustomers = vi.fn(async () => [customer]);
const getCustomerTimeline = vi.fn(async () => ({ customer, timeline }));
const runtime = { d1: { prepare: vi.fn() } };

vi.mock("next/headers", () => ({ headers: vi.fn(async () => new Headers()) }));
vi.mock("@/server/auth/authorization", () => ({ requireEmployeeFromD1 }));
vi.mock("@/server/supervision/customer-service", () => ({
  getSupervisionApiRuntime: vi.fn(() => runtime),
  createCustomerService: vi.fn(() => ({ listCustomers, getCustomerTimeline })),
}));

describe("employee supervision pages", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("requires employee context and renders only safe customer list fields", async () => {
    const CustomersPage = (await import("@/app/employee/customers/page")).default;
    render(await CustomersPage());

    expect(requireEmployeeFromD1).toHaveBeenCalledWith(expect.any(Headers), runtime.d1);
    expect(listCustomers).toHaveBeenCalledWith(actor);
    expect(screen.getByRole("heading", { name: "我的客户跟进" })).toBeInTheDocument();
    expect(screen.getByText("林女士")).toBeInTheDocument();
    expect(screen.getByText("C-0000000001")).toBeInTheDocument();
    expect(screen.getByText("意向中")).toBeInTheDocument();
    expect(screen.queryByText("SECRET_TENANT_ID")).toBeNull();
  });

  it("requires employee context before rendering the new-customer vault flow", async () => {
    const NewCustomerPage = (await import("@/app/employee/customers/new/page")).default;
    render(await NewCustomerPage());

    expect(requireEmployeeFromD1).toHaveBeenCalledWith(expect.any(Headers), runtime.d1);
    expect(screen.getByRole("heading", { name: "建立客户档案", level: 1 })).toBeInTheDocument();
    expect(screen.getAllByText(/真实姓名只进入保险柜/)).toHaveLength(2);
    expect(screen.queryByLabelText(/tenant|operator|租户|操作员/i)).toBeNull();
  });

  it("requires employee context and renders a customer workspace without any real name", async () => {
    const CustomerPage = (await import("@/app/employee/customers/[customerId]/page")).default;
    const tree = await CustomerPage({ params: Promise.resolve({ customerId: "customer-1" }) });
    const serialized = JSON.stringify(tree);
    render(tree);

    expect(getCustomerTimeline).toHaveBeenCalledWith(actor, "customer-1");
    expect(screen.getByRole("heading", { name: "林女士 · C-0000000001" })).toBeInTheDocument();
    expect(screen.getAllByText(/负责人：staff@shop.test/)).toHaveLength(2);
    expect(screen.queryByText("林敏")).toBeNull();
    expect(screen.queryByRole("button", { name: /编辑|删除/ })).toBeNull();
    for (const secret of [
      "SECRET_TENANT_ID",
      "SECRET_ACTOR_ID",
      "SECRET_CUSTOMER_IDEMPOTENCY",
      "SECRET_CUSTOMER_FINGERPRINT",
      "SECRET_CUSTOMER_IDENTITY_FINGERPRINT",
      "SECRET_FOLLOW_UP_IDEMPOTENCY",
      "SECRET_FOLLOW_UP_FINGERPRINT",
      "SECRET_VAULT_ID",
      "SECRET_CARD_ID",
      "SECRET_CARD_INSTANCE",
      "SECRET_REPLY_ID",
      "SECRET_FINAL_TEXT",
    ]) {
      expect(serialized).not.toContain(secret);
    }
  });
});
