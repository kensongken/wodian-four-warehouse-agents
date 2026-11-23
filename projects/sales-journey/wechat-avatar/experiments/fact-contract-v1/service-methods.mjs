// Draft service methods: no merchant approval, catalogue, pricing, capability or policy facts.
export const SERVICE_METHODS={
 remodel:{title:'原有首饰改款',status:'draft',steps:{
  discover:{goal:'了解原物改款目标',do:'接住客户已有首饰想重新设计；只补一个真正缺少的目标或原件结构信息。',avoid:'不转成新购，不保证能改，不猜原件形态。'},
  preserve:{goal:'按已知保留部分和目标继续',do:'保留已明确的原玉、日常或简洁方向；下一步了解尚未明确的原件结构，不重问风格。',avoid:'不把方向说成已经可执行的方案，不保证效果或舒适度。'},
  estimate:{goal:'核对改款的可行性、费用与工期边界',do:'如实说明需原件、设计方案及门店核准资料；当前只能整理诉求。',avoid:'不给金额、天数或能改的保证，不假装已安排评估或联系工厂。'}
 }},
 aftercare:{title:'已购首饰售后',status:'draft',steps:{
  missing_part:{goal:'配件缺失时先避免继续佩戴风险',do:'建议先暂停佩戴、妥善保管原件，再了解款式或配件信息，以便后续核对补配可能。',avoid:'不建议细线、小夹子等临时固定后佩戴；不保证能补配、免费或有现货。'},
  replace_part:{goal:'接住只想补配原配件的纠正',do:'明确按原耳钉补配耳堵这个诉求聊；最多核对款式编号或凭证，不重问佩戴风格。',avoid:'不转成换款改款，不声称系统已能查询或办理，不保证补配成功。'},
  receipt:{goal:'使用已有凭证继续核对',do:'客户有小票就按已有凭证说明下一步，不强迫先找订单号；是否满足售后条件需门店核实。',avoid:'不保证小票能精准匹配结构，不保证免费修、退换或赔付。'}
 }},
 gift:{title:'送礼选择与尺寸顾虑',status:'draft',steps:{
  known_preferences:{goal:'按收礼人已知偏好给选择办法',do:'不再问已明确的佩戴人、品类、用途、预算或风格；给一个可观察的比较标准，如整体轮廓与装饰繁简。',avoid:'不把买家的喜好当收礼人的喜好，不把预算说成已核实商品售价或有货。'},
  unknown_size:{goal:'处理戒指圈号未知与惊喜之间的取舍',do:'先回应想保留惊喜，再说明圈号未知就不能判断合适；可先确认佩戴尺寸的可靠依据，再决定是否选戒指。',avoid:'不建议先买偏大或偏小，不假定通用圈号、可调圈或能退换；不鼓励秘密测量身体或冒用资料。'},
  pause:{goal:'客户暂不买时收住',do:'尊重先听收礼人意见的决定，简短收尾。',avoid:'不追加推荐，不以错过、稀缺或亲情施压，不继续追问。'}
 }},
 everyday:{title:'家常与情绪边界',status:'draft',steps:{
  mood:{goal:'暂停销售，接住当前心情',do:'只回应当下处境，简短体谅；不急着分析或解决，不要求客户回来购买。',avoid:'不拿情绪引导消费，不编造本人经历，不保证解决私人问题。'},
  privacy:{goal:'尊重不想解释、不想被分析的边界',do:'不问原因、不做心理分析，自然表达本轮可以简单聊几句。',avoid:'不命令冷静，不暗示长期或随时陪伴，不虚构本人私事。'},
  resume:{goal:'客户明确恢复选购后接回',do:'只按最新明确需求继续，未改内容保留；不要把之前聊家事当成还在拒绝选购。',avoid:'不强迫继续聊隐私，不虚构客户有待改款的旧首饰。'}
 }}
};
export function serviceMethodRequest(request,methodId,stepId){
 if(!Object.hasOwn(SERVICE_METHODS,methodId)||!Object.hasOwn(SERVICE_METHODS[methodId].steps,stepId))throw Error('Invalid service method');
 const method=SERVICE_METHODS[methodId],step=method.steps[stepId];
 const note={role:'system',content:'以下是系统编写、尚待门店审核的服务办法草稿，只能指导一般接待，不证明门店实际能力或政策。方法与步骤由实验预先指定，不代表模型能自动识别。\n'+JSON.stringify({method:method.title,...step})};
 const messages=[...request.messages.slice(0,-1),note,request.messages.at(-1)];
 if(Buffer.byteLength(JSON.stringify(messages),'utf8')>7000)throw Error('Invalid service method context size');
 return{...request,messages};
}
