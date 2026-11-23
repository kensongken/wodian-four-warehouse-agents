import Foundation
import AppKit
import ApplicationServices
import ScreenCaptureKit
import Vision
import CryptoKit

// Local RPC helper. Status does not request permissions or open applications.
let allowedBundle = "com.tencent.xinWeChat"
let injectedMarker: Int64 = 0x53415632
let stateLock = NSLock()
var physicalEpoch: Int64 = 0
var stopEpoch: Int64 = 0
var tap: CFMachPort?
struct NodeRecord { let element: AXUIElement; let json: [String:Any] }
struct Frame { let id: String; let at: Double; let pid: pid_t; let windowID: CGWindowID; let bounds: CGRect; let epoch: Int64; let stopped: Int64; let nodes: [NodeRecord] }
var lastFrame: Frame?

enum BridgeError: Error { case blocked(String) }
func nowMS() -> Double { Date().timeIntervalSince1970 * 1000 }
func epochs() -> (Int64,Int64) { stateLock.lock(); defer { stateLock.unlock() }; return (physicalEpoch,stopEpoch) }
func fail(_ message:String) throws -> Never { throw BridgeError.blocked(message) }
func attr(_ element: AXUIElement, _ name:String) -> CFTypeRef? { var value: CFTypeRef?; guard AXUIElementCopyAttributeValue(element,name as CFString,&value) == .success else { return nil }; return value }
func str(_ element: AXUIElement,_ name:String) -> String { attr(element,name) as? String ?? "" }
func rect(_ element:AXUIElement) -> CGRect? {
  guard let p=attr(element,kAXPositionAttribute),let s=attr(element,kAXSizeAttribute),CFGetTypeID(p)==AXValueGetTypeID(),CFGetTypeID(s)==AXValueGetTypeID() else { return nil }
  var point=CGPoint.zero; var size=CGSize.zero
  AXValueGetValue(unsafeBitCast(p,to:AXValue.self),.cgPoint,&point); AXValueGetValue(unsafeBitCast(s,to:AXValue.self),.cgSize,&size)
  return CGRect(origin:point,size:size)
}
func asRect(_ r:CGRect) -> [String:Double] { ["x":r.origin.x,"y":r.origin.y,"width":r.width,"height":r.height] }
func app() throws -> NSRunningApplication {
  guard let a=NSRunningApplication.runningApplications(withBundleIdentifier:allowedBundle).first else { try fail("WECHAT_NOT_RUNNING") }
  guard NSWorkspace.shared.frontmostApplication?.processIdentifier == a.processIdentifier else { try fail("WECHAT_NOT_FRONTMOST") }
  return a
}
func ensureMonitoring() throws {
  if let existing=tap { guard CGEvent.tapIsEnabled(tap:existing) else { try fail("EVENT_MONITOR_DISABLED") }; return }
  guard CGPreflightListenEventAccess() else { try fail("INPUT_MONITORING_REQUIRED") }
  let types:[CGEventType] = [.keyDown,.leftMouseDown,.rightMouseDown,.otherMouseDown,.scrollWheel,.mouseMoved]
  let mask=types.reduce(CGEventMask(0)) { $0 | (CGEventMask(1) << $1.rawValue) }
  guard let t=CGEvent.tapCreate(tap:.cgSessionEventTap,place:.headInsertEventTap,options:.listenOnly,eventsOfInterest:mask,callback:{ _,type,event,_ in
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput { stateLock.lock(); stopEpoch += 1; stateLock.unlock() }
    else if event.getIntegerValueField(.eventSourceUserData) != injectedMarker { stateLock.lock(); physicalEpoch += 1; stateLock.unlock() }
    return Unmanaged.passUnretained(event)
  },userInfo:nil) else { try fail("EVENT_MONITOR_UNAVAILABLE") }
  tap=t
  DispatchQueue.global().async { let source=CFMachPortCreateRunLoopSource(kCFAllocatorDefault,t,0); CFRunLoopAddSource(CFRunLoopGetCurrent(),source,.commonModes); CGEvent.tapEnable(tap:t,enable:true); CFRunLoopRun() }
}
func collect(_ element:AXUIElement,_ path:String,_ depth:Int,_ out:inout [NodeRecord]) {
  if depth>16 || out.count>=1800 { return }
  let role=str(element,kAXRoleAttribute)
  let box=rect(element)
  var json:[String:Any] = ["nodeId":path,"role":role,"identifier":str(element,kAXIdentifierAttribute),"title":str(element,kAXTitleAttribute),"value":String(str(element,kAXValueAttribute).prefix(4000)),"description":str(element,kAXDescriptionAttribute),"roleDescription":str(element,kAXRoleDescriptionAttribute)]
  if let box=box { json["rect"]=asRect(box) }
  out.append(NodeRecord(element:element,json:json))
  if let children=attr(element,kAXChildrenAttribute) as? [AXUIElement] { for (i,c) in children.enumerated() { collect(c,"\(path).\(i)",depth+1,&out) } }
}
func hasLoginMarker(_ nodes:[[String:Any]]) -> Bool {
  let paths=nodes.filter{($0["identifier"] as? String)=="chat_message_list"}.compactMap{$0["nodeId"] as? String}
  for n in nodes {
    let identifier=n["identifier"] as? String ?? "";let path=n["nodeId"] as? String ?? ""
    if identifier=="chat_bubble_item_view" || paths.contains(where:{path==$0 || path.hasPrefix($0+".")}) { continue }
    let text=["title","value","description"].compactMap{n[$0] as? String}.joined().filter{!$0.isWhitespace}
    if identifier=="current_login_nick_name" || (text.contains("为了你的账号安全") && text.contains("重新登录")) || text.contains("需在手机上完成登录") || text.contains("扫描二维码登录") || text.contains("扫码登录微信") { return true }
    if (n["role"] as? String)=="AXButton",let title=n["title"] as? String,["进入微信","切换账号","仅传输文件"].contains(title) { return true }
  }
  return false
}
func requireChatWindow(_ bounds:CGRect,_ nodes:[[String:Any]]) throws {
  guard bounds.width>=500,bounds.height>=450,!hasLoginMarker(nodes) else { try fail("WECHAT_LOGIN_OR_SECURITY_REQUIRED") }
}
@available(macOS 14.0, *)
func image(_ windowID:CGWindowID) async throws -> (Data,Int,Int) {
  let content=try await SCShareableContent.excludingDesktopWindows(true,onScreenWindowsOnly:true)
  guard let window=content.windows.first(where:{$0.windowID==windowID}) else { try fail("WINDOW_DISAPPEARED") }
  let config=SCStreamConfiguration(); config.width=Int(window.frame.width);config.height=Int(window.frame.height);config.showsCursor=false
  let cg=try await SCScreenshotManager.captureImage(contentFilter:SCContentFilter(desktopIndependentWindow:window),configuration:config)
  let bitmap=NSBitmapImageRep(cgImage:cg)
  guard let png=bitmap.representation(using:.png,properties:[:]),png.count<=2*1024*1024 else { try fail("SCREENSHOT_TOO_LARGE") }
  return (png,cg.width,cg.height)
}
@available(macOS 14.0, *)
func snapshot(readOnly:Bool = false) async throws -> [String:Any] {
  guard AXIsProcessTrusted(),CGPreflightScreenCaptureAccess() else { try fail("ACCESSIBILITY_OR_SCREEN_PERMISSION_REQUIRED") }
  if !readOnly { try ensureMonitoring() }
  let a:NSRunningApplication
  if readOnly { let running=NSRunningApplication.runningApplications(withBundleIdentifier:allowedBundle);guard running.count==1 else { try fail("WECHAT_PROCESS_COUNT_\(running.count)") };a=running[0] }
  else { a=try app() }
  let application=AXUIElementCreateApplication(a.processIdentifier)
  guard let axWindows=attr(application,kAXWindowsAttribute) as? [AXUIElement] else { try fail("WECHAT_AX_WINDOWS_UNAVAILABLE") }
  let windowList=CGWindowListCopyWindowInfo([.optionOnScreenOnly,.excludeDesktopElements],kCGNullWindowID) as? [[String:Any]] ?? []
  let owned=windowList.filter{($0[kCGWindowOwnerPID as String] as? Int)==Int(a.processIdentifier) && ($0[kCGWindowLayer as String] as? Int)==0}
  func screenMatches(_ element:AXUIElement) -> [[String:Any]] {
    guard let box=rect(element) else { return [] }
    return owned.filter{item in
      guard let dictionary=item[kCGWindowBounds as String] as? [String:Any],let cg=CGRect(dictionaryRepresentation:dictionary as CFDictionary) else { return false }
      return abs(cg.minX-box.minX)<=2 && abs(cg.minY-box.minY)<=2 && abs(cg.width-box.width)<=2 && abs(cg.height-box.height)<=2
    }
  }
  let window:AXUIElement
  if readOnly {
    let titles=Set(["微信","WeChat"])
    let matches=axWindows.filter{element in
      let screens=screenMatches(element);guard screens.count==1 else { return false }
      return titles.contains(str(element,kAXTitleAttribute)) || titles.contains(screens[0][kCGWindowName as String] as? String ?? "")
    }
    guard matches.count==1 else { try fail("WECHAT_CHAT_WINDOW_REQUIRED_\(matches.count)") };window=matches[0]
  } else {
    guard axWindows.count==1 else { try fail("WECHAT_AX_WINDOW_COUNT_\(axWindows.count)") };window=axWindows[0]
  }
  guard let bounds=rect(window),bounds.width>100,bounds.height>100 else { try fail("WECHAT_WINDOW_BOUNDS_UNAVAILABLE") }
  // Refuse login-size windows before collecting or capturing image pixels.
  try requireChatWindow(bounds,[])
  let candidates=readOnly ? screenMatches(window) : owned
  guard candidates.count==1 else { try fail("WECHAT_SCREEN_WINDOW_COUNT_\(candidates.count)") }
  guard let number=candidates[0][kCGWindowNumber as String] as? UInt32 else { try fail("WECHAT_WINDOW_NUMBER_UNAVAILABLE") }
  var nodes:[NodeRecord]=[];collect(window,"w",0,&nodes)
  try requireChatWindow(bounds,nodes.map{$0.json})
  if let focused=attr(application,kAXFocusedWindowAttribute),CFGetTypeID(focused)==AXUIElementGetTypeID(),!CFEqual(focused,window) { let focusedWindow=unsafeBitCast(focused,to:AXUIElement.self);var focusNodes:[NodeRecord]=[];collect(focusedWindow,"focus",0,&focusNodes);if hasLoginMarker(focusNodes.map{$0.json}) { try fail("WECHAT_LOGIN_OR_SECURITY_REQUIRED") } }
  let before=epochs(); let shot=try await image(number); let after=epochs()
  guard before.1==after.1 else { try fail("CAPTURE_STOPPED") }
  if !readOnly && before.0 != after.0 { try fail("HUMAN_ACTIVITY_DURING_CAPTURE") }
  if readOnly {
    guard !a.isTerminated,rect(window)==bounds else { try fail("WINDOW_CHANGED_DURING_CAPTURE") }
  }
  var freshNodes:[NodeRecord]=[];collect(window,"w",0,&freshNodes);try requireChatWindow(bounds,freshNodes.map{$0.json})
  let f=Frame(id:UUID().uuidString,at:nowMS(),pid:a.processIdentifier,windowID:number,bounds:bounds,epoch:after.0,stopped:after.1,nodes:nodes);lastFrame=f
  return ["frameId":f.id,"at":f.at,"windowID":number,"bounds":asRect(bounds),"epoch":f.epoch,"nodes":nodes.map{$0.json},"image":shot.0.base64EncodedString(),"width":shot.1,"height":shot.2,"humanActive":before.0 != after.0]
}
@available(macOS 14.0, *)
func preview() async throws -> [String:Any] {
  // Explicit owner read only: select the verified WeChat main window without activating apps.
  // The independent-window screenshot API does not capture any covering application.
  return try await snapshot(readOnly:true)
}

func requireFresh(_ f:Frame,_ expires:Double) throws {
  let a=try app();let current=epochs()
  guard a.processIdentifier==f.pid,nowMS()<expires,nowMS()-f.at<2200,current.0==f.epoch,current.1==f.stopped,let window=f.nodes.first,rect(window.element)==f.bounds else { try fail("WINDOW_OR_HUMAN_STATE_CHANGED") }
}
func requireSameMessages(_ f:Frame,_ prefix:String) throws {
  guard !prefix.isEmpty,let window=f.nodes.first else { try fail("MESSAGE_CONTEXT_REQUIRED") }
  var current:[NodeRecord]=[];collect(window.element,"w",0,&current)
  func signature(_ nodes:[NodeRecord]) -> [String] { nodes.filter{($0.json["identifier"] as? String ?? "").hasPrefix(prefix)}.map { n in
    ["identifier","value","description"].map{n.json[$0] as? String ?? ""}.joined(separator:"\u{001F}")
  } }
  let old=signature(f.nodes);guard !old.isEmpty,old==signature(current) else { try fail("CUSTOMER_MESSAGE_CHANGED") }
}
func node(_ f:Frame,_ name:String,_ request:[String:Any]) throws -> NodeRecord {
  guard let key=request[name] as? String,let n=f.nodes.first(where:{$0.json["nodeId"] as? String==key}) else { try fail("CONTROL_NOT_FOUND") };return n
}
@available(macOS 14.0, *)
func send(_ request:[String:Any]) async throws -> [String:Any] {
  guard let f=lastFrame,request["frameId"] as? String==f.id,let text=request["text"] as? String,!text.isEmpty,text.count<=1000,let expectedHash=request["textHash"] as? String,let expires=request["expires"] as? Double else { try fail("SEND_REQUEST_INVALID") }
  guard SHA256.hash(data:Data(text.utf8)).map({String(format:"%02x",$0)}).joined()==expectedHash else { try fail("TEXT_HASH_MISMATCH") }
  let input=try node(f,"inputNodeId",request),button=try node(f,"sendNodeId",request),identity=try node(f,"identityNodeId",request)
  guard str(input.element,kAXRoleAttribute)==kAXTextAreaRole || str(input.element,kAXRoleAttribute)==kAXTextFieldRole else { try fail("NOT_AN_EDITABLE_INPUT") }
  guard str(button.element,kAXRoleAttribute)==kAXButtonRole,str(button.element,kAXTitleAttribute)=="发送" || str(button.element,kAXDescriptionAttribute)=="发送",let identityValue=request["identityValue"] as? String,!identityValue.isEmpty,str(identity.element,kAXValueAttribute)==identityValue || str(identity.element,kAXTitleAttribute)==identityValue else { try fail("IDENTITY_OR_SEND_CONTROL_CHANGED") }
  guard str(input.element,kAXValueAttribute).isEmpty,let box=rect(input.element),let x=request["x"] as? Double,let y=request["y"] as? Double,box.contains(CGPoint(x:x+f.bounds.origin.x,y:y+f.bounds.origin.y)) else { try fail("INPUT_NOT_EMPTY_OR_GROUNDING_OUTSIDE_INPUT") }
  try requireFresh(f,expires)
  guard let messagePrefix=request["messagePrefix"] as? String else { try fail("MESSAGE_CONTEXT_REQUIRED") }
  try requireSameMessages(f,messagePrefix)
  var settable=DarwinBoolean(false);AXUIElementIsAttributeSettable(input.element,kAXValueAttribute as CFString,&settable)
  // Only the approved text may be inserted. Never paste, press Return, or run arbitrary shortcuts.
  if settable.boolValue {
    guard AXUIElementSetAttributeValue(input.element,kAXValueAttribute as CFString,text as CFString) == .success else { try fail("INPUT_WRITE_FAILED") }
  } else {
    let point=CGPoint(x:x+f.bounds.origin.x,y:y+f.bounds.origin.y)
    for type in [CGEventType.leftMouseDown,CGEventType.leftMouseUp] { guard let e=CGEvent(mouseEventSource:nil,mouseType:type,mouseCursorPosition:point,mouseButton:.left) else { try fail("INPUT_FOCUS_FAILED") };e.setIntegerValueField(.eventSourceUserData,value:injectedMarker);e.post(tap:.cghidEventTap) }
    usleep(50000)
    let application=AXUIElementCreateApplication(f.pid)
    guard let focused=attr(application,kAXFocusedUIElementAttribute),CFEqual(focused,input.element) else { return ["status":"unknown"] }
    let codes=Array(text.utf16);var offset=0
    while offset<codes.count {
      do { try requireFresh(f,expires) } catch { return ["status":"unknown"] }
      var end=min(offset+16,codes.count)
      if end<codes.count && codes[end-1]>=0xD800 && codes[end-1]<=0xDBFF { end -= 1 }
      let part=Array(codes[offset..<end]);offset=end
      for down in [true,false] { if let event=CGEvent(keyboardEventSource:nil,virtualKey:0,keyDown:down) { event.setIntegerValueField(.eventSourceUserData,value:injectedMarker);event.keyboardSetUnicodeString(stringLength:part.count,unicodeString:part);event.post(tap:.cghidEventTap) } }
    }
  }
  usleep(50000)
  guard str(input.element,kAXValueAttribute)==text else { return ["status":"unknown"] }
  do { try requireFresh(f,expires) } catch { return ["status":"unknown"] }
  guard str(identity.element,kAXValueAttribute)==identityValue || str(identity.element,kAXTitleAttribute)==identityValue else { return ["status":"unknown"] }
  do { try requireSameMessages(f,messagePrefix) } catch { return ["status":"unknown"] }
  guard AXUIElementPerformAction(button.element,kAXPressAction as CFString) == .success else { return ["status":"unknown"] }
  usleep(200000)
  let after=try await snapshot()
  guard let nodes=after["nodes"] as? [[String:Any]],let prefix=request["messagePrefix"] as? String,let outgoing=request["outgoingDescription"] as? String else { return ["status":"unknown"] }
  let oldIds=Set(f.nodes.compactMap{$0.json["identifier"] as? String})
  let newMessages=nodes.filter { let identifier=$0["identifier"] as? String ?? "";return !identifier.isEmpty && identifier.hasPrefix(prefix) && !oldIds.contains(identifier) && $0["value"] as? String==text && $0["description"] as? String==outgoing }
  guard newMessages.count==1,str(input.element,kAXValueAttribute).isEmpty,let imageBase64=after["image"] as? String,let png=Data(base64Encoded:imageBase64) else { return ["status":"unknown"] }
  return ["status":"observed_sent","outgoingId":newMessages[0]["identifier"] as? String ?? "","evidenceHash":SHA256.hash(data:png).map{String(format:"%02x",$0)}.joined()]
}
func wechatInstallation(useRegistry:Bool = true) -> (Bundle,String)? {
  var candidates:[(URL,String)]=[]
  if useRegistry,let registered=NSWorkspace.shared.urlForApplication(withBundleIdentifier:allowedBundle) { candidates.append((registered,"launch_services")) }
  // A command-line helper may have no Launch Services entry even when the app exists.
  // Check known application folders, then validate the exact bundle ID and executable.
  for root in [URL(fileURLWithPath:"/Applications",isDirectory:true),FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications",isDirectory:true)] {
    for name in ["WeChat.app","微信.app"] { candidates.append((root.appendingPathComponent(name,isDirectory:true),"application_folder")) }
  }
  for (url,source) in candidates {
    guard let bundle=Bundle(url:url),bundle.bundleIdentifier==allowedBundle,let executable=bundle.executableURL,FileManager.default.isExecutableFile(atPath:executable.path) else { continue }
    return (bundle,source)
  }
  return nil
}
func status(useRegistry:Bool = true) -> [String:Any] {
  let installation=wechatInstallation(useRegistry:useRegistry)
  return ["platform":"macOS","accessibility":AXIsProcessTrusted(),"screenRecording":CGPreflightScreenCaptureAccess(),"inputMonitoring":CGPreflightListenEventAccess(),"wechatInstalled":installation != nil,"wechatRunning":!NSRunningApplication.runningApplications(withBundleIdentifier:allowedBundle).isEmpty,"wechatVersion":installation?.0.object(forInfoDictionaryKey:"CFBundleShortVersionString") as? String ?? "","installationSource":installation?.1 ?? "not_found","nativeVersion":"1.0.7"]
}
func ocrImage(_ image:String,region:[String:Any]? = nil) throws -> [String:Any] {
  guard image.utf8.count<=2_800_000,let data=Data(base64Encoded:image),data.count<=2*1024*1024,data.prefix(8)==Data([137,80,78,71,13,10,26,10]),let bitmap=NSBitmapImageRep(data:data),bitmap.pixelsWide>0,bitmap.pixelsHigh>0,bitmap.pixelsWide<=4000,bitmap.pixelsHigh<=4000 else { try fail("OCR_PNG_INVALID") }
  var recognitionData=data;var croppedOutput:[String:Any]=[:]
  if let region=region {
    guard let x=region["x"] as? Int,let y=region["y"] as? Int,let width=region["width"] as? Int,let height=region["height"] as? Int,x>=0,y>=0,width>=16,height>=16,x+width<=bitmap.pixelsWide,y+height<=bitmap.pixelsHigh,let cg=bitmap.cgImage,let cropped=cg.cropping(to:CGRect(x:x,y:y,width:width,height:height)),let png=NSBitmapImageRep(cgImage:cropped).representation(using:.png,properties:[:]),png.count<=2*1024*1024 else { try fail("OCR_REGION_INVALID") }
    recognitionData=png;croppedOutput=["image":png.base64EncodedString(),"width":width,"height":height]
  }
  let request=VNRecognizeTextRequest();request.recognitionLevel = .accurate;request.recognitionLanguages=["zh-Hans","en-US"];request.usesLanguageCorrection=true
  try VNImageRequestHandler(data:recognitionData).perform([request])
  let observations=(request.results ?? []).sorted { a,b in
    if abs(a.boundingBox.midY-b.boundingBox.midY)>0.008 { return a.boundingBox.midY>b.boundingBox.midY }
    return a.boundingBox.minX<b.boundingBox.minX
  }
  guard observations.count<=200 else { try fail("OCR_TOO_MANY_LINES") }
  let lines=observations.compactMap { observation -> [String:Any]? in
    guard let text=observation.topCandidates(1).first else { return nil }
    return ["text":text.string,"confidence":text.confidence]
  }
  croppedOutput["lines"]=lines;croppedOutput["provider"]="apple_vision_local";return croppedOutput
}
func ocrFile(_ path:String) throws -> [String:Any] {
  let request=VNRecognizeTextRequest();request.recognitionLevel = .accurate;request.recognitionLanguages=["zh-Hans","en-US"]
  try VNImageRequestHandler(url:URL(fileURLWithPath:path)).perform([request])
  return ["lines":(request.results ?? []).compactMap{$0.topCandidates(1).first?.string}]
}
func output(_ object:[String:Any]) { if let data=try? JSONSerialization.data(withJSONObject:object,options:[.sortedKeys]),let text=String(data:data,encoding:.utf8) { print(text);fflush(stdout) } }
signal(SIGUSR1,SIG_IGN)
let stopSource=DispatchSource.makeSignalSource(signal:SIGUSR1,queue:.global());stopSource.setEventHandler{stateLock.lock();stopEpoch += 1;stateLock.unlock()};stopSource.resume()
if CommandLine.arguments.contains("--chat-gate-self-test") {
  let large=CGRect(x:0,y:0,width:924,height:1003);let small=CGRect(x:0,y:0,width:280,height:380)
  let cases:[(CGRect,[[String:Any]],Bool)]=[
    (small,[],false),(large,[["identifier":"current_login_nick_name"]],false),
    (large,[["role":"AXStaticText","value":"提示 为了你的账号安全，请重新登录。"]],false),
    (large,[["role":"AXStaticText","value":"需在手机上完成登录"]],false),
    (large,[["role":"AXTextArea","identifier":"chat_input_field"]],true),
    (large,[["nodeId":"w.30","identifier":"chat_message_list"],["nodeId":"w.30.1","value":"为了你的账号安全，请重新登录。"]],true)]
  var passed=0
  for (bounds,nodes,expected) in cases { var allowed=true;do { try requireChatWindow(bounds,nodes) } catch { allowed=false };if allowed==expected { passed += 1 } }
  output(["status":passed==cases.count ? "passed":"failed","checks":passed,"cases":cases.count,"actualWindowCapture":false,"actualMessagesSent":0]);exit(passed==cases.count ? 0:1)
}
if CommandLine.arguments.contains("--status") { output(status(useRegistry:!CommandLine.arguments.contains("--skip-application-registry")));exit(0) }
if CommandLine.arguments.count==3 && CommandLine.arguments[1]=="--ocr-file" { do { output(try ocrFile(CommandLine.arguments[2])) } catch { output(["error":"OCR_FAILED"]);exit(1) };exit(0) }
while let line=readLine() {
  guard let data=line.data(using:.utf8),let req=(try? JSONSerialization.jsonObject(with:data)) as? [String:Any],let rid=req["id"] as? String,let command=req["command"] as? String else { output(["error":"INVALID_RPC"]);continue }
  if command=="status" { output(["id":rid,"result":status()]);continue }
  if command=="stop" { stateLock.lock();stopEpoch += 1;stateLock.unlock();lastFrame=nil;output(["id":rid,"result":["stopped":true]]);continue }
  let done=DispatchSemaphore(value:0)
  Task { @MainActor in
    do {
      guard #available(macOS 14.0,*) else { try fail("MACOS_14_REQUIRED") }
      let result:[String:Any]
      if command=="snapshot" { result=try await snapshot() }
      else if command=="preview" { result=try await preview() }
      else if command=="ocr",let image=req["image"] as? String { result=try ocrImage(image,region:req["region"] as? [String:Any]) }
      else if command=="send" { result=try await send(req) }
      else { try fail("COMMAND_NOT_ALLOWED") }
      output(["id":rid,"result":result])
    } catch BridgeError.blocked(let message) { output(["id":rid,"error":message]) }
    catch { output(["id":rid,"error":"NATIVE_OPERATION_FAILED"] ) }
    done.signal()
  }
  // Keep AppKit state fresh while asynchronous capture and activation complete.
  while done.wait(timeout:.now()) != .success { _ = RunLoop.current.run(mode:.default,before:Date(timeIntervalSinceNow:0.02)) }
}
