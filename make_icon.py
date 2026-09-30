"""临时脚本：生成应用图标 icon.ico（不进项目，产物进 src-tauri/icons）。"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image, ImageDraw

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src-tauri", "icons")
os.makedirs(OUT_DIR, exist_ok=True)


def draw(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64.0  # 归一化比例

    # 圆角背景（暖棕）
    d.rounded_rectangle(
        (4 * s, 4 * s, 60 * s, 60 * s),
        radius=14 * s,
        fill=(140, 95, 60, 255),
    )
    # 高光弧
    d.arc((10 * s, 8 * s, 54 * s, 40 * s), start=200, end=320, fill=(255, 255, 255, 60), width=max(1, int(4 * s)))

    # 松鼠尾巴（右下大圆 + 内圆渐层）
    d.ellipse((30 * s, 26 * s, 58 * s, 56 * s), fill=(190, 135, 85, 255))
    d.ellipse((36 * s, 32 * s, 52 * s, 50 * s), fill=(160, 110, 70, 255))

    # 身体（左中椭圆）
    d.ellipse((10 * s, 24 * s, 38 * s, 52 * s), fill=(210, 165, 120, 255))

    # 头（左上圆）
    d.ellipse((8 * s, 10 * s, 30 * s, 32 * s), fill=(222, 180, 135, 255))

    # 耳朵
    d.ellipse((9 * s, 6 * s, 17 * s, 15 * s), fill=(222, 180, 135, 255))
    d.ellipse((21 * s, 5 * s, 29 * s, 14 * s), fill=(222, 180, 135, 255))

    # 眼睛
    es = max(1, int(2 * s))
    d.ellipse((13 * s, 17 * s, 13 * s + es * 2, 17 * s + es * 2), fill=(40, 30, 25, 255))
    d.ellipse((22 * s, 17 * s, 22 * s + es * 2, 17 * s + es * 2), fill=(40, 30, 25, 255))

    # 腮红
    d.ellipse((11 * s, 23 * s, 16 * s, 26 * s), fill=(255, 150, 140, 120))

    return img


base = draw(256)
base.save(os.path.join(OUT_DIR, "icon.png"), "PNG")
base.save(
    os.path.join(OUT_DIR, "icon.ico"),
    format="ICO",
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
base.resize((32, 32)).save(os.path.join(OUT_DIR, "32x32.png"), "PNG")
base.resize((128, 128)).save(os.path.join(OUT_DIR, "128x128.png"), "PNG")
print("图标已生成:", sorted(os.listdir(OUT_DIR)))
