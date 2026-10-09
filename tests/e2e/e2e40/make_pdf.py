#!/usr/bin/env python3
"""生成一份「图片被反相一眼就能看出来」的 PDF。

页面 200×200pt，只有三样东西：
  · 白底
  · 一块黑色矩形（当"文字"）—— 深色模式下**应该**被反成白
  · 一张 2×2 的**纯红**位图（当"图片"）—— 深色模式下**不应该**被反相

为什么用纯红：`invert(1) hue-rotate(180deg)` 作用在 (255,0,0) 上会把 R 分量顶到
1.574、落到 [0,1] 之外**被截断**，于是反出来的不是"另一种红"而是明显的**粉色
(255,146,146)**。浅色/深色两种状态在这一个像素上差异极大，判据不会含糊。

手写 PDF 而不是找现成文件：不依赖任何库，而且图像 XObject 的内容完全可控。
"""

# --- 路径推导 ---------------------------------------------------------
import pathlib as _pl

_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                      # 仓库根
# ---------------------------------------------------------------------
import pathlib

W = H = 200


def build(path: pathlib.Path) -> None:
    img = bytes([255, 0, 0] * 4)  # 2×2 纯红
    content = (
        b"1 1 1 rg 0 0 200 200 re f\n"          # 白底
        b"0 0 0 rg 20 150 60 30 re f\n"          # 黑块（文字）
        b"q 100 0 0 100 50 50 cm /Im0 Do Q\n"    # 红图，铺满 (50,50)-(150,150)
    )
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        b"/Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB "
        b"/BitsPerComponent 8 /Length %d >>\nstream\n" % len(img) + img + b"\nendstream",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1,
        xref,
    )
    path.write_bytes(bytes(out))


if __name__ == "__main__":
    target = _HERE / "dark.pdf"
    build(target)
    print(f"wrote {target} ({target.stat().st_size} bytes)")
