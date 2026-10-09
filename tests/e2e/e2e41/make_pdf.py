#!/usr/bin/env python3
"""生成 e2e41 用的多页 PDF：**每页都有位图图片**，其中第 1 页带一组"重复戳记"。

为什么非要有图片：`collectImageQuads` 只对图片算子记四边形。页面里没有位图，
`pageImageQuads` 永远是空的 —— 那这套探针量的就是空气（"没有泄漏"变成一句空话）。

第 1 页特意放了 **6 个只差平移的相同戳记**：

    q 60 0 0 60  60 640 cm /Im0 Do Q
    q 60 0 0 60 130 640 cm /Im0 Do Q
    ...

⚠️ **订正**：当初放这 6 个戳记是想让 pdf.js 的 worker 把它们合并成一条
`OPS.paintImageXObjectRepeat`(88)，好覆盖那条分支。**实测证明本版本不会合并**
（`OperatorList` 构造时 `streamSink && !(intent & RenderingIntentFlag.OPLIST)` 才用
`QueueOptimizer`，而 `PDFPageProxy.getOperatorList()` 硬编码传 `isOpList = true`
→ 走 `NullOptimizer`；只有 `render()` 那条路才合并）。所以这 6 个戳记在算子表里
**就是 6 条独立的 `paintImageXObject`(85)**。

于是这个样本现在的角色是「**一页里有多块位图**」的普通样本：用来验
"每块都单独抠进图片层"（`rowMax = 6`）、以及"翻页后这些记录都会还回去"。
88/87 两条分支**只能靠 `harness.js` 的 `?mode=units` 喂合成算子表来测** —— 见那里的注释。

6 个戳记是**同一行**（y 相同）→ 判据可以数"同一行上有几块"，不依赖具体缩放。

第 1 页另有一张 200×200pt 的单张大图（也走 `paintImageXObject`，当作配对）。
所以第 1 页的图片层应有 **7** 块，其中同一行上最多 **6** 块。

手写 PDF，不依赖任何库。用法：`python make_pdf.py <输出路径>`（默认 `sample.pdf`）。
"""

import pathlib
import sys

W, H = 595, 842
PAGES = 24
# 第 1 页那组重复戳记：6 个，尺寸与起点一致，只有 x 平移不同。
STAMP = 60
STAMP_N = 6
STAMP_X0 = 60
STAMP_Y = 640
STAMP_DX = 70


def build(pages: int = PAGES) -> bytes:
    img = bytes([255, 0, 0] * 4)  # 2×2 纯红

    # 对象号：1 目录 / 2 页面树 / 3 字体 / 4 图像 XObject / 之后每页两个
    first_page_id = 5
    kids = " ".join(f"{first_page_id + i * 2} 0 R" for i in range(pages))
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB "
        b"/BitsPerComponent 8 /Length %d >>\nstream\n" % len(img) + img + b"\nendstream",
    ]

    for i in range(pages):
        page_id = first_page_id + i * 2
        content_id = page_id + 1
        lines = [
            f"1 1 1 rg 0 0 {W} {H} re f",
            f"BT /F1 24 Tf 0 0 0 rg 72 780 Td (FOLIA e2e41 page {i + 1}) Tj ET",
        ]
        if i == 0:
            # ---- 重复戳记：6 组，只有平移不同 → pdf.js 合并成一条 repeat 算子
            for k in range(STAMP_N):
                x = STAMP_X0 + k * STAMP_DX
                lines.append(f"q {STAMP} 0 0 {STAMP} {x} {STAMP_Y} cm /Im0 Do Q")
        # 每页一张单张大图（普通 paintImageXObject）
        lines.append(f"q 200 0 0 200 72 300 cm /Im0 Do Q")
        content = "\n".join(lines).encode("latin-1")

        objs.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W} {H}] "
                f"/Resources << /Font << /F1 3 0 R >> /XObject << /Im0 4 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("latin-1")
        )
        objs.append(
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def main() -> None:
    target = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).parent / "sample.pdf"
    data = build()
    target.write_bytes(data)
    print(f"wrote {target} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
