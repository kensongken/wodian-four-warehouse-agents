import { spawn } from "node:child_process";
import { copyFile, mkdir, readdir, readFile, stat } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import type { JianyingAutomationResult, JianyingDesktopAutomation } from "@wearable/mobile-agent";

const VERIFIED_JIANYING_VERSIONS = new Set(["11.5.3", "11.6.0"]);
const JIANYING_BRIDGE_FOLDER = "内容工作台剪映桥";
const JIANYING_TIMELINE_COMMAND = "根据选中素材新建时间线";
const JIANYING_AX_MENU_HELPER = fileURLToPath(new URL("../scripts/jianying-ax-menu-click.swift", import.meta.url));
const FRONTMOST_APP_SCRIPT = String.raw`
tell application "System Events"
  set activeProcess to first application process whose frontmost is true
  try
    return bundle identifier of activeProcess
  on error
    return ""
  end try
end tell
`;
const RESTORE_FRONTMOST_APP_SCRIPT = String.raw`
on run argv
  if (count of argv) is 0 then return "skipped"
  tell application "System Events"
    try
      set visible of process "剪映专业版" to false
    end try
  end tell
  tell application id (item 1 of argv) to activate
  return "restored"
end run
`;

const JIANYING_CREATE_SCRIPT = String.raw`
on run
  try
    tell application id "com.lemon.lvpro" to activate
    delay 0.5
    tell application "System Events"
      tell process "剪映专业版"
        set frontmost to true
        -- A Jianying promotion or stale picker can sit above the editor and
        -- make the File menu unreachable. Escape only dismisses that transient
        -- surface; it does not change or delete the current draft.
        key code 53
        delay 0.4
        repeat 30 times
          try
            click menu bar item "文件" of menu bar 1
            delay 0.1
            click menu item "新建草稿" of menu 1 of menu bar item "文件" of menu bar 1
            return "started"
          end try
          delay 0.1
        end repeat
        return "create-failed"
      end tell
    end tell
  on error errorMessage
    if errorMessage contains "辅助" or errorMessage contains "not authorized" or errorMessage contains "not allowed" then return "accessibility-required"
    return "create-failed"
  end try
end run
`;

const JIANYING_CONFIRM_SCRIPT = String.raw`
ObjC.import('CoreGraphics');
function post(type, x, y) {
  const event = $.CGEventCreateMouseEvent(null, type, $.CGPointMake(x, y), $.kCGMouseButtonLeft);
  $.CGEventPost($.kCGHIDEventTap, event);
}
function findConfirmButton(processValue) {
  for (const windowValue of processValue.windows()) {
    try {
      const buttonValue = windowValue.buttons.byName('automationconfirmBtn');
      if (buttonValue.exists()) return buttonValue;
    } catch (_) {}
  }
  return null;
}
function run() {
  const processValue = Application('System Events').processes.byName('剪映专业版');
  let buttonValue = null;
  // A busy Jianying can show the confirmation several seconds after the File
  // menu action. Wait for the actual dialog instead of treating a late dialog
  // as "not required" and then failing at the import step.
  for (let attempt = 0; attempt < 300 && !buttonValue; attempt += 1) {
    buttonValue = findConfirmButton(processValue);
    if (!buttonValue) delay(0.1);
  }
  if (!buttonValue) return 'confirm-failed';
  processValue.frontmost = true;
  delay(0.2);
  const position = buttonValue.position();
  const size = buttonValue.size();
  const x = Number(position[0]) + Number(size[0]) / 2;
  const y = Number(position[1]) + Number(size[1]) / 2;
  post($.kCGEventMouseMoved, x, y); delay(0.1);
  post($.kCGEventLeftMouseDown, x, y); delay(0.05);
  post($.kCGEventLeftMouseUp, x, y);
  for (let attempt = 0; attempt < 60; attempt += 1) {
    if (!findConfirmButton(processValue)) return 'confirmed';
    delay(0.1);
  }
  return 'confirm-failed';
}
`;

const JIANYING_IMPORT_SCRIPT = String.raw`
on run argv
  set mediaPath to item 1 of argv
  set currentStage to "open-import"
  try
    tell application id "com.lemon.lvpro" to activate
    delay 1.5
    tell application "System Events"
      tell process "剪映专业版"
        set frontmost to true
        click menu bar item "文件" of menu bar 1
        delay 0.1
        click menu item "导入" of menu 1 of menu bar item "文件" of menu bar 1
        set currentStage to "wait-import-panel"
        set importPanelReady to false
        repeat 40 times
          try
            if exists sheet 1 of window "剪映专业版" then
              set importPanelReady to true
              exit repeat
            end if
          end try
          delay 0.1
        end repeat
        if not importPanelReady then error "import-panel-not-found"
        set currentStage to "locate-file"
        set openPanel to sheet 1 of window "剪映专业版"
        -- Finder remembers the last folder, but relying on sidebar positions is
        -- fragile. Go to the exact staged file path so Jianying UI reorderings
        -- do not send us to the wrong place.
        set frontmost to true
        delay 0.2
        keystroke "g" using {command down, shift down}
        set goToReady to false
        set goToWindow to missing value
        repeat 30 times
          try
            -- macOS exposes the Go To Path dialog to System Events as a
            -- sheet nested inside the open panel (Cocoa AX reports it as its
            -- own GoToWindow). Target that real hierarchy directly.
            set goToWindow to sheet 1 of openPanel
            if exists text field 1 of goToWindow then
              set goToReady to true
              exit repeat
            end if
          end try
          delay 0.1
        end repeat
        if not goToReady then error "go-to-field-not-found"
        set pathField to text field 1 of goToWindow
        set focused of pathField to true
        set value of pathField to mediaPath
        key code 36
        set fileReady to false
        repeat 30 times
          try
            if not (exists sheet 1 of openPanel) then
              set fileReady to true
              exit repeat
            end if
          end try
          delay 0.1
        end repeat
        if not fileReady then error "go-to-confirm-failed"
        set currentStage to "choose-file"
        -- The selected file makes Import the default action. A second Return
        -- is more stable than walking the deeply nested native button tree.
        key code 36
        set currentStage to "import-file"
        set importFinished to false
        repeat 50 times
          try
            if not (exists sheet 1 of window "剪映专业版") then
              set importFinished to true
              exit repeat
            end if
          end try
          delay 0.1
        end repeat
        if not importFinished then error "import-panel-did-not-close"
      end tell
    end tell
    delay 2
    return "{\"code\":\"timeline-step-required\",\"projectCreated\":true,\"materialImported\":true}"
  on error errorMessage
    if errorMessage contains "辅助" or errorMessage contains "not authorized" or errorMessage contains "not allowed" then
      return "{\"code\":\"accessibility-required\",\"projectCreated\":false,\"materialImported\":false}"
    end if
    return "{\"code\":\"automation-failed\",\"projectCreated\":true,\"materialImported\":false,\"stage\":\"" & currentStage & "\"}"
  end try
end run
`;

const JIANYING_TIMELINE_SCRIPT = String.raw`
ObjC.import('CoreGraphics');
function text(value) { try { return String(value()); } catch (_) { return ''; } }
function walk(element, visit, depth) {
  if (!element || depth > 12) return null;
  if (visit(element)) return element;
  let children = [];
  try { children = element.uiElements(); } catch (_) { return null; }
  for (const child of children) {
    const found = walk(child, visit, depth + 1);
    if (found) return found;
  }
  return null;
}
function post(type, x, y) {
  const event = $.CGEventCreateMouseEvent(null, type, $.CGPointMake(x, y), $.kCGMouseButtonLeft);
  $.CGEventPost($.kCGHIDEventTap, event);
}
function run() {
  const processValue = Application('System Events').processes.byName('剪映专业版');
  let editorWindow = null;
  for (const windowValue of processValue.windows()) {
    if (text(windowValue.subrole) === 'AXStandardWindow') { editorWindow = windowValue; break; }
  }
  if (!editorWindow) return 'timeline-not-found';
  let card = null;
  // In Jianying 11.6 the imported media card is a top-level AXButton on the
  // editor window. Avoid a full recursive scan, which can exceed the timeout.
  for (const button of editorWindow.buttons()) {
    if (text(button.name).includes('.parent.mp4.apply')) { card = button; break; }
  }
  if (!card) return 'material-card-not-found';
  const cardPosition = card.position();
  const cardSize = card.size();
  const startX = Number(cardPosition[0]) + Number(cardSize[0]) / 2;
  const startY = Number(cardPosition[1]) + Number(cardSize[1]) / 2;
  post($.kCGEventMouseMoved, startX, startY, $.kCGMouseButtonLeft); delay(0.2);
  post($.kCGEventRightMouseDown, startX, startY, $.kCGMouseButtonRight); delay(0.08);
  post($.kCGEventRightMouseUp, startX, startY, $.kCGMouseButtonRight); delay(0.5);
  // System Events can open this menu reliably, but Jianying 11.6 does not
  // expose the menu items back through its scripting bridge. A narrowly
  // scoped AX helper performs the exact-title press in the next step.
  return 'timeline-menu-opened';
}
`;

type ScriptReceipt = Readonly<{
  code: "accessibility-required" | "ui-version-unsupported" | "automation-failed" | "timeline-check-required" | "timeline-step-required";
  projectCreated: boolean;
  materialImported: boolean;
  stage?: string;
}>;

export const automateJianyingOnMac: JianyingDesktopAutomation = async (input) => {
  if (process.platform !== "darwin" || !isVerifiedJianyingVersion(input.app.appVersion)) {
    return failure("jianying-ui-version-unsupported", "version-check");
  }
  const startedAt = Date.now();
  const stagedMediaPath = await stageJianyingMedia(input.mediaPath);
  if (!stagedMediaPath) return failure("jianying-automation-failed");
  const scriptReceipt = await runAutomationScript(stagedMediaPath);
  if (!scriptReceipt) return failure("jianying-automation-failed");
  const timelineImported = scriptReceipt.materialImported
    ? await waitForTimeline(input.app.draftRoot, startedAt)
    : false;
  if (timelineImported) {
    return {
      attempted: true,
      projectCreated: true,
      materialImported: true,
      timelineImported: true,
      error: null,
      stage: "complete",
    };
  }
  if (scriptReceipt.code === "accessibility-required") {
    return failure("jianying-accessibility-permission-required");
  }
  if (scriptReceipt.code === "ui-version-unsupported") {
    return {
      ...failure("jianying-ui-version-unsupported"),
      projectCreated: scriptReceipt.projectCreated,
      stage: scriptReceipt.stage ?? "version-check",
    };
  }
  if (scriptReceipt.materialImported) {
    return {
      attempted: true,
      projectCreated: scriptReceipt.projectCreated,
      materialImported: true,
      timelineImported: false,
      error: "jianying-timeline-step-required",
      stage: scriptReceipt.stage ?? "timeline",
    };
  }
  return {
    ...failure("jianying-automation-failed", scriptReceipt.stage ?? "unknown"),
    projectCreated: scriptReceipt.projectCreated,
  };
};

export function isVerifiedJianyingVersion(appVersion: string): boolean {
  return VERIFIED_JIANYING_VERSIONS.has(appVersion);
}

function failure(
  error: Exclude<JianyingAutomationResult["error"], "jianying-timeline-step-required" | null>,
  stage: string | null = null,
): JianyingAutomationResult {
  return {
    attempted: true,
    projectCreated: false,
    materialImported: false,
    timelineImported: false,
    error,
    stage,
  };
}

async function runAutomationScript(mediaPath: string): Promise<ScriptReceipt | null> {
  const previousBundleId = await runOsaScript(["-e", FRONTMOST_APP_SCRIPT], 5_000);
  try {
    return await runForegroundAutomationScript(mediaPath);
  } finally {
    if (previousBundleId && previousBundleId !== "com.lemon.lvpro") {
      await runOsaScript(["-e", RESTORE_FRONTMOST_APP_SCRIPT, "--", previousBundleId], 5_000);
    }
  }
}

async function runForegroundAutomationScript(mediaPath: string): Promise<ScriptReceipt | null> {
  const created = await runOsaScript(["-e", JIANYING_CREATE_SCRIPT], 20_000);
  if (!created) return null;
  if (created === "accessibility-required") {
    return { code: "accessibility-required", projectCreated: false, materialImported: false };
  }
  if (created !== "started") {
    return { code: "automation-failed", projectCreated: false, materialImported: false, stage: "create-project" };
  }
  const confirmed = await runOsaScript(["-l", "JavaScript", "-e", JIANYING_CONFIRM_SCRIPT], 45_000);
  if (!confirmed || confirmed === "confirm-failed") {
    return { code: "automation-failed", projectCreated: false, materialImported: false, stage: "confirm-project" };
  }
  const imported = await runOsaScript(["-e", JIANYING_IMPORT_SCRIPT, "--", mediaPath, JIANYING_BRIDGE_FOLDER], 40_000);
  if (!imported) return null;
  let receipt: ScriptReceipt;
  try {
    receipt = JSON.parse(imported) as ScriptReceipt;
  } catch {
    process.stderr.write(`[jianying-automation] import script: ${imported.replaceAll(/\s+/g, " ").slice(0, 500)}\n`);
    return null;
  }
  if (!receipt.materialImported && receipt.stage) {
    process.stderr.write(`[jianying-automation] stopped at ${receipt.stage}\n`);
  }
  if (!receipt.materialImported) return receipt;
  const timelineMenu = await runOsaScript(["-l", "JavaScript", "-e", JIANYING_TIMELINE_SCRIPT], 15_000);
  const timelineCommand = timelineMenu === "timeline-menu-opened"
    ? await runProcess(
      "/usr/bin/xcrun",
      ["swift", JIANYING_AX_MENU_HELPER, JIANYING_TIMELINE_COMMAND],
      20_000,
    )
    : timelineMenu;
  if (timelineCommand !== "timeline-created") {
    process.stderr.write(`[jianying-automation] ${timelineCommand ?? "timeline-command-failed"}\n`);
  }
  return { ...receipt, code: "timeline-check-required" };
}

async function stageJianyingMedia(mediaPath: string): Promise<string | null> {
  try {
    const bridgeRoot = join(homedir(), "Documents", JIANYING_BRIDGE_FOLDER);
    const stagedMediaPath = join(bridgeRoot, "parent.mp4");
    await mkdir(bridgeRoot, { recursive: true });
    await copyFile(mediaPath, stagedMediaPath);
    return stagedMediaPath;
  } catch {
    return null;
  }
}

async function runOsaScript(args: string[], timeoutMs: number): Promise<string | null> {
  return runProcess("/usr/bin/osascript", args, timeoutMs);
}

async function runProcess(executable: string, args: string[], timeoutMs: number): Promise<string | null> {
  return new Promise((resolvePromise) => {
    const child = spawn(executable, args, {
      stdio: ["ignore", "pipe", "pipe"],
    });
    const chunks: Buffer[] = [];
    const errorChunks: Buffer[] = [];
    let settled = false;
    const finish = (value: string | null) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolvePromise(value);
    };
    child.stdout.on("data", (chunk: Buffer) => {
      if (Buffer.concat(chunks).length < 16_384) chunks.push(chunk);
    });
    child.stderr.on("data", (chunk: Buffer) => {
      if (Buffer.concat(errorChunks).length < 4_096) errorChunks.push(chunk);
    });
    child.once("error", () => finish(null));
    child.once("close", (code) => {
      if (code !== 0) {
        const diagnostic = Buffer.concat(errorChunks).toString("utf8").trim().replaceAll(/\s+/g, " ").slice(0, 500);
        if (diagnostic) process.stderr.write(`[jianying-automation] ${executable.split("/").at(-1)}: ${diagnostic}\n`);
        return finish(null);
      }
      finish(Buffer.concat(chunks).toString("utf8").trim());
    });
    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      finish(null);
    }, timeoutMs);
  });
}

async function waitForTimeline(draftRoot: string, startedAt: number): Promise<boolean> {
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const projectPath = await newestProjectPath(draftRoot, startedAt);
    if (projectPath && await projectHasTimeline(projectPath)) return true;
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 500));
  }
  return false;
}

async function newestProjectPath(draftRoot: string, startedAt: number): Promise<string | null> {
  try {
    const entries = await readdir(draftRoot, { withFileTypes: true });
    const candidates = await Promise.all(entries.filter((entry) => entry.isDirectory()).map(async (entry) => {
      const path = join(draftRoot, entry.name);
      const info = await stat(path);
      return { path, modifiedAt: info.mtimeMs };
    }));
    return candidates
      .filter((candidate) => candidate.modifiedAt >= startedAt - 2_000)
      .sort((left, right) => right.modifiedAt - left.modifiedAt)[0]?.path ?? null;
  } catch {
    return null;
  }
}

async function projectHasTimeline(projectPath: string): Promise<boolean> {
  try {
    const value = JSON.parse(await readFile(join(projectPath, "Timelines", "project.json"), "utf8")) as {
      timelines?: unknown[];
    };
    return (value.timelines?.length ?? 0) > 1;
  } catch {
    return false;
  }
}
