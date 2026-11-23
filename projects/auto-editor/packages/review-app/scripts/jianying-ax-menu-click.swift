import AppKit
import ApplicationServices
import Foundation

private let allowedCommand = "根据选中素材新建时间线"
private let bundleIdentifier = "com.lemon.lvpro"

private func attribute(_ element: AXUIElement, _ name: CFString) -> CFTypeRef? {
  var value: CFTypeRef?
  guard AXUIElementCopyAttributeValue(element, name, &value) == .success else { return nil }
  return value
}

private func stringAttribute(_ element: AXUIElement, _ name: CFString) -> String? {
  attribute(element, name) as? String
}

private func elementAttribute(_ element: AXUIElement, _ name: CFString) -> AXUIElement? {
  guard let value = attribute(element, name), CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
  return unsafeBitCast(value, to: AXUIElement.self)
}

private func children(of element: AXUIElement) -> [AXUIElement] {
  let direct = attribute(element, kAXChildrenAttribute as CFString) as? [AXUIElement] ?? []
  let windows = attribute(element, kAXWindowsAttribute as CFString) as? [AXUIElement] ?? []
  return direct + windows
}

private func exactCommand(in element: AXUIElement, depth: Int = 0) -> AXUIElement? {
  guard depth <= 14 else { return nil }
  let labels = [
    stringAttribute(element, kAXTitleAttribute as CFString),
    stringAttribute(element, kAXDescriptionAttribute as CFString),
    stringAttribute(element, kAXValueAttribute as CFString),
    stringAttribute(element, kAXHelpAttribute as CFString),
    stringAttribute(element, kAXIdentifierAttribute as CFString),
  ]
  if labels.contains(where: { $0 == allowedCommand }) { return element }
  for child in children(of: element) {
    if let match = exactCommand(in: child, depth: depth + 1) { return match }
  }
  return nil
}

private func debugTree(_ element: AXUIElement, depth: Int = 0) {
  guard depth <= 6 else { return }
  let role = stringAttribute(element, kAXRoleAttribute as CFString) ?? ""
  let title = stringAttribute(element, kAXTitleAttribute as CFString) ?? ""
  let description = stringAttribute(element, kAXDescriptionAttribute as CFString) ?? ""
  let value = stringAttribute(element, kAXValueAttribute as CFString) ?? ""
  let identifier = stringAttribute(element, kAXIdentifierAttribute as CFString) ?? ""
  print("\(depth)|\(role)|\(title)|\(description)|\(value)|\(identifier)")
  for child in children(of: element) { debugTree(child, depth: depth + 1) }
}

guard CommandLine.arguments.dropFirst().first == allowedCommand else {
  print("timeline-command-rejected")
  exit(2)
}

let matchingApps = NSRunningApplication.runningApplications(withBundleIdentifier: bundleIdentifier)
guard !matchingApps.isEmpty else {
  print("jianying-not-running")
  exit(3)
}

let applicationElements = matchingApps.map { AXUIElementCreateApplication($0.processIdentifier) }
let systemWideElement = AXUIElementCreateSystemWide()
var searchRoots = applicationElements
if let focused = elementAttribute(systemWideElement, kAXFocusedUIElementAttribute as CFString) {
  searchRoots.append(focused)
}
if let focused = elementAttribute(systemWideElement, kAXFocusedApplicationAttribute as CFString) {
  searchRoots.append(focused)
}
if ProcessInfo.processInfo.environment["JIANYING_AX_DEBUG"] == "1" {
  for root in searchRoots { debugTree(root) }
  exit(0)
}
guard let command = searchRoots.lazy.compactMap({ exactCommand(in: $0) }).first else {
  print("timeline-command-not-found")
  exit(4)
}

guard AXUIElementPerformAction(command, kAXPressAction as CFString) == .success else {
  print("timeline-command-failed")
  exit(5)
}

print("timeline-created")
