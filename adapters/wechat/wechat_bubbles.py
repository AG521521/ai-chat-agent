"""微信气泡归属判定：只认"绿色=自己"，其余一律不算自己的。

为什么不用坐标：自己的长消息会从屏幕中间开始（实测 x 比 0.30），
按"过半屏"判断必然把长消息误判成对方的。

为什么不用"白色=对方"：微信聊天背景是浅灰 (250,250,250)，和白色的对方气泡
几乎分不开，硬判会把背景当成气泡。所以只做单向判定：

    near(157,242,159)  → own
    其它               → not_own（交给上层用系统提示正则再过滤）

本机实测 Weixin 4.1.15.50 自己气泡底色 = **RGB(157,242,159)**。

用法（只读）::

    python src/wechat_bubbles.py --image logs/xxx.png [--full]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(r"D:\weixin_agent")
sys.path.insert(0, str(ROOT))

import src.wechat_ui as w  # noqa: E402

OWN_GREEN = (157, 242, 159)
GREEN_TOL = 26
MIN_VOTES = 3


def is_green(px: tuple[int, int, int], tol: int = GREEN_TOL) -> bool:
    return all(abs(px[i] - OWN_GREEN[i]) <= tol for i in range(3))


def _sample_points(line: dict[str, Any], img_w: int, img_h: int) -> list[tuple[int, int]]:
    x, y, w_, h_ = int(line["X"]), int(line["Y"]), int(line["W"]), int(line["H"])
    mid = y + max(h_ // 2, 1)
    pts: list[tuple[int, int]] = []
    for dx in (-16, -10, -4):
        pts.append((x + dx, mid))
        pts.append((x + dx, mid - max(h_ // 3, 1)))
        pts.append((x + dx, mid + max(h_ // 3, 1)))
    for dx in (4, 10, 16):
        pts.append((x + w_ + dx, mid))
    return [(px, py) for px, py in pts if 0 <= px < img_w and 0 <= py < img_h]


def classify_line(img: Any, line: dict[str, Any]) -> dict[str, Any]:
    """判定这一行是否落在"自己发出的绿色气泡"里。"""
    px = img.load()
    pts = _sample_points(line, img.width, img.height)
    greens = [(x, y, px[x, y][:3]) for x, y in pts if is_green(px[x, y][:3])]
    own = len(greens) >= MIN_VOTES
    return {
        "own": own,
        "green_votes": len(greens),
        "samples": len(pts),
        "color": greens[0][2] if greens else None,
    }


def annotate(image_path: str | Path) -> dict[str, Any]:
    from PIL import Image

    png = Path(image_path)
    lines = w.ocr(png)
    img = Image.open(png).convert("RGB")
    rows = []
    for line in lines:
        info = classify_line(img, line)
        rows.append(
            {
                "y": line["Y"],
                "x": line["X"],
                "w": line["W"],
                "h": line["H"],
                "left_ratio": round(line["X"] / img.width, 3),
                "right_ratio": round((line["X"] + line["W"]) / img.width, 3),
                "own": info["own"],
                "green_votes": info["green_votes"],
                "chars": len(line["n"]),
            }
        )
    own = [r for r in rows if r["own"]]
    other = [r for r in rows if not r["own"]]
    return {
        "image": str(png),
        "size": [img.width, img.height],
        "lines": len(rows),
        "own_lines": len(own),
        "other_lines": len(other),
        # 自洽性：自己的气泡右边缘应当贴近窗口右侧
        "own_mean_right_ratio": round(sum(r["right_ratio"] for r in own) / len(own), 3) if own else None,
        "other_mean_right_ratio": round(sum(r["right_ratio"] for r in other) / len(other), 3) if other else None,
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="微信气泡归属（绿色=自己）只读判定")
    ap.add_argument("--image", required=True)
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()
    result = annotate(args.image)
    if not args.full:
        result.pop("rows")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
