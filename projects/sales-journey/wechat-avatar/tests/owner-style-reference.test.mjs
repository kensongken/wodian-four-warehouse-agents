import test from 'node:test';import assert from 'node:assert/strict';import {ownerStyleView} from '../web/persona-studio.js';
test('style-reference presentation distinguishes quoted owner fragments from owner approval of a generated reply and escapes content',()=>{
 const html=ownerStyleView({styleSamplesUsed:[{source:'approved_voice',expression:'可以看看<script>x</script>',excerpted:true}]});assert.ok(html.includes('已确认本人问答'));assert.ok(html.includes('原话片段'));assert.ok(html.includes('不代表本人已经认可'));assert.ok(!html.includes('<script>'));assert.ok(html.includes('&lt;script&gt;'));assert.equal(ownerStyleView({}),'');assert.equal(ownerStyleView({styleSamplesUsed:[]}),'');
});
