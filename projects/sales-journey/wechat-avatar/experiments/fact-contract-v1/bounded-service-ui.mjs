const currentPath=typeof location!=='undefined'?location.pathname:'';
const topicEndpoint=currentPath==='/two-step'?'/api/two-step-topic':currentPath==='/topic-frame'?'/api/service-topic-frame':'/api/service-topic';
const $=id=>document.getElementById(id);
$('run').onclick=async()=>{
 if(!$('scenario').value||!$('message').value.trim()){$('status').textContent='先选择已核对的场景，再填客户原话。';return;}
 lock(true);$('status').textContent='模型正在选择表达，不会自由生成服务建议…';$('reply').textContent='';$('receipt').textContent='';
 try{const response=await fetch('/api/bounded-service',{method:'POST',headers:{'Content-Type':'application/json','X-Sandbox':'1'},body:JSON.stringify({scenario:$('scenario').value,message:$('message').value})});const r=await response.json();if(!response.ok)throw Error(r.error);$('reply').textContent=r.reply;$('status').textContent=r.choiceAccepted?'已采用模型选择的固定表达。':r.error;$('receipt').textContent='固定草稿 · 场景人工选定 · 未发送微信 · 未更新档案 · 门店未核准';}catch(e){$('status').textContent=e.message||'本次未完成，请主动重试。';}finally{lock(false);}
};
let proposed=null,adopted=false;const history=[];
function lock(value){document.querySelectorAll('button,select,textarea').forEach(e=>e.disabled=value);}
function clearRenderedReply(){ $('reply').textContent='';$('receipt').textContent='';$('status').textContent=''; }
$('message').oninput=()=>{clearRenderedReply();proposed=null;$('use-proposal').hidden=true;$('proposal').textContent='';if(adopted){$('scenario').value='';adopted=false;}};
$('suggest').onclick=async()=>{
 const message=$('message').value.trim();if(!message){$('proposal').textContent='先填客户原话。';return;}clearRenderedReply();proposed=null;adopted=false;$('scenario').value='';$('use-proposal').hidden=true;lock(true);$('proposal').textContent='正在提出服务方向，尚未选择场景…';
 try{const response=await fetch(topicEndpoint,{method:'POST',headers:{'Content-Type':'application/json','X-Sandbox':'1'},body:JSON.stringify({message,context:history.slice(-4)})});const r=await response.json();if(!response.ok)throw Error(r.error);proposed=r;$('proposal').textContent='待核对方向：'+r.label+(r.quote?'；依据原话：'+r.quote:'；当前意思仍不明确，请补充。')+'。原文匹配不代表理解正确，不会自动应用。';$('use-proposal').hidden=!r.suggestedScenario;history.push(message);history.splice(0,Math.max(0,history.length-4));}catch(e){$('proposal').textContent=e.message||'本轮没有有效建议。';}finally{lock(false);}
};
$('use-proposal').onclick=()=>{if(!proposed?.suggestedScenario)return;$('scenario').value=proposed.suggestedScenario;adopted=true;$('status').textContent='已按你的核对选择场景；可再点测试表达选择。';};

$('scenario').onchange=()=>{clearRenderedReply();adopted=false;};

if(topicEndpoint.includes('frame'))$('page-title').textContent='新版试验：区分聊天内容与购物意愿（仍需核对）';

if(currentPath==='/two-step')$('page-title').textContent='两步理解试验：先认清本轮需求，再判断服务方向（仍需核对）';
