import type {
  PrivateTextVaultWrite,
  VaultObject,
  VaultObjectWrite,
  VaultStorage,
} from "@/server/supervision/vault";

export class TestVaultStorage implements VaultStorage {
  private readonly objects = new Map<string, VaultObject>();
  readonly deletedKeys: string[] = [];
  readonly readKeys: string[] = [];

  async putText(input: PrivateTextVaultWrite): Promise<void> {
    await this.putObject({
      vaultKey: input.vaultKey,
      body: new TextEncoder().encode(input.content),
      contentType: "text/plain; charset=utf-8",
    });
  }

  async deleteText(vaultKey: string): Promise<void> {
    await this.deleteObject(vaultKey);
  }

  async putObject(input: VaultObjectWrite): Promise<void> {
    this.objects.set(input.vaultKey, {
      body: new Uint8Array(input.body),
      contentType: input.contentType,
      byteSize: input.body.byteLength,
    });
  }

  async getObject(vaultKey: string): Promise<VaultObject | undefined> {
    this.readKeys.push(vaultKey);
    const object = this.objects.get(vaultKey);
    if (!object) return undefined;
    return object.body instanceof ReadableStream
      ? object
      : { ...object, body: new Uint8Array(object.body) };
  }

  async deleteObject(vaultKey: string): Promise<void> {
    this.deletedKeys.push(vaultKey);
    this.objects.delete(vaultKey);
  }

  keys(): string[] {
    return [...this.objects.keys()];
  }
}
