export async function checkMicrophoneAccess({
  mediaDevices,
  speechRecognitionAvailable,
}) {
  if (!mediaDevices || typeof mediaDevices.getUserMedia !== "function") {
    return {
      state: "microphone_unsupported",
      message: "当前浏览器无法调用麦克风。请使用文字指令，或改用 Chrome 打开本页。",
    };
  }

  try {
    const stream = await mediaDevices.getUserMedia({ audio: true });
    for (const track of stream.getTracks()) track.stop();
  } catch (error) {
    if (error?.name === "NotAllowedError" || error?.name === "SecurityError") {
      return {
        state: "permission_denied",
        message: "麦克风被浏览器或系统拦截。请允许 Codex 使用麦克风，然后刷新本页。",
      };
    }
    if (error?.name === "NotFoundError" || error?.name === "DevicesNotFoundError") {
      return {
        state: "device_missing",
        message: "没有找到可用的麦克风。请连接或启用麦克风后重试。",
      };
    }
    return {
      state: "microphone_error",
      message: "暂时无法打开麦克风。请刷新页面后重试，或使用文字指令。",
    };
  }

  if (!speechRecognitionAvailable) {
    return {
      state: "speech_unsupported",
      message: "麦克风权限已允许，但当前浏览器不支持语音识别。请使用文字指令，或改用 Chrome 打开本页。",
    };
  }

  return {
    state: "ready",
    message: "麦克风已允许，可以开始语音。",
  };
}

export function describeRecognitionError(errorCode) {
  if (errorCode === "not-allowed" || errorCode === "service-not-allowed") {
    return {
      state: "permission_denied",
      message: "语音识别服务没有获得权限。请允许 Codex 使用麦克风，然后刷新本页。",
    };
  }
  if (errorCode === "network") {
    return {
      state: "service_unavailable",
      message: "麦克风已打开，但语音识别服务当前不可用。请使用文字指令，或改用 Chrome 重试。",
    };
  }
  if (errorCode === "no-speech") {
    return {
      state: "no_speech",
      message: "没有听到清晰语音，请靠近麦克风后重试。",
    };
  }
  if (errorCode === "audio-capture") {
    return {
      state: "audio_capture_failed",
      message: "浏览器无法读取麦克风声音。请检查系统输入设备后重试。",
    };
  }
  return {
    state: "recognition_failed",
    message: "语音识别没有完成，请重新说一次或使用文字指令。",
  };
}
