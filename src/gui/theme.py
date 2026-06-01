"""主题系统 — 严格遵循主视觉规定文档的原子级视觉元素。"""

# ============================================================
# 品牌色彩系统
# ============================================================

class Light:
    BRAND_PINK   = "#FB7299"
    BRAND_BLUE   = "#00A1D6"
    BRAND_GREEN  = "#00B5AD"
    BRAND_ORANGE = "#F25D2E"
    BRAND_RED    = "#F44336"

    BG_PRIMARY      = "#F4F5F7"
    BG_CARD         = "#FFFFFF"
    BG_PANEL        = "#FAFBFC"
    BG_HOVER        = "#F8F8FA"
    TEXT_PRIMARY    = "#1E1E24"
    TEXT_SECONDARY  = "#6C6C7A"
    TEXT_TERTIARY   = "#A0A0B0"
    BORDER          = "#E2E4E8"
    SCROLLBAR       = "#D0D0D8"


class Dark:
    BRAND_PINK   = "#FF85B0"
    BRAND_BLUE   = "#00B5E6"
    BRAND_GREEN  = "#1DD3CA"
    BRAND_ORANGE = "#FF754A"
    BRAND_RED    = "#FF554F"

    BG_PRIMARY      = "#18181A"
    BG_CARD         = "#242424"
    BG_PANEL        = "#202024"
    BG_HOVER        = "#2A2A2E"
    TEXT_PRIMARY    = "#E9E9EF"
    TEXT_SECONDARY  = "#9A9AAB"
    TEXT_TERTIARY   = "#6A6A7A"
    BORDER          = "#33333F"
    SCROLLBAR       = "#3A3A46"


# ============================================================
# 字体系统
# ============================================================

FONT_FAMILY = '"Segoe UI", "Microsoft YaHei", -apple-system, BlinkMacSystemFont, sans-serif'
FONT_MONO   = '"Fira Code", "JetBrains Mono", "Consolas", monospace'

FONT_SIZES = {
    "h1":      "28px",    # 窗口标题
    "h2":      "20px",    # 分区标题
    "h3":      "16px",    # 卡片标题
    "base":    "14px",    # 正文
    "small":   "12px",    # 辅助文字
    "caption": "11px",    # 角标
}

FONT_WEIGHTS = {
    "regular":  "400",
    "medium":   "500",
    "semibold": "600",
    "bold":     "700",
}

# ============================================================
# 间距系统（8px 基数）
# ============================================================

SPACING = {
    "xs":  4,
    "sm":  8,
    "md":  16,
    "lg":  24,
    "xl":  32,
    "2xl": 48,
}

# ============================================================
# 圆角 / 阴影
# ============================================================

RADIUS = {
    "sm":    "4px",
    "md":    "6px",
    "lg":    "8px",
    "xl":    "12px",
    "round": "50%",
}

SHADOW = {
    "sm":   "0 1px 2px rgba(0,0,0,0.03), 0 1px 3px rgba(0,0,0,0.05)",
    "md":   "0 4px 12px rgba(0,0,0,0.08)",
    "hover": "0 8px 24px rgba(0,0,0,0.12)",
}


# ============================================================
# 全局 QSS 生成
# ============================================================

def build_stylesheet(theme) -> str:
    """生成完整的 QSS 样式表。"""
    return f"""
    /* === 全局 === */
    QMainWindow, QDialog {{
        background-color: {theme.BG_PRIMARY};
    }}
    QWidget {{
        font-family: {FONT_FAMILY};
        font-size: {FONT_SIZES["base"]};
        color: {theme.TEXT_PRIMARY};
    }}

    /* === 结构区 === */
    QFrame#sidebar {{
        background: {theme.BG_CARD};
        border-right: 1px solid {theme.BORDER};
    }}
    QFrame#workspace {{
        background: transparent;
        border: none;
    }}
    QFrame#topbar {{
        background: {theme.BG_CARD};
        border-bottom: 1px solid {theme.BORDER};
    }}
    QFrame#heroCard {{
        background: {theme.BG_CARD};
        border: 1px solid {theme.BORDER};
        border-radius: {RADIUS["lg"]};
    }}
    QLabel#appTitle {{
        color: {theme.BRAND_PINK};
        font-size: {FONT_SIZES["h1"]};
        font-weight: {FONT_WEIGHTS["bold"]};
    }}
    QLabel#appSubtitle, QLabel#sectionCaption {{
        color: {theme.TEXT_TERTIARY};
        font-size: {FONT_SIZES["caption"]};
    }}
    QLabel#sectionTitle {{
        color: {theme.TEXT_PRIMARY};
        font-size: {FONT_SIZES["h3"]};
        font-weight: {FONT_WEIGHTS["semibold"]};
    }}
    QLabel#metricValue {{
        color: {theme.TEXT_PRIMARY};
        font-size: {FONT_SIZES["h2"]};
        font-weight: {FONT_WEIGHTS["bold"]};
    }}
    QLabel#metricLabel {{
        color: {theme.TEXT_SECONDARY};
        font-size: {FONT_SIZES["caption"]};
    }}
    QFrame#metricCard {{
        background: {theme.BG_PANEL};
        border: 1px solid {theme.BORDER};
        border-radius: {RADIUS["lg"]};
    }}

    /* === 卡片容器 === */
    QFrame#card {{
        background: {theme.BG_CARD};
        border: 1px solid {theme.BORDER};
        border-radius: {RADIUS["lg"]};
    }}

    /* === 按钮 === */
    QPushButton {{
        padding: {SPACING["sm"]}px {SPACING["md"]}px;
        border-radius: {RADIUS["md"]};
        font-weight: {FONT_WEIGHTS["medium"]};
        border: 1px solid {theme.BORDER};
        background: {theme.BG_CARD};
        color: {theme.TEXT_PRIMARY};
        min-height: 32px;
    }}
    QPushButton:hover {{
        background: {theme.BG_HOVER};
        border-color: {theme.BRAND_PINK};
    }}
    QPushButton:pressed {{
        background: {theme.BORDER};
    }}
    QPushButton:disabled {{
        color: {theme.TEXT_TERTIARY};
        background: {theme.BG_PRIMARY};
        border-color: {theme.BORDER};
    }}

    /* 主按钮 */
    QPushButton#primary {{
        background: {theme.BRAND_PINK};
        color: #FFFFFF;
        border: none;
        font-weight: {FONT_WEIGHTS["semibold"]};
    }}
    QPushButton#primary:hover {{
        background: {theme.BRAND_PINK}DD;
    }}
    QPushButton#primary:pressed {{
        background: {theme.BRAND_PINK}BB;
    }}
    QPushButton#primary:disabled {{
        background: {theme.BORDER};
        color: {theme.TEXT_TERTIARY};
    }}

    /* 危险按钮 */
    QPushButton#danger {{
        background: transparent;
        color: {theme.BRAND_RED};
        border: 1px solid {theme.BRAND_RED};
    }}
    QPushButton#danger:hover {{
        background: {theme.BRAND_RED};
        color: #FFFFFF;
    }}

    /* === 输入框 === */
    QLineEdit, QTextEdit {{
        padding: {SPACING["sm"]}px {SPACING["md"]}px;
        border: 1px solid {theme.BORDER};
        border-radius: {RADIUS["md"]};
        background: {theme.BG_CARD};
        color: {theme.TEXT_PRIMARY};
    }}
    QLineEdit:focus {{
        border-color: {theme.BRAND_PINK};
    }}
    QComboBox, QSpinBox, QDoubleSpinBox {{
        padding: {SPACING["xs"]}px {SPACING["sm"]}px;
        border: 1px solid {theme.BORDER};
        border-radius: {RADIUS["md"]};
        background: {theme.BG_CARD};
        color: {theme.TEXT_PRIMARY};
        min-height: 28px;
    }}
    QComboBox {{
        padding-right: {SPACING["xl"]}px;
    }}
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: top right;
        width: 28px;
        border-left: 1px solid {theme.BORDER};
        border-top-right-radius: {RADIUS["md"]};
        border-bottom-right-radius: {RADIUS["md"]};
        background: {theme.BG_PANEL};
    }}
    QComboBox::down-arrow {{
        image: none;
        width: 0;
        height: 0;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {theme.TEXT_SECONDARY};
        margin-right: 9px;
    }}
    QComboBox::drop-down:hover {{
        background: {theme.BG_HOVER};
    }}
    QComboBox QAbstractItemView {{
        border: 1px solid {theme.BORDER};
        border-radius: {RADIUS["md"]};
        background: {theme.BG_CARD};
        color: {theme.TEXT_PRIMARY};
        selection-background-color: {theme.BRAND_PINK};
        selection-color: #FFFFFF;
        outline: 0;
    }}
    QSpinBox, QDoubleSpinBox {{
        padding-right: {SPACING["sm"]}px;
    }}
    QSpinBox::up-button, QSpinBox::down-button,
    QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
        width: 0px;
        height: 0px;
        border: none;
    }}
    QSpinBox::up-arrow, QSpinBox::down-arrow,
    QDoubleSpinBox::up-arrow, QDoubleSpinBox::down-arrow {{
        image: none;
        width: 0px;
        height: 0px;
    }}
    QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
        border-color: {theme.BRAND_PINK};
    }}
    QLineEdit::placeholder {{
        color: {theme.TEXT_TERTIARY};
    }}

    /* === 进度条 === */
    QProgressBar {{
        border: none;
        border-radius: {RADIUS["sm"]};
        background: {theme.BORDER};
        text-align: center;
        height: 10px;
        font-size: {FONT_SIZES["caption"]};
    }}
    QProgressBar::chunk {{
        border-radius: {RADIUS["sm"]};
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
            stop:0 {theme.BRAND_PINK}, stop:1 {theme.BRAND_BLUE});
    }}

    /* === 表格 === */
    QTableWidget {{
        background: {theme.BG_CARD};
        border: 1px solid {theme.BORDER};
        border-radius: {RADIUS["lg"]};
        gridline-color: {theme.BORDER};
        selection-background-color: {theme.BRAND_BLUE};
        selection-color: #FFFFFF;
        alternate-background-color: {theme.BG_PANEL};
    }}
    QTableWidget::item {{
        padding: {SPACING["xs"]}px {SPACING["sm"]}px;
    }}
    QHeaderView::section {{
        background: {theme.BG_HOVER};
        border: none;
        border-bottom: 2px solid {theme.BORDER};
        padding: {SPACING["sm"]}px {SPACING["md"]}px;
        font-weight: {FONT_WEIGHTS["semibold"]};
        font-size: {FONT_SIZES["small"]};
        color: {theme.TEXT_SECONDARY};
    }}

    /* === 分组 === */
    QGroupBox {{
        border: 1px solid {theme.BORDER};
        border-radius: {RADIUS["lg"]};
        margin-top: {SPACING["md"]}px;
        padding: {SPACING["md"]}px {SPACING["sm"]}px {SPACING["sm"]}px {SPACING["sm"]}px;
        font-weight: {FONT_WEIGHTS["semibold"]};
        color: {theme.TEXT_PRIMARY};
        background: {theme.BG_PANEL};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: {SPACING["sm"]}px;
        padding: 0 {SPACING["xs"]}px;
        color: {theme.TEXT_SECONDARY};
    }}

    /* === 滚动条 === */
    QScrollBar:vertical {{
        width: 8px;
        background: transparent;
    }}
    QScrollBar::handle:vertical {{
        border-radius: 4px;
        background: {theme.SCROLLBAR};
        min-height: 30px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
    }}

    /* === 标签页 === */
    QTabWidget::pane {{
        border: 1px solid {theme.BORDER};
        border-radius: {RADIUS["lg"]};
        background: {theme.BG_CARD};
    }}
    QTabBar::tab {{
        padding: {SPACING["sm"]}px {SPACING["md"]}px;
        border-bottom: 2px solid transparent;
        color: {theme.TEXT_SECONDARY};
        font-weight: {FONT_WEIGHTS["medium"]};
    }}
    QTabBar::tab:selected {{
        color: {theme.BRAND_PINK};
        border-bottom-color: {theme.BRAND_PINK};
    }}

    /* === 状态栏 === */
    QStatusBar {{
        background: {theme.BG_CARD};
        border-top: 1px solid {theme.BORDER};
        color: {theme.TEXT_SECONDARY};
        font-size: {FONT_SIZES["small"]};
    }}
    """
