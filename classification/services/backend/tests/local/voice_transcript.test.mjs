import assert from "node:assert/strict";

import { createVoiceTranscriptBuffer } from "../../src/product_engine/api/voice_transcript.mjs";

{
  const buffer = createVoiceTranscriptBuffer(2000);
  for (let index = 0; index < 80; index += 1) {
    buffer.appendFinal("卖点增加颜色醒目");
  }

  assert.ok(buffer.length >= 500, `expected at least 500 chars, got ${buffer.length}`);
  assert.ok(buffer.length <= 2000);
  assert.equal(buffer.preview("材质改成绿松").endsWith("材质改成绿松"), true);
  assert.equal(buffer.value.endsWith("材质改成绿松"), false);
}

{
  const buffer = createVoiceTranscriptBuffer(2000);
  buffer.appendFinal("长".repeat(2100));

  assert.equal(buffer.length, 2000);
  assert.equal(buffer.isFull, true);
  buffer.reset();
  assert.equal(buffer.value, "");
  assert.equal(buffer.length, 0);
  assert.equal(buffer.isFull, false);
}

console.log("voice transcript tests passed");
