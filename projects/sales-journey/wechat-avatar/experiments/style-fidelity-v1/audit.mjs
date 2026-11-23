import {readFileSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
import {MODEL, OPTIONS, requestFor, verifyDesign, parseReply} from './design.mjs';
const root = resolve(import.meta.dirname, '../..');
const out = resolve(process.argv[2]);
const raw = JSON.parse(readFileSync(join(out, 'results.json')));
const fixture = JSON.parse(readFileSync(new URL('./fixture.json', import.meta.url)));
const hash = value => createHash('sha256').update(value).digest('hex');
assert.equal(raw.status, 'completed_not_business_acceptance');
assert.deepEqual(raw.schedule, verifyDesign(fixture));
assert.deepEqual(raw.rows.map(x => x.id), raw.schedule.map(x => x.id));
let calls = 0;
for (const row of raw.rows) {
  const item = fixture.cases.find(x => x.id === row.caseId);
  assert.equal(row.referenceContent, item.content);
  assert.equal(row.contentHash, hash(item.content));
  assert.deepEqual(row.mustRetain, item.mustRetain);
  assert.deepEqual(row.forbiddenAdds, item.forbiddenAdds);
  assert.deepEqual(row.customerStatements, item.statements);
  if (row.arm === 'direct') {
    assert.equal(row.reply, item.content); assert.equal(row.status, 'generated');
    assert.equal(row.modelCalls, 0); assert.equal(row.trace.length, 0);
    continue;
  }
  assert.equal(row.trace.length, 1);
  const phase = row.trace[0];
  assert.deepEqual(phase.request, requestFor(fixture, item));
  assert.equal(row.modelCalls, phase.transport ? 1 : 0);
  if (phase.transport) {
    calls++;
    assert.equal(phase.transport.endpoint, 'http://127.0.0.1:11434/api/chat');
    assert.equal(phase.transport.body.model, MODEL);
    assert.deepEqual(phase.transport.body.options, {...OPTIONS, seed: row.seed});
    assert.deepEqual(phase.transport.body.messages, phase.request.messages);
    assert.equal(phase.transport.body.format, 'json');
    assert.equal(phase.transport.body.keep_alive, '120s');
  }
  if (row.status === 'generated') assert.equal(parseReply(phase.response, item).validate(), row.reply);
}
for (const [f, h] of Object.entries(raw.baselineHashes)) assert.equal(hash(readFileSync(join(root, f))), h, f);
for (const [f, h] of Object.entries(raw.experimentHashes)) assert.equal(hash(readFileSync(new URL('./' + f, import.meta.url))), h, f);
assert.equal(raw.originalAccountRead, false); assert.equal(raw.realWechatCalls, 0);
const result = {
  status: 'integrity_passed_not_business_acceptance', plannedOutputs: 48,
  directCopiesVerified: 24, matchedFrozenSchedule: true, frozenReferencesVerified: true,
  actualParametersVerified: true, recordedModelCalls: calls,
  baselineFilesUnchanged: Object.keys(raw.baselineHashes).length,
  experimentFilesUnchanged: Object.keys(raw.experimentHashes).length,
  rawResultsSha256: hash(readFileSync(join(out, 'results.json'))),
  originalAccountRead: false, realWechatCalls: 0,
};
writeFileSync(join(out, 'integrity.json'), JSON.stringify(result, null, 2));
console.log(JSON.stringify(result));
