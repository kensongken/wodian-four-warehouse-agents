import {readFileSync,writeFileSync} from 'node:fs';import assert from 'node:assert/strict';import {ClarifyingDialogue} from './dialogue.mjs';import {EvidenceLedger} from './contract.mjs';
const source=JSON.parse(readFileSync(new URL('./evidence/routing-v3-comparison.json',import.meta.url)));
const failures=source.rows.filter(x=>x.arm==='v3'&&!x.matches);assert.equal(failures.length,2);
const actor={storeId:'demo',customerId:'demo',conversationId:'demo-chat'};const records=[];
for(const failure of failures){let calls=0;
 const dialogue=new ClarifyingDialogue({scope:actor,model:'recorded-route',complete:async()=>{calls++;return failure.output.trace.response;},ledger:new EvidenceLedger(),resolveBinding:async()=>({storeId:'demo',customerId:'demo',subjects:{inventory:'product',order:'order',owner_presence:'owner',delivery_today:'product'}}),lookupEvidence:async()=>null});
 const first=await dialogue.turn(actor,{type:'message',message:failure.message});
 const selected=await dialogue.turn(actor,{type:'confirm',token:first.clarification.token,topics:failure.expected});
 assert.equal(calls,1);assert.deepEqual(selected.route.topics,failure.expected);assert.ok(selected.parts.every(x=>x.state==='unknown'));assert.equal(selected.sent,false);
 records.push({customerMessage:failure.message,recordedWrongRoute:failure.output.route,firstReply:first.reply,offeredChoices:first.choices,simulatedCustomerSelection:failure.expected,finalReply:selected.reply,finalStates:selected.parts.map(x=>({kind:x.kind,state:x.state})),additionalModelCallsForConfirmation:0});
}
const result={scope:'offline replay of two previously recorded model misroutes; customer choices simulated, not real user/WeChat interaction',newInferenceCalls:0,realWechatCalls:0,records};writeFileSync(new URL('./evidence/dialogue-replay.json',import.meta.url),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
