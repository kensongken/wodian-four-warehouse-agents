import test from 'node:test';import assert from 'node:assert/strict';import {customerNeedState,customerNeedSummary,customerNeedIssues}from '../lib/customer-needs.mjs';
import {customerNeedsView}from '../web/persona-studio.js';
test('need evidence rendering escapes source text and distinguishes intent from facts or permanent profiles',()=>{
 const html=customerNeedsView({customerNeedState:{fields:{occasion:{label:'场景',values:['周末'],quote:'周末<script>bad()</script>',excluded:[{value:'上班',quote:'不看上班的。'}]}}}});assert.ok(html.includes('明确排除：上班'));assert.ok(html.includes('&lt;script&gt;'));assert.ok(!html.includes('<script>'));assert.ok(html.includes('不作为商品资料或永久客户画像'));assert.equal(customerNeedsView({}),'');
});
test('current intent uses explicit later occasion/style and preserves unchanged wearer/category with evidence',()=>{
 const quotes=['我想给自己选条上班戴的手链，简单一点。','先不看上班的了，周末出去玩想显眼一点。','不过还是想要手链，别绕到项链去了。'];const state=customerNeedState(quotes),summary=customerNeedSummary(state);assert.deepEqual(summary.occasion.values,['周末']);assert.deepEqual(summary.occasion.excluded,['上班']);assert.deepEqual(summary.style.values,['显眼']);assert.deepEqual(summary.wearer.values,['给自己']);assert.deepEqual(summary.category.values,['手链']);assert.deepEqual(summary.category.excluded,['项链']);assert.equal(state.fields.occasion.quote,quotes[1]);assert.equal(state.source,'literal_current_branch_quotes');
});
test('questions and ambiguous options do not become confirmed current requirements',()=>{
 const state=customerNeedState(['我自己戴手链。','手链还是项链更合适？','上班或者周末都行吗','你说哪种显眼？']);assert.deepEqual(state.fields.category.values,['手链']);assert.equal(state.fields.occasion,undefined);assert.equal(state.fields.style,undefined);
});
test('a later explicit return to a former occasion reopens it rather than permanently excluding it',()=>{
 const state=customerNeedState(['上班戴手链。','不看上班的，周末戴。','又改了，还是上班戴。']);assert.deepEqual(state.fields.occasion.values,['上班']);assert.deepEqual(state.fields.occasion.excluded,[]);
});
test('stale positive requirements are checked while explicit historical or negative mentions remain valid',()=>{
 const state=customerNeedState(['我自己戴手链，上班用。','不看上班的，周末玩想显眼。','不要项链，还是手链。']);assert.ok(customerNeedIssues(state,'上班戴手链，周末也戴。').length);assert.deepEqual(customerNeedIssues(state,'之前是上班用，现在周末戴手链，想显眼一点。'),[]);assert.deepEqual(customerNeedIssues(state,'你自己戴手链，周末玩想显眼一点，不考虑项链。'),[]);
});
test('ordinary negative style phrases stay exclusions, including gentle negative advice',()=>{
 const state=customerNeedState(['想给我妈选条日常戴的手链，打扮简单，不爱花哨。']);assert.deepEqual(state.fields.style.values,['简单']);assert.deepEqual(state.fields.style.excluded.map(x=>x.value),['花哨']);assert.deepEqual(customerNeedIssues(state,'可以看线条简单、不花哨的方向。'),[]);assert.deepEqual(customerNeedIssues(state,'别太花哨，重点看跟日常衣服搭不搭。'),[]);
});

test('confirmed self use retains own preference clause, not quoted other-person tastes or uncertain options',()=>{
 const q='我妈喜欢张扬的，我喜欢简单的，要听谁的？',s=customerNeedState(['是给我自己选手链。',q]);assert.deepEqual(s.fields.wearer.values,['给我自己']);assert.deepEqual(s.fields.style.values,['简单']);assert.equal(s.fields.style.quote,q);
 for(const q of ['我喜欢简单还是复杂的？','我喜欢简单的吗？','她说我喜欢简单的，对吗？'])assert.equal(customerNeedState(['给自己选手链。',q]).fields.style,undefined);
 assert.equal(customerNeedState(['送妈妈项链。','我喜欢显眼的，要听我的吗？']).fields.style,undefined);
});

test('an explicit own preference after but remains literal evidence in a confirmed self-use branch',()=>{const s=customerNeedState(['给自己选戒指。','家人让我选显眼的，但我喜欢低调，怎么想？']);assert.deepEqual(s.fields.style.values,['低调']);});
