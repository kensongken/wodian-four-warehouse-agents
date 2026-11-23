import CryptoKit
import Flutter
import PhotosUI
import UIKit

final class MediaBridge: NSObject, FlutterPlugin, PHPickerViewControllerDelegate {
  private static let channelName = "photo_organizer/media_library"
  static let maximumAcceptedAssets = 10_000
  // PHPicker exposes selectionLimit but no runtime maximum query. A value of 0
  // delegates to the system maximum, so this adapter declares a finite ceiling.
  static let maximumPickerSelectionLimit = 10_000
  private static let maximumAssetBytes: Int64 = 100 * 1024 * 1024
  private static let hashBufferBytes = 1024 * 1024

  private let processingQueue = DispatchQueue(
    label: "photo_organizer.media_library.processing",
    qos: .userInitiated
  )
  private var pendingPick: PendingPick?

  static func register(with registrar: FlutterPluginRegistrar) {
    let channel = FlutterMethodChannel(name: channelName, binaryMessenger: registrar.messenger())
    let instance = MediaBridge()
    registrar.addMethodCallDelegate(instance, channel: channel)
  }

  func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
    switch call.method {
    case "pickImages":
      pickImages(call.arguments, result: result)
    case "computeSha256":
      runFileOperation(result: result) {
        let ref = try Self.stagedReference(from: call.arguments)
        return try Self.sha256(of: ref.fileURL)
      }
    case "release":
      runFileOperation(result: result) {
        let ref = try Self.stagedReference(from: call.arguments)
        try Self.releaseStagedFile(
          id: ref.id,
          locator: ref.locator,
          stagingRoot: try Self.stagingRoot()
        )
        return nil
      }
    default:
      result(FlutterMethodNotImplemented)
    }
  }

  private func pickImages(_ arguments: Any?, result: @escaping FlutterResult) {
    dispatchPrecondition(condition: .onQueue(.main))
    guard
      let map = arguments as? [String: Any],
      let remainingSlots = map["remainingSlots"] as? Int,
      Self.isValidRemainingSlots(remainingSlots)
    else {
      result(Self.flutterError(.invalidArguments))
      return
    }
    guard pendingPick == nil else {
      result(Self.flutterError(.pickerBusy))
      return
    }
    guard let presenter = Self.topViewController() else {
      result(Self.flutterError(.pickerUnavailable))
      return
    }

    let operation = PendingPick(result: result)
    pendingPick = operation
    let configuration = Self.pickerConfiguration(remainingSlots: remainingSlots)
    let picker = PHPickerViewController(configuration: configuration)
    picker.delegate = self
    presenter.present(picker, animated: true)
  }

  func picker(_ picker: PHPickerViewController, didFinishPicking results: [PHPickerResult]) {
    dispatchPrecondition(condition: .onQueue(.main))
    guard let operation = pendingPick, operation.beginProcessing() else {
      picker.dismiss(animated: true)
      return
    }
    picker.dismiss(animated: true) { [weak self] in
      guard let self else { return }
      processingQueue.async {
        let payload = self.process(results)
        DispatchQueue.main.async { [weak self] in
          guard let self else { return }
          if pendingPick === operation {
            pendingPick = nil
          }
          operation.complete(payload)
        }
      }
    }
  }

  private func process(_ results: [PHPickerResult]) -> [String: Any] {
    var selected: [[String: Any]] = []
    var unsupported: [[String: Any]] = []
    for pickerResult in results {
      switch processOne(pickerResult) {
      case .selected(let value): selected.append(value)
      case .unsupported(let value): unsupported.append(value)
      }
    }
    return ["selected": selected, "unsupported": unsupported]
  }

  private func processOne(_ pickerResult: PHPickerResult) -> ItemOutcome {
    let provider = pickerResult.itemProvider
    let token = Self.stableToken(pickerResult.assetIdentifier)
    let id = "ios:\(token)"
    let name = Self.safeDisplayName(provider.suggestedName)
    guard let type = Self.preferredStaticType(provider.registeredTypeIdentifiers) else {
      return .unsupported([
        "id": id,
        "name": name,
        "reason": "unsupported_media_type",
      ])
    }
    guard let mediaType = Self.normalizedMediaType(type) else {
      return .unsupported([
        "id": id,
        "name": name,
        "reason": "unsupported_media_type",
      ])
    }

    let semaphore = DispatchSemaphore(value: 0)
    var outcome: ItemOutcome = .unsupported(
      Self.itemFailure(id: id, displayName: name, mediaType: mediaType)
    )
    provider.loadFileRepresentation(forTypeIdentifier: type) { sourceURL, _ in
      defer { semaphore.signal() }
      guard let sourceURL else { return }
      do {
        let knownSize = try sourceURL.resourceValues(forKeys: [.fileSizeKey]).fileSize.map(Int64.init)
        if let knownSize, knownSize > Self.maximumAssetBytes {
          outcome = .unsupported([
            "id": id,
            "name": name,
            "reason": "image_too_large",
            "mediaType": mediaType,
            "byteSize": knownSize,
          ])
          return
        }
        let staged = try Self.stage(
          sourceURL: sourceURL,
          stableToken: token,
          leaseToken: UUID().uuidString.lowercased(),
          stagingRoot: try Self.stagingRoot()
        )
        if staged.byteSize > Self.maximumAssetBytes {
          try? Self.releaseStagedFile(
            id: id,
            locator: staged.locator,
            stagingRoot: try Self.stagingRoot()
          )
          outcome = .unsupported([
            "id": id,
            "name": name,
            "reason": "image_too_large",
            "mediaType": mediaType,
            "byteSize": staged.byteSize,
          ])
          return
        }
        if staged.byteSize <= 0 {
          try? Self.releaseStagedFile(
            id: id,
            locator: staged.locator,
            stagingRoot: try Self.stagingRoot()
          )
          outcome = .unsupported(
            Self.itemFailure(id: id, displayName: name, mediaType: mediaType)
          )
          return
        }
        outcome = .selected([
          "id": id,
          "displayName": name,
          "mediaType": mediaType,
          "byteSize": staged.byteSize,
          "accessKind": "stagedFile",
          "locator": staged.locator,
        ])
      } catch {
        outcome = .unsupported(Self.itemFailure(id: id, displayName: name, mediaType: mediaType))
      }
    }
    semaphore.wait()
    return outcome
  }

  private func runFileOperation(result: @escaping FlutterResult, operation: @escaping () throws -> Any?) {
    let reply = ReplyOnce(result)
    processingQueue.async {
      do {
        let value = try operation()
        DispatchQueue.main.async { reply.complete(value) }
      } catch let error as BridgeError {
        DispatchQueue.main.async { reply.complete(Self.flutterError(error)) }
      } catch {
        DispatchQueue.main.async { reply.complete(Self.flutterError(.readFailure)) }
      }
    }
  }

  static func normalizedMediaType(_ typeIdentifier: String) -> String? {
    switch typeIdentifier.lowercased() {
    case "public.heic", "public.heif": "image/heic"
    case "public.jpeg", "public.jpg": "image/jpeg"
    case "public.png": "image/png"
    default: nil
    }
  }

  static func pickerConfiguration(remainingSlots: Int) -> PHPickerConfiguration {
    var configuration = PHPickerConfiguration(photoLibrary: .shared())
    configuration.filter = .images
    configuration.selectionLimit = min(remainingSlots, maximumPickerSelectionLimit)
    configuration.preferredAssetRepresentationMode = .current
    return configuration
  }

  static func isValidRemainingSlots(_ value: Int) -> Bool {
    (1...maximumAcceptedAssets).contains(value)
  }

  static func preferredStaticType(_ identifiers: [String]) -> String? {
    identifiers.first(where: { normalizedMediaType($0) != nil })
  }

  static func itemFailure(
    id: String,
    displayName: String,
    mediaType: String?
  ) -> [String: Any] {
    var value: [String: Any] = [
      "id": id,
      "name": displayName,
      "reason": "read_failure",
    ]
    if let mediaType { value["mediaType"] = mediaType }
    return value
  }

  static func releaseStagedFile(
    id: String,
    locator: String,
    stagingRoot: URL
  ) throws {
    let reference = try validatedStagedReference(id: id, locator: locator, stagingRoot: stagingRoot)
    guard FileManager.default.fileExists(atPath: reference.directoryURL.path) else { return }
    let resolvedRoot = stagingRoot.resolvingSymlinksInPath().standardizedFileURL
    let resolvedStableDirectory = reference.directoryURL.deletingLastPathComponent()
      .resolvingSymlinksInPath().standardizedFileURL
    let resolvedDirectory = reference.directoryURL.resolvingSymlinksInPath().standardizedFileURL
    guard
      resolvedStableDirectory.deletingLastPathComponent() == resolvedRoot,
      resolvedDirectory.deletingLastPathComponent() == resolvedStableDirectory
    else {
      throw BridgeError.invalidAssetRef
    }
    try FileManager.default.removeItem(at: reference.directoryURL)
    if (try? FileManager.default.contentsOfDirectory(atPath: resolvedStableDirectory.path).isEmpty) == true {
      try? FileManager.default.removeItem(at: resolvedStableDirectory)
    }
  }

  static func stagedFileURL(id: String, locator: String) throws -> URL {
    try validatedStagedReference(
      id: id,
      locator: locator,
      stagingRoot: try stagingRoot()
    ).fileURL
  }

  private static func stagedReference(from arguments: Any?) throws -> StagedReference {
    guard
      let map = arguments as? [String: Any],
      map["accessKind"] as? String == "stagedFile",
      let id = map["id"] as? String,
      let locator = map["locator"] as? String
    else { throw BridgeError.invalidAssetRef }
    return try validatedStagedReference(id: id, locator: locator, stagingRoot: stagingRoot())
  }

  private static func validatedStagedReference(
    id: String,
    locator: String,
    stagingRoot: URL
  ) throws -> StagedReference {
    let components = locator.split(separator: "/", omittingEmptySubsequences: false).map(String.init)
    guard
      components.count == 3,
      components[2] == "original",
      isSafeToken(components[0]),
      isSafeLeaseToken(components[1]),
      id == "ios:\(components[0])"
    else { throw BridgeError.invalidAssetRef }
    let stableDirectory = stagingRoot
      .appendingPathComponent(components[0], isDirectory: true)
      .standardizedFileURL
    let directory = stableDirectory
      .appendingPathComponent(components[1], isDirectory: true)
      .standardizedFileURL
    guard
      stableDirectory.deletingLastPathComponent() == stagingRoot.standardizedFileURL,
      directory.deletingLastPathComponent() == stableDirectory
    else {
      throw BridgeError.invalidAssetRef
    }
    return StagedReference(
      id: id,
      locator: locator,
      directoryURL: directory,
      fileURL: directory.appendingPathComponent("original", isDirectory: false)
    )
  }

  static func stage(
    sourceURL: URL,
    stableToken: String,
    leaseToken: String,
    stagingRoot: URL
  ) throws -> (locator: String, byteSize: Int64) {
    guard isSafeToken(stableToken), isSafeLeaseToken(leaseToken) else {
      throw BridgeError.invalidAssetRef
    }
    let manager = FileManager.default
    let root = stagingRoot.standardizedFileURL
    try manager.createDirectory(at: root, withIntermediateDirectories: true)
    var rootValues = URLResourceValues()
    rootValues.isExcludedFromBackup = true
    var mutableRoot = root
    try? mutableRoot.setResourceValues(rootValues)

    let stableDirectory = root.appendingPathComponent(stableToken, isDirectory: true)
    let directory = stableDirectory.appendingPathComponent(leaseToken, isDirectory: true)
    try manager.createDirectory(at: directory, withIntermediateDirectories: true)
    let destination = directory.appendingPathComponent("original", isDirectory: false)
    let temporary = directory.appendingPathComponent(".partial-\(UUID().uuidString)", isDirectory: false)
    do {
      defer { try? manager.removeItem(at: temporary) }
      try manager.copyItem(at: sourceURL, to: temporary)
      if manager.fileExists(atPath: destination.path) {
        _ = try manager.replaceItemAt(destination, withItemAt: temporary)
      } else {
        try manager.moveItem(at: temporary, to: destination)
      }
      try manager.setAttributes(
        [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication],
        ofItemAtPath: destination.path
      )
      let attributes = try manager.attributesOfItem(atPath: destination.path)
      guard let size = attributes[.size] as? NSNumber else { throw BridgeError.readFailure }
      return ("\(stableToken)/\(leaseToken)/original", size.int64Value)
    } catch {
      try? manager.removeItem(at: directory)
      if (try? manager.contentsOfDirectory(atPath: stableDirectory.path).isEmpty) == true {
        try? manager.removeItem(at: stableDirectory)
      }
      throw error
    }
  }

  private static func sha256(of url: URL) throws -> String {
    guard FileManager.default.isReadableFile(atPath: url.path) else { throw BridgeError.readFailure }
    let handle = try FileHandle(forReadingFrom: url)
    defer { try? handle.close() }
    var hasher = SHA256()
    while true {
      let data = try handle.read(upToCount: hashBufferBytes) ?? Data()
      if data.isEmpty { break }
      hasher.update(data: data)
    }
    return hasher.finalize().map { String(format: "%02x", $0) }.joined()
  }

  private static func stagingRoot() throws -> URL {
    let applicationSupport = try FileManager.default.url(
      for: .applicationSupportDirectory,
      in: .userDomainMask,
      appropriateFor: nil,
      create: true
    )
    return applicationSupport.appendingPathComponent("UploadStaging", isDirectory: true)
      .standardizedFileURL
  }

  private static func stableToken(_ assetIdentifier: String?) -> String {
    guard let assetIdentifier, !assetIdentifier.isEmpty else {
      return UUID().uuidString.lowercased()
    }
    return SHA256.hash(data: Data(assetIdentifier.utf8))
      .map { String(format: "%02x", $0) }
      .joined()
  }

  private static func safeDisplayName(_ suggestedName: String?) -> String {
    let trimmed = suggestedName?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    return trimmed.isEmpty ? "照片" : String(trimmed.prefix(255))
  }

  private static func isSafeToken(_ value: String) -> Bool {
    value.range(
      of: #"^(?:[0-9a-f]{64}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"#,
      options: .regularExpression
    ) != nil
  }

  private static func isSafeLeaseToken(_ value: String) -> Bool {
    value.range(
      of: #"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"#,
      options: .regularExpression
    ) != nil
  }

  private static func topViewController() -> UIViewController? {
    let root = UIApplication.shared.connectedScenes
      .compactMap { $0 as? UIWindowScene }
      .flatMap(\.windows)
      .first(where: \.isKeyWindow)?
      .rootViewController
    return topViewController(from: root)
  }

  private static func topViewController(from controller: UIViewController?) -> UIViewController? {
    if let presented = controller?.presentedViewController {
      return topViewController(from: presented)
    }
    if let navigation = controller as? UINavigationController {
      return topViewController(from: navigation.visibleViewController)
    }
    if let tabs = controller as? UITabBarController {
      return topViewController(from: tabs.selectedViewController)
    }
    return controller
  }

  private static func flutterError(_ error: BridgeError) -> FlutterError {
    FlutterError(code: error.rawValue, message: error.message, details: nil)
  }
}

private enum ItemOutcome {
  case selected([String: Any])
  case unsupported([String: Any])
}

private struct StagedReference {
  let id: String
  let locator: String
  let directoryURL: URL
  let fileURL: URL
}

private enum BridgeError: String, Error {
  case invalidArguments = "invalid_arguments"
  case pickerBusy = "picker_busy"
  case pickerUnavailable = "picker_unavailable"
  case invalidAssetRef = "invalid_asset_ref"
  case readFailure = "read_failure"

  var message: String {
    switch self {
    case .invalidArguments: "选择数量无效"
    case .pickerBusy: "照片选择器正在使用中"
    case .pickerUnavailable: "暂时无法打开照片选择器"
    case .invalidAssetRef: "照片引用无效"
    case .readFailure: "无法读取照片"
    }
  }
}

private final class PendingPick {
  private let reply: ReplyOnce
  private let lock = NSLock()
  private var processing = false

  init(result: @escaping FlutterResult) {
    reply = ReplyOnce(result)
  }

  func beginProcessing() -> Bool {
    lock.lock()
    defer { lock.unlock() }
    guard !processing else { return false }
    processing = true
    return true
  }

  func complete(_ value: Any?) {
    reply.complete(value)
  }
}

private final class ReplyOnce: @unchecked Sendable {
  private let lock = NSLock()
  private var result: FlutterResult?

  init(_ result: @escaping FlutterResult) {
    self.result = result
  }

  func complete(_ value: Any?) {
    lock.lock()
    let callback = result
    result = nil
    lock.unlock()
    callback?(value)
  }
}
