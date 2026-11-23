import Flutter
import UIKit

@main
@objc class AppDelegate: FlutterAppDelegate, FlutterImplicitEngineDelegate {
  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }

  func didInitializeImplicitFlutterEngine(_ engineBridge: FlutterImplicitEngineBridge) {
    GeneratedPluginRegistrant.register(with: engineBridge.pluginRegistry)
    Self.registerMediaBridge(
      using: engineBridge.pluginRegistry.registrar(forPlugin: "MediaBridge")
    )
    Self.registerBackgroundTransferBridge(
      using: engineBridge.pluginRegistry.registrar(forPlugin: "BackgroundTransferBridge")
    )
  }

  static func registerMediaBridge(
    using registrar: FlutterPluginRegistrar?,
    register: (FlutterPluginRegistrar) -> Void = { MediaBridge.register(with: $0) }
  ) {
    guard let registrar else { return }
    register(registrar)
  }

  static func registerBackgroundTransferBridge(
    using registrar: FlutterPluginRegistrar?,
    register: (FlutterPluginRegistrar) -> Void = { BackgroundTransferBridge.register(with: $0) }
  ) {
    guard let registrar else { return }
    register(registrar)
  }

  override func application(
    _ application: UIApplication,
    handleEventsForBackgroundURLSession identifier: String,
    completionHandler: @escaping () -> Void
  ) {
    if !BackgroundTransferBridge.shared.handleEvents(
      identifier: identifier,
      completionHandler: completionHandler
    ) {
      super.application(
        application,
        handleEventsForBackgroundURLSession: identifier,
        completionHandler: completionHandler
      )
    }
  }
}
