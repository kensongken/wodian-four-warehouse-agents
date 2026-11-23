"use client";

import { useEffect, useState } from "react";

import type { VaultAssetKind } from "@/domain/supervision/contracts";

type VaultViewerProps = Readonly<{
  assetId: string;
  assetKind: VaultAssetKind;
  deleteAfter: string;
}>;

type LoadedAsset =
  | Readonly<{ kind: "text"; content: string }>
  | Readonly<{ kind: "image"; objectUrl: string }>;

function expiryLabel(timestamp: string): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return timestamp;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function VaultViewer({ assetId, assetKind, deleteAfter }: VaultViewerProps) {
  const [status, setStatus] = useState<"idle" | "loading" | "loaded" | "failed">("idle");
  const [asset, setAsset] = useState<LoadedAsset>();

  useEffect(() => () => {
    if (asset?.kind === "image") URL.revokeObjectURL(asset.objectUrl);
  }, [asset]);

  async function authorizeAndLoad(): Promise<void> {
    if (status === "loading" || status === "loaded") return;
    setStatus("loading");
    try {
      const response = await fetch(
        `/api/supervision/owner/vault/${encodeURIComponent(assetId)}`,
        { cache: "no-store", credentials: "same-origin" },
      );
      if (!response.ok) throw new Error("vault access refused");
      const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
      if (contentType.startsWith("image/")) {
        const objectUrl = URL.createObjectURL(await response.blob());
        setAsset({ kind: "image", objectUrl });
      } else {
        setAsset({ kind: "text", content: await response.text() });
      }
      setStatus("loaded");
    } catch {
      setAsset(undefined);
      setStatus("failed");
    }
  }

  return (
    <section className="owner-panel vault-viewer">
      <div className="vault-retention-note">
        <strong>原始资料将在 30 天后删除</strong>
        <span>预计删除：{expiryLabel(deleteAfter)}</span>
      </div>
      <p className="privacy-note">每次打开都会重新校验老板身份并写入查看记录。</p>
      <div className="vault-actions">
        <button
          className="primary-action"
          type="button"
          disabled={status === "loading" || status === "loaded"}
          onClick={authorizeAndLoad}
        >
          {status === "loading" ? "正在校验身份…" : "授权并查看原始资料"}
        </button>
        <button type="button" disabled>下载已禁用</button>
      </div>
      {status === "failed" ? <p className="sync-failed" role="alert">无法查看原始资料，请确认权限或资料是否已到期。</p> : null}
      {asset?.kind === "text" ? <pre className="vault-text">{asset.content}</pre> : null}
      {asset?.kind === "image" ? (
        <img className="vault-image" src={asset.objectUrl} alt={assetKind === "product_image" ? "客户商品原始图片" : "原始资料"} />
      ) : null}
    </section>
  );
}
