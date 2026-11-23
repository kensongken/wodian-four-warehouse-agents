import { headers } from "next/headers";
import { notFound } from "next/navigation";

import { VaultViewer } from "@/components/supervision/vault-viewer";
import { requireOwnerFromD1 } from "@/server/auth/authorization";
import { getSupervisionApiRuntime } from "@/server/supervision/customer-service";
import { getOwnerVaultAssetView } from "@/server/supervision/owner-view";

export const dynamic = "force-dynamic";

type OwnerVaultPageProps = Readonly<{
  params: Promise<Readonly<{ assetId: string }>>;
}>;

export default async function OwnerVaultPage({ params }: OwnerVaultPageProps) {
  const runtime = getSupervisionApiRuntime();
  const actor = await requireOwnerFromD1(await headers(), runtime.d1);
  const { assetId } = await params;
  const now = runtime.now?.() ?? new Date().toISOString();
  const asset = await getOwnerVaultAssetView(
    runtime.d1,
    actor.tenantId,
    assetId,
    now,
  );
  if (!asset) notFound();

  return (
    <main className="app-shell owner-workspace">
      <header className="app-header owner-page-header">
        <div>
          <p className="eyebrow">老板专用 · 每次查看留痕</p>
          <h1>原始资料保险库</h1>
          <p>页面源码不携带原始内容，点击查看后才走授权与审计接口。</p>
        </div>
        <a href="/owner">返回老板看板</a>
      </header>
      <VaultViewer
        assetId={asset.id}
        assetKind={asset.kind}
        deleteAfter={asset.deleteAfter}
      />
    </main>
  );
}
