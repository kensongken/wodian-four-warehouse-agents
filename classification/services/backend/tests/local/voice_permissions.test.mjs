import assert from "node:assert/strict";

import {
  checkMicrophoneAccess,
  describeRecognitionError,
} from "../../src/product_engine/api/voice_permissions.mjs";

function fakeStream(stopped) {
  return {
    getTracks() {
      return [{ stop() { stopped.count += 1; } }];
    },
  };
}

{
  const stopped = { count: 0 };
  const result = await checkMicrophoneAccess({
    mediaDevices: {
      async getUserMedia(constraints) {
        assert.deepEqual(constraints, { audio: true });
        return fakeStream(stopped);
      },
    },
    speechRecognitionAvailable: true,
  });

  assert.deepEqual(result, {
    state: "ready",
    message: "麦克风已允许，可以开始语音。",
  });
  assert.equal(stopped.count, 1);
}

{
  const stopped = { count: 0 };
  const result = await checkMicrophoneAccess({
    mediaDevices: {
      async getUserMedia() {
        return fakeStream(stopped);
      },
    },
    speechRecognitionAvailable: false,
  });

  assert.deepEqual(result, {
    state: "speech_unsupported",
    message: "麦克风权限已允许，但当前浏览器不支持语音识别。请使用文字指令，或改用 Chrome 打开本页。",
  });
  assert.equal(stopped.count, 1);
}

{
  const error = new Error("blocked");
  error.name = "NotAllowedError";
  const result = await checkMicrophoneAccess({
    mediaDevices: {
      async getUserMedia() {
        throw error;
      },
    },
    speechRecognitionAvailable: true,
  });

  assert.deepEqual(result, {
    state: "permission_denied",
    message: "麦克风被浏览器或系统拦截。请允许 Codex 使用麦克风，然后刷新本页。",
  });
}

assert.deepEqual(describeRecognitionError("not-allowed"), {
  state: "permission_denied",
  message: "语音识别服务没有获得权限。请允许 Codex 使用麦克风，然后刷新本页。",
});
assert.deepEqual(describeRecognitionError("network"), {
  state: "service_unavailable",
  message: "麦克风已打开，但语音识别服务当前不可用。请使用文字指令，或改用 Chrome 重试。",
});
assert.deepEqual(describeRecognitionError("no-speech"), {
  state: "no_speech",
  message: "没有听到清晰语音，请靠近麦克风后重试。",
});

console.log("voice permission tests passed");
