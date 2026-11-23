import test from 'node:test';
import assert from 'node:assert/strict';
import {everydayFocus,everydayNeeds,everydayIssues} from '../lib/everyday-conversation.mjs';
import {allowedMerchantActions,buyerStageGoal} from '../lib/customer-duel.mjs';
test('classifies everyday goals without treating a gift recipient as family chit-chat',()=>{
 for(const [text,key] of [['孩子最近太闹了，有点累','family_chat'],['工作特别烦','work_or_mood'],['不想细说家事','privacy_boundary'],['你吃饭了吗？','owner_life_status'],['你家孩子最近怎样？','owner_life_status'],['回到刚才的手链，你记得我的要求吗？','resume_selection']])assert.equal(everydayFocus(text)?.key,key,text);
 for(const text of ['送妈妈，她喜欢简单低调','孩子闹，这个多少钱？','工作累，这款材质是什么？','我要退款'])assert.equal(everydayFocus(text),null,text);
});
test('everyday reception rejects pressure, invented family, intrusive followups and unauthorized commerce',()=>{
 const family=everydayFocus('带孩子太累了');
 for(const reply of ['买个手链犒劳自己','我家孩子也这样','你老公太自私','瞒着他买就好'])assert.ok(everydayIssues(family,reply,{type:'none'}).length,reply);
 assert.ok(everydayIssues(family,'好。',{type:'offer'}).length);
 assert.ok(everydayIssues(everydayFocus('不想细说家事'),'为什么不说？',{type:'none'}).length);
 assert.ok(everydayIssues(everydayFocus('不想细说家事'),'孩子闹腾确实让人疲惫。',{type:'none'}).length);
 assert.ok(everydayIssues(everydayFocus('你吃饭了吗？'),'刚吃过。',{type:'none'}).length);
 for(const [text,reply] of [['带孩子太累了','忙了一天，先歇一会儿。'],['不想细说家事','好，那就不说这个。'],['你吃饭了吗？','这会儿由分身接待，店主今天的饭点我还不清楚。']])assert.deepEqual(everydayIssues(everydayFocus(text),reply,{type:'none'}),[]);
});
test('literal selection needs survive chit-chat without borrowing merchant claims or family preferences',()=>{
 const history=[{customer:'给自己选上班戴的手链，喜欢简单低调。',shop:'你喜欢宽一点和设计感。'},{customer:'孩子闹，我上班也很累。',shop:'送妈妈吧。'}];
 const needs=everydayNeeds(history,'回到刚才的手链，你记得我的要求吗？');
 assert.deepEqual(needs.wearer.values,['给自己']);assert.deepEqual(needs.category.values,['手链']);assert.deepEqual(needs.occasion.values,['上班']);assert.deepEqual(needs.style.values,['简单','低调']);
 assert.deepEqual(everydayNeeds([{customer:'工作累，周末还要带孩子。'}],'想随便聊聊'),{});
});
test('chat actions stay none even with an existing offer; scripted goals are stages not prerecorded replies',()=>{
 for(const text of ['工作特别烦','不想细说家事','你家孩子怎样？'])assert.deepEqual(allowedMerchantActions({offer:{sku:'DEMO-A'}},{intent:'consult',reply:text}),['none']);
 assert.match(buyerStageGoal({}, {id:'family_chat'},4),/不要自己重述答案/);
 assert.match(buyerStageGoal({}, {id:'work_chat'},3),/leave/);
});
test('a completed early customer exit does not count as full everyday scenario coverage',async()=>{
 const {everydayCoverage}=await import('../lib/customer-duel.mjs');
 const run={scenarioId:'family_chat',status:'completed',turns:[{receptionFocus:{key:'family_chat'},merchant:{reply:'先休息。'}}]};
 assert.deepEqual(everydayCoverage(run).missing,['privacy_boundary','resume_selection']);assert.equal(everydayCoverage(run).complete,false);
 run.turns.push({receptionFocus:{key:'privacy_boundary'},merchant:{reply:'好。'}},{receptionFocus:{key:'resume_selection'},merchant:null});assert.equal(everydayCoverage(run).complete,false);
 run.turns.at(-1).merchant={reply:'记得。'};assert.equal(everydayCoverage(run).complete,true);
});
test('unseen natural topic boundaries are recognized without stealing a mixed product question',()=>{
 for(const text of ['这个不方便说。','换个话题吧。','家里的事先不提了。','算了，不说了。'])assert.equal(everydayFocus(text)?.key,'privacy_boundary',text);
 assert.equal(everydayFocus('先不聊家事了，DEMO-A多少钱？'),null);
 assert.equal(everydayFocus('我不想买这款'),null);
});
test('boundary generation omits the closed topic but preserves current words and speaking style without canned answers',async()=>{
 const {everydayReplyRequest}=await import('../lib/everyday-conversation.mjs');
 const data={buyer:{message:'这个不方便说。'},style:{warmth:'热情直接'},recentConversation:[{customer:'孩子闹',shop:'辛苦了'}],catalog:[{sku:'DEMO-A'}]};
 const request={model:'same-local-model',messages:[{role:'system',content:'original rules'},{role:'user',content:JSON.stringify(data)}]};
 const focused=everydayReplyRequest(request,everydayFocus(data.buyer.message));
 assert.equal(focused.model,request.model);assert.equal(focused.messages.length,3);
 const input=JSON.parse(focused.messages[1].content);assert.equal(input.buyer.message,data.buyer.message);assert.deepEqual(input.style,data.style);assert.deepEqual(input.allowedActions,['none']);assert.equal(input.recentConversation,undefined);assert.equal(input.catalog,undefined);
 assert.ok(!JSON.stringify(focused.messages).includes('孩子闹'));assert.ok(Buffer.byteLength(JSON.stringify(focused.messages))<2400);assert.deepEqual(JSON.parse(request.messages[1].content),data);
 assert.equal(everydayReplyRequest(request,everydayFocus('给自己选手链')),request);
});
test('customer departure uses the merchant role and keeps actions at none without closed-topic history',async()=>{
 const {everydayReplyRequest}=await import('../lib/everyday-conversation.mjs');
 const request={model:'local',messages:[{role:'system',content:'long business context'},{role:'user',content:JSON.stringify({buyer:{message:'好，我先去忙了。'},style:{length:'简短'},recentConversation:[{customer:'家里的事先不提了'}]})}]};
 const focused=everydayReplyRequest(request,{key:'customer_exit'});assert.match(focused.messages[0].content,/以店主身份简短道别/);const input=JSON.parse(focused.messages[1].content);assert.equal(input.buyer.message,'好，我先去忙了。');assert.deepEqual(input.allowedActions,['none']);assert.equal(input.recentConversation,undefined);assert.equal(input.receptionFocus.key,'customer_exit');
});
test('everyday chat generation has no catalog and cannot turn old needs into an unsolicited recommendation',async()=>{
 const {everydayReplyRequest}=await import('../lib/everyday-conversation.mjs');const focus=everydayFocus('孩子太闹，有点累。');
 const original={messages:[{role:'system',content:'old business context'},{role:'user',content:JSON.stringify({buyer:{message:'孩子太闹，有点累。'},style:{warmth:'自然'},catalog:[{sku:'DEMO-A'}],currentCustomerNeeds:{style:{values:['低调']}}})}]};const out=everydayReplyRequest(original,focus),data=JSON.parse(out.messages[1].content);assert.equal(data.catalog,undefined);assert.equal(data.currentCustomerNeeds,undefined);assert.deepEqual(data.allowedActions,['none']);assert.match(out.messages[0].content,/没有问商品/);assert.ok(everydayIssues(focus,'DEMO-A更适合日常上班，线条简洁。',{type:'none'}).length);assert.deepEqual(everydayIssues(focus,'先歇会儿吧。',{type:'none'}),[]);
});
