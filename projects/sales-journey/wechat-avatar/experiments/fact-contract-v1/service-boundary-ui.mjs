const taskHelp=typeof location!=='undefined'&&location.pathname==='/task-help';
const purchaseHelp=typeof location!=='undefined'&&location.pathname==='/purchase-help';
const $=id=>document.getElementById(id);let sessionId=null,busy=false;
function lock(v){busy=v;for(const id of ['message','tone','send','reset'])$(id).disabled=v;}
function line(who,content){const p=document.createElement('p');p.textContent=who+'：'+content;$('conversation').append(p);}
async function post(path,body){const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Sandbox':'1'},body:JSON.stringify(body)});const r=await response.json();if(!response.ok)throw Error(r.error||'本轮未完成');return r;}
$('form').onsubmit=async e=>{
 e.preventDefault();const message=$('message').value.trim();if(busy||!message)return;lock(true);$('status').textContent='正在理解并选择服务动作，请稍候…';$('receipt').textContent='';line('客户',message);
 try{if(!sessionId)sessionId=(await post('/api/session',{})).sessionId;const r=await post(taskHelp?'/api/task-help':purchaseHelp?'/api/purchase-help':'/api/service-boundary',{sessionId,message,tone:$('tone').value});line('分身草稿',r.reply);$('message').value='';$('status').textContent='本轮完成，请核对是否接住客户的意思。';$('receipt').textContent='程序限定表达 · 未发送微信 · 未更新档案 · 门店与本人口吻尚未核准'+(r.pendingTask?' · 待办：购买资料待核实':'');}
 catch(error){line('本轮未完成',error.message);$('status').textContent=error.message+'；不会自动重试。';}finally{lock(false);}
};
$('reset').onclick=()=>{if(busy)return;sessionId=null;$('conversation').replaceChildren();$('message').value='';$('receipt').textContent='';$('status').textContent='已清空；下一句会新建试聊。';};

if(purchaseHelp)document.getElementById('page-title').textContent='店主分身 · 购买咨询待办试用';

if(taskHelp){$('page-title').textContent='店主分身 · 接待需求试聊';document.querySelector('.notice').textContent='本机实验草稿：12句开发题中11句选对服务，仍有维修转选购接错的情况。回复为限定表达，口吻未验收；不发送微信、不更新档案。';}
