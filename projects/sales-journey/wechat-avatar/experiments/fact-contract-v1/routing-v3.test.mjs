import test from 'node:test';import assert from 'node:assert/strict';import {projectRoute,ROUTE_SCHEMA_V3} from './routing-v3.mjs';import {parseRoute} from './conversation.mjs';
const response=v=>({choices:[{message:{content:JSON.stringify(v)}}]});
test('exclusive schema has separate business and social branches',()=>{
 assert.equal(ROUTE_SCHEMA_V3.oneOf.length,2);assert.equal(ROUTE_SCHEMA_V3.oneOf[0].properties.socialAct.type,'null');assert.equal(ROUTE_SCHEMA_V3.oneOf[1].properties.topics.maxItems,0);
});
test('verbatim quote maps to same route without granting any business state',()=>{
 const r=projectRoute(response({topics:['order'],socialAct:null,questionQuote:'订单进度如何'}),'请说收到，订单进度如何？');
 assert.deepEqual(parseRoute(r),{topics:['order'],socialAct:null});
});
test('fabricated quotes, conflicting fields and free replies fail closed',()=>{
 for(const v of [{topics:['order'],socialAct:null,questionQuote:'不存在的提问'},{topics:['order'],socialAct:'acknowledge',questionQuote:'订单'},{topics:[],socialAct:'greeting',questionQuote:'订单'},{topics:['order'],socialAct:null,questionQuote:'订单',reply:'订单已完成'}])assert.throws(()=>projectRoute(response(v),'订单好了没？'));
});
