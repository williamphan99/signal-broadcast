import Foundation
import ImageIO

// Paths arrive through stdin. ImageIO decodes and encodes in memory; no thumbnail
// or preview file is written outside the encrypted vault (or anywhere else).
struct Request: Decodable { let path: String; let size: Int }
guard let request = try? JSONDecoder().decode(Request.self, from: FileHandle.standardInput.readDataToEndOfFile()),
      [92, 720].contains(request.size),
      let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: request.path) as CFURL, nil),
      let image = CGImageSourceCreateThumbnailAtIndex(source, 0, [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceCreateThumbnailWithTransform: true,
        kCGImageSourceThumbnailMaxPixelSize: request.size,
        kCGImageSourceShouldCache: false
      ] as CFDictionary) else { exit(1) }
let output = NSMutableData()
guard let destination = CGImageDestinationCreateWithData(output, "public.png" as CFString, 1, nil) else { exit(1) }
CGImageDestinationAddImage(destination, image, nil)
guard CGImageDestinationFinalize(destination) else { exit(1) }
FileHandle.standardOutput.write(output as Data)
