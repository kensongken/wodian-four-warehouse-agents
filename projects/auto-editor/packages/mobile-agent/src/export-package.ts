import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";

import {
  ExportDeliverySchema,
  ExportPackageSchema,
  type ExportDelivery,
  type ExportPackage,
  type VideoVariant,
} from "@wearable/contracts";

import type { ContentLibrary } from "./content-library";

export interface PhonePublisherAdapter {
  deliver(input: Readonly<{
    idempotencyKey: string;
    variant: VideoVariant;
    videoPath: string;
    title: string;
    caption: string;
    coverPath: string;
    stillPaths: string[];
  }>): Promise<Readonly<{ receiptId: string; receivedAt: string }>>;
}

export async function createExportPackage(
  input: Readonly<{
    approvalId: string;
    artifactId: string;
    revisionId: string;
    creativeSessionId?: string;
  }>,
  library: ContentLibrary,
): Promise<ExportPackage> {
  const approval = (await library.listApprovals()).find(
    (item) => item.id === input.approvalId,
  );
  if (!approval || approval.status !== "active") {
    throw new Error("active-approval-required");
  }
  if (approval.artifactId !== input.artifactId) {
    throw new Error("approval-artifact-mismatch");
  }
  if (approval.revisionId !== input.revisionId) {
    throw new Error("approval-revision-mismatch");
  }
  await library.validateRevisionTimelines(approval.revisionId, { ...approval, creativeSessionId: input.creativeSessionId });
  const artifact = (await library.listArtifacts(approval.revisionId)).find(
    (item) => item.id === input.artifactId,
  );
  if (
    !artifact ||
    artifact.status !== "active" ||
    artifact.kind !== approval.variant
  ) {
    throw new Error("active-approved-artifact-required");
  }
  if (
    approval.artifactDigest !== artifact.digest ||
    approval.coverDigest !== artifact.coverDigest ||
    approval.renderInputDigest !== artifact.renderInputDigest ||
    JSON.stringify(approval.stillDigests) !== JSON.stringify(artifact.stillDigests)
  ) {
    throw new Error("approved-artifact-digest-mismatch");
  }
  if (!artifact.coverPath || !artifact.stillPaths) {
    throw new Error("approved-visual-artifacts-required");
  }
  if (
    (await digestFile(artifact.videoPath)) !== approval.artifactDigest ||
    (await digestFile(artifact.coverPath)) !== approval.coverDigest
  ) throw new Error("approved-artifact-bytes-changed");
  for (const [index, path] of artifact.stillPaths.entries()) {
    if ((await digestFile(path)) !== approval.stillDigests[index]) {
      throw new Error("approved-artifact-bytes-changed");
    }
  }
  const revision = (await library.listRevisions(approval.storyId)).find(
    (item) =>
      item.id === approval.revisionId && item.revision === approval.revision,
  );
  if (!revision) throw new Error("approved-revision-not-found");
  const variantPlan = revision.variants.find(
    (variant) => variant.kind === approval.variant,
  );
  if (!variantPlan) throw new Error("approved-variant-plan-not-found");

  const idempotencyKey = `${approval.id}:${artifact.digest}`;
  const id = `export_${approval.id}_${artifact.digest.slice(0, 12)}`;
  const value = ExportPackageSchema.parse({
    id,
    approvalId: approval.id,
    artifactId: artifact.id,
    variant: artifact.kind,
    videoPath: artifact.videoPath,
    videoDigest: artifact.digest,
    title: variantPlan.title,
    caption: variantPlan.caption,
    coverPath: artifact.coverPath,
    coverDigest: artifact.coverDigest,
    stillPaths: artifact.stillPaths,
    stillDigests: artifact.stillDigests,
    renderInputDigest: artifact.renderInputDigest,
    idempotencyKey,
    status: "pending",
    createdAt: new Date().toISOString(),
  });
  return library.commitExportPackage(value, input.creativeSessionId);
}

async function digestFile(path: string): Promise<string> {
  const hash = createHash("sha256");
  await new Promise<void>((resolve, reject) => {
    const stream = createReadStream(path);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("error", reject);
    stream.on("end", resolve);
  });
  return hash.digest("hex");
}

export async function deliverExportPackage(
  packageId: string,
  adapter: PhonePublisherAdapter,
  library: ContentLibrary,
  creativeSessionId?: string,
): Promise<ExportDelivery> {
  const value = (await library.listExportPackages()).find(
    (item) => item.id === packageId,
  );
  if (!value) throw new Error(`export-package-not-found:${packageId}`);
  if (value.status === "invalidated") {
    throw new Error("active-export-package-required");
  }
  const approval = (await library.listApprovals()).find((item) => item.id === value.approvalId);
  if (approval) await library.validateRevisionTimelines(approval.revisionId, { ...approval, creativeSessionId });
  const existingReceipt = (await library.listDeliveries()).find(
    (delivery) =>
      delivery.packageId === packageId && delivery.status === "received",
  );
  if (
    (await digestFile(value.videoPath)) !== value.videoDigest ||
    (await digestFile(value.coverPath)) !== value.coverDigest
  ) throw new Error("export-package-bytes-changed");
  for (const [index, path] of value.stillPaths.entries()) {
    if ((await digestFile(path)) !== value.stillDigests[index]) {
      throw new Error("export-package-bytes-changed");
    }
  }
  await library.validateExportPackageForDelivery(packageId, creativeSessionId);
  if (existingReceipt) return existingReceipt;

  let rawReceipt: Readonly<{ receiptId: string; receivedAt: string }>;
  try {
    rawReceipt = await adapter.deliver({
      idempotencyKey: value.idempotencyKey,
      variant: value.variant,
      videoPath: value.videoPath,
      title: value.title,
      caption: value.caption,
      coverPath: value.coverPath,
      stillPaths: value.stillPaths,
    });
  } catch {
    const delivery = ExportDeliverySchema.parse({
      packageId,
      status: "pending-retry",
      receiptId: null,
      receivedAt: null,
      publishedAt: null,
      publishedClaimed: false,
    });
    return library.commitExportDelivery({
      packageId,
      expectedPackage: value,
      delivery,
      packageStatus: "pending-retry",
      creativeSessionId,
    });
  }
  let delivery: ExportDelivery;
  try {
    delivery = ExportDeliverySchema.parse({
      packageId,
      status: "received",
      receiptId: rawReceipt.receiptId,
      receivedAt: rawReceipt.receivedAt,
      publishedAt: null,
      publishedClaimed: false,
    });
  } catch {
    throw new Error("phone-receipt-invalid");
  }
  return library.commitExportDelivery({
    packageId,
    expectedPackage: value,
    delivery,
    packageStatus: "delivered",
    creativeSessionId,
  });
}
