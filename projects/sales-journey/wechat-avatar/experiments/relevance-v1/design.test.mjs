import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {MODEL, inputFor, verifyDesign} from './design.mjs';
import {contentRequest, styleRequest} from '../contrast-v1/layered.mjs';
const fixture = JSON.parse(readFileSync(new URL('./fixture.json', import.meta.url)));

test('48 unique jobs preserve all cases, matched seeds, and counterbalanced pair order', () => {
  const jobs = verifyDesign(fixture);
  assert.equal(jobs.length, 48);
  assert.equal(new Set(jobs.map(x => x.id)).size, 48);
  for (const item of fixture.cases) {
    const pairs = [1, 2].map(repeat => jobs.filter(x => x.caseId === item.id && x.repeat === repeat));
    for (const pair of pairs) {
      assert.equal(pair.length, 2);
      assert.equal(pair[0].seed, pair[1].seed);
      assert.notEqual(pair[0].arm, pair[1].arm);
      assert.equal(jobs.indexOf(pair[0]) + 1, jobs.indexOf(pair[1]));
    }
    assert.notEqual(pairs[0][0].arm, pairs[1][0].arm);
    assert.notEqual(pairs[0][0].seed, pairs[1][0].seed);
  }
});

test('paired content requests differ only in approvedFacts, while style inputs are identical for a given plan', () => {
  for (const item of fixture.cases) {
    const all = inputFor(fixture, item, 'all');
    const relevant = inputFor(fixture, item, 'relevant');
    const [a, b] = [all, relevant].map(input => contentRequest(MODEL, input));
    const [userA, userB] = [a, b].map(x => JSON.parse(x.messages[1].content));
    assert.equal(userA.approvedFacts.length, 2);
    assert.deepEqual(userB.approvedFacts.map(f => f.id), item.relevantFactIds);
    delete userA.approvedFacts; delete userB.approvedFacts;
    assert.deepEqual(userA, userB);
    a.messages[1].content = ''; b.messages[1].content = '';
    assert.deepEqual(a, b);
    assert.deepEqual(styleRequest(MODEL, all, {content: '相同内容'}), styleRequest(MODEL, relevant, {content: '相同内容'}));
  }
});

test('fixture retains the original owner profile but uses new customer questions', () => {
  const before = JSON.parse(readFileSync(new URL('../contrast-v1/fixture.json', import.meta.url)));
  assert.deepEqual(fixture.profile, before.profile);
  const used = before.scenarios.flatMap(x => x.turns);
  for (const item of fixture.cases) assert.ok(!used.includes(item.statements.at(-1)));
});
