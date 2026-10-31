# investment-mcp

日本株データを J-Quants v2 から SQLite に保存し、OAuth 認証付き MCP で参照する個人向けサーバー。

**無料・カード登録なしを優先。クラウド無料版は移行方式を確認中です。** Fly.ioの有料デプロイ設定は削除しました。[無料構成と制限](investment_mcp_deploy/FREE_HOSTING.md)を参照してください。

実装は `investment_mcp_deploy/`。運用・認証・料金・ChatGPT接続は [DEPLOYMENT.md](investment_mcp_deploy/DEPLOYMENT.md)、設計と検証範囲は [IMPLEMENTATION.md](investment_mcp_deploy/IMPLEMENTATION.md) を参照。

```sh
cd investment_mcp_deploy
python3.12 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/python -m unittest discover -v
# ローカル所有者向け stdio。環境変数でDB保存先を明示する。
INVESTMENT_DB=/absolute/persistent/path/investment.sqlite .venv/bin/python server.py
```

リモート公開には HTTPS と外部 OAuth 認可サーバーが必須。キー不要のテストは一時DBだけを利用する。実データ・APIキー・OAuthトークンをGitに保存しない。
