import {readFileSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
import {MODEL, OPTIONS, inputFor, verifyDesign} from './design.mjs';
import {contentRequest, styleRequest} from '../contrast-v1/layered.mjs';
const out = resolve(process.argv[2]);
const root = resolve(import.meta.dirname, '../..');
const r = JSON.parse(readFileSync(join(out, 'results.json')));
const fixture = JSON.parse(readFileSync(new URL('./fixture.json', import.meta.url)));
const hash = value => createHash('sha256').update(value).digest('hex');
assert.equal(r.status, 'completed_not_business_acceptance');
assert.deepEqual(r.schedule, verifyDesign(fixture));
assert.deepEqual(r.rows.map(x => x.id), r.schedule.map(x => x.id));
let calls = 0;
for (const row of r.rows) {
  const item = fixture.cases.find(x => x.id === row.caseId);
  const input = inputFor(fixture, item, row.arm);
  assert.deepEqual(row.providedFactIds, input.facts.map(f => f.id));
  assert.deepEqual(row.customerStatements, input.statements);
  assert.deepEqual(row.trace[0].request, contentRequest(MODEL, input));
  let perRow = 0;
  for (const phase of row.trace) {
    if (!phase.transport) continue;
    perRow++; calls++;
    assert.equal(phase.transport.endpoint, 'http://127.0.0.1:11434/api/chat');
    const actual = phase.transport.body;
    assert.deepEqual(actual.options, {...OPTIONS, seed: row.seed});
    assert.deepEqual(actual.messages, phase.request.messages);
    assert.equal(actual.model, MODEL);
    assert.equal(actual.format, 'json');
    assert.equal(actual.keep_alive, '120s');
  }
  assert.equal(row.modelCalls, perRow);
  if (row.status === 'generated') {
    assert.equal(row.trace.length, 2);
    assert.deepEqual(row.trace[1].request, styleRequest(MODEL, input, row.plan));
  }
}
for (const [f, h] of Object.entries(r.baselineHashes)) assert.equal(hash(readFileSync(join(root, f))), h, f);
for (const [f, h] of Object.entries(r.experimentHashes)) assert.equal(hash(readFileSync(new URL('./' + f, import.meta.url))), h, f);
assert.equal(r.originalAccountRead, false);
assert.equal(r.realWechatCalls, 0);
const checks = {
  status: 'integrity_passed_not_business_acceptance', uniqueRows: 48,
  matchedFrozenSchedule: true, fixedOptionsVerifiedEveryRecordedCall: true,
  pairedMaterialsVerified: true, localModelCalls: calls,
  baselineFilesUnchanged: Object.keys(r.baselineHashes).length,
  experimentFilesUnchanged: Object.keys(r.experimentHashes).length,
  rawResultsSha256: hash(readFileSync(join(out, 'results.json'))),
  originalAccountRead: false, realWechatCalls: 0,
};
writeFileSync(join(out, 'integrity.json'), JSON.stringify(checks, null, 2));
console.log(JSON.stringify(checks));
