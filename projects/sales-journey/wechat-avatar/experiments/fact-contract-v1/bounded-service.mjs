// Local draft demonstration. Scenario is explicitly selected, not inferred from free text.
export const BOUNDED_SCENARIOS={
 selection_general:{label:'自用选购搭配',direct:'可以先按平时常穿的衣服和佩戴场合，比较首饰的轮廓、大小与装饰繁简。这里先聊选择方向，不代表已经找到对应商品。',warm:'咱们先看它能不能搭上常穿的衣服，再比较轮廓和细节，不急着定。具体款式还需要商品资料，先不假装已经找到了。'},
 gift_general:{label:'送礼选择',direct:'送礼先围绕收礼人的习惯比较，不用按买家自己的喜好决定。已说清的条件继续保留，具体商品、价格和库存仍需核实。',warm:'送礼咱们就围着收礼人的习惯挑，不急着证明贵的就好。先聊她会不会愿意戴，具体款式和价格还得核实。'},
 repair_general:{label:'维修或补配咨询',direct:'先围绕现有首饰的问题处理，不转成选新款。是否能修或补配，需要核对原件情况和门店承接规则，暂时不能承诺结果或费用。',warm:'知道了，先处理原来这件的问题。能不能修、怎么收费，还得看原件和门店规则，现在先不打包票。'},
 care_unknown:{label:'保养咨询，资料待核',direct:'保养方法要结合实际材质、结构和商品说明确认。这里没有核准资料，先不建议自行用清洁剂、加热或其他具体处理办法。',warm:'先把材质和原来的保养说明弄清楚，再定处理办法。现在资料还没核实，咱们不猜具体清洁方法。'},
 return_unknown:{label:'退换规则待核',direct:'退换要核对购买凭证、商品情况和门店规则。目前没有核准政策，不能保证能退换、免费或已经受理。',warm:'这件事得结合购买凭证、商品情况和店里的规则确认。现在还不能答应能退换，也不能说已经办理。'},
 recycle_unknown:{label:'回收规则待核',direct:'是否承接回收、如何核价，需要核对门店规则和原件资料。目前不能承诺回收，也不能给估价或保值保证。',warm:'回收这件事先不猜价格。店里是否承接、原件如何核价，都还需要核实，现在不能先答应。'},
 chat_general:{label:'普通家常闲聊',direct:'可以，咱们先聊这个。你接着说。',warm:'好呀，先不急着聊首饰，咱们就说说这个。'},
 repair_missing:{label:'耳堵缺失：先保管原件',direct:'先暂停佩戴，把耳钉妥善放好。补配要核对原件款式和门店资料，暂时不能保证配到。',warm:'先别急着戴，把耳钉收好。能不能补配，还得核对款式和门店资料，现在不先给你保证。'},
 repair_replace:{label:'只补原配件：不转改款',direct:'明白，你只想补回原耳堵。接下来需要核对耳钉的款式编号或购买凭证，是否能配到还要确认。',warm:'知道了，就围绕原耳堵补配聊，不换款。你手边有款式编号或购买凭证吗？能否补配还不能保证。'},
 ring_size:{label:'送礼圈号未知：不乱猜尺寸',direct:'想保留惊喜可以理解，但圈号不知道，现在不能判断戒指是否合适。先有可靠的尺寸依据再选，不先买偏大或偏小，也不默认能调圈或退换。',warm:'想给她惊喜，这个心意我明白。圈号还不清楚，咱们先不猜大小；有可靠的尺寸依据，再决定戒指，调圈和退换条件也得先核实。'},
 mood:{label:'心情不好：暂停销售',direct:'今天先不选了也没关系。事情不顺已经够烦了，这会儿不用急着做决定。',warm:'那今天先不选了，给自己一点缓缓的时间。想简单聊两句也可以，不急着做决定。'},
 privacy:{label:'不问原因：尊重聊天边界',direct:'好，不问原因，也不分析。咱们就简单聊两句。',warm:'行，不追问，你不用解释。就轻松说两句。'},
 pause:{label:'今天不买：自然收住',direct:'好，今天先不买，按你的节奏来。',warm:'没事，先放一放，不用今天决定。'},
 remodel_discover:{label:'已有首饰：了解改款目标',direct:'明白，是想把现有这条重新设计一下。你主要想换个戴法，还是调整现在的样子？',warm:'知道了，是现有首饰想改款。你最想改变的是戴法，还是整体样子？'},
 remodel_preserve:{label:'保留原玉：不重复问风格',direct:'先按保留原玉、简洁日常这个方向整理。现在这条是串珠，还是带吊坠的？具体能否这样改，还需要看原件并核实。',warm:'那先记住保留原玉、简单日常这个方向。你现在这条是串珠还是带吊坠的？能否改成还要看原件，先不保证。'},
 remodel_estimate:{label:'改款报价：明确核实边界',direct:'能不能改、费用和工期，需要结合原件、设计方案和门店核准资料确认。目前只能先整理改款诉求，不能给确定承诺。',warm:'这个不能先给你打包票。得看原件和设计方案，再核实门店的费用、工期及承接能力，现在先把想怎么改说清楚。'}
};
export const BOUNDED_SCHEMA={type:'object',additionalProperties:false,required:['variantId'],properties:{variantId:{type:'string',enum:['direct','warm']}}};
export async function boundedReply({scenario,message,choose,model}){
 if(typeof scenario!=='string'||!Object.hasOwn(BOUNDED_SCENARIOS,scenario)||typeof message!=='string'||!message.trim()||message.length>800)throw Error('Invalid bounded input');
 const entry=BOUNDED_SCENARIOS[scenario];let variantId='direct',choiceAccepted=false,error=null;
 try{
  const raw=await choose({model,response_format:{type:'json_object'},messages:[{role:'system',content:'只在两个已有回复中选择更适合客户表达的一项。禁止写新回复。只输出JSON，唯一字段variantId，只能是direct或warm。场景由操作者明确指定，不推断其他场景。'}, {role:'user',content:JSON.stringify({customerMessage:message,selectedScenario:entry.label,allowedReplies:{direct:entry.direct,warm:entry.warm}})}]});
  const value=JSON.parse(raw.choices?.[0]?.message?.content);
  if(!value||Object.keys(value).length!==1||!['direct','warm'].includes(value.variantId))throw Error('Invalid wording choice');
  variantId=value.variantId;choiceAccepted=true;
 }catch{error='模型选择未通过或请求失败，使用固定参考表达；未采用模型自由文本。';}
 return{reply:entry[variantId],scenario,variantId,choiceAccepted,error,source:'fixed_draft_service_expression',simulation:true,sent:false,profileChanged:false,autoScenarioRecognized:false,merchantApproved:false,ownerVoiceVerified:false};
}
