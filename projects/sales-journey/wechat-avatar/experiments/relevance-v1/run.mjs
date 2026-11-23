import {readFileSync, writeFileSync, mkdirSync, renameSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {totalmem} from 'node:os';
import {LocalQwen, localOllamaJSON} from '../../lib/qwen-model.mjs';
import {layeredReply} from '../contrast-v1/layered.mjs';
import {MODEL, OPTIONS, inputFor, verifyDesign} from './design.mjs';

const root = resolve(import.meta.dirname, '../..');
const out = resolve(process.env.AVATAR_RELEVANCE_OUTPUT || join(root, '.acceptance/relevance-pilot-1'));
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
const fixture = JSON.parse(readFileSync(new URL('./fixture.json', import.meta.url)));
const order = verifyDesign(fixture);
mkdirSync(out, {recursive: true});
// Never overwrite an earlier run or select a preferred repeat after seeing results.
try { readFileSync(join(out, 'results.json')); throw Error('Output already exists; no automatic rerun'); }
catch (error) { if (error.code !== 'ENOENT') throw error; }
const tracked = execFileSync('git', ['ls-files', '-z', '*.mjs', '*.js'], {cwd: root, encoding: 'utf8'}).split('\0').filter(Boolean);
const baselineHashes = Object.fromEntries(tracked.map(f => [f, sha256(readFileSync(join(root, f)))]));
const frozenFiles = ['fixture.json', 'design.mjs', 'run.mjs', 'PROTOCOL.txt'];
const experimentHashes = Object.fromEntries(frozenFiles.map(f => [f, sha256(readFileSync(new URL('./' + f, import.meta.url)))]));
const getJSON = async path => {
  const response = await fetch('http://127.0.0.1:11434' + path, {signal: AbortSignal.timeout(10000)});
  if (!response.ok) throw Error('Local preflight failed');
  return response.json();
};
const tags = await getJSON('/api/tags');
const model = tags.models.find(m => m.name === MODEL);
if (!model || model.digest !== fixture.modelDigest) throw Error('Frozen model unavailable or changed');
const resident = await getJSON('/api/ps');
if (resident.models.some(m => m.name !== MODEL)) throw Error('Another model is resident; no forced unload or simultaneous allocation');
const report = {
  version: fixture.version, startedAt: new Date().toISOString(),
  baselineSha: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: root, encoding: 'utf8'}).trim(),
  baselineHashes, experimentHashes, model: MODEL, modelDigest: model.digest,
  fixedOptions: OPTIONS, schedule: order, rows: [], status: 'running',
  memoryBytes: totalmem(), residentAtStart: resident.models.map(m => m.name),
  originalAccountRead: false, realWechatCalls: 0, productionChanged: false,
  ownerRatingsAssigned: 0, independentReviewers: 0,
  designScope: 'paired relevance-list ablation; synthetic customer-statement snapshots; not live conversational branches or production acceptance',
};
const save = () => {
  writeFileSync(join(out, 'results.tmp'), JSON.stringify(report, null, 2));
  renameSync(join(out, 'results.tmp'), join(out, 'results.json'));
};
save();
for (const job of order) {
  const item = fixture.cases.find(x => x.id === job.caseId);
  const input = inputFor(fixture, item, job.arm);
  const trace = [];
  const row = {...job, question: item.statements.at(-1), customerStatements: input.statements,
    providedFactIds: input.facts.map(f => f.id), rubric: item.rubric, trace};
  let calls = 0;
  const modelClient = new LocalQwen({timeout: 120000, keepAlive: '120s', post: async (path, body, options) => {
    if (path === '/api/chat') {
      if (Buffer.byteLength(JSON.stringify(body.messages)) > 3000) throw Error('Fixed context budget exceeded');
      body = {...body, options: {...OPTIONS, seed: job.seed}};
      calls++;
      trace.at(-1).transport = {endpoint: 'http://127.0.0.1:11434/api/chat', body};
    }
    return localOllamaJSON(path, body, options);
  }});
  const started = Date.now();
  try {
    Object.assign(row, await layeredReply(input, {model: MODEL, trace, complete: p => modelClient.complete(p)}), {status: 'generated'});
  } catch (error) {
    const message = error.message;
    Object.assign(row, {status: 'not_delivered', error: message,
      failureClass: /invalid plan|invalid JSON|unexpected response|invalid styled/.test(message) ? 'protocol' : /超时|连接|中断|请求失败/.test(message) ? 'transport_or_timeout' : 'guard_or_other'});
  } finally { modelClient.close(); }
  row.elapsedMs = Date.now() - started;
  row.modelCalls = calls;
  report.rows.push(row);
  save();
  console.log(JSON.stringify({completed: report.rows.length, planned: order.length, id: job.id,
    status: row.status, reply: row.reply, error: row.error, elapsedMs: row.elapsedMs}));
}
const after = await getJSON('/api/tags');
if (!after.models.some(m => m.name === MODEL && m.digest === model.digest)) throw Error('Model changed');
for (const [f, h] of Object.entries(baselineHashes)) if (sha256(readFileSync(join(root, f))) !== h) throw Error('Baseline changed: ' + f);
for (const [f, h] of Object.entries(experimentHashes)) if (sha256(readFileSync(new URL('./' + f, import.meta.url))) !== h) throw Error('Experiment changed: ' + f);
if (report.rows.length !== 48) throw Error('Incomplete planned denominator');
report.status = 'completed_not_business_acceptance';
report.finishedAt = new Date().toISOString();
save();
