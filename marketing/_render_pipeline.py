"""
Renders the Website Audit Agent pipeline diagram per the
'Systemic Cartography' design philosophy.

Output: /Users/tomasmaxim/Documents/GitHub/WebsiteAuditAgent/marketing/website_audit_pipeline.png
Format: 2400x1256 (rendered at 2x then output, target visual = 1200x628 landscape)
"""

from PIL import Image, ImageDraw, ImageFont
import os
import math

# ----------------------------------------------------------------------------
# CONSTANTS
# ----------------------------------------------------------------------------

FONT_DIR = "/Users/tomasmaxim/Library/Application Support/Claude/local-agent-mode-sessions/skills-plugin/65ae8711-26da-4941-9fb7-d35370f19006/cb8e2ba8-0195-4bcb-bd35-2c5cdb19836a/skills/canvas-design/canvas-fonts"
OUT_PATH = "/Users/tomasmaxim/Documents/GitHub/WebsiteAuditAgent/marketing/website_audit_pipeline.png"

# 2x scale for crisp output
SCALE = 2
W = 1200 * SCALE
H = 628 * SCALE

# Palette — calibrated for phosphor-on-slate
INK = (10, 14, 26)         # background, deep navy-black
INK_LIGHT = (16, 22, 38)   # slight elevation tone
RULE = (38, 48, 70)        # hairline rule
RULE_FAINT = (24, 32, 50)  # very faint rule
ACCENT = (56, 189, 248)    # electric cyan #38bdf8
ACCENT_DIM = (28, 100, 140)
TEXT = (235, 240, 250)     # primary white
TEXT_DIM = (130, 145, 170) # secondary text
TEXT_FAINT = (75, 90, 115) # tertiary, indexes

def F(name, size):
    return ImageFont.truetype(os.path.join(FONT_DIR, name), size * SCALE)

# ----------------------------------------------------------------------------
# CANVAS
# ----------------------------------------------------------------------------

img = Image.new("RGB", (W, H), INK)
d = ImageDraw.Draw(img, "RGBA")

# Subtle grid texture — calibrated silence with a whisper of structure
grid_color = (255, 255, 255, 6)
step = 24 * SCALE
for x in range(0, W, step):
    d.line([(x, 0), (x, H)], fill=grid_color, width=1)
for y in range(0, H, step):
    d.line([(0, y), (W, y)], fill=grid_color, width=1)

# Hairline frame margin
margin = 56 * SCALE
d.rectangle([margin, margin, W - margin, H - margin], outline=RULE, width=1)

# Inner corner ticks (cartographer markers)
tick = 14 * SCALE
for cx, cy in [(margin, margin), (W-margin, margin), (margin, H-margin), (W-margin, H-margin)]:
    pass  # corner ticks added via crosshairs below

# Crosshairs at corners
def crosshair(cx, cy, length=10*SCALE):
    d.line([(cx-length, cy), (cx+length, cy)], fill=ACCENT, width=1)
    d.line([(cx, cy-length), (cx, cy+length)], fill=ACCENT, width=1)

crosshair(margin, margin)
crosshair(W-margin, margin)
crosshair(margin, H-margin)
crosshair(W-margin, H-margin)

# ----------------------------------------------------------------------------
# HEADER BAND
# ----------------------------------------------------------------------------

f_meta_mono = F("GeistMono-Regular.ttf", 9)
f_meta_mono_b = F("GeistMono-Bold.ttf", 9)
f_index = F("GeistMono-Regular.ttf", 8)
f_title = F("InstrumentSans-Bold.ttf", 22)
f_title_thin = F("InstrumentSans-Regular.ttf", 22)
f_sub = F("GeistMono-Regular.ttf", 10)
f_stage_label = F("InstrumentSans-Bold.ttf", 13)
f_stage_index = F("GeistMono-Bold.ttf", 8)
f_stage_desc = F("GeistMono-Regular.ttf", 9)
f_chip = F("InstrumentSans-Bold.ttf", 11)
f_chip_meta = F("GeistMono-Regular.ttf", 8)
f_signature = F("GeistMono-Regular.ttf", 8)

# Top-left: project mark + index
header_y = margin + 22 * SCALE

# Plate identifier (top-left)
d.text((margin + 14*SCALE, margin + 14*SCALE),
       "PLATE 01 · COLD OUTREACH PIPELINE",
       fill=TEXT_DIM, font=f_meta_mono)

# Top-right: coordinates / version
ver = "REV. 2026.04 — EMTD STUDIO"
ver_w = d.textlength(ver, font=f_meta_mono)
d.text((W - margin - 14*SCALE - ver_w, margin + 14*SCALE),
       ver, fill=TEXT_DIM, font=f_meta_mono)

# Title block (centered or left)
title_y = margin + 56 * SCALE
title_x = margin + 14 * SCALE
d.text((title_x, title_y), "WEBSITE AUDIT AGENT", fill=TEXT, font=f_title)
title_w = d.textlength("WEBSITE AUDIT AGENT", font=f_title)

# Tiny accent square next to title
sq = 6 * SCALE
d.rectangle([title_x + title_w + 12*SCALE, title_y + 8*SCALE,
             title_x + title_w + 12*SCALE + sq, title_y + 8*SCALE + sq],
            fill=ACCENT)

# Subtitle
sub_y = title_y + 34 * SCALE
d.text((title_x, sub_y),
       "An autonomous system for finding, auditing, and contacting prospects.",
       fill=TEXT_DIM, font=f_sub)

# Horizontal rule under header
rule_y = sub_y + 30 * SCALE
d.line([(margin + 14*SCALE, rule_y), (W - margin - 14*SCALE, rule_y)],
       fill=RULE, width=1)

# Tiny axis labels above rule
d.text((margin + 14*SCALE, rule_y - 14*SCALE), "FIG. A", fill=TEXT_FAINT, font=f_index)
right_label = "FIVE-STAGE FLOW · LEFT TO RIGHT"
rl_w = d.textlength(right_label, font=f_index)
d.text((W - margin - 14*SCALE - rl_w, rule_y - 14*SCALE),
       right_label, fill=TEXT_FAINT, font=f_index)

# ----------------------------------------------------------------------------
# PIPELINE — 5 STAGES
# ----------------------------------------------------------------------------

stages = [
    {
        "idx": "01",
        "label": "PROSPECT",
        "desc": "Find leads via search APIs.\nScore & qualify.",
        "icon": "lens",
    },
    {
        "idx": "02",
        "label": "AUDIT",
        "desc": "Scrape HTML, PageSpeed,\nSEO signals.",
        "icon": "chart",
    },
    {
        "idx": "03",
        "label": "ANALYZE",
        "desc": "Claude LLM identifies issues,\ngenerates email.",
        "icon": "brain",
    },
    {
        "idx": "04",
        "label": "SEND",
        "desc": "Zoho SMTP, 3 senders,\ndedup registry.",
        "icon": "envelope",
    },
    {
        "idx": "05",
        "label": "FOLLOW-UP",
        "desc": "Auto follow-up + IMAP\nreply detection.",
        "icon": "loop",
    },
]

n = len(stages)
flow_top = rule_y + 50 * SCALE
flow_h = 220 * SCALE
flow_bottom = flow_top + flow_h

# Compute cell layout
content_left = margin + 14 * SCALE
content_right = W - margin - 14 * SCALE
content_w = content_right - content_left
gap = 18 * SCALE
cell_w = (content_w - gap * (n - 1)) / n
cell_h = flow_h

# Spine — the connecting current
spine_y = flow_top + cell_h * 0.42

# Draw connectors first (behind cells)
for i in range(n - 1):
    x1 = content_left + (i + 1) * cell_w + i * gap
    x2 = x1 + gap
    cy = spine_y
    # connector line
    d.line([(x1, cy), (x2, cy)], fill=ACCENT, width=2)
    # arrow tip at receiving cell
    arrow = 6 * SCALE
    d.polygon([
        (x2, cy),
        (x2 - arrow, cy - arrow*0.6),
        (x2 - arrow, cy + arrow*0.6),
    ], fill=ACCENT)
    # node markers
    nr = 3 * SCALE
    d.ellipse([x1-nr, cy-nr, x1+nr, cy+nr], fill=INK, outline=ACCENT, width=1)

# Draw each stage cell
def draw_icon(d, kind, cx, cy, size, color):
    s = size
    if kind == "lens":
        # magnifying glass
        r = s * 0.42
        d.ellipse([cx-r, cy-r-s*0.05, cx+r, cy+r-s*0.05], outline=color, width=2)
        # handle
        hx1 = cx + r*0.7
        hy1 = cy + r*0.7 - s*0.05
        hx2 = cx + r*1.1
        hy2 = cy + r*1.1 - s*0.05
        d.line([(hx1, hy1), (hx2, hy2)], fill=color, width=2)
    elif kind == "chart":
        # bar chart
        bar_w = s * 0.13
        base = cy + s * 0.32
        heights = [0.30, 0.55, 0.42, 0.70]
        start = cx - (len(heights) * bar_w + (len(heights)-1) * bar_w*0.4) / 2
        for i, hv in enumerate(heights):
            x = start + i * (bar_w + bar_w*0.4)
            top = base - s * hv
            d.rectangle([x, top, x+bar_w, base], outline=color, width=2)
    elif kind == "brain":
        # node-network glyph
        nodes = [(-0.25, -0.18), (0.20, -0.22), (0.25, 0.10), (-0.18, 0.18), (0.0, -0.02)]
        pts = [(cx + s*x, cy + s*y) for (x,y) in nodes]
        # edges
        edges = [(0,4),(1,4),(2,4),(3,4),(0,1),(2,3)]
        for a,b in edges:
            d.line([pts[a], pts[b]], fill=color, width=1)
        # nodes
        nr = s * 0.045
        for (px, py) in pts:
            d.ellipse([px-nr, py-nr, px+nr, py+nr], fill=color)
    elif kind == "envelope":
        ew = s * 0.78
        eh = s * 0.50
        x1, y1 = cx - ew/2, cy - eh/2
        x2, y2 = cx + ew/2, cy + eh/2
        d.rectangle([x1, y1, x2, y2], outline=color, width=2)
        # flap
        d.line([(x1, y1), (cx, cy + eh*0.05), (x2, y1)], fill=color, width=2)
    elif kind == "loop":
        # circular arrow
        r = s * 0.40
        # arc as polyline
        pts = []
        for deg in range(-30, 290, 8):
            a = math.radians(deg)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        for i in range(len(pts)-1):
            d.line([pts[i], pts[i+1]], fill=color, width=2)
        # arrow tip
        last = pts[-1]
        prev = pts[-3]
        ang = math.atan2(last[1]-prev[1], last[0]-prev[0])
        ar = s * 0.10
        tip = last
        b1 = (tip[0] - ar*math.cos(ang - math.radians(28)),
              tip[1] - ar*math.sin(ang - math.radians(28)))
        b2 = (tip[0] - ar*math.cos(ang + math.radians(28)),
              tip[1] - ar*math.sin(ang + math.radians(28)))
        d.polygon([tip, b1, b2], fill=color)

for i, st in enumerate(stages):
    x = content_left + i * (cell_w + gap)
    y = flow_top
    rect = [x, y, x + cell_w, y + cell_h]
    # Cell base
    d.rounded_rectangle(rect, radius=10*SCALE, fill=INK_LIGHT, outline=RULE, width=1)

    # Index in top-left corner
    d.text((x + 14*SCALE, y + 12*SCALE), st["idx"], fill=ACCENT, font=f_stage_index)

    # tiny vertical mark next to index
    d.line([(x + 14*SCALE + 22*SCALE, y + 14*SCALE),
            (x + 14*SCALE + 22*SCALE, y + 22*SCALE)], fill=RULE, width=1)
    # tiny code
    code = f"§{i+1:02d}"
    d.text((x + 14*SCALE + 28*SCALE, y + 12*SCALE), code, fill=TEXT_FAINT, font=f_stage_index)

    # Icon area
    icon_cy = y + cell_h * 0.36
    icon_cx = x + cell_w / 2
    draw_icon(d, st["icon"], icon_cx, icon_cy, 56*SCALE, ACCENT)

    # Label
    label = st["label"]
    lbl_w = d.textlength(label, font=f_stage_label)
    label_y = y + cell_h * 0.62
    d.text((x + cell_w/2 - lbl_w/2, label_y), label, fill=TEXT, font=f_stage_label)

    # Hairline rule under label
    rl_w = 28 * SCALE
    rl_y = label_y + 22 * SCALE
    d.line([(x + cell_w/2 - rl_w/2, rl_y),
            (x + cell_w/2 + rl_w/2, rl_y)], fill=ACCENT, width=1)

    # Description (two lines, monospace, dim)
    desc_lines = st["desc"].split("\n")
    dy = rl_y + 12 * SCALE
    for line in desc_lines:
        lw = d.textlength(line, font=f_stage_desc)
        d.text((x + cell_w/2 - lw/2, dy), line, fill=TEXT_DIM, font=f_stage_desc)
        dy += 13 * SCALE

# ----------------------------------------------------------------------------
# CHIPS — specification manifest
# ----------------------------------------------------------------------------

chips_top = flow_bottom + 38 * SCALE

chips = [
    ("~150", "EMAILS / DAY"),
    ("3×", "SENDER ROTATION"),
    ("CI/CD", "GITHUB ACTIONS · AUTOMATED"),
]

# Compute chip widths individually
chip_h = 56 * SCALE
chip_pad_x = 18 * SCALE
chip_gap = 14 * SCALE

# Pre-measure
chip_widths = []
for big, small in chips:
    bw = d.textlength(big, font=f_chip)
    sw = d.textlength(small, font=f_chip_meta)
    cw = max(bw, sw) + chip_pad_x * 2 + 14*SCALE  # + small accent bar room
    chip_widths.append(cw)

total_chip_w = sum(chip_widths) + chip_gap * (len(chips)-1)
chip_x = (W - total_chip_w) / 2

# Section label above chips
sect_label = "SYSTEM SPECIFICATION"
sl_w = d.textlength(sect_label, font=f_meta_mono_b)
d.text((W/2 - sl_w/2, chips_top - 22*SCALE),
       sect_label, fill=TEXT_FAINT, font=f_meta_mono_b)

# Tiny accent ticks framing the section label
tick_y = chips_top - 16*SCALE
d.line([(W/2 - sl_w/2 - 16*SCALE, tick_y), (W/2 - sl_w/2 - 6*SCALE, tick_y)],
       fill=ACCENT, width=1)
d.line([(W/2 + sl_w/2 + 6*SCALE, tick_y), (W/2 + sl_w/2 + 16*SCALE, tick_y)],
       fill=ACCENT, width=1)

# Draw chips
cx = chip_x
for (big, small), cw in zip(chips, chip_widths):
    rect = [cx, chips_top, cx + cw, chips_top + chip_h]
    d.rounded_rectangle(rect, radius=6*SCALE, outline=RULE, width=1, fill=INK_LIGHT)

    # left accent bar
    d.rectangle([cx + 8*SCALE, chips_top + 12*SCALE,
                 cx + 8*SCALE + 2, chips_top + chip_h - 12*SCALE],
                fill=ACCENT)

    # big text
    bw = d.textlength(big, font=f_chip)
    d.text((cx + 18*SCALE, chips_top + 10*SCALE), big, fill=TEXT, font=f_chip)
    # small text
    d.text((cx + 18*SCALE, chips_top + 32*SCALE), small, fill=TEXT_DIM, font=f_chip_meta)

    cx += cw + chip_gap

# ----------------------------------------------------------------------------
# FOOTER SIGNATURE
# ----------------------------------------------------------------------------

foot_y = H - margin - 16 * SCALE
d.text((margin + 14*SCALE, foot_y),
       "PYTHON · CLAUDE SONNET · ZOHO SMTP · IMAP · SERPER · PAGESPEED",
       fill=TEXT_FAINT, font=f_signature)

right_sig = "EMTD/WAA — github.com/tomasmaxim"
rs_w = d.textlength(right_sig, font=f_signature)
d.text((W - margin - 14*SCALE - rs_w, foot_y),
       right_sig, fill=TEXT_FAINT, font=f_signature)

# ----------------------------------------------------------------------------
# DOWNSCALE FOR CRISP OUTPUT
# ----------------------------------------------------------------------------

final = img.resize((1200, 628), Image.LANCZOS)
final.save(OUT_PATH, "PNG", optimize=True)
print(f"Saved: {OUT_PATH}")
print(f"Size: {final.size}")
