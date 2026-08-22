import Foundation
import Vision
import ImageIO

guard CommandLine.arguments.count > 1 else {
    FileHandle.standardError.write(Data("usage: macos_vision_ocr.swift IMAGE\n".utf8))
    exit(2)
}

let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard let source = CGImageSourceCreateWithURL(imageURL as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    FileHandle.standardError.write(Data("cannot read image\n".utf8))
    exit(1)
}

let request = VNRecognizeTextRequest { request, error in
    if let error = error {
        FileHandle.standardError.write(Data("OCR error: \(error)\n".utf8))
        exit(1)
    }
    let observations = (request.results as? [VNRecognizedTextObservation]) ?? []
    for observation in observations {
        if let candidate = observation.topCandidates(1).first {
            print(candidate.string)
        }
    }
}
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages = ["zh-Hans", "en-US"]

do {
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    try handler.perform([request])
} catch {
    FileHandle.standardError.write(Data("OCR error: \(error)\n".utf8))
    exit(1)
}
