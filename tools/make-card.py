"""Compose the catalog card (1200x600, 2:1).

Copy across the top, the real desktop pane full-width underneath — the pane is a
wide strip, so giving it the full width is what keeps its text legible. Drawn at
2x and downsampled so type and rounded corners stay crisp.
"""

import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

REPO = os.path.expanduser("~/.hermes/workspace/hermes-localsend")
PANE = os.path.join(REPO, "docs", "screenshots", "pane.png")
OUT = os.path.join(REPO, "docs", "banner.png")

S = 2
W, H = 1200 * S, 600 * S
MARGIN = 64 * S

BG_TOP = (12, 15, 19)
BG_BOTTOM = (19, 25, 32)
FG = (241, 245, 249)
MUTED = (152, 164, 178)
DIM = (118, 130, 144)
ACCENT = (34, 211, 238)
BORDER = (48, 58, 68)

TITLE_FONTS = ["/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc"]
MONO_FONTS = ["/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/SFNSMono.ttf"]


def font(paths, size):
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def gradient(size, top, bottom):
    w, h = size
    strip = Image.new("RGB", (1, h))
    px = strip.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    img = strip.resize((w, h), Image.NEAREST).convert("RGB")
    noise = Image.effect_noise((w, h), 2).convert("L")
    return Image.blend(img, Image.merge("RGB", (noise, noise, noise)), 0.012)


def rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255)
    return mask


def paste_rounded(canvas, img, xy, radius, blur=16, spread=36):
    mask = rounded_mask(img.size, radius)
    pad = spread * S
    shadow = Image.new("RGBA", (img.size[0] + pad * 2, img.size[1] + pad * 2), (0, 0, 0, 0))
    shadow.paste(Image.new("RGBA", img.size, (0, 0, 0, 200)), (pad, pad + 6 * S), mask)
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur * S))
    canvas.paste(shadow, (xy[0] - pad, xy[1] - pad), shadow)
    canvas.paste(img, xy, mask)


card = gradient((W, H), BG_TOP, BG_BOTTOM)
draw = ImageDraw.Draw(card)
draw.rectangle([0, 0, W, 3 * S], fill=ACCENT)

title = font(TITLE_FONTS, 60 * S)
sub = font(TITLE_FONTS, 24 * S)
pill = font(TITLE_FONTS, 17 * S)
mono = font(MONO_FONTS, 19 * S)
label = font(TITLE_FONTS, 15 * S)

# ---- top-left: identity
x, y = MARGIN, 56 * S
draw.ellipse([x, y + 5 * S, x + 9 * S, y + 14 * S], fill=ACCENT)
draw.text((x + 20 * S, y), "HERMES AGENT PLUGIN", font=label, fill=DIM)

y += 38 * S
draw.text((x, y), "hermes-localsend", font=title, fill=FG)

y += 82 * S
draw.text((x, y), "Peer-to-peer file transfer, native in Hermes Desktop.", font=sub, fill=MUTED)

# ---- top-right: install command
cmd = "hermes plugins install localsend"
tw = draw.textlength(cmd, font=mono)
bx1 = W - MARGIN
bx0 = bx1 - tw - 34 * S
by0 = 58 * S
draw.rounded_rectangle([bx0, by0, bx1, by0 + 46 * S], radius=10 * S, fill=(17, 23, 29),
                       outline=(58, 70, 82), width=1 * S)
draw.text((bx0 + 17 * S, by0 + 13 * S), cmd, font=mono, fill=(227, 237, 245))

# ---- feature pills
pills = ["encrypted peer support", "pane, chip and Cmd-K commands", "48 tests, stdlib only"]
px_cursor = MARGIN
py = 206 * S
for text in pills:
    tw = draw.textlength(text, font=pill)
    draw.rounded_rectangle([px_cursor, py, px_cursor + tw + 30 * S, py + 38 * S], radius=19 * S,
                           fill=(22, 30, 38), outline=(50, 60, 71), width=1 * S)
    draw.text((px_cursor + 15 * S, py + 10 * S), text, font=pill, fill=(196, 208, 220))
    px_cursor += tw + 30 * S + 12 * S

# ---- caption above the shot
caption = "The pane, live in Hermes Desktop"
cw = draw.textlength(caption, font=label)
draw.text((W - MARGIN - cw, 252 * S), caption, font=label, fill=DIM)

# ---- bottom: the real pane, full-bleed so its text stays legible
pane = Image.open(PANE).convert("RGB")
scale = W / pane.width
pane = pane.resize((W, int(pane.height * scale)), Image.LANCZOS)
ph = pane.height
card.paste(pane, (0, H - ph))
draw.rectangle([0, H - ph - 2 * S, W, H - ph], fill=BORDER)

card = card.resize((1200, 600), Image.LANCZOS)
card.save(OUT, optimize=True)
print("wrote", OUT, card.size, os.path.getsize(OUT) // 1024, "KB",
      "| pane rendered", pane.size, "-> scale", round(scale / S, 2), "x")
