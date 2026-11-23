import test from 'node:test';import assert from 'node:assert/strict';import {studioReviewQueue,studioView} from '../web/persona-studio.js';
const trial=(id,extra={})=>({id,profileId:'current',version:2,question:'客户想先看看',reply:'你慢慢看',rating:null,...extra});
test('review queue excludes old versions, blocked results and fact quotes without inventing owner ratings',()=>{
 const state={personaStudio:{profile:{id:'current',version:2,examples:[]},trials:[trial('pending'),trial('rated',{rating:'unlike'}),trial('old',{profileId:'old'}),trial('blocked',{blocked:true}),trial('faq',{replySource:'approved_faq'})]}};
 const before=JSON.stringify(state),queue=studioReviewQueue(state);assert.deepEqual(queue.pending.map(t=>t.id),['pending']);assert.deepEqual(queue.rated.map(t=>t.id),['rated']);assert.equal(JSON.stringify(state),before);assert.ok(studioView(state).includes('待你点评的回复（1）'));
});
test('selecting an older current trial displays that result and its feedback target, while stale selection cannot revive old replies',()=>{
 const state={personaStudio:{profile:{id:'current',version:2,examples:[]},trials:[trial('older',{reply:'旧一句但仍是当前口吻'}),trial('newer',{reply:'最新一句'}),trial('stale',{profileId:'old',reply:'已换口吻的旧句'})]}};
 const html=studioView(state,'older');assert.ok(html.includes('正在点评选中的历史试聊'));assert.ok(html.includes('data-form="studio-correction" data-id="older"'));assert.ok(!html.includes('data-form="studio-correction" data-id="newer"'));
 const stale=studioView(state,'stale');assert.ok(stale.includes('data-form="studio-correction" data-id="newer"'));assert.ok(!stale.includes('data-form="studio-correction" data-id="stale"'));
});
test('queue escapes customer and model content and gives an honest empty state without an owner profile',()=>{
 const s={personaStudio:{profile:{id:'current',version:2,examples:[]},trials:[trial('x',{question:'<img src=x onerror=alert(1)>',reply:'<script>bad()</script>'})]}};
 const html=studioView(s);assert.ok(!html.includes('<script>bad()'));assert.ok(html.includes('&lt;script&gt;'));assert.equal(studioReviewQueue({}).pending.length,0);assert.ok(studioView({}).includes('先选基础口吻并保存，再生成试聊'));
});
