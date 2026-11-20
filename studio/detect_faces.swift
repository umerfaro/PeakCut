// Sampled face detection for framing QA. Outputs normalized top-left rectangles.
import Foundation
import Vision
import ImageIO

let directory = URL(fileURLWithPath: CommandLine.arguments[1])
let files = try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
    .filter { $0.pathExtension.lowercased() == "jpg" }.sorted { $0.path < $1.path }
var output: [[String: Any]] = []
for file in files {
    guard let source = CGImageSourceCreateWithURL(file as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { continue }
    let request = VNDetectFaceRectanglesRequest()
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do {
        try handler.perform([request])
    } catch {
        request.usesCPUOnly = true
        do { try handler.perform([request]) } catch { exit(1) }
    }
    let faces = (request.results ?? []).map { face -> [String: Double] in
        let b = face.boundingBox
        return ["x": b.minX, "y": 1-b.maxY, "width": b.width, "height": b.height,
                "confidence": Double(face.confidence)]
    }
    output.append(["file": file.lastPathComponent, "faces": faces])
}
let data = try JSONSerialization.data(withJSONObject: output, options: [.prettyPrinted, .sortedKeys])
print(String(data: data, encoding: .utf8)!)
