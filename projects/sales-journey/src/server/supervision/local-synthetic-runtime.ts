import type { CurrentUser } from "@/server/auth/current-user";
import type {
  D1DatabaseLike,
  D1PreparedStatementLike,
  D1RunResult,
} from "@/server/supervision/d1-repository";
import { MemorySupervisionRepository } from "@/server/supervision/memory-repository";
import type {
  SupervisionApiRuntimeBindings,
} from "@/server/supervision/customer-service";
import type {
  PrivateTextVaultWrite,
  VaultObject,
  VaultObjectWrite,
  VaultStorage,
} from "@/server/supervision/vault";
import { isExplicitSyntheticLocalMode } from "@/server/synthetic-local-mode";

const GLOBAL_RUNTIME_KEY = "__salesDecisionCardSyntheticSupervisionRuntimeV1";

const SYNTHETIC_USERS: readonly CurrentUser[] = [
  {
    accountUserId: "owner-1",
    email: "owner@shop.test",
    tenantId: "tenant-synthetic",
    role: "owner",
  },
  {
    accountUserId: "employee-1",
    email: "employee-1@shop.test",
    tenantId: "tenant-synthetic",
    role: "employee",
  },
  {
    accountUserId: "employee-2",
    email: "employee-2@shop.test",
    tenantId: "tenant-synthetic",
    role: "employee",
  },
  {
    accountUserId: "owner-2",
    email: "owner@other-shop.test",
    tenantId: "tenant-synthetic-other",
    role: "owner",
  },
  {
    accountUserId: "employee-3",
    email: "employee-3@other-shop.test",
    tenantId: "tenant-synthetic-other",
    role: "employee",
  },
];

class LocalSyntheticVault implements VaultStorage {
  private readonly objects = new Map<string, VaultObject>();

  async putText(input: PrivateTextVaultWrite): Promise<void> {
    const body = new TextEncoder().encode(input.content);
    this.objects.set(input.vaultKey, {
      body,
      contentType: "text/plain; charset=utf-8",
      byteSize: body.byteLength,
    });
  }

  async deleteText(vaultKey: string): Promise<void> {
    this.objects.delete(vaultKey);
  }

  async putObject(input: VaultObjectWrite): Promise<void> {
    const body = new Uint8Array(input.body);
    this.objects.set(input.vaultKey, {
      body,
      contentType: input.contentType,
      byteSize: body.byteLength,
    });
  }

  async getObject(vaultKey: string): Promise<VaultObject | undefined> {
    const object = this.objects.get(vaultKey);
    if (!object || object.body instanceof ReadableStream) return object;
    return { ...object, body: new Uint8Array(object.body) };
  }

  async deleteObject(vaultKey: string): Promise<void> {
    this.objects.delete(vaultKey);
  }
}

class LocalSyntheticD1 implements D1DatabaseLike {
  constructor(private readonly repository: MemorySupervisionRepository) {}

  prepare(sql: string): D1PreparedStatementLike {
    let bindings: unknown[] = [];
    const rows = async (): Promise<Record<string, unknown>[]> => {
      if (sql.includes("FROM users") && sql.includes("LOWER(email) = ?")) {
        const [id, email] = bindings;
        return SYNTHETIC_USERS
          .filter((user) => (
            user.accountUserId === id
            && user.email === email
          ))
          .map((user) => ({
            id: user.accountUserId,
            tenant_id: user.tenantId,
            email: user.email,
            role: user.role,
          }));
      }
      if (sql.includes("FROM users")) {
        const [tenantId] = bindings;
        return SYNTHETIC_USERS
          .filter((user) => user.tenantId === tenantId && user.role === "employee")
          .map((user) => ({ id: user.accountUserId, email: user.email }));
      }
      if (sql.includes("FROM customers")) {
        const [tenantId] = bindings;
        const employees = SYNTHETIC_USERS.filter((user) => (
          user.tenantId === tenantId && user.role === "employee"
        ));
        const customers = (await Promise.all(employees.map((employee) => (
          this.repository.listEmployeeCustomers({
            tenantId: employee.tenantId,
            employeeUserId: employee.accountUserId,
          })
        )))).flat();
        return customers.map((customer) => ({
          id: customer.id,
          customer_number: customer.customerNumber,
          customer_alias: customer.customerAlias,
          employee_user_id: customer.employeeUserId,
          category: customer.category,
          planned_follow_up_at: customer.plannedFollowUpAt,
          status: customer.status,
        }));
      }
      if (sql.includes("FROM vault_assets")) return [];
      return [];
    };
    const statement: D1PreparedStatementLike = {
      bind: (...values) => {
        bindings = values;
        return statement;
      },
      first: async <T>() => (await rows())[0] as T | undefined ?? null,
      all: async <T>() => ({ results: await rows() as T[] }),
      run: async (): Promise<D1RunResult> => ({ success: true }),
    };
    return statement;
  }
}

function buildRuntime(): SupervisionApiRuntimeBindings {
  const repository = new MemorySupervisionRepository();
  return {
    d1: new LocalSyntheticD1(repository),
    repository,
    vault: new LocalSyntheticVault(),
    usageTokenSecret: "synthetic-usage-token-secret-32-bytes-minimum",
    idempotencyKeyring: [{
      version: "v1",
      secret: "synthetic-stable-idempotency-secret-32-bytes",
    }],
    retentionSchedulerSecret: "synthetic-retention-scheduler-secret-32-bytes",
  };
}

/**
 * Enables only the loopback Playwright fixture. Production builds and an
 * unacknowledged developer process both remain fail-closed.
 */
export function getLocalSyntheticSupervisionRuntime():
  | SupervisionApiRuntimeBindings
  | undefined {
  if (!isExplicitSyntheticLocalMode()) {
    return undefined;
  }
  const globalState = globalThis as typeof globalThis & Record<string, unknown>;
  const existing = globalState[GLOBAL_RUNTIME_KEY];
  if (existing) return existing as SupervisionApiRuntimeBindings;
  const runtime = buildRuntime();
  globalState[GLOBAL_RUNTIME_KEY] = runtime;
  return runtime;
}
