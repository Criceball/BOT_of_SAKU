# BOT_of_SAKU

## 機能

- `!join` / `!leave`：ボイスチャンネルへの参加・退出
- `!talk <文章>`：AIが返答し、音声でボイスチャンネルに読み上げ
- `@ボット名 <文章>`：メンションすると会話できます（チャンネルごとに直近の会話を記憶）
- `@ボット名 <変換先の形式>` + ファイル添付：ファイルの形式を変換します
  - 例：PDFを添付して「これをjpegにして」→ 各ページをJPEGで返信（先頭10ページまで）
  - 返信でファイルを指定しても変換できます

### 対応する変換

| 変換元 | 変換先 |
| --- | --- |
| jpg / png / webp / gif / bmp | jpg / png / webp / gif / bmp / pdf |
| pdf | jpg / png / webp / gif / bmp（ページごと）、txt（テキスト抽出） |
| mp3 / wav / ogg / mp4 / gif | mp3 / wav / ogg / mp4 / gif |

添付ファイルは25MBまでです。

## セットアップ

1. `.env` を作成し、以下を設定します。
   - `DISCORD_BOT_TOKEN`
   - `GROQ_API_KEY`
   - `FISH_API_KEY`
   - `FISH_REFERENCE_ID`（Fish Audioの音声モデルID）
2. Discord Developer Portal で Message Content Intent を有効にします。
3. 音声再生とファイル変換（音声・動画）には ffmpeg が必要です。
4. Python 3.10 以上で、依存パッケージ（discord.py、python-dotenv、groq、Fish Audio SDK、Pillow、PyMuPDF）をインストールし、`python bot.py` で起動します。
