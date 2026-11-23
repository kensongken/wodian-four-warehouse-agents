import XCTest
import PhotosUI
@testable import Runner

final class MediaBridgeTests: XCTestCase {
  func testNilPluginRegistrarSkipsMediaBridgeRegistration() {
    var registrations = 0

    AppDelegate.registerMediaBridge(using: nil) { _ in
      registrations += 1
    }

    XCTAssertEqual(registrations, 0)
  }

  func testPickerUsesCurrentAssetRepresentationWithoutLibraryPermission() {
    let configuration = MediaBridge.pickerConfiguration(remainingSlots: 17)

    XCTAssertEqual(configuration.selectionLimit, 17)
    XCTAssertEqual(configuration.preferredAssetRepresentationMode, .current)
  }

  func testPickerConfigurationAcceptsDeclaredTenThousandAdapterCeiling() {
    let configuration = MediaBridge.pickerConfiguration(remainingSlots: 10_000)

    XCTAssertEqual(configuration.selectionLimit, 10_000)
  }

  func testPickerRemainingSlotBoundaryAcceptsTenThousandAndRejectsNextValue() {
    XCTAssertTrue(MediaBridge.isValidRemainingSlots(10_000))
    XCTAssertFalse(MediaBridge.isValidRemainingSlots(10_001))
  }

  func testNormalizesHEICHEIFJPEGAndPNGWithoutRecompression() {
    XCTAssertEqual(MediaBridge.normalizedMediaType("public.heic"), "image/heic")
    XCTAssertEqual(MediaBridge.normalizedMediaType("public.heif"), "image/heic")
    XCTAssertEqual(MediaBridge.normalizedMediaType("public.jpeg"), "image/jpeg")
    XCTAssertEqual(MediaBridge.normalizedMediaType("public.png"), "image/png")
    XCTAssertNil(MediaBridge.normalizedMediaType("com.adobe.raw-image"))
  }

  func testLivePhotoSelectionPrefersItsStaticImageRepresentation() {
    XCTAssertEqual(
      MediaBridge.preferredStaticType([
        "com.apple.live-photo",
        "public.heic",
        "public.jpeg",
      ]),
      "public.heic"
    )
  }

  func testItemProviderFailureIsReturnedAsVisibleUnsupportedAsset() {
    let unsupported = MediaBridge.itemFailure(
      id: "ios:failed-item",
      displayName: "无法读取的照片",
      mediaType: nil
    )

    XCTAssertEqual(unsupported["id"] as? String, "ios:failed-item")
    XCTAssertEqual(unsupported["name"] as? String, "无法读取的照片")
    XCTAssertEqual(unsupported["reason"] as? String, "read_failure")
  }

  func testReleaseRejectsTraversalAndDoesNotDeleteOutsideStaging() throws {
    let sandbox = FileManager.default.temporaryDirectory
      .appendingPathComponent(UUID().uuidString, isDirectory: true)
    let root = sandbox.appendingPathComponent("UploadStaging", isDirectory: true)
    let outside = sandbox.appendingPathComponent("keep.txt")
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    try Data("keep".utf8).write(to: outside)
    defer { try? FileManager.default.removeItem(at: sandbox) }

    XCTAssertThrowsError(
      try MediaBridge.releaseStagedFile(
        id: "ios:../../keep",
        locator: "../../keep.txt/original",
        stagingRoot: root
      )
    )
    XCTAssertTrue(FileManager.default.fileExists(atPath: outside.path))
  }

  func testDuplicateAcquisitionsUseIndependentLeaseDirectories() throws {
    let sandbox = FileManager.default.temporaryDirectory
      .appendingPathComponent(UUID().uuidString, isDirectory: true)
    let root = sandbox.appendingPathComponent("UploadStaging", isDirectory: true)
    let source = sandbox.appendingPathComponent("source.jpg")
    let token = String(repeating: "a", count: 64)
    let id = "ios:\(token)"
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    try Data("photo".utf8).write(to: source)
    defer { try? FileManager.default.removeItem(at: sandbox) }

    let first = try MediaBridge.stage(
      sourceURL: source,
      stableToken: token,
      leaseToken: "11111111-1111-4111-8111-111111111111",
      stagingRoot: root
    )
    let second = try MediaBridge.stage(
      sourceURL: source,
      stableToken: token,
      leaseToken: "22222222-2222-4222-8222-222222222222",
      stagingRoot: root
    )

    XCTAssertNotEqual(first.locator, second.locator)
    try MediaBridge.releaseStagedFile(id: id, locator: first.locator, stagingRoot: root)
    let secondURL = root.appendingPathComponent(second.locator)
    XCTAssertEqual(try Data(contentsOf: secondURL), Data("photo".utf8))
    try MediaBridge.releaseStagedFile(id: id, locator: second.locator, stagingRoot: root)
    XCTAssertFalse(
      FileManager.default.fileExists(
        atPath: root.appendingPathComponent(token, isDirectory: true).path
      )
    )
  }
}
