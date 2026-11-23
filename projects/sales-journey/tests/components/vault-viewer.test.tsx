import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { VaultViewer } from "@/components/supervision/vault-viewer";

describe("VaultViewer", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads raw material only through the audited owner endpoint after an intentional view", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("客户聊天原文", {
      status: 200,
      headers: { "content-type": "text/plain; charset=utf-8" },
    }));
    vi.stubGlobal("fetch", fetchMock);

    render(
      <VaultViewer
        assetId="asset-1"
        assetKind="chat_text"
        deleteAfter="2026-09-29T08:00:00.000Z"
      />,
    );

    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.queryByText("客户聊天原文")).toBeNull();
    expect(screen.getByText("原始资料将在 30 天后删除")).toBeInTheDocument();
    expect(screen.getByText(/2026/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下载已禁用" })).toBeDisabled();
    expect(document.querySelector("a[download]")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "授权并查看原始资料" }));

    expect(await screen.findByText("客户聊天原文")).toBeVisible();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/supervision/owner/vault/asset-1",
      expect.objectContaining({ cache: "no-store", credentials: "same-origin" }),
    );
  });

  it("keeps the viewer closed when the audited endpoint refuses access", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 403 })));
    render(
      <VaultViewer
        assetId="asset-2"
        assetKind="customer_identity"
        deleteAfter="2026-09-29T08:00:00.000Z"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "授权并查看原始资料" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("无法查看原始资料");
    expect(screen.queryByText("客户聊天原文")).toBeNull();
  });
});
