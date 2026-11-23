import {Problem} from './engine.mjs';
export const loginRequiredMessage='微信处于登录或安全确认页面，已停止读取。请先确认手机和桌面的登录状态，稳定后再手动重试；本次未识别、导入或发送。';
export function requireChatPreview(output){
  const {width,height}=output;
  if(!Number.isFinite(width)||!Number.isFinite(height)||width<500||height<300)throw new Problem(loginRequiredMessage,409);
  const nodes=Array.isArray(output.nodes)?output.nodes:[];
  const messagePaths=nodes.filter(n=>n.identifier==='chat_message_list').map(n=>n.nodeId).filter(Boolean);
  const outside=nodes.filter(n=>n.identifier!=='chat_bubble_item_view'&&!messagePaths.some(p=>n.nodeId===p||n.nodeId?.startsWith(p+'.')));
  for(const node of outside){
    const text=[node.title,node.value,node.description].filter(v=>typeof v==='string').join(' ').replace(/\s/g,'');
    if(node.identifier==='current_login_nick_name'||(/为了你的账号安全/.test(text)&&/重新登录/.test(text))||/需在手机上完成登录|扫描二维码登录|扫码登录微信/.test(text)||node.role==='AXButton'&&['进入微信','切换账号','仅传输文件'].includes(node.title))throw new Problem(loginRequiredMessage,409);
  }
}
