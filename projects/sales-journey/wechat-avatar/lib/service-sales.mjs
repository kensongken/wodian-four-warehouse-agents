import {serviceScenario} from './service-curriculum.mjs';
// Bounded, assistant-designed reception choices. Signals are quoted, not customer diagnoses.
const approaches={
 choose:{label:'帮助理解选择方向',instruction:'客户在问普通选择问题，结合明确需求给一个通用比较重点；缺少真正必要的信息才问一项，不编造具体商品。'},
 recap:{label:'整理最新明确需求',instruction:'只回顾客户现在明确想要的佩戴对象、品类、场景与风格，后来说的修改覆盖同类旧要求，没改变的对象品类保留。已经明确放弃的旧场景不要并列成当前需求。别继续荐款，不反问，不补未说过的偏好。earlierCustomerNotes在先，recentCustomerStatements在后，本轮原话最后。'},
 update_preferences:{label:'接住新需求',instruction:'顺着客户现在明确的新场景或偏好说话，修改同类旧要求，保留未改的对象与品类。不要把旧场景与新场景混成同时想要，不编造商品。'},
 pause:{label:'给客户空间，结束本轮',instruction:'客户现在要暂停。只表达确认暂停、让她按自己的节奏来，用温和的完整短句。系统设计示例：好，今天先不聊，你慢慢来。可以依本人语气改写；该示例不是本人原话。本轮不提问、不荐款、不追加销售理由，不要求回来或购买。'},
 compare_shops:{label:'尊重比较，帮客户看清标准',instruction:'客户想比较，先尊重这个选择，当下给一个与已知穿戴需求有关的比较标准，例如线条细节与日常衣服搭不搭；不能只说先考虑或等比较完再帮。不贬低别家、不反问去哪家、不要求现在决定。'},
 reassure:{label:'先让客户安心，再帮她理清',instruction:'客户明确担心问多了麻烦或不懂被笑。用店主身份说明我不嫌客户问多，不让客户反过来安慰店主，不说不嫌烦就好，不从问多了推断她真想买或有购买诚意。自然说明可以慢慢问、不评判她。关系顾虑得到回应就足够，本轮不必硬加选款或追问；客户同时问具体问题时再帮她理清，不以专业压人。'},
 repair:{label:'接住反馈，把建议讲具体',instruction:'客户说之前没讲清。简短认领后，直接解释一个可观察的选择差别和理由，让她知道先看哪里、为什么符合已说的需求。按本轮selectionFocus和currentNeeds决定比较重点，不把所有人都答成线条干净或配衬衫。只用客户明确说过的场景，没说的衣服不猜。不反问想看搭配还是场合，不再让她先选风格，不只重复简单、日常、适合这些词。只给通用观察办法，不编造尺寸或假装找到了具体商品。'},
 deliberate:{label:'接住喜欢，也尊重犹豫',instruction:'客户怕冲动或闲置，给一个能判断的使用检查点，不替她证明值得买。系统设计示例：先看能不能搭上平时常穿的衣服，暂时想不到怎么搭就先放着。这是演练办法，不是本人原话，不要求照抄。若客户明确只在聚会等偶尔场合戴，就按这个场景判断，不强求日常或天天戴，也不替她补上显眼、别太日常等新偏好。只说选款方向，禁止用这条、这款、它指向未提供的商品。不保证最实用、不会闲置，不反问。'},
 advance:{label:'顺着意向，推进一个小步骤',instruction:'客户已认可方向并问下一步。基于已知需求给一个低压力、可立即理解的比较或确认步骤，不再从头问用途风格；没有商品数据就不假装已找款、发图、预约或下单，不跨到成交承诺。'}
};
export function interpretedServiceGoal(key,question){
 if(typeof key!=='string'||!Object.hasOwn(approaches,key))return null;
 return{key,...approaches[key],customerQuote:question,source:'local_model_suggested_service_goal'};
}
export function serviceSalesPlan(question){
 const scenario=serviceScenario(question);if(scenario)return{...scenario,label:approaches[scenario.key].label,customerQuote:question};
 let key;
 if(/别.{0,4}催|不要.{0,4}催|不想.{0,5}(?:聊|说|选|买)|今天.{0,5}先不买|想静静|让我自己.{0,4}(?:看|逛|想)/u.test(question))key='pause';
 else if(/比较.{0,4}(?:几家|别家)|多看.{0,4}(?:几家|别家)|别家.{0,5}(?:看看|比较)/u.test(question))key='compare_shops';
 else if(/嫌我烦|问.{0,4}太多|问.{0,6}这么多|会不会.{0,8}(?:麻烦|嫌.{0,2}烦)|怕.{0,6}麻烦你|不懂.{0,8}(?:笑|丢人)|会不会.{0,5}笑话/u.test(question))key='reassure';
 else if(/(?:太|很|有点).{0,2}(?:笼统|敷衍)|没.{0,3}说清|还是不知道怎么选/u.test(question))key='repair';
 else if(/(?:怕|担心).{0,5}冲动|怕.{0,8}(?:买|买回去).{0,8}(?:不戴|闲置|放.{0,3}抽屉)|喜欢.{0,10}(?:犹豫|拿不准|不敢决定)/u.test(question))key='deliberate';
 else if(/(?:接下来|下一步).{0,7}(?:看什么|怎么选|怎么挑|怎么比较|做什么)|就按这个方向/u.test(question))key='advance';
 if(!key)return null;
 return{key,...approaches[key],customerQuote:question,source:'assistant_designed_service_sales_policy'};
}
export function serviceSalesIssues(guidance,reply){
 if(!guidance)return[];
 const issues=[];
 if(/(?:^|[，,。；;])(?:你|我)?妈的/u.test(reply))issues.push('商业接待请用完整亲属称呼，例如家人、妈妈或母亲，避免单字简称造成冒犯。仍按客户已明确的喜好给建议。');
 if(guidance.scenario==='missing_visual'&&/左边.{0,6}(?:好|更|适合)|右边.{0,6}(?:好|更|适合)|图(?:片|里).{0,6}(?:看起来|明显)|我看到/u.test(reply))issues.push('没有图片内容，不能声称看到了图或替客户选左边右边。只补清缺失的视觉信息。');
 if(guidance.scenario==='self_doubt'&&/你(?:的)?(?:手|皮肤|肤色|手指).{0,6}(?:漂亮|好看|白皙|显白)/u.test(reply))issues.push('没有见过客户外貌，不能用未经观察的赞美来安慰。接住顾虑，按明确喜好讨论方向。');
 if(/配得上[你您]|[你您].{0,4}配得上|衬得起|买得起/u.test(reply))issues.push('不要评价客户的身份、身价或配不配得上。把判断放回她已说的日常穿搭与使用需求。');
 if(/(?:我|我们)(?:给|帮|为)[你您].{0,2}(?:挑|选|找|发|整理)/u.test(reply))issues.push('当前没有商品检索或发图执行结果，不承诺我给你挑、找或发。直接给当前就能理解的比较标准或帮助。');
 if(guidance.key==='reassure'&&/问.{0,8}(?:说明|证明|代表).{0,8}(?:想买|想选|会买|要买|购买|诚意)/u.test(reply))issues.push('问多不代表购买意愿，不推测客户真想买或有购买诚意；简短说明不麻烦，可以问清楚再决定。');
 if(guidance.key==='reassure'&&/(?:不嫌.{0,2}烦就(?:好|行)|你别嫌我烦)/u.test(reply))issues.push('客户担心麻烦店主；用店主身份明确不嫌客户问多，不把不嫌烦的责任交回客户，不说不嫌烦就好。');
 if(guidance.key==='reassure'&&!/没事|没关系|不麻烦|不.{0,4}嫌|不会|怎么会|当然不|不觉得|放心|安心|踏实|慢慢|尽管问|随时问|随便问|不用.{0,4}(?:怕|担心)|问清楚|正常/u.test(reply))issues.push('客户担心被嫌烦或被笑，要先自然接住这点，再给帮助；不能直接冷冰冰地荐款。');
 if(/你想太多|别想太多|别(?:再)?纠结|别矫情|这有什么好纠结|不买就|买不起|别人都买|这都不懂|你不懂就别|你不懂别|现在不买|错过就|(?:挑|买)了(?:就)?别(?:放|闲置|后悔)/u.test(reply))issues.push('不能贬低、责怪或用别纠结打发客户，也不能用别人都买、错过来施压。担心闲置时，帮她看日常会不会用，不说挑了就别放或买了别后悔。');
 if(guidance.key==='pause'&&(/[?？]|看看|试试|挑|选|推荐|款式/u.test(reply)))issues.push('客户要停下，本轮只尊重收住，不提问、不追加选款或销售建议。');
 if(guidance.key==='pause'&&/你(?:静静|冷静)(?:就好|一下)|你去(?:静静|冷静)/u.test(reply))issues.push('不要命令客户去静静或冷静。自然接住她想暂停的意思，把聊天节奏交还给她。');
 if(guidance.key==='compare_shops'&&/别家.{0,8}(?:差|不行|不好|坑|骗人)|(?:只有|还是)我们.{0,8}(?:好|靠谱)/u.test(reply))issues.push('尊重客户比较，不贬低其他店，也不无依据宣称本店更好。');
 if(guidance.key==='compare_shops'&&/(?:等你|选完|看完|比完).{0,10}(?:再|帮)/u.test(reply))issues.push('不要把帮助推到客户去别家比较完之后。现在给一个具体比较标准，保持客户自己决定。');
 if(guidance.key==='compare_shops'&&(!/比|看|留意/u.test(reply)||!/线条|细节|搭|风格|日常|穿|戴/u.test(reply)))issues.push('本轮需要给一个与日常穿戴有关的比较标准，不能只说先考虑、慢慢比。');
 if(guidance.key==='recap'&&/[?？]/u.test(reply))issues.push('本轮只回顾最新明确需求，不继续问客户或推销。');
 if(guidance.key==='deliberate'&&/(?:别|不用|不必|无需)(?:再)?担心(?:会)?闲置|(?:不会|不可能|肯定不|绝对不)(?:会)?(?:闲置|放抽屉|后悔)/u.test(reply))issues.push('不能替客户保证不会闲置或让她别担心闲置。给一个按实际穿搭或使用场景能判断的检查点，想不到用处就先放着。');
 if(guidance.key==='repair'&&/你.{0,10}(?:想|在意|看|选).{0,24}还是/u.test(reply)&&/[?？]/u.test(reply))issues.push('客户已经说没听懂，不把比较任务重新交给她二选一。直接讲一个选择差别和理由，不重复盘问。');
 if(['repair','deliberate','advance'].includes(guidance.key)&&!/[?？]|可以|先|看|挑|选|对比|比较|留意|想想|平时|日常|穿|戴|线条|细节/u.test(reply))issues.push('客户在问如何判断或下一步，不能只用一句安慰带过；给一个小帮助，缺信息才问一项。');
 return issues;
}
