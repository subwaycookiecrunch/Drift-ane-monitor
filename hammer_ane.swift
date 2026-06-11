import Vision
import CoreImage
import Foundation
import Dispatch

print("🔥 Brutally Hammering ANE... Press Ctrl+C to stop.")

let args = CommandLine.arguments
var mode = "ocr"
var threads = 8

var i = 1
while i < args.count {
    if args[i] == "--mode" && i + 1 < args.count {
        mode = args[i+1]
        i += 2
    } else if let t = Int(args[i]) {
        threads = t
        i += 1
    } else {
        i += 1
    }
}

print("Mode: \(mode.uppercased()) | Threads: \(threads)")

let width = 2000
let height = 2000
let bytesPerPixel = 4
let bytesPerRow = width * bytesPerPixel
var pixelData = [UInt8](repeating: 128, count: width * height * bytesPerPixel)

guard let cgImage = CGImage(
    width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32,
    bytesPerRow: bytesPerRow, space: CGColorSpaceCreateDeviceRGB(),
    bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
    provider: CGDataProvider(data: Data(pixelData) as CFData)!,
    decode: nil, shouldInterpolate: false, intent: .defaultIntent
) else {
    fatalError("Could not create image")
}

var totalInferences: Int64 = 0
let queue = DispatchQueue(label: "reporter")
var lastReports = [Int: Date]()
var counts = [Int: Int]()

for t in 0..<threads {
    lastReports[t] = Date()
    counts[t] = 0
}

signal(SIGINT) { _ in
    print("\nTotal inferences completed: \(totalInferences)")
    exit(0)
}

DispatchQueue.concurrentPerform(iterations: threads) { threadId in
    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    var request: VNRequest
    if mode == "face" {
        request = VNDetectFaceRectanglesRequest()
    } else {
        let r = VNRecognizeTextRequest()
        r.recognitionLevel = .accurate
        request = r
    }
    
    while true {
        do {
            try handler.perform([request])
            queue.async {
                totalInferences += 1
                counts[threadId]! += 1
                let now = Date()
                if now.timeIntervalSince(lastReports[threadId]!) > 1.0 {
                    print("Thread \(threadId): \(counts[threadId]!) inferences/sec · ANE Active")
                    lastReports[threadId] = now
                    counts[threadId] = 0
                }
            }
        } catch {
        }
    }
}
