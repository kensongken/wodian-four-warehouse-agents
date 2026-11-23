import {readFileSync, writeFileSync, mkdirSync, renameSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {LocalTextTeacher, localOllamaJSON} from '../../lib/qwen-model.mjs';
import {inputFor, onePassRequest, OPTIONS} from './design.mjs';
import {parseReply} from '../style-fidelity-v1/design.mjs';
const root = resolve(import.meta.dirname, '../..');
const out = resolve(process.env.AVATAR_MODEL_SCREEN_OUTPUT || join(root, '.acceptance/onepass-text8b-screen-1'));
const basePath = resolve(process.env.AVATAR_MODEL_SCREEN_BASE || join(root, '.acceptance/onepass-pilot-1/results.json'));
const hash = value => createHash('sha256').update(value).digest('hex');
const config = JSON.parse(readFileSync(new URL('./screen-config.json', import.meta.url)));
const fixture = JSON.parse(readFileSync(new URL('./fixture.json', import.meta.url)));
if (hash(readFileSync(basePath)) !== config.baseResultSha256) throw Error('Base result changed');
const base = JSON.parse(readFileSync(basePath));
if (base.status !== 'completed_not_business_acceptance' || config.jobs.length !== 24) throw Error('Incomplete base/design');
mkdirSync(out, {recursive: true});
try { readFileSync(join(out, 'results.json')); throw Error('Output exists; no automatic rerun'); }
catch (error) { if (error.code !== 'ENOENT') throw error; }
const files = execFileSync('git', ['ls-files', '-z', '*.mjs', '*.js'], {cwd: root, encoding: 'utf8'}).split('\0').filter(Boolean);
const baselineHashes = Object.fromEntries(files.map(f => [f, hash(readFileSync(join(root, f)))]));
const frozen = ['fixture.json', 'design.mjs', 'screen-config.json', 'run-screen.mjs', 'SCREEN-PROTOCOL.txt'];
const experimentHashes = Object.fromEntries(frozen.map(f => [f, hash(readFileSync(new URL('./' + f, import.meta.url)))]));
const getJSON = async path => {
  const response = await fetch('http://127.0.0.1:11434' + path, {signal: AbortSignal.timeout(10000)});
  if (!response.ok) throw Error('Local preflight failed');
  return response.json();
};
const tags = await getJSON('/api/tags');
if (!tags.models.some(x => x.name === config.model && x.digest === config.modelDigest)) throw Error('Frozen 8B unavailable');
const result = {...config, baselineSha: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: root, encoding: 'utf8'}).trim(),
  baselineHashes, experimentHashes, startedAt: new Date().toISOString(), status: 'waiting_for_resource',
  rows: [], originalAccountRead: false, realWechatCalls: 0, productionChanged: false,
  ownerRatingsAssigned: 0, independentReviewers: 0, fixedOptions: OPTIONS};
const save = () => {writeFileSync(join(out, 'results.tmp'), JSON.stringify(result, null, 2)); renameSync(join(out, 'results.tmp'), join(out, 'results.json'));};
save();
const waiting = Date.now(); let clear = false;
while (Date.now() - waiting < 180000) {
  const ps = await getJSON('/api/ps');
  if (!ps.models.some(x => x.name !== config.model)) {clear = true; break;}
  await new Promise(resolve => setTimeout(resolve, 2000));
}
result.resourceWaitMs = Date.now() - waiting;
if (!clear) {result.status = 'not_run_resource_busy'; result.reason = 'Other model stayed resident; no forced unload'; save(); process.exit(0);}
result.status = 'running'; save();
for (const job of config.jobs) {
  const item = fixture.cases.find(x => x.id === job.caseId);
  const input = inputFor(fixture, item);
  const request = {...onePassRequest(input), model: config.model};
  const phase = {stage: 'onepass8b', request};
  const row = {...job, id: `${job.caseId}:r${job.repeat}:onepass8b`, arm: 'onepass8b',
    materialHash: hash(JSON.stringify(input)), customerStatements: input.statements, providedFacts: input.facts,
    mustAddress: item.mustAddress, forbiddenAdds: item.forbiddenAdds, trace: [phase], modelCalls: 0};
  const client = new LocalTextTeacher({timeout: 120000, keepAlive: '120s', post: async (path, body, options) => {
    if (path === '/api/chat') {
      if (Buffer.byteLength(JSON.stringify(body.messages)) > 3000) throw Error('Fixed context exceeded');
      body = {...body, options: {...OPTIONS, seed: job.seed}};
      phase.transport = {endpoint: 'http://127.0.0.1:11434/api/chat', body}; row.modelCalls++;
    }
    return localOllamaJSON(path, body, options);
  }});
  const started = Date.now();
  try {
    try {phase.response = await client.complete(request);}
    catch (error) {phase.error = error.message; throw error;}
    const parsed = parseReply(phase.response, item);
    if (typeof parsed.candidate === 'string') row.candidateReply = parsed.candidate;
    row.reply = parsed.validate(); row.status = 'generated';
  } catch (error) {
    row.status = 'not_delivered'; row.error = error.message;
    row.failureClass = /invalid JSON|unexpected response|invalid styled/.test(error.message) ? 'protocol' : /超时|连接|中断|请求失败/.test(error.message) ? 'transport_or_timeout' : 'guard_or_other';
  } finally {client.close();}
  row.elapsedMs = Date.now() - started; result.rows.push(row); save();
  console.log(JSON.stringify({completed: result.rows.length, planned: 24, id: row.id, status: row.status, reply: row.reply, candidateReply: row.status !== 'generated' ? row.candidateReply : undefined, error: row.error, elapsedMs: row.elapsedMs}));
}
const after = await getJSON('/api/tags');
if (!after.models.some(x => x.name === config.model && x.digest === config.modelDigest)) throw Error('Model changed');
if (hash(readFileSync(basePath)) !== config.baseResultSha256) throw Error('Base result changed');
for (const [f, h] of Object.entries(baselineHashes)) if (hash(readFileSync(join(root, f))) !== h) throw Error('Baseline changed: ' + f);
for (const [f, h] of Object.entries(experimentHashes)) if (hash(readFileSync(new URL('./' + f, import.meta.url))) !== h) throw Error('Experiment changed: ' + f);
if (result.rows.length !== 24) throw Error('Incomplete denominator');
result.status = 'completed_not_business_acceptance'; result.finishedAt = new Date().toISOString(); save();
