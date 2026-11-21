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
    var faces: [[String: Double]] = []
    // A small webcam face can be missed when Vision resizes a full 1080p
    // gameplay frame. Search each side separately as well, then map every
    // result back to source coordinates. No source-specific camera position.
    let width = Double(image.width), height = Double(image.height)
    let regions = [CGRect(x: 0, y: 0, width: width, height: height),
                   CGRect(x: 0, y: 0, width: floor(width * 0.32), height: height),
                   CGRect(x: floor(width * 0.68), y: 0,
                          width: width - floor(width * 0.68), height: height)]
    for region in regions {
        guard let sample = image.cropping(to: region) else { continue }
        let request = VNDetectFaceRectanglesRequest()
        let handler = VNImageRequestHandler(cgImage: sample, options: [:])
        do {
            try handler.perform([request])
        } catch {
            request.usesCPUOnly = true
            do { try handler.perform([request]) } catch { continue }
        }
        for face in request.results ?? [] {
            let b = face.boundingBox
            let mapped: [String: Double] = [
                "x": (region.minX + b.minX * region.width) / width,
                "y": (region.minY + (1 - b.maxY) * region.height) / height,
                "width": b.width * region.width / width,
                "height": b.height * region.height / height,
                "confidence": Double(face.confidence)]
            let rect = CGRect(x: mapped["x"]!, y: mapped["y"]!,
                              width: mapped["width"]!, height: mapped["height"]!)
            if let index = faces.firstIndex(where: { old in
                let prior = CGRect(x: old["x"]!, y: old["y"]!,
                                   width: old["width"]!, height: old["height"]!)
                let overlap = rect.intersection(prior)
                return !overlap.isNull && overlap.width * overlap.height >
                    min(rect.width * rect.height, prior.width * prior.height) * 0.6
            }) {
                if mapped["confidence"]! > faces[index]["confidence"]! { faces[index] = mapped }
            } else {
                faces.append(mapped)
            }
        }
    }
    output.append(["file": file.lastPathComponent, "faces": faces])
}
let data = try JSONSerialization.data(withJSONObject: output, options: [.prettyPrinted, .sortedKeys])
print(String(data: data, encoding: .utf8)!)
