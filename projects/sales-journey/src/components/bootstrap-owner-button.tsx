"use client";

import { useState } from "react";

export function BootstrapOwnerButton() {
  const [state, setState] = useState<"idle" | "saving" | "error">("idle");

  async function initialize(): Promise<void> {
    setState("saving");
    try {
      const response = await fetch("/api/internal/bootstrap-owner", {
        method: "POST",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("owner initialization failed");
      window.location.assign("/owner");
    } catch {
      setState("error");
    }
  }

  return (
    <div>
      <button type="button" disabled={state === "saving"} onClick={initialize}>
        {state === "saving" ? "正在初始化…" : "初始化并进入老板后台"}
      </button>
      {state === "error" ? <p role="alert">初始化失败，请刷新后重试。</p> : null}
    </div>
  );
}
