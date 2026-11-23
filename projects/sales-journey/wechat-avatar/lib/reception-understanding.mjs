// Explicit, auditable intent coverage. Unknown phrasing stays on the existing route;
// this is not a general semantic model or permanent customer personality inference.
const rules=[
 {intent:'privacy',key:'privacy_boundary',re:/别再问.{0,8}(?:家庭|家人|孩子)|(?:不想|不方便).{0,8}(?:谈|聊|说).{0,8}(?:家里|家庭)|孩子的事先不聊|家里的事.{0,6}(?:不说|不聊)/u,goal:'尊重本话题边界',instruction:'客户只是不想谈家事，没有结束整段聊天。只自然确认不再追问这个话题，不说先忙、再见或聊到这里，不转回购物，不盘问。'},
 {intent:'pause_sales',key:'sales_pause',re:/别再.{0,5}(?:劝|推销|推荐)|希望自己决定|不(?:要|想).{0,5}(?:劝我|被推销)/u,goal:'停止劝买',instruction:'客户要自己决定，本轮停止劝买。简短尊重她的决定，不再提问、不说不嫌问多、不荐款、不让她继续看看，不替她结束其他话题。'},
 {intent:'aftercare_resolution',key:'aftercare',re:/不接受继续修|不想再修|不愿.{0,3}修|处理.{0,4}(?:这件事|售后)|(?:退货|退款|退钱).{0,8}(?:答应|条件|处理)/u,goal:'核实售后处理条件',instruction:'客户在说售后处理，不是选款。先尊重她希望解决问题的意思，说明具体处理条件尚待核实，最多补问一个问题细节。不能提版型或选款，不答应或拒绝，不报期限，不说我帮你查查、已查到或没处理，不承诺以后联系。可以说退款条件需要核实，不复述金额。'},
 {intent:'store_information',key:'store_information',re:/店铺位置|门店位置|(?:地址|位置).{0,6}(?:到店|营业|时间)|(?:哪里|在哪).{0,8}(?:店|门店)|到店时间/u,goal:'回答门店资料的可确认范围',instruction:'客户问门店位置或到店时间。当前没有核准资料，明确说明这些资料还需核实，不能给出地址、时间或附近门店，也不反问什么时候来，不说我帮你找店。只说明资料尚未核准，不承诺等一下或以后补消息。不要告别或安慰问多。'},
 {intent:'purchase_steps',key:'purchase',re:/建单|单子建好|购买步骤|替我.{0,6}单子/u,goal:'说明购买步骤尚待核实',instruction:'客户希望购买或建单。当前没有订单工具和核准购买步骤。明确说明具体购买信息尚待核实，最多补问具体哪款。不能声称能建单、查单、已选中商品、已经安排或保证匹配，不结束聊天。'},
 {intent:'owner_status',key:'owner_life_status',re:/(?:你|老板).{0,12}(?:中午吃|早饭|晚饭|在店里|在外面)|你(?:儿子|女儿).{0,10}(?:成绩|考试)|你这会儿是在/u,goal:'如实说明分身不掌握本人近况',instruction:'客户关心店主本人实时近况。自然说明现在由AI分身接话，具体本人近况不清楚。不猜位置、忙闲、吃饭和家人情况，不声称查询过，也不安排店主回复。'},
 {intent:'gift_relationship',key:'gift',re:/送同事.{0,12}(?:感谢|欠人情)|给姐姐.{0,12}(?:礼物|刻意)/u,goal:'帮客户把握送礼分寸',instruction:'客户在意送礼的人情分寸。先接住不想给对方压力这点，围绕对方明确喜好或日常佩戴给通用方向，缺资料最多问一个收礼人偏好。不谈问多不嫌烦，不问买家自己喜欢什么，不假定收礼人的性格与喜好，不承诺商品或价格。'},
 {intent:'emotional_chat',key:'work_or_mood',re:/聊点别的.{0,8}别推荐|先听我说.{0,8}(?:一会|可以)|今天心里乱/u,goal:'继续听客户说',instruction:'客户希望继续聊天或被倾听。自然表示愿意听，不替她结束对话，不说先忙，不推荐购物，也不用不嫌问多的套话。'},
 {intent:'idle_concern',key:'idle_concern',re:/日常.{0,8}(?:用不用得上|用不上)|心动.{0,8}闲置/u,goal:'帮助判断日常使用机会',instruction:'客户担心闲置。本轮直接给一个判断办法：结合她常穿衣服或真实日常场合看看有无使用机会。信息未提供时最多补问一项。不说需要核实，不保证会常戴，不许催买，不虚构穿搭习惯。'}
];
export function understandReception(question){const r=rules.find(r=>r.re.test(question));return r?{version:1,intent:r.intent,quote:question,goal:r.goal,source:'explicit_rule_coverage',completeSemanticUnderstanding:false,plan:{key:r.key,label:r.goal,instruction:r.instruction,source:'explicit_understanding_v1',customerQuote:question}}:null;}
export function understandingIssues(u,reply){if(!u)return[];const issues=[];
 if(['store_information','purchase_steps'].includes(u.intent)&&!/(?:核实|核准|确认|不清楚|未知|还没|尚未)/u.test(reply))issues.push('本轮缺少核准业务资料，必须自然说明尚待核实，不假装已掌握步骤或门店信息。');
 if(['privacy','emotional_chat','store_information','purchase_steps'].includes(u.intent)&&/先忙|先走|再见|聊到这|先聊到|有空再聊/u.test(reply))issues.push('客户没有要求结束，不替客户结束对话。');
 if(['pause_sales','gift_relationship','emotional_chat'].includes(u.intent)&&/不嫌|嫌你|多问|你问.{0,8}(?:越多|就行)|问多/u.test(reply))issues.push('本轮不是担心问多，不套用不嫌问多的安慰。');
 if(u.intent==='pause_sales'&&/[?？]|推荐|看看|试试|选款/u.test(reply))issues.push('客户要求停止劝买，只接住自主决定，不追加选款或追问。');
 if(u.intent==='owner_status'&&!/不知道|不清楚|不了解|不详|未知|无法确认|不能确认/u.test(reply))issues.push('本轮必须坦诚表达不知道本人实时近况，不能说在店里或因为保密而不告诉。');
 if(u.intent==='aftercare_resolution'&&/版型|选款|搭配|款式|对比|再决定/u.test(reply))issues.push('客户在说售后，不要答成选款或版型比较；回应处理诉求并说明条件尚待核实。');
 if(u.intent==='idle_concern'&&!/日常|平时|衣服|场合|穿|搭|用得上|使用/u.test(reply))issues.push('本轮需要给日常使用的判断办法，不能用核实或空泛安慰代替。');
 return issues;
}
