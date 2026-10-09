from PIL import ImageFont

for path, index in (
    ("/System/Library/Fonts/PingFang.ttc", 2),
    ("/System/Library/Fonts/PingFang.ttc", 0),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", 0),
):
    try:
        f = ImageFont.truetype(path, 15, index=index)
        print("OK  ", path, index, "->", f.getname())
    except OSError as exc:
        print("FAIL", path, index, exc)
