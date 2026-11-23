import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {MODEL, inputFor, onePassRequest, verifyDesign} from './design.mjs';
import {contentRequest, styleRequest} from '../contrast-v1/layered.mjs';
const fixture = JSON.parse(readFileSync(new URL('./fixture.json', import.meta.url)));

test('48 jobs cover both workflows twice with matched seeds and reversed pair order', () => {
  const jobs = verifyDesign(fixture);
  assert.equal(jobs.length, 48); assert.equal(new Set(jobs.map(x => x.id)).size, 48);
  for (const item of fixture.cases) {
    const pairs = [1, 2].map(repeat => jobs.filter(x => x.caseId === item.id && x.repeat === repeat));
    for (const pair of pairs) {
      assert.deepEqual(new Set(pair.map(x => x.arm)), new Set(['onepass', 'layered']));
      assert.equal(pair[0].seed, pair[1].seed);
      assert.equal(jobs.indexOf(pair[1]), jobs.indexOf(pair[0]) + 1);
    }
    assert.notEqual(pairs[0][0].arm, pairs[1][0].arm);
  }
});

test('onepass uses exactly the same facts, customer statements and voice excerpts as legacy requests', () => {
  for (const item of fixture.cases) {
    const input = inputFor(fixture, item);
    const once = JSON.parse(onePassRequest(input).messages[1].content);
    const content = JSON.parse(contentRequest(MODEL, input).messages[1].content);
    const voice = JSON.parse(styleRequest(MODEL, input, {content: ''}).messages[1].content);
    delete voice.content;
    assert.deepEqual(once, {...content, ...voice});
    assert.ok(!JSON.stringify(once).includes('mustAddress'));
    for (const example of fixture.profile.examples) assert.ok(!JSON.stringify(once.ownerExpressions).includes(example.question));
  }
});

test('owner material remains unchanged and no final customer question reuses prior pilot wording', () => {
  const before = ['contrast-v1', 'relevance-v1', 'style-fidelity-v1'].map(name => JSON.parse(readFileSync(new URL('../' + name + '/fixture.json', import.meta.url))));
  assert.deepEqual(fixture.profile, before[0].profile);
  const used = before.flatMap(f => f.cases ? f.cases.flatMap(x => x.statements) : f.scenarios.flatMap(x => x.turns));
  for (const item of fixture.cases) assert.ok(!used.includes(item.statements.at(-1)));
});
