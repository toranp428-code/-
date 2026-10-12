# ペンギンBot（Discord専用）

Python `discord.py` で動く、パネル中心のDiscord Botです

## 現在の機能
- `/panel`：常設メインパネルを投稿
- `/adminpanel`：管理者パネルを表示
- 共通通貨「ペン」（全Discordサーバーで同じユーザー残高）
- スロット：1回500ペン
- ガチャ：1回300ペン、景品ウェイト設定
- モデレーター選挙：管理者が作成、匿名投票、1人1票、管理者が締め切り・集計
- SQLiteによる残高・選挙・履歴の保存

## 注意
- この版のガチャ景品はBot内の結果表示です。Discordロールや実物の景品を自動付与する機能はまだありません
- 匿名投票は一般メンバーに投票先を表示しません。Botの運用者がデータベースを直接確認できる可能性まではなくせません
- Renderの通常の一時ファイル領域は再デプロイ等で消える場合があります。永続化するにはRenderのPersistent Disk等を設定し、`DATABASE_PATH`をそのマウント先にしてください
- スロットの配当・確率はコード内の設定です。正式公開前にテスト用サーバーで確認してください

## ローカル実行
1. Python 3.11以上を用意
2. `pip install -r requirements.txt`
3. Discord Developer PortalでBotを作成し、Bot Tokenを取得
4. 環境変数 `DISCORD_TOKEN` にTokenを設定
5. `python bot.py`

Windows PowerShellの例：
```powershell
$env:DISCORD_TOKEN="ここにBot Token"
python bot.py
```

## Render
- GitHubにこのフォルダの中身をアップロード
- Renderで **Web Service** を作成（この版はHTTPヘルスチェック用サーバーを内蔵）
- Build Command: `pip install -r requirements.txt`
- Start Command: `python bot.py`
- Environment Variables: `DISCORD_TOKEN`
- `/` または `/health` にアクセスするとHTTP 200を返します
- 無料Web Serviceは一定時間アクセスがないとスリープすることがあり、その間Botもオフラインになります。24時間ずっとオンラインになる保証はありません
- 永続ディスクを追加する場合は `DATABASE_PATH` をディスク内のファイルパスに設定

Discord Developer PortalのInstallationで、`bot` と `applications.commands` のスコープを付けてサーバーに招待してください
