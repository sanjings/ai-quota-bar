// QuotaBarView — 菜单栏自绘视图（Stats 风格）
//
// 为什么不用 NSStatusItem.button.attributedTitle：
//   1. 垂直居中：attributedTitle 贴菜单栏上沿，只能用 baselineOffset 猜；
//      自绘可以按字体度量精确居中。
//   2. 列内对齐：自绘让每列标签相对自己的数值居中（Stats 的样子），
//      空格补齐方案做不到。
//   3. 无行高裁切风险（attributedTitle 的 maximumLineHeight 小于字体
//      行高时会裁掉字形）。
//
// 配色约定：菜单栏一律系统文字色（深色菜单栏下呈白色，自动适配），
// 预警配色只出现在下拉菜单里（见 main.swift 的 buildMenu）。

import Cocoa

struct QuotaColumn {
    let label: String
    let value: String
}

enum QuotaBarContent {
    case placeholder(String)
    case error(String)
    case columns([QuotaColumn])
}

final class QuotaBarView: NSView {

    var content: QuotaBarContent = .placeholder("…") {
        didSet {
            needsDisplay = true
            invalidateIntrinsicContentSize()
        }
    }

    /// 点击（左/右键）回调，由 main.swift 接去弹菜单
    var onClick: (() -> Void)?

    // MARK: - 布局参数

    /// 布局参考高度：状态栏项窗口的真实高度（Tahoe 刘海屏为 33pt）。
    /// 系统给按钮的高度仍是老公约 22pt（即便窗口 33pt），按 bounds
    /// 布局会永远拿不到大字号分支，垂直中心也会偏低约 5pt。
    private var layoutHeight: CGFloat { window?.frame.height ?? bounds.height }

    /// 数值字号随菜单栏高度自适应：高栏（刘海机型）用 12pt 对齐 Stats 观感，
    /// 矮栏保持 10.5pt（再大两行排不下会裁切）。
    private var valueFontSize: CGFloat { layoutHeight >= 23.5 ? 12 : 10.5 }
    // 字体与字重对齐 Stats 源码（Kit/Widgets/Mini.swift）：
    // 数值 systemFont regular（比例字体）、标签 7pt light
    private var valueFont: NSFont {
        .systemFont(ofSize: valueFontSize, weight: .regular)
    }
    private let labelFont = NSFont.systemFont(ofSize: 7, weight: .light)
    private let singleFont = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)

    /// 列间距（pt）
    private let columnGap: CGFloat = 10
    /// 标签基线高出数值大写字顶的距离（pt）。像素实测（纯数字底部对齐
    /// Stats）：rowGap=3.0 时数字底比 Stats 高 1px，3.5 正好齐平
    private let rowGap: CGFloat = 3.5

    // 注意：不要使用 isFlipped = true。
    // 实测 NSString.draw(at:) 在翻转上下文里绘制点会变成"文本顶部"，
    // 在普通（y 向上）上下文里才是 baseline 起点。本视图保持非翻转，
    // 所有 y 坐标从视图底边向上计算。

    // MARK: - 尺寸

    override var intrinsicContentSize: NSSize {
        let width: CGFloat
        switch content {
        case .placeholder(let text), .error(let text):
            width = textWidth(text, font: singleFont)
        case .columns(let cols):
            width = cols.map(columnWidth).reduce(0, +)
                + columnGap * CGFloat(max(cols.count - 1, 0))
        }
        return NSSize(width: ceil(width), height: NSView.noIntrinsicMetric)
    }

    private func textWidth(_ text: String, font: NSFont) -> CGFloat {
        (text as NSString).size(withAttributes: [.font: font]).width
    }

    private var valueAttrs: [NSAttributedString.Key: Any] {
        [.font: valueFont, .foregroundColor: NSColor.labelColor]
    }

    private var labelAttrs: [NSAttributedString.Key: Any] {
        [.font: labelFont, .foregroundColor: NSColor.labelColor]
    }

    private func columnWidth(_ col: QuotaColumn) -> CGFloat {
        max(textWidth(col.label, font: labelFont),
            (col.value as NSString).size(withAttributes: valueAttrs).width)
    }

    // MARK: - 绘制

    override func draw(_ dirtyRect: NSRect) {
        switch content {
        case .placeholder(let text):
            drawSingleLine(text)
        case .error(let text):
            drawSingleLine(text)
        case .columns(let cols):
            drawColumns(cols)
        }
    }

    private func drawSingleLine(_ text: String) {
        let attrs: [NSAttributedString.Key: Any] = [
            .font: singleFont, .foregroundColor: NSColor.labelColor,
        ]
        let x = (bounds.width - textWidth(text, font: singleFont)) / 2
        (text as NSString).draw(at: NSPoint(x: x, y: centeredBaseline(for: singleFont)),
                                withAttributes: attrs)
    }

    private func drawColumns(_ cols: [QuotaColumn]) {
        // draw(at:) 的点 = 该行 baseline 起点（y 自底向上，已离屏实测确认）。
        // 垂直锚点 = 标签墨水顶距（像素实测 Stats 同一菜单栏截图：
        // 标签顶距 7.5pt）；数值基线 = 标签基线 - 大写高 - rowGap
        let labelBaseline = layoutHeight - labelTopPad - labelFont.capHeight
        let valueBaseline = max(labelBaseline - valueFont.capHeight - rowGap,
                                1 - valueFont.descender)   // 矮栏兜底：不裁切

        var x: CGFloat = 0
        for col in cols {
            // 列内左对齐：标签与数值的左边缘对齐（列宽仍取两者较大值，
            // 保证列间距一致）
            (col.label as NSString).draw(at: NSPoint(x: x, y: labelBaseline),
                                         withAttributes: labelAttrs)
            (col.value as NSString).draw(at: NSPoint(x: x, y: valueBaseline),
                                         withAttributes: valueAttrs)
            x += columnWidth(col) + columnGap
        }
    }

    /// 标签墨水顶到菜单栏窗口顶的距离（pt）。像素实测对齐 Stats：
    /// 同一截图里 Stats 标签顶比我们低 5px（≈2.5pt）——Tahoe 浮动菜单栏
    /// 窗口顶部有约 3pt 透明边距，故锚点值小于视觉上的栏顶距
    private let labelTopPad: CGFloat = 5.0

    private func centeredBaseline(for font: NSFont) -> CGFloat {
        (layoutHeight - (font.ascender - font.descender)) / 2 - font.descender
    }

    // MARK: - 视图尺寸跟随窗口

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        syncFrameToWindow()
    }

    override func layout() {
        super.layout()
        syncFrameToWindow()
    }

    /// 视图扩到整个状态栏项窗口：系统给的按钮只有 22pt 老公约高度且
    /// 贴在窗口底边，而窗口本身是新菜单栏高度（33pt）。AppKit 默认
    /// 不裁剪子视图，绘制与点击可以延伸到按钮之外的窗口区域。
    private func syncFrameToWindow() {
        guard let window, let button = superview, window.frame.height > 0 else { return }
        let target = NSRect(x: -button.frame.origin.x, y: -button.frame.origin.y,
                            width: window.frame.width, height: window.frame.height)
        if frame != target { frame = target }
    }

    // MARK: - 事件与外观

    override func mouseDown(with event: NSEvent) { onClick?() }
    override func rightMouseDown(with event: NSEvent) { onClick?() }

    override func viewDidChangeEffectiveAppearance() {
        needsDisplay = true
    }
}
