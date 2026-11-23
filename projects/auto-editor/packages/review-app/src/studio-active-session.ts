import { randomUUID } from "node:crypto";
import { mkdir, rename, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";

export type StudioActiveSession = Readonly<{
  schemaVersion: 1;
  token: string;
  actorId: string;
  customerId: string;
  customerLabel: string;
  sessionId: string;
  updatedAt: string;
}>;

const ACTIVE_SESSION_FILE = "active-session.json";

export async function writeStudioActiveSession(
  rootDir: string,
  session: Omit<StudioActiveSession, "schemaVersion" | "updatedAt">,
  now = new Date().toISOString(),
): Promise<void> {
  const value: StudioActiveSession = {
    schemaVersion: 1,
    ...session,
    updatedAt: now,
  };
  const path = join(rootDir, ACTIVE_SESSION_FILE);
  const temporary = `${path}.${randomUUID()}.tmp`;
  await mkdir(rootDir, { recursive: true, mode: 0o700 });
  try {
    await writeFile(temporary, `${JSON.stringify(value)}\n`, { mode: 0o600, flag: "wx" });
    await rename(temporary, path);
  } finally {
    await rm(temporary, { force: true });
  }
}
