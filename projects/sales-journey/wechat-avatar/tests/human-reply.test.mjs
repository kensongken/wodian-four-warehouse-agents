import test from'node:test';import assert from'node:assert/strict';
import{humanReplyRequest,humanReplyIssues}from'../lib/human-reply.mjs';
test('human expression guidance preserves source data, facts, role and model without adding fake owner examples',()=>{
 const original={model:'local',messages:[{role:'system',content:'事实不得编造。'},{role:'user',content:JSON.stringify({ownerVoice:['你可以先试试'],buyer:{message:'我怕选错'}})}]};const copy=structuredClone(original),out=humanReplyRequest(original);
 assert.deepEqual(original,copy);assert.equal(out.model,original.model);assert.equal(out.messages[1].content,original.messages[1].content);assert.match(out.messages[0].content,/事实不得编造/);assert.match(out.messages[0].content,/原有事实与业务限制优先/);assert.match(out.messages[0].content,/不编生活经历/);
});
test('empty suggestions and formal service slogans are flagged while short meaningful replies remain natural',()=>{
 for(const text of ['可以看看','你可以先试试。','欢迎下次光临。','感谢您的咨询。'])assert.ok(humanReplyIssues(text).length,text);
 for(const text of ['先看线条简单的，上班戴不会太显眼。','好，你先忙。','没事，咱们慢慢选。','明白了。'])assert.deepEqual(humanReplyIssues(text),[],text);
});
test('chit-chat avoids canned emotional summaries without banning honest service discussion',()=>{
 const reply='听起来最近挺辛苦的，孩子闹腾确实让人疲惫。';assert.ok(humanReplyIssues(reply,{focus:{key:'family_chat'}}).length);assert.ok(humanReplyIssues(reply,{focus:{key:'work_or_mood'}}).length);assert.deepEqual(humanReplyIssues('先歇会儿，别着急。',{focus:{key:'family_chat'}}),[]);assert.deepEqual(humanReplyIssues('这处确实需要先核实资料。'),[]);
});
test('a new question cannot receive the exact same recommendation while brief boundary acknowledgments remain valid',()=>{
 assert.ok(humanReplyIssues('A线条简单，适合低调方向。',{previousReply:'A线条简单，适合低调方向。',previousCustomer:'给自己选手链',customer:'两个有什么区别？'}).length);
 assert.deepEqual(humanReplyIssues('明白了。',{focus:{key:'privacy_boundary'},previousReply:'明白了。',previousCustomer:'不方便说',customer:'不聊了'}),[]);
});
test('idle consultation keeps needs without product nudges and preserves transaction limits',async()=>{
 const {focusedNaturalConsultation}=await import('../lib/human-reply.mjs');const input={buyer:{intent:'consult',message:'我怕闲置。'},catalog:[{sku:'DEMO-A',features:'线条简单'}],currentCustomerNeeds:{occasion:{values:['上班']}},ledger:{order:null},ownerVoice:[{expression:'可以看看'}],style:{warmth:'自然'},allowedActions:['none','offer'],recentConversation:[]};const request={model:'local',messages:[{role:'system',content:'long instructions'},{role:'user',content:JSON.stringify(input)}]};const out=focusedNaturalConsultation(request),data=JSON.parse(out.messages[1].content);assert.equal(data.naturalGuidanceFocus,'idle_concern');assert.deepEqual(data.catalog,[]);assert.equal(out.selectionAdviceEvidence.candidates[0].sku,'DEMO-A');assert.deepEqual(data.currentCustomerNeeds,input.currentCustomerNeeds);assert.deepEqual(data.allowedActions,['none']);assert.equal(data.ownerVoice,undefined);assert.match(out.messages[0].content,/不编佩戴效果/);assert.ok(humanReplyIssues('适合上班 wear',{customer:'上班戴'}).length);
 input.ledger.order={status:'paid'};request.messages[1].content=JSON.stringify(input);assert.equal(focusedNaturalConsultation(request),request);
});
test('literal selection needs cannot hijack a current family or work conversation',async()=>{
 const {focusedNaturalConsultation}=await import('../lib/human-reply.mjs');
 for(const key of ['family_chat','work_or_mood','privacy_boundary']){const request={messages:[{role:'system',content:'original'},{role:'user',content:JSON.stringify({buyer:{intent:'consult',message:'孩子太闹，有点累。'},catalog:[{sku:'DEMO-A'}],currentCustomerNeeds:{occasion:{values:['上班']},style:{values:['低调']}},receptionFocus:{key},ledger:{order:null}})}]};assert.equal(focusedNaturalConsultation(request),request);}
});
test('mixed transaction or policy questions retain the full business context instead of becoming style advice',async()=>{
 const {focusedNaturalConsultation}=await import('../lib/human-reply.mjs');
 for(const message of ['A和B选哪个，能退款吗？','两款多少钱？','我怕闲置，有没有退换？']){const request={messages:[{role:'system',content:'business verification rules'},{role:'user',content:JSON.stringify({buyer:{intent:'consult',message},catalog:[{sku:'DEMO-A'}],currentCustomerNeeds:{style:{values:['低调']}},ledger:{order:null}})}]};assert.equal(focusedNaturalConsultation(request),request);}
});
test('comparison context retains customer quotes without letting a previous merchant pitch become the next answer',async()=>{
 const {focusedNaturalConsultation}=await import('../lib/human-reply.mjs');const request={messages:[{role:'system',content:'rules'},{role:'user',content:JSON.stringify({buyer:{intent:'consult',message:'A和B选哪个？'},catalog:[{sku:'DEMO-A',features:'简单低调'},{sku:'DEMO-B',features:'设计感'}],currentCustomerNeeds:{style:{values:['低调']}},allowedActions:['none'],ledger:{order:null},recentConversation:[{customer:'喜欢低调',shop:'上一轮固定推荐句'}]})}]};const out=focusedNaturalConsultation(request),data=JSON.parse(out.messages[1].content);assert.deepEqual(data.recentConversation,[{customer:'喜欢低调'}]);assert.ok(!JSON.stringify(out).includes('上一轮固定推荐句'));assert.equal(data.selectionAdvice.recommendedSku,'DEMO-A');
});
test('comparison metadata stays outside the model input and compact facts remain available',async()=>{
 const {focusedNaturalConsultation}=await import('../lib/human-reply.mjs');const {demoCatalog}=await import('../lib/customer-duel.mjs');
 const input={buyer:{intent:'consult',message:'A和B怎么选？'},catalog:demoCatalog,currentCustomerNeeds:{style:{values:['简单','低调']}},style:{warmth:'自然直接',length:'简短'},allowedActions:['none','offer'],ledger:{order:null},recentConversation:[]};const out=focusedNaturalConsultation({messages:[{role:'system',content:'rules'},{role:'user',content:JSON.stringify(input)}]});const data=JSON.parse(out.messages[1].content);assert.deepEqual(data.catalog,demoCatalog);assert.equal(data.selectionAdvice.recommendedSku,'DEMO-A');assert.equal(data.selectionAdvice.candidates,undefined);assert.equal(out.selectionAdviceEvidence.candidates.length,2);assert.ok(Buffer.byteLength(JSON.stringify(out.messages))<3000);
});
