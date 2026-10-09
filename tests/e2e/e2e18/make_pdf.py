#!/usr/bin/env python3
"""生成一个"裁没裁一眼就看出来"的 PDF。

页面四边画满贴边的标记：最外圈 1pt 的边框、四角 40×40 的实心方块、
每 100pt 一条横线和数字标尺。只要缩略图裁掉哪怕 5%，角上的方块就会缺一块。

手写 PDF 而不是找现成文件：不依赖任何库，而且内容可控 —— 缩略图里
"哪里少了"是能直接读出来的。
"""

# --- 路径推导 ---------------------------------------------------------
# 原先这里硬编码了 /tmp/e2eNN 和仓库的绝对路径。搬进仓库后改成从本文件
# 位置推导，这样仓库放哪儿都能跑。
import pathlib as _pl
_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                      # 仓库根
# ---------------------------------------------------------------------
import pathlib

W, H = 595, 842  # A4，pt
PAGES = 5


def build_content(page: int) -> bytes:
    parts = []
    # 最外圈贴边边框：任何裁剪都会把它切掉
    parts.append("1 w 0 0 0 RG")
    parts.append(f"0.5 0.5 {W - 1} {H - 1} re S")
    # 内圈边框
    parts.append("0.5 w 0.55 0.55 0.6 RG")
    parts.append(f"20 20 {W - 40} {H - 40} re S")

    # 四角实心方块（40×40）—— 裁多少一眼可见
    parts.append("0 0 0 rg")
    for x, y in ((0, 0), (W - 40, H - 40), (0, H - 40), (W - 40, 0)):
        parts.append(f"{x} {y} 40 40 re f")

    # 每 100pt 一条横线 + 左端数字
    parts.append("0.7 0.7 0.75 RG 0.5 w")
    parts.append("BT /F1 11 Tf 0 0 0 rg")
    for i in range(1, 9):
        y = i * 100
        parts.append(f"50 {y} m {W - 50} {y} l S")
        parts.append(f"1 0 0 1 26 {y + 3} Tm (y={y}) Tj")
    parts.append("ET")

    # 中间一行大字，用来确认没有被整体缩放/位移
    parts.append(f"BT /F1 28 Tf 0 0 0 rg 1 0 0 1 150 430 Tm (FOLIA PAGE {page}) Tj ET")
    parts.append(
        "BT /F1 12 Tf 1 0 0 1 150 405 Tm "
        "(top-left corner must be a solid black square) Tj ET"
    )

    return "\n".join(parts).encode("latin-1")


def build_pdf() -> bytes:
    contents = [build_content(p) for p in range(1, PAGES + 1)]
    # 对象编号：1 catalog, 2 pages, 3 font, 然后每页两个（page + contents）
    font_id = 3
    first_page_id = 4

    objs: list[bytes] = []
    kids = " ".join(f"{first_page_id + i * 2} 0 R" for i in range(PAGES))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {PAGES} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for i, content in enumerate(contents):
        page_id = first_page_id + i * 2
        content_id = page_id + 1
        objs.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W} {H}] "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>"
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


if __name__ == "__main__":
    dest = pathlib.Path(str(_HERE) + "/sample.pdf")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(build_pdf())
    print(
        f"wrote {dest} ({dest.stat().st_size} bytes), {PAGES} pages of {W}x{H} "
        f"ratio={W / H:.4f}"
    )
