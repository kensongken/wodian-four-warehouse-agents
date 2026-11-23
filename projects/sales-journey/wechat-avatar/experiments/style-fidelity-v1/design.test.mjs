import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {verifyDesign, requestFor, parseReply} from './design.mjs';
const fixture = JSON.parse(readFileSync(new URL('./fixture.json', import.meta.url)));

test('48 planned outputs use 12 frozen contents twice per arm, with pair order reversed', () => {
  const jobs = verifyDesign(fixture);
  assert.equal(jobs.length, 48);
  assert.equal(new Set(jobs.map(x => x.id)).size, 48);
  for (const item of fixture.cases) {
    const pairs = [1, 2].map(repeat => jobs.filter(x => x.caseId === item.id && x.repeat === repeat));
    for (const pair of pairs) {
      assert.equal(pair.length, 2);
      assert.deepEqual(new Set(pair.map(x => x.arm)), new Set(['direct', 'rewrite']));
      assert.equal(pair[0].seed, pair[1].seed);
      assert.equal(jobs.indexOf(pair[1]), jobs.indexOf(pair[0]) + 1);
    }
    assert.notEqual(pairs[0][0].arm, pairs[1][0].arm);
  }
});

test('rewriter receives only fixed content and unchanged owner references, not customer dialogue or rubric', () => {
  const prior = JSON.parse(readFileSync(new URL('../contrast-v1/fixture.json', import.meta.url)));
  assert.deepEqual(fixture.profile, prior.profile);
  for (const item of fixture.cases) {
    const request = requestFor(fixture, item);
    const data = JSON.parse(request.messages[1].content);
    assert.equal(data.content, item.content);
    assert.deepEqual(Object.keys(data).sort(), ['content', 'ownerExpressions', 'style']);
    for (const statement of item.statements) assert.ok(!JSON.stringify(request).includes(statement));
    assert.ok(!JSON.stringify(request).includes('mustRetain'));
  }
});

test('unknown-status reference survives mechanical check, but fake action candidate is retained then rejected', () => {
  const item = fixture.cases.find(x => x.id === 'order_unknown');
  const response = text => ({choices: [{message: {content: JSON.stringify({reply: text})}}]});
  assert.equal(parseReply(response(item.content), item).validate(), item.content);
  const parsed = parseReply(response('我帮你查查。'), item);
  assert.equal(parsed.candidate, '我帮你查查。');
  assert.throws(parsed.validate, /执行回执|查询/);
});

test('malformed or extra-field output cannot become a successful reply', () => {
  const item = fixture.cases[0];
  const response = text => ({choices: [{message: {content: text}}]});
  assert.throws(() => parseReply(response('not-json'), item), /invalid JSON/);
  assert.throws(() => parseReply(response('{"reply":"hi","success":true}'), item), /unexpected response/);
  assert.throws(() => parseReply(response('{"reply":null}'), item).validate(), /invalid styled/);
});
