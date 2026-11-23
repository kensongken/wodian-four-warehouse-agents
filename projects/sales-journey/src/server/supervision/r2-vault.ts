import type {
  PrivateTextVaultWrite,
  VaultObject,
  VaultObjectWrite,
  VaultStorage,
} from "./vault";

type R2Body = string | ArrayBuffer | ArrayBufferView;

export interface R2BucketLike {
  put(
    key: string,
    body: R2Body,
    options?: { httpMetadata?: { contentType?: string } },
  ): Promise<unknown>;
  get(key: string): Promise<{
    body: ReadableStream<Uint8Array<ArrayBufferLike>> | Uint8Array<ArrayBufferLike>;
    size: number;
    httpMetadata?: { contentType?: string };
  } | null>;
  delete(key: string): Promise<unknown>;
}

/** Cloud object-store adapter. It deliberately exposes no public URL. */
export class R2VaultStorage implements VaultStorage {
  constructor(private readonly bucket: R2BucketLike) {}

  async putText(input: PrivateTextVaultWrite): Promise<void> {
    await this.bucket.put(input.vaultKey, input.content, {
      httpMetadata: { contentType: "text/plain; charset=utf-8" },
    });
  }

  async deleteText(vaultKey: string): Promise<void> {
    await this.bucket.delete(vaultKey);
  }

  async putObject(input: VaultObjectWrite): Promise<void> {
    await this.bucket.put(input.vaultKey, input.body, {
      httpMetadata: { contentType: input.contentType },
    });
  }

  async getObject(vaultKey: string): Promise<VaultObject | undefined> {
    const object = await this.bucket.get(vaultKey);
    if (!object) return undefined;
    return {
      body: object.body instanceof Uint8Array
        ? Uint8Array.from(object.body)
        : object.body as ReadableStream<Uint8Array<ArrayBuffer>>,
      contentType: object.httpMetadata?.contentType ?? "application/octet-stream",
      byteSize: object.size,
    };
  }

  async deleteObject(vaultKey: string): Promise<void> {
    await this.bucket.delete(vaultKey);
  }
}
