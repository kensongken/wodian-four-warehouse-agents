import CoreImage
import Foundation
import ImageIO
import UniformTypeIdentifiers
import Vision

enum ExtractorError: Error {
    case invalidArguments
    case unsafePath
    case unavailable
    case noForeground
    case invalidImage
}

func parseArguments() throws -> [String: String] {
    let arguments = Array(CommandLine.arguments.dropFirst())
    guard arguments.count == 6 else { throw ExtractorError.invalidArguments }
    let allowed = Set(["--input", "--mask-output", "--foreground-output"])
    var parsed: [String: String] = [:]
    var index = 0
    while index < arguments.count {
        let key = arguments[index]
        guard allowed.contains(key), parsed[key] == nil else {
            throw ExtractorError.invalidArguments
        }
        parsed[key] = arguments[index + 1]
        index += 2
    }
    guard parsed.count == allowed.count else { throw ExtractorError.invalidArguments }
    return parsed
}

func checkedURLs(_ arguments: [String: String]) throws -> (URL, URL, URL) {
    guard
        let inputValue = arguments["--input"],
        let maskValue = arguments["--mask-output"],
        let foregroundValue = arguments["--foreground-output"]
    else { throw ExtractorError.invalidArguments }

    let input = URL(fileURLWithPath: inputValue).standardizedFileURL
    let mask = URL(fileURLWithPath: maskValue).standardizedFileURL
    let foreground = URL(fileURLWithPath: foregroundValue).standardizedFileURL
    let root = input.deletingLastPathComponent().resolvingSymlinksInPath()
    guard
        mask.deletingLastPathComponent().resolvingSymlinksInPath() == root,
        foreground.deletingLastPathComponent().resolvingSymlinksInPath() == root,
        input.resolvingSymlinksInPath().deletingLastPathComponent() == root,
        FileManager.default.fileExists(atPath: input.path),
        !FileManager.default.fileExists(atPath: mask.path),
        !FileManager.default.fileExists(atPath: foreground.path)
    else { throw ExtractorError.unsafePath }
    return (input, mask, foreground)
}

@available(macOS 14.0, *)
func writeAlphaMask(from foregroundURL: URL, to maskOutput: URL) throws {
    guard
        let source = CGImageSourceCreateWithURL(foregroundURL as CFURL, nil),
        let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
    else { throw ExtractorError.invalidImage }

    let width = image.width
    let height = image.height
    var rgba = [UInt8](repeating: 0, count: width * height * 4)
    guard let rgbaContext = CGContext(
        data: &rgba,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: width * 4,
        space: CGColorSpace(name: CGColorSpace.sRGB)!,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else { throw ExtractorError.invalidImage }
    rgbaContext.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))

    var alpha = [UInt8](repeating: 0, count: width * height)
    for pixel in 0..<(width * height) {
        alpha[pixel] = rgba[pixel * 4 + 3]
    }
    guard
        let maskContext = CGContext(
            data: &alpha,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: width,
            space: CGColorSpaceCreateDeviceGray(),
            bitmapInfo: CGImageAlphaInfo.none.rawValue
        ),
        let maskImage = maskContext.makeImage(),
        let destination = CGImageDestinationCreateWithURL(
            maskOutput as CFURL,
            UTType.png.identifier as CFString,
            1,
            nil
        )
    else { throw ExtractorError.invalidImage }
    CGImageDestinationAddImage(destination, maskImage, nil)
    guard CGImageDestinationFinalize(destination) else {
        throw ExtractorError.invalidImage
    }
}

@available(macOS 14.0, *)
func extract(input: URL, maskOutput: URL, foregroundOutput: URL) throws {
    guard let source = CIImage(contentsOf: input) else {
        throw ExtractorError.invalidImage
    }
    let handler = VNImageRequestHandler(url: input, orientation: .up, options: [:])
    let request = VNGenerateForegroundInstanceMaskRequest()
    try handler.perform([request])
    guard let observation = request.results?.first, !observation.allInstances.isEmpty else {
        throw ExtractorError.noForeground
    }

    let maskBuffer = try observation.generateScaledMaskForImage(
        forInstances: observation.allInstances,
        from: handler
    )
    let mask = CIImage(cvPixelBuffer: maskBuffer).cropped(to: source.extent)
    let transparent = CIImage(color: .clear).cropped(to: source.extent)
    let foreground = source.applyingFilter(
        "CIBlendWithMask",
        parameters: [
            kCIInputBackgroundImageKey: transparent,
            kCIInputMaskImageKey: mask,
        ]
    )

    let context = CIContext(options: [.cacheIntermediates: false])
    try context.writePNGRepresentation(
        of: foreground,
        to: foregroundOutput,
        format: .RGBA8,
        colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
        options: [:]
    )
    try writeAlphaMask(from: foregroundOutput, to: maskOutput)
}

do {
    guard #available(macOS 14.0, *) else { throw ExtractorError.unavailable }
    let arguments = try parseArguments()
    let (input, mask, foreground) = try checkedURLs(arguments)
    try extract(input: input, maskOutput: mask, foregroundOutput: foreground)
} catch {
    FileHandle.standardError.write(Data("foreground_extraction_failed\n".utf8))
    exit(1)
}
