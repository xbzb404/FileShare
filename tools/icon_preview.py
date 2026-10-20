import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image

here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ico = os.path.join(here, "assets", "icon.ico")
out = os.path.join(here, "screenshots")

for size, name, scale in ((256, "icon256.png", 1), (32, "icon32_zoom.png", 4),
                          (16, "icon16_zoom.png", 8)):
    im = Image.open(ico)
    im.size = (size, size)
    im.load()
    img = im.convert("RGBA")
    if scale > 1:
        img = img.resize((size * scale, size * scale), Image.NEAREST)
    img.save(os.path.join(out, name))
    print("写出", name, img.size)
