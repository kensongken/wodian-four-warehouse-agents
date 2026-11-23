export function createVoiceTranscriptBuffer(maxLength = 2000) {
  let finalText = "";

  function clamp(value) {
    return value.slice(0, maxLength);
  }

  return {
    appendFinal(fragment) {
      const cleaned = String(fragment || "").trim();
      if (!cleaned || finalText.length >= maxLength) return finalText;
      const separator = finalText && !/[，,。；;、\s]$/.test(finalText) ? "，" : "";
      finalText = clamp(`${finalText}${separator}${cleaned}`);
      return finalText;
    },
    preview(interim = "") {
      const cleaned = String(interim || "").trim();
      const separator = finalText && cleaned && !/[，,。；;、\s]$/.test(finalText) ? "，" : "";
      return clamp(`${finalText}${separator}${cleaned}`);
    },
    reset() {
      finalText = "";
    },
    get value() {
      return finalText;
    },
    get length() {
      return finalText.length;
    },
    get isFull() {
      return finalText.length >= maxLength;
    },
  };
}
