import Flutter
import Foundation

final class BackgroundTransferBridge: NSObject, FlutterPlugin, FlutterStreamHandler {
  static let shared = BackgroundTransferBridge()
  static let methodChannelName = "photo_organizer/background_transfer"
  static let eventChannelName = "photo_organizer/background_transfer_events"
  static let sessionIdentifier = "com.photo-organizer.background-upload"
  static let maximumQueryJobIds = 10_000

  private let ledger = BackgroundTransferLedger()
  private let delegateQueue: OperationQueue = {
    let queue = OperationQueue()
    queue.name = "photo_organizer.background_transfer.delegate"
    queue.maxConcurrentOperationCount = 1
    return queue
  }()
  private lazy var session: URLSession = {
    let configuration = URLSessionConfiguration.background(
      withIdentifier: Self.sessionIdentifier
    )
    configuration.sessionSendsLaunchEvents = true
    configuration.isDiscretionary = false
    configuration.httpMaximumConnectionsPerHost = 3
    return URLSession(
      configuration: configuration,
      delegate: self,
      delegateQueue: delegateQueue
    )
  }()
  private var eventSink: FlutterEventSink?
  private var completionHandler: (() -> Void)?
  private let pendingTerminals = PendingTerminalStore()
  private var completionRetryScheduled = false

  static func register(with registrar: FlutterPluginRegistrar) {
    let methods = FlutterMethodChannel(
      name: methodChannelName,
      binaryMessenger: registrar.messenger()
    )
    let events = FlutterEventChannel(
      name: eventChannelName,
      binaryMessenger: registrar.messenger()
    )
    registrar.addMethodCallDelegate(shared, channel: methods)
    events.setStreamHandler(shared)
    _ = shared.session
    shared.recoverPendingTransfers()
  }

  func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
    switch call.method {
    case "enqueuePart": enqueue(call.arguments, result: result)
    case "queryJobs": query(call.arguments, result: result)
    case "acknowledge": acknowledge(call.arguments, result: result)
    case "cancel": cancel(call.arguments, result: result)
    default: result(FlutterMethodNotImplemented)
    }
  }

  func onListen(withArguments _: Any?, eventSink events: @escaping FlutterEventSink) -> FlutterError? {
    eventSink = events
    return nil
  }

  func onCancel(withArguments _: Any?) -> FlutterError? {
    eventSink = nil
    return nil
  }

  func handleEvents(
    identifier: String,
    completionHandler: @escaping () -> Void
  ) -> Bool {
    guard identifier == Self.sessionIdentifier else { return false }
    delegateQueue.addOperation { [weak self] in
      self?.completionHandler = completionHandler
      self?.recoverPendingTransfers()
    }
    return true
  }

  private func enqueue(_ arguments: Any?, result: @escaping FlutterResult) {
    do {
      let request = try NativePartRequest(arguments)
      if try ledger.record(for: request.jobId) != nil {
        recoverPendingTransfers()
        result(request.jobId)
        return
      }
      let partFileName = Self.stableFilename(request.jobId)
      let intent = BackgroundTransferRecord(
        jobId: request.jobId,
        taskIdentifier: nil,
        state: "queued",
        progress: 0,
        partFileName: partFileName,
        etag: nil,
        code: nil,
        retryable: nil
      )
      do {
        try ledger.put(intent)
      } catch {
        throw BackgroundTransferError.persistenceFailure
      }
      var task: URLSessionUploadTask?
      do {
        let partFile = try Self.stagePart(request)
        var upload = URLRequest(url: request.signedURL)
        upload.httpMethod = "PUT"
        upload.allowsCellularAccess = request.allowMobileData
        upload.setValue(String(request.byteLength), forHTTPHeaderField: "Content-Length")
        let created = session.uploadTask(with: upload, fromFile: partFile)
        created.taskDescription = request.jobId
        task = created
        let record = intent.assigning(taskIdentifier: created.taskIdentifier)
        try ledger.put(record)
        created.resume()
        emit(record)
        result(request.jobId)
      } catch {
        task?.cancel()
        if let removed = try? ledger.remove(jobId: request.jobId) {
          Self.deletePart(named: removed.partFileName)
        } else {
          Self.deletePart(named: partFileName)
        }
        if error is BackgroundTransferError { throw error }
        throw BackgroundTransferError.persistenceFailure
      }
    } catch let error as BackgroundTransferError {
      result(error.flutterError)
    } catch {
      result(BackgroundTransferError.readFailure.flutterError)
    }
  }

  private func query(_ arguments: Any?, result: @escaping FlutterResult) {
    guard let ids = Self.validatedQueryJobIds(arguments) else {
      result(BackgroundTransferError.invalidArguments.flutterError)
      return
    }
    session.getAllTasks { [weak self] tasks in
      guard let self else { return }
      do {
        try reconcileSessionTasks(tasks)
        let payloads: [[String: Any]] = try ids.compactMap { id -> [String: Any]? in
          guard let record = try self.ledger.record(for: id),
                record.cancelRequested != true else { return nil }
          return record.eventPayload
        }
        result(payloads)
      } catch {
        result(BackgroundTransferError.persistenceFailure.flutterError)
      }
    }
  }

  private func acknowledge(_ arguments: Any?, result: @escaping FlutterResult) {
    guard let jobId = Self.jobId(arguments) else {
      result(BackgroundTransferError.invalidArguments.flutterError)
      return
    }
    do {
      if let removed = try ledger.remove(jobId: jobId) {
        Self.deletePart(named: removed.partFileName)
      }
      result(nil)
    } catch {
      result(BackgroundTransferError.persistenceFailure.flutterError)
    }
  }

  private func cancel(_ arguments: Any?, result: @escaping FlutterResult) {
    guard let jobId = Self.jobId(arguments) else {
      result(BackgroundTransferError.invalidArguments.flutterError)
      return
    }
    do {
      guard let current = try ledger.record(for: jobId) else {
        result(nil)
        return
      }
      try ledger.put(current.requestingCancellation())
      session.getAllTasks { tasks in
        let task = tasks.first(where: {
          $0.taskIdentifier == current.taskIdentifier ||
            $0.taskDescription == current.jobId
        })
        if let task {
          task.cancel()
        } else if let removed = try? self.ledger.remove(jobId: jobId) {
          Self.deletePart(named: removed.partFileName)
        }
      }
      result(nil)
    } catch {
      result(BackgroundTransferError.persistenceFailure.flutterError)
    }
  }

  @discardableResult
  private func persistAndEmit(_ record: BackgroundTransferRecord) -> Bool {
    guard (try? ledger.put(record)) != nil else {
      if record.isTerminal { pendingTerminals.retain(record) }
      return false
    }
    emit(record)
    return true
  }

  private func emit(_ record: BackgroundTransferRecord) {
    guard let sink = eventSink else { return }
    DispatchQueue.main.async { sink(record.eventPayload) }
  }

  private static func jobId(_ arguments: Any?) -> String? {
    guard
      let map = arguments as? [String: Any],
      let jobId = map["jobId"] as? String,
      isSafeJobId(jobId)
    else { return nil }
    return jobId
  }

  private static func stagePart(_ request: NativePartRequest) throws -> URL {
    let source = try MediaBridge.stagedFileURL(
      id: request.assetId,
      locator: request.locator
    )
    guard FileManager.default.isReadableFile(atPath: source.path) else {
      throw BackgroundTransferError.sourceMissing
    }
    let directory = try partDirectory()
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    let name = stableFilename(request.jobId)
    let destination = directory.appendingPathComponent(name)
    let temporary = directory.appendingPathComponent(".partial-\(UUID().uuidString)")
    let input = try FileHandle(forReadingFrom: source)
    defer { try? input.close() }
    guard try input.seekToEnd() >= UInt64(request.byteOffset + request.byteLength) else {
      throw BackgroundTransferError.readFailure
    }
    try input.seek(toOffset: UInt64(request.byteOffset))
    FileManager.default.createFile(atPath: temporary.path, contents: nil)
    let output = try FileHandle(forWritingTo: temporary)
    do {
      var remaining = request.byteLength
      while remaining > 0 {
        let data = try input.read(upToCount: min(1024 * 1024, remaining)) ?? Data()
        guard !data.isEmpty else { throw BackgroundTransferError.readFailure }
        try output.write(contentsOf: data)
        remaining -= data.count
      }
      try output.synchronize()
      try output.close()
      if FileManager.default.fileExists(atPath: destination.path) {
        _ = try FileManager.default.replaceItemAt(destination, withItemAt: temporary)
      } else {
        try FileManager.default.moveItem(at: temporary, to: destination)
      }
      try FileManager.default.setAttributes(
        [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication],
        ofItemAtPath: destination.path
      )
      return destination
    } catch {
      try? output.close()
      try? FileManager.default.removeItem(at: temporary)
      throw error
    }
  }

  private static func stableFilename(_ jobId: String) -> String {
    Data(jobId.utf8).base64EncodedString()
      .replacingOccurrences(of: "/", with: "_")
      .replacingOccurrences(of: "+", with: "-")
      .replacingOccurrences(of: "=", with: "") + ".part"
  }

  private static func partDirectory() throws -> URL {
    try FileManager.default.url(
      for: .applicationSupportDirectory,
      in: .userDomainMask,
      appropriateFor: nil,
      create: true
    ).appendingPathComponent("BackgroundUploadParts", isDirectory: true)
  }

  private static func deletePart(named name: String) {
    guard !name.contains("/") && !name.contains("..") else { return }
    guard let directory = try? partDirectory() else { return }
    try? FileManager.default.removeItem(at: directory.appendingPathComponent(name))
  }

  private static func deleteUnownedPartFiles(owned: Set<String>) {
    guard
      let directory = try? partDirectory(),
      let files = try? FileManager.default.contentsOfDirectory(
        at: directory,
        includingPropertiesForKeys: nil
      )
    else { return }
    for file in files {
      let name = file.lastPathComponent
      guard
        name.hasPrefix(".partial-") || name.hasSuffix(".part"),
        !owned.contains(name)
      else { continue }
      try? FileManager.default.removeItem(at: file)
    }
  }

  private func recoverPendingTransfers() {
    session.getAllTasks { [weak self] tasks in
      guard let self else { return }
      do {
        try reconcileSessionTasks(tasks)
      } catch {
        // Keep every durable enqueue/cancellation intent for a later pass.
      }
    }
  }

  private func reconcileSessionTasks(_ tasks: [URLSessionTask]) throws {
    var recordsByJob = Dictionary(
      uniqueKeysWithValues: try ledger.allRecords().map { ($0.jobId, $0) }
    )
    for task in tasks {
      let recordByIdentifier = recordsByJob.values.first {
        $0.taskIdentifier == task.taskIdentifier
      }
      let jobId = task.taskDescription ?? recordByIdentifier?.jobId
      guard
        let jobId,
        Self.isSafeJobId(jobId),
        var record = recordsByJob[jobId]
      else {
        // This dedicated session must never keep work without durable
        // ownership. Cancelling is idempotent and prevents duplicate bytes.
        task.cancel()
        continue
      }
      if task.taskDescription == nil { task.taskDescription = jobId }
      if record.taskIdentifier != task.taskIdentifier {
        record = record.assigning(taskIdentifier: task.taskIdentifier)
        try ledger.put(record)
        recordsByJob[jobId] = record
      }
      if record.cancelRequested == true {
        task.cancel()
      } else if Self.shouldResume(record: record, taskState: task.state) {
        // A crash after ledger persistence but before resume is recoverable.
        task.resume()
      }
    }

    let activeByIdentifier = Dictionary(
      uniqueKeysWithValues: tasks.map { ($0.taskIdentifier, $0) }
    )
    for pending in try ledger.cancellationRecords() {
      if let taskIdentifier = pending.taskIdentifier,
         let task = activeByIdentifier[taskIdentifier] {
        task.cancel()
      } else if let removed = try ledger.remove(jobId: pending.jobId) {
        Self.deletePart(named: removed.partFileName)
      }
    }
    let removedPartFiles = try ledger.reconcileMissingTasks(
      activeTaskIdentifiers: Set(tasks.map(\.taskIdentifier))
    )
    removedPartFiles.forEach { Self.deletePart(named: $0) }
    let ownedPartFiles = Set(try ledger.allRecords().map(\.partFileName))
    Self.deleteUnownedPartFiles(owned: ownedPartFiles)
  }

  private func record(for task: URLSessionTask) throws -> BackgroundTransferRecord? {
    if let current = try ledger.record(taskIdentifier: task.taskIdentifier) {
      return current
    }
    guard
      let jobId = task.taskDescription,
      var intent = try ledger.record(for: jobId)
    else { return nil }
    intent = intent.assigning(taskIdentifier: task.taskIdentifier)
    try ledger.put(intent)
    return intent
  }

  static func isSafeJobId(_ value: String) -> Bool {
    !value.isEmpty &&
      value.count <= 512 &&
      !value.unicodeScalars.contains {
        CharacterSet.controlCharacters.contains($0)
      }
  }

  static func validatedQueryJobIds(_ arguments: Any?) -> [String]? {
    guard
      let map = arguments as? [String: Any],
      let ids = map["jobIds"] as? [String],
      ids.count <= maximumQueryJobIds,
      ids.allSatisfy(isSafeJobId)
    else { return nil }
    return ids
  }

  static func isSafeRange(offset: Int, length: Int) -> Bool {
    offset >= 0 && length > 0 && offset <= Int.max - length
  }

  static func shouldResume(
    record: BackgroundTransferRecord,
    taskState: URLSessionTask.State
  ) -> Bool {
    !record.isTerminal &&
      record.cancelRequested != true &&
      taskState == .suspended
  }
}

extension BackgroundTransferBridge: URLSessionTaskDelegate, URLSessionDataDelegate {
  func urlSession(
    _: URLSession,
    task: URLSessionTask,
    didSendBodyData _: Int64,
    totalBytesSent: Int64,
    totalBytesExpectedToSend: Int64
  ) {
    guard
      totalBytesExpectedToSend > 0,
      let current = try? record(for: task),
      !current.isTerminal
    else { return }
    let progress = Int((totalBytesSent * 100 / totalBytesExpectedToSend).clamped(to: 0...100))
    persistAndEmit(current.updating(state: "progress", progress: progress))
  }

  func urlSession(
    _: URLSession,
    task: URLSessionTask,
    didCompleteWithError error: Error?
  ) {
    guard
      let current = try? record(for: task)
    else { return }
    if current.cancelRequested == true {
      if let removed = try? ledger.remove(jobId: current.jobId) {
        Self.deletePart(named: removed.partFileName)
      }
      return
    }
    let response = task.response as? HTTPURLResponse
    let status = response?.statusCode
    let rawETag = response?.value(forHTTPHeaderField: "ETag")?.trimmingCharacters(
      in: .whitespacesAndNewlines
    )
    let terminal: BackgroundTransferRecord
    if error == nil,
       let status,
       (200..<300).contains(status),
       let rawETag,
       !rawETag.isEmpty,
       rawETag.count <= 256 {
      terminal = current.terminal(state: "succeeded", etag: rawETag)
    } else {
      let classification = Self.classify(error: error, statusCode: status)
      terminal = current.terminal(
        state: "failed",
        code: classification.code,
        retryable: classification.retryable
      )
    }
    if persistAndEmit(terminal) {
      Self.deletePart(named: current.partFileName)
    }
  }

  func urlSessionDidFinishEvents(forBackgroundURLSession _: URLSession) {
    // Delegate callbacks run on a serial queue. Every terminal event above is
    // synchronously flushed before this background completion is released.
    finishBackgroundEventsWhenDurable()
  }

  private func finishBackgroundEventsWhenDurable() {
    let recovered = pendingTerminals.retryPersist { try ledger.put($0) }
    recovered.forEach {
      Self.deletePart(named: $0.partFileName)
      emit($0)
    }
    guard pendingTerminals.isEmpty, ledger.flush() else {
      scheduleDurabilityRetry()
      return
    }
    let handler = completionHandler
    completionHandler = nil
    BackgroundCompletionGate.persistThenComplete(
      persist: {},
      completion: { DispatchQueue.main.async { handler?() } }
    )
  }

  private func scheduleDurabilityRetry() {
    guard !completionRetryScheduled else { return }
    completionRetryScheduled = true
    DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + .milliseconds(250)) {
      self.delegateQueue.addOperation { [weak self] in
        guard let self else { return }
        completionRetryScheduled = false
        finishBackgroundEventsWhenDurable()
      }
    }
  }

  static func classify(
    error: Error?,
    statusCode: Int?
  ) -> (code: String, retryable: Bool) {
    if statusCode == 401 || statusCode == 403 {
      return ("signed_url_expired", true)
    }
    if let urlError = error as? URLError {
      switch urlError.code {
      case .fileDoesNotExist, .cannotOpenFile:
        return ("source_missing", false)
      case .backgroundSessionWasDisconnected, .timedOut:
        return ("background_expired", true)
      case .notConnectedToInternet, .networkConnectionLost, .cannotConnectToHost:
        return ("network_unavailable", true)
      default: break
      }
    }
    if let statusCode, statusCode >= 500 { return ("service_unavailable", true) }
    return ("service_unavailable", false)
  }
}

private struct NativePartRequest {
  init(_ arguments: Any?) throws {
    guard
      let map = arguments as? [String: Any],
      let jobId = map["jobId"] as? String,
      BackgroundTransferBridge.isSafeJobId(jobId),
      let asset = map["asset"] as? [String: Any],
      asset["accessKind"] as? String == "stagedFile",
      let assetId = asset["id"] as? String,
      let locator = asset["locator"] as? String,
      let partNumber = map["partNumber"] as? Int,
      partNumber > 0,
      let byteOffset = map["byteOffset"] as? Int,
      let byteLength = map["byteLength"] as? Int,
      BackgroundTransferBridge.isSafeRange(offset: byteOffset, length: byteLength),
      let rawURL = map["signedUrl"] as? String,
      let signedURL = URL(string: rawURL),
      signedURL.scheme == "https",
      signedURL.host != nil,
      let allowMobileData = map["allowMobileData"] as? Bool
    else { throw BackgroundTransferError.invalidArguments }
    self.jobId = jobId
    self.assetId = assetId
    self.locator = locator
    self.byteOffset = byteOffset
    self.byteLength = byteLength
    self.signedURL = signedURL
    self.allowMobileData = allowMobileData
  }

  let jobId: String
  let assetId: String
  let locator: String
  let byteOffset: Int
  let byteLength: Int
  let signedURL: URL
  let allowMobileData: Bool
}

struct BackgroundTransferRecord: Codable {
  let jobId: String
  let taskIdentifier: Int?
  let state: String
  let progress: Int
  let partFileName: String
  let etag: String?
  let code: String?
  let retryable: Bool?
  var cancelRequested: Bool? = nil

  var isTerminal: Bool { state == "succeeded" || state == "failed" }

  func assigning(taskIdentifier: Int) -> Self {
    Self(
      jobId: jobId,
      taskIdentifier: taskIdentifier,
      state: state,
      progress: progress,
      partFileName: partFileName,
      etag: etag,
      code: code,
      retryable: retryable,
      cancelRequested: cancelRequested
    )
  }

  var eventPayload: [String: Any] {
    var payload: [String: Any] = ["jobId": jobId, "state": state]
    if state == "progress" { payload["progress"] = progress }
    if let etag { payload["etag"] = etag }
    if let code { payload["code"] = code }
    if let retryable { payload["retryable"] = retryable }
    return payload
  }

  func updating(state: String, progress: Int) -> Self {
    Self(
      jobId: jobId,
      taskIdentifier: taskIdentifier,
      state: state,
      progress: progress,
      partFileName: partFileName,
      etag: etag,
      code: code,
      retryable: retryable,
      cancelRequested: cancelRequested
    )
  }

  func terminal(
    state: String,
    etag: String? = nil,
    code: String? = nil,
    retryable: Bool? = nil
  ) -> Self {
    Self(
      jobId: jobId,
      taskIdentifier: taskIdentifier,
      state: state,
      progress: state == "succeeded" ? 100 : progress,
      partFileName: partFileName,
      etag: etag,
      code: code,
      retryable: retryable,
      cancelRequested: cancelRequested
    )
  }

  func requestingCancellation() -> Self {
    var copy = self
    copy.cancelRequested = true
    return copy
  }
}

final class BackgroundTransferLedger {
  private let defaults: UserDefaults
  private let key: String
  private let lock = NSLock()

  init(
    defaults: UserDefaults = .standard,
    key: String = "photo_organizer.background_transfer.ledger.v1"
  ) {
    self.defaults = defaults
    self.key = key
  }

  func record(for jobId: String) throws -> BackgroundTransferRecord? {
    try records().first(where: { $0.jobId == jobId })
  }

  func record(taskIdentifier: Int) throws -> BackgroundTransferRecord? {
    try records().first(where: { $0.taskIdentifier == taskIdentifier })
  }

  func allRecords() throws -> [BackgroundTransferRecord] {
    try records()
  }

  func cancellationRecords() throws -> [BackgroundTransferRecord] {
    try records().filter { $0.cancelRequested == true }
  }

  func put(_ record: BackgroundTransferRecord) throws {
    lock.lock()
    defer { lock.unlock() }
    var values = try loadUnlocked()
    values.removeAll(where: { $0.jobId == record.jobId })
    values.append(record)
    try saveUnlocked(values)
  }

  func remove(jobId: String) throws -> BackgroundTransferRecord? {
    lock.lock()
    defer { lock.unlock() }
    var values = try loadUnlocked()
    guard let index = values.firstIndex(where: { $0.jobId == jobId }) else { return nil }
    let removed = values.remove(at: index)
    try saveUnlocked(values)
    return removed
  }

  func reconcileMissingTasks(activeTaskIdentifiers: Set<Int>) throws -> [String] {
    lock.lock()
    defer { lock.unlock() }
    var values = try loadUnlocked()
    var changed = false
    var removedPartFiles: [String] = []
    var reconciled: [BackgroundTransferRecord] = []
    for record in values {
      guard
        !record.isTerminal,
        record.taskIdentifier.map(activeTaskIdentifiers.contains) != true
      else {
        reconciled.append(record)
        continue
      }
      if record.cancelRequested == true {
        removedPartFiles.append(record.partFileName)
        changed = true
        continue
      }
      reconciled.append(
        record.terminal(
          state: "failed",
          code: "background_expired",
          retryable: true
        )
      )
      changed = true
    }
    if changed { try saveUnlocked(reconciled) }
    return removedPartFiles
  }

  func flush() -> Bool {
    lock.lock()
    defer { lock.unlock() }
    guard (try? loadUnlocked()) != nil else { return false }
    return defaults.synchronize()
  }

  private func records() throws -> [BackgroundTransferRecord] {
    lock.lock()
    defer { lock.unlock() }
    return try loadUnlocked()
  }

  private func loadUnlocked() throws -> [BackgroundTransferRecord] {
    guard let data = defaults.data(forKey: key) else { return [] }
    return try JSONDecoder().decode([BackgroundTransferRecord].self, from: data)
  }

  private func saveUnlocked(_ records: [BackgroundTransferRecord]) throws {
    defaults.set(try JSONEncoder().encode(records), forKey: key)
    guard defaults.synchronize() else { throw BackgroundTransferError.persistenceFailure }
  }
}

enum BackgroundCompletionGate {
  static func persistThenComplete(
    persist: () -> Void,
    completion: () -> Void
  ) {
    persist()
    completion()
  }
}

final class PendingTerminalStore {
  private var records: [String: BackgroundTransferRecord] = [:]

  var isEmpty: Bool { records.isEmpty }

  func retain(_ record: BackgroundTransferRecord) {
    records[record.jobId] = record
  }

  func retryPersist(
    _ persist: (BackgroundTransferRecord) throws -> Void
  ) -> [BackgroundTransferRecord] {
    var persisted: [BackgroundTransferRecord] = []
    for (jobId, record) in records {
      do {
        try persist(record)
        records.removeValue(forKey: jobId)
        persisted.append(record)
      } catch {
        continue
      }
    }
    return persisted
  }
}

private enum BackgroundTransferError: String, Error {
  case invalidArguments = "invalid_arguments"
  case sourceMissing = "source_missing"
  case readFailure = "source_read_failed"
  case persistenceFailure = "persistence_failure"

  var flutterError: FlutterError {
    let message = switch self {
    case .invalidArguments: "后台上传参数无效"
    case .sourceMissing, .readFailure: "无法读取照片"
    case .persistenceFailure: "无法保存后台上传状态"
    }
    return FlutterError(code: rawValue, message: message, details: nil)
  }
}

private extension Comparable {
  func clamped(to range: ClosedRange<Self>) -> Self {
    min(max(self, range.lowerBound), range.upperBound)
  }
}
