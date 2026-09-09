from __future__ import annotations

import re
from typing import Any


# 小红书互动数会出现 “1,234”“1.2万”“2k” 等格式。
# 后续排序、预算和信号聚合都依赖整数，所以统一在这里转换。
def to_int(value: Any) -> int:
    """把小红书互动数字转成 int，兼容 1,234 / 1万 / 1.2万 / 2k。"""

    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip().lower().replace(",", "")
    if not text:
        return 0

    multiplier = 1
    if text.endswith("万"):
        multiplier = 10000
        text = text[:-1]
    elif text.endswith("w"):
        multiplier = 10000
        text = text[:-1]
    elif text.endswith("k"):
        multiplier = 1000
        text = text[:-1]

    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return 0
    return int(float(match.group()) * multiplier)
