import {EvidenceLedger} from './contract.mjs';
const l=new EvidenceLedger({clock:()=>150});
const context={storeId:'demo',customerId:'demo',subjectId:'earring-demo',kind:'inventory'};
const guessed={state:'available',evidenceId:'model-invented',evidenceVersion:1};
const withoutEvidence=l.reply(context,guessed,{tone:'warm'});
l.ingest({...context,id:'demo-snapshot',version:1,state:'available',source:'inventory_snapshot',issuedAt:100,expiresAt:200,simulation:true});
const withEvidence=l.reply(context,{...guessed,evidenceId:'demo-snapshot'},{tone:'warm'});
const delivery=l.reply({...context,kind:'delivery_today'},{state:'confirmed',evidenceId:'demo-snapshot',evidenceVersion:1});
console.log(JSON.stringify({scope:'仅模拟结构化状态请求，不是旧模型文本的自动识别或微信联测',withoutEvidence,withEvidence,stockDoesNotAuthorizeDelivery:delivery},null,2));
