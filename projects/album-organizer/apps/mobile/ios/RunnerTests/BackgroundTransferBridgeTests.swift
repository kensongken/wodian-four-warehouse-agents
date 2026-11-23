import XCTest
@testable import Runner

final class BackgroundTransferBridgeTests: XCTestCase {
  func testNilRegistrarSkipsBackgroundBridgeRegistration() {
    var registrations = 0

    AppDelegate.registerBackgroundTransferBridge(using: nil) { _ in
      registrations += 1
    }

    XCTAssertEqual(registrations, 0)
  }

  func testSignedURLExpiryAndMissingSourceHaveStableClassifications() {
    let expired = BackgroundTransferBridge.classify(error: nil, statusCode: 403)
    let missing = BackgroundTransferBridge.classify(
      error: URLError(.fileDoesNotExist),
      statusCode: nil
    )

    XCTAssertEqual(expired.code, "signed_url_expired")
    XCTAssertTrue(expired.retryable)
    XCTAssertEqual(missing.code, "source_missing")
    XCTAssertFalse(missing.retryable)
  }

  func testLedgerPersistsETagForTerminatedFlutterReconciliation() throws {
    let suite = "BackgroundTransferBridgeTests.\(UUID().uuidString)"
    let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
    defer { defaults.removePersistentDomain(forName: suite) }
    let key = "test-ledger"
    let record = BackgroundTransferRecord(
      jobId: "job-1",
      taskIdentifier: 17,
      state: "succeeded",
      progress: 100,
      partFileName: "job-1.part",
      etag: "etag-persisted",
      code: nil,
      retryable: nil
    )

    try BackgroundTransferLedger(defaults: defaults, key: key).put(record)
    let reopened = BackgroundTransferLedger(defaults: defaults, key: key)

    XCTAssertEqual(try reopened.record(for: "job-1")?.etag, "etag-persisted")
    XCTAssertEqual(try reopened.record(taskIdentifier: 17)?.state, "succeeded")
  }

  func testCorruptLedgerFailsClosedInsteadOfReportingJobsMissing() throws {
    let suite = "BackgroundTransferBridgeTests.\(UUID().uuidString)"
    let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
    defer { defaults.removePersistentDomain(forName: suite) }
    let key = "corrupt-ledger"
    defaults.set(Data("not-json".utf8), forKey: key)

    let ledger = BackgroundTransferLedger(defaults: defaults, key: key)

    XCTAssertThrowsError(try ledger.record(for: "job-1"))
  }

  func testBackgroundCompletionRunsOnlyAfterDurablePersistence() {
    var order: [String] = []

    BackgroundCompletionGate.persistThenComplete(
      persist: { order.append("persisted") },
      completion: { order.append("completed") }
    )

    XCTAssertEqual(order, ["persisted", "completed"])
  }

  func testUndurableTerminalIsRetainedAndRetriedBeforeCompletion() {
    let store = PendingTerminalStore()
    let terminal = BackgroundTransferRecord(
      jobId: "job-retry",
      taskIdentifier: 23,
      state: "succeeded",
      progress: 100,
      partFileName: "job-retry.part",
      etag: "etag-retry",
      code: nil,
      retryable: nil
    )
    store.retain(terminal)
    var attempts = 0

    let first = store.retryPersist { _ in
      attempts += 1
      throw CocoaError(.fileWriteUnknown)
    }
    let second = store.retryPersist { _ in attempts += 1 }

    XCTAssertTrue(first.isEmpty)
    XCTAssertEqual(second.map(\.etag), ["etag-retry"])
    XCTAssertEqual(attempts, 2)
    XCTAssertTrue(store.isEmpty)
  }

  func testJobIdentifierValidationRejectsControlCharacters() {
    XCTAssertTrue(BackgroundTransferBridge.isSafeJobId("task:asset:1"))
    XCTAssertFalse(BackgroundTransferBridge.isSafeJobId("task\nasset"))
    XCTAssertFalse(BackgroundTransferBridge.isSafeJobId("task\u{0}asset"))
    XCTAssertFalse(BackgroundTransferBridge.isSafeJobId(String(repeating: "x", count: 513)))
  }

  func testQueryRecoveryJobIdBoundaryAcceptsTenThousandAndRejectsNextValue() {
    let accepted = (0..<10_000).map { "task:asset:\($0)" }
    let rejected = (0..<10_001).map { "task:asset:\($0)" }

    XCTAssertEqual(
      BackgroundTransferBridge.validatedQueryJobIds(["jobIds": accepted])?.count,
      10_000
    )
    XCTAssertNil(BackgroundTransferBridge.validatedQueryJobIds(["jobIds": rejected]))
  }

  func testNativeByteRangeRejectsIntegerOverflow() {
    XCTAssertTrue(BackgroundTransferBridge.isSafeRange(offset: 0, length: 1))
    XCTAssertFalse(BackgroundTransferBridge.isSafeRange(offset: Int.max, length: 1))
    XCTAssertFalse(BackgroundTransferBridge.isSafeRange(offset: 0, length: 0))
  }

  func testCancellationIntentSurvivesBridgeRecreation() throws {
    let suite = "BackgroundTransferBridgeTests.\(UUID().uuidString)"
    let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
    defer { defaults.removePersistentDomain(forName: suite) }
    let key = "cancel-ledger"
    let record = BackgroundTransferRecord(
      jobId: "cancel-job",
      taskIdentifier: 31,
      state: "running",
      progress: 50,
      partFileName: "cancel-job.part",
      etag: nil,
      code: nil,
      retryable: nil
    )
    try BackgroundTransferLedger(defaults: defaults, key: key).put(
      record.requestingCancellation()
    )

    let reopened = BackgroundTransferLedger(defaults: defaults, key: key)

    XCTAssertEqual(
      try reopened.cancellationRecords().compactMap(\.taskIdentifier),
      [31]
    )
  }

  func testEnqueueIntentIsDurableBeforeTaskAssignmentAndCanBeAdopted() throws {
    let suite = "BackgroundTransferBridgeTests.\(UUID().uuidString)"
    let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
    defer { defaults.removePersistentDomain(forName: suite) }
    let key = "enqueue-intent-ledger"
    let intent = BackgroundTransferRecord(
      jobId: "intent-job",
      taskIdentifier: nil,
      state: "queued",
      progress: 0,
      partFileName: "intent-job.part",
      etag: nil,
      code: nil,
      retryable: nil
    )

    try BackgroundTransferLedger(defaults: defaults, key: key).put(intent)
    let reopened = BackgroundTransferLedger(defaults: defaults, key: key)
    let restored = try XCTUnwrap(reopened.record(for: "intent-job"))
    let adopted = restored.assigning(taskIdentifier: 44)
    try reopened.put(adopted)

    XCTAssertNil(restored.taskIdentifier)
    XCTAssertEqual(try reopened.record(taskIdentifier: 44)?.jobId, "intent-job")
  }

  func testMappedSuspendedTaskIsResumedButCancellationIntentIsNot() {
    let queued = BackgroundTransferRecord(
      jobId: "queued-job",
      taskIdentifier: 44,
      state: "queued",
      progress: 0,
      partFileName: "queued-job.part",
      etag: nil,
      code: nil,
      retryable: nil
    )

    XCTAssertTrue(
      BackgroundTransferBridge.shouldResume(
        record: queued,
        taskState: .suspended
      )
    )
    XCTAssertFalse(
      BackgroundTransferBridge.shouldResume(
        record: queued.requestingCancellation(),
        taskState: .suspended
      )
    )
    XCTAssertFalse(
      BackgroundTransferBridge.shouldResume(record: queued, taskState: .running)
    )
  }

  func testEnqueueIntentWithoutNativeTaskBecomesDurableRetry() throws {
    let suite = "BackgroundTransferBridgeTests.\(UUID().uuidString)"
    let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
    defer { defaults.removePersistentDomain(forName: suite) }
    let ledger = BackgroundTransferLedger(
      defaults: defaults,
      key: "missing-intent-ledger"
    )
    try ledger.put(
      BackgroundTransferRecord(
        jobId: "missing-intent",
        taskIdentifier: nil,
        state: "queued",
        progress: 0,
        partFileName: "missing-intent.part",
        etag: nil,
        code: nil,
        retryable: nil
      )
    )

    let deleted = try ledger.reconcileMissingTasks(activeTaskIdentifiers: [])
    let recovered = try XCTUnwrap(ledger.record(for: "missing-intent"))

    XCTAssertTrue(deleted.isEmpty)
    XCTAssertEqual(recovered.state, "failed")
    XCTAssertEqual(recovered.code, "background_expired")
    XCTAssertEqual(recovered.retryable, true)
  }
}
