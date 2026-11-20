import AppKit
import Foundation

// Source of assets/sudoku-icon.png (1024 px). To regenerate:
//   swiftc scripts/mac-icon.swift -o /tmp/mac-icon && /tmp/mac-icon assets/sudoku-icon.png
guard CommandLine.arguments.count == 2 else { exit(64) }
let size: CGFloat = 1024
guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size),
                                 bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                 colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { exit(1) }
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

func color(_ hex: Int, _ alpha: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat(hex >> 16 & 255) / 255, green: CGFloat(hex >> 8 & 255) / 255,
            blue: CGFloat(hex & 255) / 255, alpha: alpha)
}

// macOS icon grid: an 824 px rounded square centred on the 1024 canvas.
let tile = NSRect(x: 100, y: 100, width: 824, height: 824)
let shape = NSBezierPath(roundedRect: tile, xRadius: 185, yRadius: 185)
NSGraphicsContext.current?.saveGraphicsState()
let shadow = NSShadow()
shadow.shadowColor = color(0x000000, 0.25)
shadow.shadowOffset = NSSize(width: 0, height: -12)
shadow.shadowBlurRadius = 28
shadow.set()
color(0xFFFDF9).setFill()
shape.fill()
NSGraphicsContext.current?.restoreGraphicsState()
NSGradient(starting: color(0xFFFFFF), ending: color(0xF1ECE2))?.draw(in: shape, angle: -90)

let board = tile.insetBy(dx: 132, dy: 132)
let cell = board.width / 9
let highlight = NSRect(x: board.minX + cell * 3, y: board.minY + cell * 5, width: cell, height: cell)
color(0xC9DBFF).setFill()
highlight.fill()
// Thin lines first so the thick box lines sit cleanly on top.
for line in (0...9).filter({ $0 % 3 != 0 }) + [0, 3, 6, 9] {
    let thick = line % 3 == 0
    (thick ? color(0x2E2A25) : color(0xD8D1C3)).setStroke()
    let offset = board.minX + CGFloat(line) * cell
    let path = NSBezierPath()
    path.lineWidth = thick ? 14 : 4
    path.lineCapStyle = thick ? .square : .butt
    path.move(to: NSPoint(x: offset, y: board.minY))
    path.line(to: NSPoint(x: offset, y: board.maxY))
    path.move(to: NSPoint(x: board.minX, y: board.minY + CGFloat(line) * cell))
    path.line(to: NSPoint(x: board.maxX, y: board.minY + CGFloat(line) * cell))
    path.stroke()
}
let digits: [(Int, Int, String, Bool)] = [(0, 8, "5", true), (1, 7, "3", true), (4, 8, "7", true),
                                          (3, 5, "9", false), (6, 6, "1", true), (8, 4, "6", true),
                                          (2, 3, "8", false), (5, 2, "4", true), (7, 1, "2", false),
                                          (1, 0, "6", true), (4, 1, "1", true), (8, 0, "9", true)]
for (column, row, text, given) in digits {
    let font = NSFont.systemFont(ofSize: cell * 0.62, weight: given ? .bold : .medium)
    let attributes: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: given ? color(0x2E2A25) : color(0x2F63D6)]
    let string = NSAttributedString(string: text, attributes: attributes)
    let bounds = string.size()
    string.draw(at: NSPoint(x: board.minX + CGFloat(column) * cell + (cell - bounds.width) / 2,
                            y: board.minY + CGFloat(row) * cell + (cell - bounds.height) / 2))
}
NSGraphicsContext.restoreGraphicsState()
guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
do { try png.write(to: URL(fileURLWithPath: CommandLine.arguments[1])) } catch { exit(1) }
