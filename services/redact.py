"""発言の中の、秘密らしい部分を伏せ字にする（AIは使わず、正規表現だけで判定する）。

/deta の出力に使う。判定できるのは次のものだけ:
- ||で囲まれた部分（Discord のネタバレ記法）
- メールアドレスらしい文字列
- 「パスワード」「password」などの言葉のあとに続く値

キーワードのない文だけのパスワードは、見分けられないので伏せられない。
"""

import re

# ||...|| は中身ごと伏せる（中身が何でも、外に出さない）
SPOILER_PATTERN = re.compile(r"\|\|.*?\|\|", re.DOTALL)

EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# キーワードのあとに、「:」「は」「=」などの区切りか空白があって、値が続くとき
PASSWORD_PATTERN = re.compile(
    r"((?<![A-Za-z])(?:password|passwd|pass|pw)(?![A-Za-z])|パスワード|パスコード|暗証番号)"
    r"(\s*(?:は|が|is|:|：|=|＝)\s*|\s+)"
    r"(\S+)",
    re.IGNORECASE,
)


def mask_sensitive(text: str) -> str:
    text = SPOILER_PATTERN.sub("[伏せ字]", text)
    text = EMAIL_PATTERN.sub("[メールアドレス]", text)
    text = PASSWORD_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}[パスワード]", text)
    return text
