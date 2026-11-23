import {readFileSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
import {MODEL, OPTIONS, inputFor, onePassRequest, verifyDesign} from './design.mjs';
import {contentRequest, styleRequest} from '../contrast-v1/layered.mjs';
import {parseReply} from '../style-fidelity-v1/design.mjs';
const root = resolve(import.meta.dirname, '../..');
const out = resolve(process.argv[2]);
const r = JSON.parse(readFileSync(join(out, 'results.json')));
const fixture = JSON.parse(readFileSync(new URL('./fixture.json', import.meta.url)));
const hash = x => createHash('sha256').update(x).digest('hex');
assert.equal(r.status, 'completed_not_business_acceptance');
assert.deepEqual(r.schedule, verifyDesign(fixture));
assert.deepEqual(r.rows.map(x => x.id), r.schedule.map(x => x.id));
let calls = 0;
for (const row of r.rows) {
  const item = fixture.cases.find(x => x.id === row.caseId);
  const input = inputFor(fixture, item);
  assert.equal(row.materialHash, hash(JSON.stringify(input)));
  assert.deepEqual(row.customerStatements, input.statements);
  assert.deepEqual(row.providedFacts, input.facts);
  assert.deepEqual(row.mustAddress, item.mustAddress);
  assert.deepEqual(row.forbiddenAdds, item.forbiddenAdds);
  assert.deepEqual(row.trace[0].request, row.arm === 'onepass' ? onePassRequest(input) : contentRequest(MODEL, input));
  let count = 0;
  for (const phase of row.trace) {
    if (!phase.transport) continue;
    count++; calls++;
    assert.equal(phase.transport.endpoint, 'http://127.0.0.1:11434/api/chat');
    const actual = phase.transport.body;
    assert.equal(actual.model, MODEL);
    assert.deepEqual(actual.options, {...OPTIONS, seed: row.seed});
    assert.deepEqual(actual.messages, phase.request.messages);
    assert.equal(actual.format, 'json'); assert.equal(actual.keep_alive, '120s');
  }
  assert.equal(row.modelCalls, count);
  if (row.arm === 'layered' && row.trace.length === 2) {
    const plan = JSON.parse(row.trace[0].response.choices[0].message.content);
    assert.deepEqual(row.trace[1].request, styleRequest(MODEL, input, plan));
  }
  if (row.status === 'generated') assert.equal(parseReply(row.trace.at(-1).response, item).validate(), row.reply);
}
for (const [f, h] of Object.entries(r.baselineHashes)) assert.equal(hash(readFileSync(join(root, f))), h, f);
for (const [f, h] of Object.entries(r.experimentHashes)) assert.equal(hash(readFileSync(new URL('./' + f, import.meta.url))), h, f);
assert.equal(r.originalAccountRead, false); assert.equal(r.realWechatCalls, 0);
const result = {status: 'integrity_passed_not_business_acceptance', uniqueRows: 48,
  matchedFrozenSchedule: true, pairedRawMaterialsIdentical: true, actualParametersVerified: true,
  recordedModelCalls: calls, baselineFilesUnchanged: Object.keys(r.baselineHashes).length,
  experimentFilesUnchanged: Object.keys(r.experimentHashes).length,
  rawResultsSha256: hash(readFileSync(join(out, 'results.json'))),
  originalAccountRead: false, realWechatCalls: 0};
writeFileSync(join(out, 'integrity.json'), JSON.stringify(result, null, 2));
console.log(JSON.stringify(result));
