import test from 'node:test';import assert from 'node:assert/strict';
import {routingRequest,receive,parseRoute} from './conversation.mjs';import {routingRequestV2,ROUTE_SCHEMA} from './routing-v2.mjs';import {EvidenceLedger} from './contract.mjs';
test('v2 preserves customer data and requires both fields without free reply keys',()=>{
 const q='客户原话';assert.deepEqual(routingRequest('m',q).messages[1],routingRequestV2('m',q).messages[1]);
 assert.deepEqual(ROUTE_SCHEMA.required,['topics','socialAct']);assert.equal(ROUTE_SCHEMA.additionalProperties,false);
});
test('grammar shape never replaces semantic validation',()=>{
 assert.throws(()=>parseRoute({choices:[{message:{content:'{"topics":["order"],"socialAct":"greeting"}'}}]}),/schema/);
});
test('candidate builder still cannot inject arbitrary text into final response',async()=>{
 const r=await receive({message:'订单好了没',model:'m',requestBuilder:routingRequestV2,complete:async()=>({choices:[{message:{content:'{"topics":[],"socialAct":"greeting","reply":"订单完成"}'}}]}),ledger:new EvidenceLedger(),binding:{},lookupEvidence:async()=>null});
 assert.equal(r.source,'safe_route_fallback');assert.ok(!r.reply.includes('完成'));
});
