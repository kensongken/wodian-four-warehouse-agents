import test from 'node:test';import assert from 'node:assert/strict';
import {miniReadiness} from '../lib/mini-readiness.mjs';
const native={platform:'macOS',nativeVersion:'1.0.7',wechatVersion:'4.1.15',wechatInstalled:true,wechatRunning:true,accessibility:true,screenRecording:true,inputMonitoring:true};
test('full permissions never substitute for personal-WeChat live acceptance',()=>{
 const r=miniReadiness({native,osVersion:'26.6.2',memoryBytes:16*1024**3});assert.equal(r.status,'preflight_passed_live_acceptance_pending');assert.equal(r.canSendAutomatically,false);assert.equal(r.memoryGiB,16);assert.equal(r.wechatMessagesSent,0);
});
test('missing or truthy permission strings fail the preflight',()=>{
 for(const v of [false,'true',undefined]){const r=miniReadiness({native:{...native,accessibility:v},osVersion:'26.6.2'});assert.equal(r.status,'preflight_blocked');assert.ok(r.preflightBlockers.includes('辅助功能权限'));}
});
test('unexpected native fields and version payloads never enter the report',()=>{
 const r=miniReadiness({native:{...native,image:'PRIVATE_IMAGE',token:'PRIVATE_TOKEN',nodes:['PRIVATE_CHAT'],wechatVersion:'PRIVATE_VERSION'},osVersion:'PRIVATE_OS'});assert.doesNotMatch(JSON.stringify(r),/PRIVATE/);assert.equal(r.canSendAutomatically,false);
});
