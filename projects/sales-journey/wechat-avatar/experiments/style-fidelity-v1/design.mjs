import {schedule as pairedSchedule, MODEL, OPTIONS} from '../relevance-v1/design.mjs';
import {styleRequest} from '../contrast-v1/layered.mjs';
import {previewCapabilityIssues} from '../../lib/preview-capabilities.mjs';
export {MODEL, OPTIONS};

export function schedule(fixture) {
  return pairedSchedule(fixture).map(x => {
    const arm = x.arm === 'all' ? 'direct' : 'rewrite';
    return {...x, arm, id: `${x.caseId}:r${x.repeat}:${arm}`};
  });
}

export function requestFor(fixture, item) {
  return styleRequest(MODEL, {profile: fixture.profile}, {content: item.content});
}

export function checkReply(reply, item) {
  if (typeof reply !== 'string' || !reply.trim() || reply.length > 180) throw Error('invalid styled reply');
  const issues = previewCapabilityIssues(reply, {hasBranchHistory: item.statements.length > 1});
  if (issues.length) throw Error(issues.join(' '));
  return reply.trim();
}

export function parseReply(response, item) {
  let value;
  try { value = JSON.parse(response.choices?.[0]?.message?.content); }
  catch { throw Error('invalid JSON'); }
  if (!value || Array.isArray(value) || Object.keys(value).some(k => k !== 'reply')) throw Error('unexpected response fields');
  return {candidate: value.reply, validate: () => checkReply(value.reply, item)};
}

export function verifyDesign(fixture) {
  if (fixture.cases.length !== 12 || new Set(fixture.cases.map(x => x.id)).size !== 12) throw Error('Expected 12 unique cases');
  for (const item of fixture.cases) {
    checkReply(item.content, item);
    if (!item.mustRetain.length || !item.forbiddenAdds.length) throw Error('Missing frozen evaluation criteria');
    if (Buffer.byteLength(JSON.stringify(requestFor(fixture, item).messages)) > 3000) throw Error('Fixed context exceeded');
  }
  return schedule(fixture);
}
