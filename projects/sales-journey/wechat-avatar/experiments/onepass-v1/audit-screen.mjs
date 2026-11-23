import {readFileSync, writeFileSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
import {inputFor, onePassRequest, OPTIONS} from './design.mjs';
import {parseReply} from '../style-fidelity-v1/design.mjs';
const root = resolve(import.meta.dirname, '../..');
const out = resolve(process.argv[2]);
const basePath = resolve(process.argv[3]);
const r = JSON.parse(readFileSync(join(out, 'results.json')));
const base = JSON.parse(readFileSync(basePath));
const fixture = JSON.parse(readFileSync(new URL('./fixture.json', import.meta.url)));
const config = JSON.parse(readFileSync(new URL('./screen-config.json', import.meta.url)));
const hash = x => createHash('sha256').update(x).digest('hex');
assert.equal(r.status, 'completed_not_business_acceptance');
assert.equal(hash(readFileSync(basePath)), config.baseResultSha256);
assert.deepEqual(r.jobs, base.schedule.filter(x => x.arm === 'onepass'));
assert.equal(r.rows.length, 24);
let calls = 0;
for (let i = 0; i < r.jobs.length; i++) {
  const job = r.jobs[i], row = r.rows[i];
  const item = fixture.cases.find(x => x.id === job.caseId);
  const input = inputFor(fixture, item);
  const previous = base.rows.find(x => x.id === job.id);
  assert.equal(row.id, `${job.caseId}:r${job.repeat}:onepass8b`);
  assert.equal(row.materialHash, previous.materialHash);
  assert.equal(row.materialHash, hash(JSON.stringify(input)));
  assert.equal(row.seed, previous.seed);
  assert.deepEqual(row.mustAddress, previous.mustAddress);
  assert.deepEqual(row.forbiddenAdds, previous.forbiddenAdds);
  const phase = row.trace[0];
  assert.equal(row.trace.length, 1);
  assert.deepEqual(phase.request, {...onePassRequest(input), model: config.model});
  assert.deepEqual(phase.request.messages, previous.trace[0].request.messages);
  assert.equal(row.modelCalls, phase.transport ? 1 : 0);
  if (phase.transport) {
    calls++;
    assert.equal(phase.transport.endpoint, 'http://127.0.0.1:11434/api/chat');
    const actual = phase.transport.body;
    assert.equal(actual.model, config.model);
    assert.equal(actual.think, false);
    assert.deepEqual(actual.options, {...OPTIONS, seed: job.seed});
    assert.deepEqual(actual.messages, phase.request.messages);
    assert.equal(actual.format, 'json'); assert.equal(actual.keep_alive, '120s');
  }
  if (row.status === 'generated') assert.equal(parseReply(phase.response, item).validate(), row.reply);
}
for (const [f, h] of Object.entries(r.baselineHashes)) assert.equal(hash(readFileSync(join(root, f))), h, f);
for (const [f, h] of Object.entries(r.experimentHashes)) assert.equal(hash(readFileSync(new URL('./' + f, import.meta.url))), h, f);
const result = {status: 'integrity_passed_not_business_acceptance', plannedReplies: 24,
  baseResultsUnchanged: true, sameOnepassMessagesAndMaterials: true,
  sameSeedsAndSamplingOptions: true, modelCompatibilityThinkFalse: true,
  recordedModelCalls: calls, baselineFilesUnchanged: Object.keys(r.baselineHashes).length,
  experimentFilesUnchanged: Object.keys(r.experimentHashes).length,
  rawResultsSha256: hash(readFileSync(join(out, 'results.json'))), baseResultSha256: config.baseResultSha256,
  originalAccountRead: false, realWechatCalls: 0};
writeFileSync(join(out, 'integrity.json'), JSON.stringify(result, null, 2));
console.log(JSON.stringify(result));
