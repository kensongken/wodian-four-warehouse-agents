import CoreGraphics
import Foundation
import ImageIO
import Vision

struct Classification: Codable {
    let identifier: String
    let confidence: Double
}

struct Observation: Codable {
    let classifications: [Classification]
    let dominant_color: String
    let brightness: String
    let image_width: Int
    let image_height: Int
}

func colorSummary(_ image: CGImage) -> (String, String) {
    let sampleWidth = 64
    let sampleHeight = 64
    let bytesPerPixel = 4
    let bytesPerRow = sampleWidth * bytesPerPixel
    var pixels = [UInt8](repeating: 0, count: sampleHeight * bytesPerRow)
    let bitmapInfo = CGImageAlphaInfo.premultipliedLast.rawValue
        | CGBitmapInfo.byteOrder32Big.rawValue
    guard let context = CGContext(
        data: &pixels,
        width: sampleWidth,
        height: sampleHeight,
        bitsPerComponent: 8,
        bytesPerRow: bytesPerRow,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: bitmapInfo
    ) else {
        return ("unknown", "medium")
    }
    context.interpolationQuality = .medium
    context.draw(
        image,
        in: CGRect(x: 0, y: 0, width: sampleWidth, height: sampleHeight)
    )

    var counts: [String: Int] = [:]
    var brightnessTotal = 0.0
    for offset in stride(from: 0, to: pixels.count, by: bytesPerPixel) {
        let red = Double(pixels[offset])
        let green = Double(pixels[offset + 1])
        let blue = Double(pixels[offset + 2])
        let maximum = max(red, green, blue)
        let minimum = min(red, green, blue)
        let pixelBrightness = (red + green + blue) / 3
        brightnessTotal += pixelBrightness

        if pixelBrightness < 70 || maximum - minimum < 28 { continue }
        let label: String?
        if green > red * 1.15 && green > blue * 1.08 {
            label = "green"
        } else if blue > red * 1.15 && blue > green * 1.08 {
            label = "blue"
        } else if red > 165 && green > 60 && green < 180 && blue < 115 {
            label = "orange"
        } else if red > green * 1.22 && red > blue * 1.18 {
            label = "red"
        } else if red > 70 && green > 30 && green < red * 0.86 && blue < green {
            label = "brown"
        } else {
            label = nil
        }
        if let label { counts[label, default: 0] += 1 }
    }

    let pixelCount = Double(sampleWidth * sampleHeight)
    let averageBrightness = brightnessTotal / pixelCount
    let brightness = averageBrightness > 180
        ? "light"
        : (averageBrightness < 70 ? "dark" : "medium")
    let ranked = counts.sorted { left, right in left.value > right.value }
    if let first = ranked.first, Double(first.value) / pixelCount >= 0.02 {
        if ranked.count > 1 && Double(ranked[1].value) > Double(first.value) * 0.65 {
            return ("multicolor", brightness)
        }
        return (first.key, brightness)
    }
    if averageBrightness > 215 { return ("white", brightness) }
    if averageBrightness < 45 { return ("black", brightness) }
    return ("unknown", brightness)
}

guard CommandLine.arguments.count == 2 else {
    exit(2)
}

let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard
    let source = CGImageSourceCreateWithURL(imageURL as CFURL, nil),
    let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
else {
    exit(4)
}

var classifications: [Classification] = []
let request = VNClassifyImageRequest()
do {
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    classifications = (request.results ?? []).prefix(10).map {
        Classification(identifier: $0.identifier, confidence: Double($0.confidence))
    }
} catch {
    classifications = []
}

let (dominantColor, brightness) = colorSummary(image)
let observation = Observation(
    classifications: classifications,
    dominant_color: dominantColor,
    brightness: brightness,
    image_width: image.width,
    image_height: image.height
)

do {
    let data = try JSONEncoder().encode(observation)
    FileHandle.standardOutput.write(data)
} catch {
    exit(5)
}
