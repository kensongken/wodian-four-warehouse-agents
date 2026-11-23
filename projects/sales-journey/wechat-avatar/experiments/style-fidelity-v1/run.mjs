import {readFileSync, writeFileSync, mkdirSync, renameSync} from 'node:fs';
import {resolve, join} from 'node:path';
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {LocalQwen, localOllamaJSON} from '../../lib/qwen-model.mjs';
import {MODEL, OPTIONS, requestFor, checkReply, parseReply, verifyDesign} from './design.mjs';

const root = resolve(import.meta.dirname, '../..');
const out = resolve(process.env.AVATAR_FIDELITY_OUTPUT || join(root, '.acceptance/style-fidelity-pilot-1'));
const hash = value => createHash('sha256').update(value).digest('hex');
const fixture = JSON.parse(readFileSync(new URL('./fixture.json', import.meta.url)));
const order = verifyDesign(fixture);
mkdirSync(out, {recursive: true});
try { readFileSync(join(out, 'results.json')); throw Error('Output exists; no automatic rerun'); }
catch (error) { if (error.code !== 'ENOENT') throw error; }
const files = execFileSync('git', ['ls-files', '-z', '*.mjs', '*.js'], {cwd: root, encoding: 'utf8'}).split('\0').filter(Boolean);
const baselineHashes = Object.fromEntries(files.map(f => [f, hash(readFileSync(join(root, f)))]));
const experimentHashes = Object.fromEntries(['fixture.json', 'design.mjs', 'run.mjs', 'PROTOCOL.txt'].map(f => [f, hash(readFileSync(new URL('./' + f, import.meta.url)))]));
const getJSON = async path => {
  const response = await fetch('http://127.0.0.1:11434' + path, {signal: AbortSignal.timeout(10000)});
  if (!response.ok) throw Error('Local preflight failed');
  return response.json();
};
const tags = await getJSON('/api/tags');
if (!tags.models.some(x => x.name === MODEL && x.digest === fixture.modelDigest)) throw Error('Frozen model not available');
const resident = await getJSON('/api/ps');
if (resident.models.some(x => x.name !== MODEL)) throw Error('Other model resident; not forcibly unloading');
const result = {
  version: fixture.version, startedAt: new Date().toISOString(), status: 'running',
  baselineSha: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: root, encoding: 'utf8'}).trim(),
  baselineHashes, experimentHashes, schedule: order, model: MODEL,
  modelDigest: fixture.modelDigest, fixedOptions: OPTIONS, rows: [],
  contentSource: 'developer_authored_frozen_references_not_automatic_content_generation',
  scope: 'isolated style transformation fidelity; direct control is a deterministic copy',
  originalAccountRead: false, realWechatCalls: 0, productionChanged: false,
  ownerRatingsAssigned: 0, independentReviewers: 0,
};
const save = () => {
  writeFileSync(join(out, 'results.tmp'), JSON.stringify(result, null, 2));
  renameSync(join(out, 'results.tmp'), join(out, 'results.json'));
};
save();
for (const job of order) {
  const item = fixture.cases.find(x => x.id === job.caseId);
  const row = {...job, referenceContent: item.content, contentHash: hash(item.content),
    customerStatements: item.statements, mustRetain: item.mustRetain,
    forbiddenAdds: item.forbiddenAdds, trace: [], modelCalls: 0};
  const started = Date.now();
  let client;
  try {
    if (job.arm === 'direct') {
      row.reply = checkReply(item.content, item);
    } else {
      const request = requestFor(fixture, item);
      const phase = {stage: 'style', request};
      row.trace.push(phase);
      client = new LocalQwen({timeout: 120000, keepAlive: '120s', post: async (path, body, options) => {
        if (path === '/api/chat') {
          if (Buffer.byteLength(JSON.stringify(body.messages)) > 3000) throw Error('Fixed context exceeded');
          body = {...body, options: {...OPTIONS, seed: job.seed}};
          phase.transport = {endpoint: 'http://127.0.0.1:11434/api/chat', body};
          row.modelCalls++;
        }
        return localOllamaJSON(path, body, options);
      }});
      try { phase.response = await client.complete(request); }
      catch (error) { phase.error = error.message; throw error; }
      const parsed = parseReply(phase.response, item);
      if (typeof parsed.candidate === 'string') row.candidateReply = parsed.candidate;
      row.reply = parsed.validate();
    }
    row.status = 'generated';
  } catch (error) {
    row.status = 'not_delivered'; row.error = error.message;
    row.failureClass = /invalid JSON|unexpected response|invalid styled/.test(error.message) ? 'protocol' : /超时|连接|中断|请求失败/.test(error.message) ? 'transport_or_timeout' : 'guard_or_other';
  } finally { client?.close(); }
  row.elapsedMs = Date.now() - started;
  result.rows.push(row); save();
  console.log(JSON.stringify({completed: result.rows.length, planned: 48, id: row.id,
    status: row.status, reply: row.reply, candidateReply: row.status !== 'generated' ? row.candidateReply : undefined,
    error: row.error, elapsedMs: row.elapsedMs}));
}
const after = await getJSON('/api/tags');
if (!after.models.some(x => x.name === MODEL && x.digest === fixture.modelDigest)) throw Error('Model changed');
for (const [f, h] of Object.entries(baselineHashes)) if (hash(readFileSync(join(root, f))) !== h) throw Error('Baseline changed: ' + f);
for (const [f, h] of Object.entries(experimentHashes)) if (hash(readFileSync(new URL('./' + f, import.meta.url))) !== h) throw Error('Experiment changed: ' + f);
if (result.rows.length !== 48) throw Error('Incomplete planned denominator');
result.status = 'completed_not_business_acceptance'; result.finishedAt = new Date().toISOString(); save();
