import AppKit
// Offline diagnostic surface. No WeChat, network, account or clipboard access.
final class ProbeDelegate:NSObject,NSApplicationDelegate {
  var window:NSWindow!;var field:NSTextField!;var result:NSTextField!;var actions=0
  func applicationDidFinishLaunching(_ notification:Notification){
    // Real AppKit text editors use Edit-menu actions for Command-A/V.
    // Omitting this menu makes a synthetic window falsely fail shortcut/paste tests.
    let menu=NSMenu();let appItem=NSMenuItem();menu.addItem(appItem)
    let appMenu=NSMenu();appMenu.addItem(withTitle:"退出离线诊断",action:#selector(NSApplication.terminate(_:)),keyEquivalent:"q");appItem.submenu=appMenu
    let editItem=NSMenuItem();menu.addItem(editItem);let editMenu=NSMenu(title:"编辑")
    editMenu.addItem(withTitle:"剪切",action:#selector(NSText.cut(_:)),keyEquivalent:"x")
    editMenu.addItem(withTitle:"拷贝",action:#selector(NSText.copy(_:)),keyEquivalent:"c")
    editMenu.addItem(withTitle:"粘贴",action:#selector(NSText.paste(_:)),keyEquivalent:"v")
    editMenu.addItem(withTitle:"全选",action:#selector(NSText.selectAll(_:)),keyEquivalent:"a")
    editItem.submenu=editMenu;NSApp.mainMenu=menu
    window=NSWindow(contentRect:NSRect(x:240,y:240,width:760,height:480),styleMask:[.titled,.closable,.miniaturizable],backing:.buffered,defer:false)
    window.title="销售分身 · 离线界面诊断";window.isReleasedWhenClosed=false
    let view=NSView(frame:NSRect(x:0,y:0,width:760,height:480));view.wantsLayer=true;view.layer?.backgroundColor=NSColor(calibratedRed:0.12,green:0.45,blue:0.32,alpha:1).cgColor;window.contentView=view
    let heading=NSTextField(labelWithString:"离线模拟聊天 · 不连接微信");heading.frame=NSRect(x:28,y:416,width:700,height:35);heading.font=NSFont.boldSystemFont(ofSize:24);heading.textColor = .white;heading.setAccessibilityIdentifier("probe_heading");view.addSubview(heading)
    let message=NSTextField(labelWithString:"合成客户：想选一条小巧的手链。");message.frame=NSRect(x:28,y:330,width:700,height:40);message.textColor = .white;view.addSubview(message)
    field=NSTextField(frame:NSRect(x:28,y:210,width:700,height:70));field.placeholderString="仅用于验证输入框，不向外发送";field.setAccessibilityIdentifier("probe_input");view.addSubview(field)
    let button=NSButton(title:"记录模拟操作",target:self,action:#selector(record));button.frame=NSRect(x:28,y:142,width:200,height:42);button.bezelStyle = .rounded;button.setAccessibilityIdentifier("probe_button");view.addSubview(button)
    result=NSTextField(wrappingLabelWithString:"模拟操作次数：0；外发消息：0");result.frame=NSRect(x:28,y:30,width:700,height:100);result.textColor = .white;result.setAccessibilityIdentifier("probe_result");view.addSubview(result)
    window.makeKeyAndOrderFront(nil);NSApp.activate(ignoringOtherApps:true)
  }
  @objc func record(){
    let text=field.stringValue.trimmingCharacters(in:.whitespacesAndNewlines)
    guard !text.isEmpty else {result.stringValue="模拟操作次数：\(actions)；外发消息：0\n输入为空，未记录";return}
    actions += 1;result.stringValue="模拟操作次数：\(actions)；外发消息：0\n已记录：\(text)";field.stringValue=""
  }
  func applicationShouldTerminateAfterLastWindowClosed(_ sender:NSApplication)->Bool {true}
}
let app=NSApplication.shared;let delegate=ProbeDelegate();app.delegate=delegate;app.setActivationPolicy(.regular);app.run()
