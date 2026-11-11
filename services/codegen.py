"""/code で HTML1枚のページを作る（Gemini を使う）。"""

import re

from services.gemini import GeminiError, generate

MAX_OUTPUT_TOKENS = 16000
THINKING_BUDGET = 2048

SYSTEM_PROMPT = """あなたは、ブラウザで動く Web ページを1つのHTMLファイルとして作ります。
- 出力は、```html で囲んだ単一のHTMLファイルだけにしてください（前置きや長い説明は書かない）。
- HTML・CSS・JavaScript はすべてそのファイルの中に書いてください。
- 外部の読み込みは、どうしても必要なとき以外は使わないでください。使うなら cdnjs.cloudflare.com だけにしてください。
- 画面の文字は日本語にしてください。
- 結果が見やすく、操作すると動くものを優先してください。
"""

FIX_PROMPT = """次のHTMLを、指示に従って直してください。直した後の完全なHTMLを、```html で囲んで出力してください。

--- 今のHTML ---
{html}

--- 直してほしいこと ---
{request}
"""


class CodeGenError(Exception):
    """ユーザーに説明できる失敗（長すぎる、キーがない、など）。"""


def _extract_html(text: str) -> str | None:
    match = re.search(r"```html\s*(.*?)```", text, re.S | re.I)
    if match:
        return match.group(1).strip()
    stripped = text.strip()
    if stripped.lower().startswith(("<!doctype", "<html")):
        return stripped
    return None


async def generate_html(request: str, previous_html: str | None = None) -> tuple[str, int]:
    """HTML1枚と、使ったトークン数を返す。previous_html があれば、それを直す。"""
    user_text = FIX_PROMPT.format(html=previous_html, request=request) if previous_html else request
    try:
        text, used, truncated = await generate(
            SYSTEM_PROMPT, user_text,
            max_output_tokens=MAX_OUTPUT_TOKENS, thinking_budget=THINKING_BUDGET,
        )
    except GeminiError as e:
        raise CodeGenError(str(e)) from e
    if truncated:
        raise CodeGenError("ページが長すぎて途中で切れました。もう少し小さくして頼んでください。")
    html = _extract_html(text)
    if not html:
        raise CodeGenError("HTMLとして取り出せませんでした。言い方を変えて試してください。")
    return html, used
