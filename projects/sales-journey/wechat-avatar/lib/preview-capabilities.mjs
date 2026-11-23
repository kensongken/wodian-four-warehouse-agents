// Preview has no order, search, dispatch or physical presence tools. Failures are not sends.
export const PREVIEW_CAPABILITY_VERSION='preview-capabilities-v1';
export const previewCapabilityInstruction='当前是没有外部执行工具的试聊。只能聊天、讨论通用方向或引用核准资料；不能承诺建单、查单、找附近门店、找款、发图、办理或回访，不说已处理或正在处理。你是分身，没有本人实时位置、忙闲和家庭近况；没有同分支历史时不说记得客户旧事。缺少信息直接说未知或尚待核实，不假装执行。';
export function previewCapabilityRequest(request){return{...request,messages:request.messages.map(m=>m.role==='system'?{...m,content:m.content+' '+previewCapabilityInstruction}:m)};}
export function previewCapabilityIssues(reply,{hasBranchHistory=false}={}){
 const issues=[];
 const capabilityText=reply.replace(/(?:不能|无法|没法|不支持|没有|没)(?:帮|给|替)?[你您]?(?:建单|建个单|查单|下单|找款|发图|办理|回访|查查|查一下|查询|查看|核查|调取|检索)/gu,'无法执行');
 if(/(?:我|我们).{0,5}(?:帮|给|替|来).{0,12}(?:建单|建个单|查单|下单|找款|找.{0,6}(?:店|款)|看看.{0,6}(?:店|合适款)|发图|办理|回访)|(?:已|已经|正在|还没).{0,5}(?:处理完|建单|查单|下单|转交|发图|办理)|(?:等|待)我.{0,5}(?:核实|确认).{0,8}(?:再|给).{0,5}(?:回|联系)/u.test(capabilityText))issues.push('试聊没有外部执行工具，不承诺或暗示正在建单、查找门店、查单、发图、办理或回访；只讨论方向或说明尚待核实。');
 if(/我(?:现在|这会儿|今天|这边)?(?:不)?在(?:店里|外面|家里|门店)/u.test(reply))issues.push('分身不知道店主本人实时位置，不声称本人在店里、外面或家里。');
 if(/(?:店主|本人).{0,8}(?:正忙|在忙|没空|刚吃|吃过)|我(?:刚|今天|已经).{0,5}(?:吃过|吃了|吃完)|我家(?:的)?(?:孩子|儿子|女儿)/u.test(reply))issues.push('没有店主本人实时生活资料，不编造本人忙闲、吃饭或家庭近况。');
 if(/我(?:这边)?(?:还)?没(?:有)?查到|(?:等会|稍后|回头).{0,4}(?:再|给你).{0,3}(?:回你|回复|联系)/u.test(reply))issues.push('试聊没有查询或后续回访工具，不假装查过，也不承诺等会回复。');
 if(/(?:等一下|等会(?:儿)?|稍后|之后|以后|回头|过会(?:儿)?).{0,12}(?:给[你您]|告诉[你您]|回[你您]|回复|联系|消息|回音)/u.test(reply))issues.push('没有后续触达工具，不承诺稍后通知、消息或联系；只说明当前已知或尚待核实。');
 if(/(?:等|待)(?:信息|资料|这边|核实|确认).{0,12}(?:再|就).{0,4}(?:告诉|通知|回[你您]|联系|发(?:给|送|消息))/u.test(reply))issues.push('没有后续触达工具，不承诺资料齐全后另行通知；只说明当前尚待核实。');
 if(/(?:我|我们)(?:这边|现在|正在|先|来|可以|再|刚刚)?(?:帮[你您])?(?:查查|查一下|查询|查看|核查|查单|调取|检索)|(?:帮|给|替)[你您](?:查查|查一下|查询|查看|核查|查单|调取|检索)|没(?:有)?查到|(?:尚未|还没|暂时没|已经|正在)(?:有)?(?:查到|处理|办理|退款)/u.test(capabilityText))issues.push('没有执行回执或订单状态依据，不声称正在查询、查不到、尚未处理或承诺帮查；只说明不掌握信息、资料尚待核实。');
 if(!hasBranchHistory&&/(?:我|我们).{0,6}(?:记得|记着)|还记得.{0,8}(?:上次|之前|那次)/u.test(reply.replace(/我不(?:记得|记着)/gu,'未知')))issues.push('没有当前分支历史证明，不假装记得客户旧事；按本轮提供的信息回应。');
 return issues;
}
