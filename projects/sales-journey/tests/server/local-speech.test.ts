import { afterEach, describe, expect, it } from "vitest";

import {
  __resetVoiceDependenciesForTest,
  __setVoiceDependenciesForTest,
  synthesizeLocalPreview,
} from "@/server/voice/local-speech";

const temporaryDirectory = "/private/tmp/sales-voice-test";

describe("synthesizeLocalPreview", () => {
  afterEach(() => {
    __resetVoiceDependenciesForTest();
  });

  it("优先用可用的中文女声生成 m4a", async () => {
    const commands: Array<{ command: string; arguments: string[] }> = [];
    const removedDirectories: string[] = [];
    __setVoiceDependenciesForTest({
      execute: async (command, arguments_) => {
        commands.push({ command, arguments: arguments_ });
        if (command === "say" && arguments_[1] === "?") {
          return { stdout: "Tingting zh_CN\nMeijia zh_CN\n" };
        }
        return { stdout: "" };
      },
      makeTemporaryDirectory: async () => temporaryDirectory,
      readFile: async () => new Uint8Array([1, 2, 3]),
      removeDirectory: async (directory) => {
        removedDirectories.push(directory);
      },
    });

    const result = await synthesizeLocalPreview("您好，我先帮您整理资料。", "warm");

    expect(result).toMatchObject({
      contentType: "audio/mp4",
      fileName: "销售回复.m4a",
      downloadable: true,
    });
    expect(result.audio).toEqual(new Uint8Array([1, 2, 3]));
    expect(commands).toContainEqual({
      command: "say",
      arguments: [
        "-v",
        "Tingting",
        "-r",
        "185",
        "-o",
        `${temporaryDirectory}/preview.aiff`,
        "您好，我先帮您整理资料。",
      ],
    });
    expect(removedDirectories).toEqual([temporaryDirectory]);
  });

  it("转换失败仍返回可试听的 aiff", async () => {
    const readPaths: string[] = [];
    const removedDirectories: string[] = [];
    __setVoiceDependenciesForTest({
      execute: async (command, arguments_) => {
        if (command === "say" && arguments_[1] === "?") {
          return { stdout: "Meijia zh_CN\n" };
        }
        if (command === "afconvert") {
          throw new Error("conversion unavailable");
        }
        return { stdout: "" };
      },
      makeTemporaryDirectory: async () => temporaryDirectory,
      readFile: async (path) => {
        readPaths.push(path);
        return new Uint8Array([4, 5, 6]);
      },
      removeDirectory: async (directory) => {
        removedDirectories.push(directory);
      },
    });

    const result = await synthesizeLocalPreview("您好", "direct");

    expect(result).toMatchObject({
      contentType: "audio/aiff",
      fileName: "销售回复.aiff",
      downloadable: false,
      audio: new Uint8Array([4, 5, 6]),
    });
    expect(readPaths).toEqual([`${temporaryDirectory}/preview.aiff`]);
    expect(removedDirectories).toEqual([temporaryDirectory]);
  });

  it("没有允许的中文女声时返回结构化错误", async () => {
    __setVoiceDependenciesForTest({
      execute: async () => ({ stdout: "Samantha en_US\n" }),
    });

    await expect(synthesizeLocalPreview("您好", "warm")).rejects.toMatchObject({
      code: "CHINESE_FEMALE_VOICE_UNAVAILABLE",
    });
  });
});
