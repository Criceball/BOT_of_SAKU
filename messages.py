"""ボットがユーザーに返す文章を、messages.json からまとめて読む。

使い方：get_msg("キー", 変数名=値)
- 文章の {変数名} の部分に、値が入る。
- キーが無いとき、変数が足りないときは、キーの名前をそのまま返す（ボットが止まらないように）。
"""

import json
import os

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "messages.json")

with open(_PATH, encoding="utf-8") as _f:
    _MESSAGES: dict[str, str] = json.load(_f)


def get_msg(key: str, **values) -> str:
    text = _MESSAGES.get(key)
    if text is None:
        return key
    try:
        return text.format(**values) if values else text
    except (KeyError, IndexError, ValueError):
        return text
