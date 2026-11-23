import test from'node:test';import assert from'node:assert/strict';
import{selectionAdvice,selectionReplyIssues}from'../lib/selection-advice.mjs';import{demoCatalog}from'../lib/customer-duel.mjs';
const needs=(values,excluded=[])=>({style:{values,excluded}});
test('literal style matches use catalog evidence and reverse direction when the stated preference changes',()=>{
 const catalog=structuredClone(demoCatalog),before=structuredClone(catalog);const a=selectionAdvice(catalog,needs(['简单','低调'])),b=selectionAdvice(catalog,needs(['设计感','宽一点']));assert.equal(a.recommendedSku,'DEMO-A');assert.equal(b.recommendedSku,'DEMO-B');assert.deepEqual(a.candidates[0].matched,['简单','低调']);assert.equal(a.candidates[0].features,catalog[0].features);assert.deepEqual(catalog,before);
});
test('unknowns and ties do not become arbitrary recommendations or inferred thinness',()=>{
 for(const values of [[],['细一点'],['低调','设计感'],['低调','细一点']])assert.equal(selectionAdvice(demoCatalog,needs(values)).recommendedSku,null,values.join(','));
 const excluded=selectionAdvice(demoCatalog,needs(['设计感'],['设计感']));assert.equal(excluded.recommendedSku,null);assert.deepEqual(excluded.candidates[1].conflicts,['设计感']);
 const sold=structuredClone(demoCatalog);sold[1].stock=0;assert.equal(selectionAdvice(sold,needs(['设计感'])).recommendedSku,null);
});
test('the v45 non-answer and wrong-direction pitch fail while a grounded choice with a contrast passes',()=>{
 const a=selectionAdvice(demoCatalog,needs(['简单','低调']));
 for(const text of ['DEMO-B轮廓宽一些，戴手上更有存在感。','B有设计感，A简单。','别选A，我推荐B，B更有设计感。','我会选A。','A线条简单，但我推荐B，B有设计感。'])assert.ok(selectionReplyIssues(text,a,'compare').length,text);
 for(const text of ['我会选DEMO-A，线条简单低调；B轮廓宽些，有设计感。','A吧，比B简单，低调一点。','A简单低调，B有设计感。','DEMO-A线条简洁，适合上班戴。DEMO-B轮廓较宽，设计感强。','DEMO-A线条简洁，适合上班戴。DEMO-B设计感强，但可能不够低调。'])assert.deepEqual(selectionReplyIssues(text,a,'compare'),[],text);
 const b=selectionAdvice(demoCatalog,needs(['设计感','宽一点']));assert.deepEqual(selectionReplyIssues('先看DEMO-B，轮廓较宽有设计感；A是简洁低调方向。',b,'compare'),[]);
 assert.deepEqual(selectionReplyIssues('先歇会儿。',a,'family_chat'),[]);assert.deepEqual(selectionReplyIssues('更在意低调还是设计感？',selectionAdvice(demoCatalog,needs(['低调','设计感'])),'compare'),[]);
});
test('idle concerns need use-oriented help and simple lines cannot become unsupported thinness',()=>{
 const a=selectionAdvice(demoCatalog,needs(['简单','低调'])),b=selectionAdvice(demoCatalog,needs(['设计感','宽一点']));
 assert.ok(selectionReplyIssues('DEMO-A线条简洁，适合上班戴。',a,'idle_concern').length);
 for(const text of ['你平时上班会戴手链吗？','你上班时手链会戴吗？','不急着买，先想想有没有会戴的场合。'])assert.deepEqual(selectionReplyIssues(text,a,'idle_concern'),[]);
 assert.ok(selectionReplyIssues('先看B，轮廓较宽有设计感；A整体偏细。',b,'compare').length);
 assert.deepEqual(selectionReplyIssues('先看B，轮廓较宽有设计感；A线条简洁低调。',b,'compare'),[]);
});
