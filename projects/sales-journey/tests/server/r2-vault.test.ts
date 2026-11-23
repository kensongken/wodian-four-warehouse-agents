import { R2VaultStorage, type R2BucketLike } from "@/server/supervision/r2-vault";

describe("R2VaultStorage", () => {
  it("keeps text and images private behind opaque keys", async () => {
    const objects = new Map<string, { bytes: Uint8Array; contentType?: string }>();
    const bucket: R2BucketLike = {
      put: vi.fn(async (key, body, options) => {
        objects.set(key, {
          bytes: typeof body === "string" ? new TextEncoder().encode(body) : new Uint8Array(body),
          contentType: options?.httpMetadata?.contentType,
        });
      }),
      get: vi.fn(async (key) => {
        const object = objects.get(key);
        if (!object) return null;
        return {
          body: object.bytes,
          size: object.bytes.byteLength,
          httpMetadata: { contentType: object.contentType },
        };
      }),
      delete: vi.fn(async (key) => { objects.delete(key); }),
    };
    const vault = new R2VaultStorage(bucket);

    await vault.putText({
      tenantId: "tenant-1",
      customerId: "customer-1",
      uploadedByUserId: "employee-1",
      kind: "chat_text",
      vaultKey: "opaque/chat",
      content: "合成聊天记录",
      deleteAfter: "2026-09-29T00:00:00.000Z",
    });
    await vault.putObject({
      vaultKey: "opaque/image",
      body: new Uint8Array([1, 2, 3]),
      contentType: "image/png",
    });

    await expect(vault.getObject("opaque/chat")).resolves.toMatchObject({
      contentType: "text/plain; charset=utf-8",
      byteSize: 18,
    });
    await vault.deleteText("opaque/chat");
    await vault.deleteObject("opaque/image");
    expect(objects.size).toBe(0);
  });
});
