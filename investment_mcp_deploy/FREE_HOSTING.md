# 無料・カード登録なしの構成

予算は月額0円。**Workers FreeとD1 Freeを維持し、上限では停止する。** 有料トライアル、Workers Paid、R2、課金への自動移行は使わない。外部アカウント登録・デプロイ・シークレット登録はまだ実行していない。

## 構成

- **公開MCP**：`free_worker/` のCloudflare Worker。公式TypeScript MCP SDKを使用。標準のworkers.dev HTTPS URLを使い、ドメイン購入は不要。
- **永続DB**：Cloudflare D1（SQLiteベース）。当初のSQLiteファイルを直接マウントする方式からの変更はユーザー確認済み。元の銘柄・価格・出典IDと追加型スキーマを再利用する。
- **取込・検証**：既存Pythonの`jquants.py`・`sync.py`・`database.py`を再利用。GitHub Actionsの一時SQLite上で検証し、変更したAPI/日付単位をD1の1回のbatchトランザクションで反映する。
- **自動更新**：`.github/workflows/sync-free.yml`。日本時間平日18:15に無料契約で確認した最新利用可能日までの7暦日を更新。PCの常時起動は不要。GitHubスケジュールの遅延・停止時は更新も遅れる。
- **認証**：カードなしFreeで利用できる外部OAuth/OIDC認可サーバー。JWTの署名・issuer・audience・有効期限とscopeをWorker自身で検証。所有者subjectとupdate scopeの両方がある場合だけ更新を許可する。

## 無料枠（2026-10-31に公式原本で確認）

| 項目 | Workers/D1 Free |
|---|---|
| Workerリクエスト | 1日100,000回 |
| Worker CPU | 1リクエスト10ms |
| D1読み取り | 1日5,000,000行 |
| D1書き込み | 1日100,000行。インデックス更新も数える |
| D1保存容量 | 1DB 500MB、アカウント合計5GB |
| D1復旧履歴 | 7日 |

D1公式FAQでは、Freeの読み書き上限に達するとクエリがエラーになる。保存上限では追加書き込み等ができなくなる。これを理由に有料へ移行しない。CPU上限も同様に失敗として扱い、課金プランへ切り替えない。

個人向けに**指定した監視銘柄1〜50件**だけを保存する。最初は5〜10件で試す。APIから上場一覧等を取得しても、保存前に監視銘柄へ絞る。全銘柄の長期履歴を無制限に蓄積する構成ではない。既存の過去データは自動削除しないので、監視銘柄を入れ替え続ければ保存量も増える。D1の容量・行数とActionsの残り無料時間を確認する。

1回の更新は最大7暦日、1回の原子的反映は最大500行の変更（インデックス行を除く）・2MiB。超える変更を分割して部分成功させず、停止する。必要なら銘柄を減らす。ローカル検証で実クラウドの10ms CPU制限まで保証することはできないため、最初に小規模の実接続検証が必要。

## 無料条件の確認が必要な外部サービス

- **Cloudflare**：新しいアカウントでWorkers Freeを選び、カード・支払方法を登録しない。既存アカウントがPaidなら、そのアカウントにはデプロイしない。
- **GitHub Actions**：現在のprivateリポジトリを維持する。利用プランの無料時間・残量と支出上限0を本人のBilling画面で確認してから有効化する。公開リポジトリへ変更しない。無料枠がなければ更新・CIを停止する。
- **J-Quants**：本人の無料契約で利用可能なv2 API・期間だけを使う。現行のプラン表と実アカウントは未確認。無料で最新株価や全財務APIを使えると保証しない。有料契約が必要な機能はスキップする。無料v2キーを利用できない場合は実データ更新を有効にしない。
- **OAuth認可サーバー**：例としてAuth0のFreeを検討できるが、カードなしFreeの条件を本人の登録画面で確認する。カードを求められたり、必要なOAuth機能が有料なら中止する。外部認可サーバーの無料提供条件は未検証。
- **ChatGPT**：現在の契約でカスタムMCP/OAuth接続を利用できるか本人の画面で確認する。利用できない場合、追加の有料契約はしない。コード側だけでChatGPTの利用プラン制限は解除できない。

## 公式資料

- [Workers料金](https://developers.cloudflare.com/workers/platform/pricing/)
- [D1料金](https://developers.cloudflare.com/d1/platform/pricing/)・[制限](https://developers.cloudflare.com/d1/platform/limits/)
- [上限ではエラーになるD1公式FAQ](https://github.com/cloudflare/cloudflare-docs/blob/production/src/content/partials/d1/faq-pricing.mdx)
- [D1公式無料枠](https://github.com/cloudflare/cloudflare-docs/blob/production/src/content/partials/workers/d1-pricing.mdx)
