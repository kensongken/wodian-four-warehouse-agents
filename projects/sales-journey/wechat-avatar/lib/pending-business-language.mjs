// Local preview only: remove narrowly defined pending-information clauses from
// topic-word scanning. This grants no transaction permission and changes no reply.
export function pendingBusinessRiskText(text,task){
 if(!['budget','aftercare','care','store_information','purchase'].includes(task))return text;
 const noun='(?:具体|相关|对应|这款的)?(?:退款条件|退货条件|退换条件|处理条件|处理方式|购买步骤|购买信息|护理说明|保养说明|材质|价格|价位|报价|证书)';
 const pending='(?:还|尚)?(?:需要|需|待|有待|得|要)(?:先)?(?:核实|确认|核准)(?:一下|清楚)?';
 return text.replace(new RegExp(`${noun}${pending}(?=[，,。；;！？!?]|$)`,'gu'),'资料待核实')
  .replace(new RegExp(`${pending}${noun}(?=[，,。；;！？!?]|$)`,'gu'),'资料待核实');
}
