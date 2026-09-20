from PIL import Image, ImageDraw
from pathlib import Path

out = Path("assets")
out.mkdir(exist_ok=True)
sizes = [16, 32, 48, 64, 128, 256]
imgs = []
for s in sizes:
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    m = max(1, s // 10)
    d.rounded_rectangle([m, m, s - m, s - m], radius=max(2, s // 6), fill=(229, 112, 26, 255))
    tw = max(2, s // 8)
    d.rectangle([s // 3, s // 4, s // 3 + tw, s * 3 // 4], fill=(255, 255, 255, 255))
    d.rectangle([s // 3, s // 4, s * 2 // 3, s // 4 + tw], fill=(255, 255, 255, 255))
    d.rectangle([s // 3, s // 2, s * 2 // 3, s // 2 + tw], fill=(255, 255, 255, 255))
    d.rectangle([s * 2 // 3 - tw, s // 4, s * 2 // 3, s // 2 + tw], fill=(255, 255, 255, 255))
    imgs.append(im)
imgs[-1].save(out / "icon.ico", sizes=[(s, s) for s in sizes])
print("icon ok", (out / "icon.ico").stat().st_size)
