# Free Worker + D1

Cloudflare Workers **Free** / D1 **Free**向けの公開MCP。

```sh
npm ci --no-audit --no-fund
npm test
```

デプロイせずに型検査・bundle作成・本物のローカルWorkers/D1エミュレーターで検証する。公開・アカウント・無料条件・Secretsは [DEPLOYMENT.md](../investment_mcp_deploy/DEPLOYMENT.md)。カード登録・Paidへの変更はしない。

`src/schema.json`と`migrations/0001_investment.sql`はPython版のスキーマと互換。Pythonテストが両者の互換を検査する。実データの取込は既存Python処理を使い、`free_sync.py`がD1へ原子的な差分を保存する。
