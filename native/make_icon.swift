// 生成 App 图标：三条进度条（蓝/绿/橙）+ 深色 squircle 底
//
// 用法：swiftc make_icon.swift -o /tmp/make_icon && /tmp/make_icon
// 产物：native/AppIcon.iconset/（再由 iconutil 打成 AppIcon.icns，见 build.sh）
//
// 设计：macOS 26 squircle（连续曲率），1024×1024 画布，内容 824×824 居中。
// 配色与 App 内语义色呼应：蓝=正常、绿=宽裕、橙=预警。

import AppKit
import SwiftUI

let baseDir = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
let iconsetDir = baseDir.appendingPathComponent("AppIcon.iconset")

let fm = FileManager.default
try? fm.removeItem(at: iconsetDir)
try fm.createDirectory(at: iconsetDir, withIntermediateDirectories: true)

func nsColor(_ hex: UInt32, _ alpha: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255,
            green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255,
            alpha: alpha)
}

// ---------- 画 1024×1024 ----------
let canvas = NSImage(size: NSSize(width: 1024, height: 1024))
canvas.lockFocus()
let ctx = NSGraphicsContext.current!.cgContext

let squircleRect = CGRect(x: 100, y: 100, width: 824, height: 824)
let squirclePath = Path(roundedRect: squircleRect, cornerRadius: 186,
                        style: .continuous).cgPath

// 底色：深灰垂直渐变 + 柔和投影
ctx.saveGState()
ctx.setShadow(offset: NSSize(width: 0, height: -20), blur: 40,
              color: NSColor.black.withAlphaComponent(0.30).cgColor)
ctx.addPath(squirclePath)
ctx.clip()
let bgGradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                            colors: [nsColor(0x34343A).cgColor,
                                     nsColor(0x161619).cgColor] as CFArray,
                            locations: [0, 1])!
ctx.drawLinearGradient(bgGradient,
                       start: CGPoint(x: 512, y: 924),
                       end: CGPoint(x: 512, y: 100),
                       options: [])
ctx.restoreGState()

// 顶部高光：白 8% 渐隐到 55% 高度，模拟玻璃质感
ctx.saveGState()
ctx.addPath(squirclePath)
ctx.clip()
let highlight = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                           colors: [NSColor.white.withAlphaComponent(0.08).cgColor,
                                    NSColor.white.withAlphaComponent(0).cgColor] as CFArray,
                           locations: [0, 1])!
ctx.drawLinearGradient(highlight,
                       start: CGPoint(x: 512, y: 924),
                       end: CGPoint(x: 512, y: 420),
                       options: [])
ctx.restoreGState()

// ---------- 三条进度条 ----------
func drawBar(y: CGFloat, fraction: CGFloat, fillHex: UInt32) {
    let barRect = CGRect(x: 142, y: y, width: 740, height: 74)
    // 轨道：白 13%
    let track = Path(roundedRect: barRect, cornerRadius: 37,
                     style: .continuous).cgPath
    ctx.addPath(track)
    ctx.setFillColor(NSColor.white.withAlphaComponent(0.13).cgColor)
    ctx.fillPath()

    // 填充：按进度画圆角 pill（最小宽度保证两端半圆完整）
    let fillWidth = max(74, 740 * fraction)
    let fillRect = CGRect(x: barRect.origin.x, y: barRect.origin.y,
                          width: fillWidth, height: 74)
    ctx.addPath(Path(roundedRect: fillRect, cornerRadius: 37,
                     style: .continuous).cgPath)
    ctx.setFillColor(nsColor(fillHex).cgColor)
    ctx.fillPath()
}

// 三条等高、等距，整体垂直居中：高 74、距 58，总高 338
// （bottom-origin 坐标：底层条 y 最小）
drawBar(y: 343, fraction: 0.22, fillHex: 0xFF9F0A)  // 橙：预警
drawBar(y: 475, fraction: 0.48, fillHex: 0x34C759)  // 绿：宽裕
drawBar(y: 607, fraction: 0.72, fillHex: 0x4A9DF8)  // 蓝：正常

canvas.unlockFocus()

// ---------- 导出 iconset 全尺寸 ----------
// iconutil 要求的命名：每档 1x + @2x
let sizes: [(name: String, pixels: Int)] = [
    ("16x16", 16), ("16x16@2x", 32),
    ("32x32", 32), ("32x32@2x", 64),
    ("128x128", 128), ("128x128@2x", 256),
    ("256x256", 256), ("256x256@2x", 512),
    ("512x512", 512), ("512x512@2x", 1024),
]

for entry in sizes {
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: entry.pixels, pixelsHigh: entry.pixels,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    rep.size = NSSize(width: entry.pixels, height: entry.pixels)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    canvas.draw(in: NSRect(x: 0, y: 0, width: entry.pixels, height: entry.pixels),
                from: NSRect(x: 0, y: 0, width: 1024, height: 1024),
                operation: .sourceOver, fraction: 1.0,
                respectFlipped: false,
                hints: [.interpolation: NSImageInterpolation.high.rawValue])
    NSGraphicsContext.restoreGraphicsState()
    let png = rep.representation(using: .png, properties: [:])!
    let out = iconsetDir.appendingPathComponent("icon_\(entry.name).png")
    try png.write(to: out)
    print("  icon_\(entry.name).png")
}

print("iconset 写入完成: \(iconsetDir.path)")
