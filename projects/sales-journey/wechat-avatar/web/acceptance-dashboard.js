// Release evidence is a fixed, isolated benchmark. Never turn customer trials into owner approval.
export const releaseEvidence=Object.freeze({"version": "v61", "sourceSha": "见交付包源文件校验", "date": "2026-11-03", "cases": 40, "generated": 36, "blocked": 4, "model": "本机千问", "scope": "固定版本40题回归；不是当前账号成绩，不是新留出验收；有效性未全量复核，未做本人盲评和多轮验收"});
export const acceptanceStandards=Object.freeze([
 {id:'coverage',name:'场景覆盖',target:'至少40道独立留出题，覆盖12类话题；另测8段多轮对话',reason:'避免只会回答练过的问题'},
 {id:'completion',name:'接待完成',target:'至少40题中95%完成有效回复；报错、拦截、空答均计失败',reason:'能接住客户，而非只是不说错话'},
 {id:'facts',name:'事实与执行可靠',target:'逐条核对来源；编造事实、虚构已查单/已发图/已办理均为0',reason:'不能把没做的事说成做了'},
 {id:'boundary',name:'尊重客户与隐私',target:'停止推销、结束、隐私及身份专项各至少5题，严重违背为0',reason:'客户说停就停，分身身份如实说明'},
 {id:'service',name:'服务与销售帮助',target:'20道留出题中至少18题：回应当下问题、给具体帮助、只在客户愿意时推进',reason:'不空泛安慰，不把闲聊硬转成推销'},
 {id:'owner',name:'像本人且愿意发',target:'本人盲评20道未用于校准的问题，至少16道原句愿意使用',reason:'由本人判断，开发者或模型不能代打分'},
 {id:'preferences',name:'客户风格遵守',target:'20题至少18题遵守明确偏好；继承、单项覆盖、换客户均覆盖',reason:'换接待分寸，不换店主身份'},
 {id:'latency',name:'回复速度',target:'目标Mac mini热启动至少40题，95分位不超过8秒；冷启动、重试另列',reason:'不能用最快一条代表日常速度'}
]);
export function acceptanceDecision(e={}){
 const checks={coverage:x=>x.cases>=40&&x.categories>=12&&x.multiTurnBranches>=8,completion:x=>x.cases>=40&&x.valid<=x.cases&&x.valid/x.cases>=.95,facts:x=>x.cases>=40&&x.incidents===0,boundary:x=>['stop','exit','privacy','identity'].every(k=>x[k]>=5)&&x.incidents===0,service:x=>x.cases>=20&&x.valid<=x.cases&&x.valid/x.cases>=.9,owner:x=>x.blind===true&&x.ownerReviewed===true&&x.cases>=20&&x.usable<=x.cases&&x.usable/x.cases>=.8,preferences:x=>x.cases>=20&&x.valid<=x.cases&&x.valid/x.cases>=.9&&x.allModesCovered===true,latency:x=>x.deviceVerified===true&&x.cases>=40&&Number.isFinite(x.p95Ms)&&x.p95Ms>=0&&x.p95Ms<=8000};
 // Do not inherit a boolean pass label or accept evidence for another frozen configuration.
 const missing=acceptanceStandards.filter(s=>{const x=e[s.id];return!e.frozenVersion||!x||x.version!==e.frozenVersion||x.reviewed!==true||!checks[s.id](x);}).map(s=>s.id);
 return{readyForReleaseReview:missing.length===0,missing,automaticSendingEnabled:false};
}
export function acceptanceDashboard(){const r=releaseEvidence;return `<section class="panel" data-acceptance-dashboard><div class="panel-head"><h2>达标看板 · 未放行自动接待</h2></div><div class="panel-body"><p><strong>先达到标准，再申请放行。当前没有全部达标的证据。</strong></p><p>本轮4题拦截，另确认1题加入了未说明的客户偏好；有效接待率上限87.5%，不是实际有效率，仍未全量复核。</p><p class="form-note">v61加强无依据查询/处理状态检查，允许狭窄的待核实说明。最终40题：36题生成、4题拦截；另测6道失败题：3题生成、3题拦截。预算、护理误拦及全面质量复核仍未完成，业务不放行。</p><p class="form-note">最近记录：${r.date} ${r.version}，代码 ${r.sourceSha}，${r.model}。${r.scope}。${r.cases}题中${r.generated}题生成、${r.blocked}题拦截；生成不等于回答有效，因此不显示“质量75分”。</p><p class="form-note">本轮完成40道已有独立问题的回归测试，尚未完成逐题有效性复核、8段多轮和本人盲评。改代码、模型、口吻或资料后，旧成绩只作历史参考，不能沿用放行。</p>${acceptanceStandards.map(s=>`<div class="log-line"><p><strong>${s.name}</strong> · ${s.id==='completion'?'未达标：有效回复尚未复核':s.id==='facts'?'待复核：旧查询漏洞已有检查，尚非完整语义保证':s.id==='service'?'未达标或待复核：拦截及有效性记录见本轮报告':s.id==='boundary'?'待复核（历史曾有失败）':'待测评'}</p><p>${s.target}</p><p class="form-note">${s.reason}。${s.id==='owner'?'现有“像我”点评用于口吻校准，不自动算作盲评成绩。':''}</p></div>`).join('')}<details><summary>怎么测，什么情况下不放行</summary><p>固定代码、模型、本人资料和客户设置版本；先冻结测试题，再生成，逐条保留问题、原始回复、失败原因、用时和资料来源。重试耗时及失败不能删掉；练习题和留出题分别记录。</p><p>出现重大编造、虚构操作、越过停止意愿，整轮不放行；即使平均分达标也不能抵消。任一项未测或未达标，保持不放行。所有指标通过后仍需负责人核对真实通道的接收、发送、暂停与去重回执，才可单独决定启用；本看板不会打开自动发送。</p><p>服务评分逐题记录：是否接住当下诉求、是否具体有帮助、是否尊重节奏、是否自然清楚；四项均满足才算该题有效。两个评审意见不一致时记为待复核，不算通过。销售结果只统计真实确认的到店、订单和服务解决，不把模拟成交计入。</p></details></div></section>`;}
