"""公開チャンネルの会話を、Google ドライブの「/deta」フォルダに自動で保存する。

- 対象は、@everyone が見られるテキストチャンネルだけ（非公開のチャンネルは扱わない）。
- 1行＝1発言：[日時] 名前: 内容。ボットの発言は含めない。
- 書き出すときに、メールアドレス・パスワード・||伏せ字|| を伏せる（services/redact.py）。
"""

import discord

from services.redact import mask_sensitive

DEFAULT_COUNT = 1000


def is_public(channel) -> bool:
    """@everyone がチャンネルを見られて、ボットが履歴を読めるなら True。"""
    if not isinstance(channel, discord.TextChannel):
        return False
    everyone = channel.guild.default_role
    perms = channel.permissions_for(everyone)
    me = channel.permissions_for(channel.guild.me)
    return bool(perms.view_channel and me.view_channel and me.read_message_history)


def format_message(message: discord.Message, jst) -> str:
    stamp = message.created_at.astimezone(jst).strftime("%Y-%m-%d %H:%M")
    text = mask_sensitive(message.content.replace("\r", "").replace("\n", " / "))
    for attachment in message.attachments:
        text += f" [添付: {attachment.filename}]"
    return f"[{stamp}] {message.author.display_name}: {text}"


async def build_history(channel: discord.TextChannel, jst, count: int = DEFAULT_COUNT) -> bytes:
    """チャンネルの最新 count 件（ボットの発言を除く）を、古い順のテキストにする。"""
    picked = []
    async for message in channel.history(limit=None, oldest_first=False):
        if len(picked) >= count:
            break
        if message.author.bot:
            continue
        picked.append(message)
    picked.reverse()
    lines = [format_message(m, jst) for m in picked]
    body = "\n".join(lines) or "（発言がありませんでした）"
    return body.encode("utf-8-sig")


def file_name(channel: discord.TextChannel) -> str:
    return f"{channel.name}-history.txt"
