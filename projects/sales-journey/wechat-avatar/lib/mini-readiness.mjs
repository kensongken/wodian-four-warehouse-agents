// Metadata only. Do not copy arbitrary native output, screenshots or chat data.
export function miniReadiness({native={},osVersion='',memoryBytes=0,now=Date.now()}={}) {
  const checks=[
    ['macOS版本', /^\d+\.\d/.test(osVersion)&&Number(osVersion.split('.')[0])>=14],
    ['Mac执行组件', native.platform==='macOS'&&/^\d+\.\d+\.\d+$/.test(native.nativeVersion||'')],
    ['已安装微信', native.wechatInstalled===true],
    ['微信正在运行', native.wechatRunning===true],
    ['辅助功能权限', native.accessibility===true],
    ['屏幕录制权限', native.screenRecording===true],
    ['输入监控权限', native.inputMonitoring===true],
  ].map(([name,passed])=>({name,passed}));
  const passed=checks.every(c=>c.passed);
  return {
    schema:1,checkedAt:new Date(now).toISOString(),timezone:'Asia/Taipei',
    status:passed?'preflight_passed_live_acceptance_pending':'preflight_blocked',
    platform:'macOS',osVersion:/^\d+(?:\.\d+){1,2}$/.test(osVersion)?osVersion:'unknown',
    memoryGiB:Number.isFinite(memoryBytes)&&memoryBytes>0?Math.round(memoryBytes/1024**3):null,
    wechatVersion:/^\d+(?:\.\d+){1,3}$/.test(native.wechatVersion||'')?native.wechatVersion:'unknown',
    checks,preflightBlockers:checks.filter(c=>!c.passed).map(c=>c.name),
    canSendAutomatically:false,
    liveAcceptancePending:['实际联系人身份核对','新消息和收发方向识别','输入框与发送操作定位','视觉服务配置及实际定位','本人测试会话收发、防重和人工接管','持续运行与重连'],
    screenshotsCaptured:0,wechatMessagesSent:0,externalModelCalls:0,chatContentIncluded:false,
    note:'权限和组件通过不代表个人微信自动收发已验收。本检查不请求权限、不打开或操作微信、不读取客户账本。',
  };
}
