import CoreImage
import Foundation
import Vision

private func fail(_ code: Int32, _ message: String) -> Never {
    FileHandle.standardError.write(Data("\(message)\n".utf8))
    exit(code)
}

guard #available(macOS 14.0, *) else {
    fail(2, "vision_unavailable")
}

guard CommandLine.arguments.count == 6,
      let maxWidth = Double(CommandLine.arguments[4]),
      let maxHeight = Double(CommandLine.arguments[5]),
      maxWidth > 0,
      maxHeight > 0 else {
    fail(2, "usage: input subject-output mask-output max-width max-height")
}

let inputURL = URL(fileURLWithPath: CommandLine.arguments[1])
let subjectURL = URL(fileURLWithPath: CommandLine.arguments[2])
let maskURL = URL(fileURLWithPath: CommandLine.arguments[3])
guard let original = CIImage(
    contentsOf: inputURL,
    options: [.applyOrientationProperty: true]
) else {
    fail(3, "source_invalid")
}

private func foregroundMask(for image: CIImage) throws -> CIImage? {
    let request = VNGenerateForegroundInstanceMaskRequest()
    let handler = VNImageRequestHandler(ciImage: image, options: [:])
    try handler.perform([request])
    guard let observation = request.results?.first else {
        return nil
    }
    let instances = observation.allInstances
    guard instances.count == 1 else {
        fail(4, "foreground_ambiguous")
    }
    let buffer = try observation.generateScaledMaskForImage(
        forInstances: instances,
        from: handler
    )
    return CIImage(cvPixelBuffer: buffer)
}

private func salientCrop(for image: CIImage) throws -> CIImage {
    let request = VNGenerateObjectnessBasedSaliencyImageRequest()
    let handler = VNImageRequestHandler(ciImage: image, options: [:])
    try handler.perform([request])
    guard let observations = request.results?.first?.salientObjects,
          !observations.isEmpty else {
        fail(3, "foreground_missing")
    }
    let ranked = observations.sorted { $0.confidence > $1.confidence }
    if ranked.count > 1 && ranked[0].confidence - ranked[1].confidence < 0.10 {
        fail(4, "foreground_ambiguous")
    }
    let box = ranked[0].boundingBox
    let extent = image.extent
    var crop = CGRect(
        x: extent.minX + box.minX * extent.width,
        y: extent.minY + box.minY * extent.height,
        width: box.width * extent.width,
        height: box.height * extent.height
    )
    let padding = max(crop.width, crop.height) * 0.18
    crop = crop.insetBy(dx: -padding, dy: -padding).intersection(extent)
    guard crop.width > 1, crop.height > 1 else {
        fail(3, "foreground_missing")
    }
    return image.cropped(to: crop)
}

private func normalized(_ image: CIImage, maxWidth: Double, maxHeight: Double) -> CIImage {
    let originNormalized = image.transformed(
        by: CGAffineTransform(
            translationX: -image.extent.minX,
            y: -image.extent.minY
        )
    )
    let scale = min(
        maxWidth / originNormalized.extent.width,
        maxHeight / originNormalized.extent.height,
        1.0
    )
    return originNormalized.transformed(
        by: CGAffineTransform(scaleX: scale, y: scale)
    )
}

do {
    var subject = original
    var mask = try foregroundMask(for: subject)
    if mask == nil {
        subject = try salientCrop(for: original)
        mask = try foregroundMask(for: subject)
    }
    guard let extractedMask = mask else {
        fail(3, "foreground_missing")
    }

    let fittedSubject = normalized(subject, maxWidth: maxWidth, maxHeight: maxHeight)
    let fittedMask = normalized(
        extractedMask,
        maxWidth: Double(fittedSubject.extent.width),
        maxHeight: Double(fittedSubject.extent.height)
    )
    guard fittedSubject.extent.integral.size == fittedMask.extent.integral.size else {
        fail(3, "mask_dimensions_mismatch")
    }

    let context = CIContext(options: [.cacheIntermediates: false])
    try context.writePNGRepresentation(
        of: fittedSubject,
        to: subjectURL,
        format: .RGBA8,
        colorSpace: CGColorSpaceCreateDeviceRGB()
    )
    try context.writePNGRepresentation(
        of: fittedMask,
        to: maskURL,
        format: .L8,
        colorSpace: CGColorSpaceCreateDeviceGray()
    )
} catch {
    fail(3, "foreground_failed")
}
