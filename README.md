# BOT_of_SAKU

## 機能

- `!join` / `!leave`：ボイスチャンネルへの参加・退出
- `/talk <文章>`：AIが返答し、音声ファイル（mp3）を添付
- `/usage`：今日の使用量を表示（本人にだけ見える）
- `/persona`：性格の確認・変更・リセット（管理者だけ。本人にだけ見える）
- `@ボット名 <文章>`：メンションすると会話できます（チャンネルごとに直近の会話を記憶）
- 会話（`/talk` とメンション）は1人1日20回までです（日本時間で日付が変わるとリセット）。回数は再起動するとリセットされます。
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
   - `FISH_MODEL`（省略可。既定は無料枠の `s2.1-pro-free`。有料の `s2.1-pro` にもできます）
2. Discord Developer Portal で Message Content Intent を有効にします。
3. 音声再生とファイル変換（音声・動画）には ffmpeg が必要です。
4. Python 3.10 以上で、依存パッケージ（discord.py、python-dotenv、groq、Fish Audio SDK、Pillow、PyMuPDF）をインストールし、`python bot.py` で起動します。

## Koyeb へのデプロイ

Koyeb では Dockerfile でデプロイします。Web Service として作成してください（PORT で応答するヘルスチェック用サーバーを内蔵しています）。

1. GitHub リポジトリを接続し、Builder で Dockerfile を選びます。
2. 環境変数（Secret として設定）:
   - `DISCORD_BOT_TOKEN`
   - `GROQ_API_KEY`
   - `FISH_API_KEY`
   - `FISH_REFERENCE_ID`
   - `PERSONA`（性格の設定。`persona.txt` の中身をそのまま貼る）
   - 任意: `FISH_MODEL`
3. ポートは 8080（Koyeb が `PORT` を渡すので、通常は変更不要）。
4. デプロイ後、ログに「ログイン成功」と出れば起動できています。

注意:
- 無料枠は一定時間アクセスがないとスリープする場合があります。Discord の接続が切れることがあるので、料金プランと挙動を確認してください。
- 音声（VC）の送信は UDP を使います。Koyeb 上で `!join` が動くかは、実際に試して確認してください。
- `persona.txt` と `.env` はイメージに含めない設定です（`.dockerignore`）。
