import Foundation
import Vision
import AppKit

guard CommandLine.arguments.count > 1,
      let img = NSImage(contentsOfFile: CommandLine.arguments[1]),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    print("ERR: cannot load image")
    exit(1)
}
let request = VNRecognizeTextRequest { req, _ in
    guard let obs = req.results as? [VNRecognizedTextObservation] else { return }
    for o in obs {
        if let t = o.topCandidates(1).first {
            let b = o.boundingBox
            // Vision coords: bottom-left origin, normalized. Convert to top-left pixels.
            let x = Int(b.origin.x * CGFloat(cg.width))
            let y = Int((1 - b.origin.y - b.height) * CGFloat(cg.height))
            let w = Int(b.width * CGFloat(cg.width))
            let h = Int(b.height * CGFloat(cg.height))
            print("\(t.string)\t[\(x),\(y),\(w),\(h)]")
        }
    }
}
request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hans", "en-US"]
let handler = VNImageRequestHandler(cgImage: cg, options: [:])
try? handler.perform([request])
