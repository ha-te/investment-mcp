# 無料版のセットアップ・ChatGPT接続

費用方針と上限は [FREE_HOSTING.md](FREE_HOSTING.md)。以下は**本人がカードなしFreeを確認した後だけ**実行する。有料プランへの変更やカード登録が必要になったら進めない。まだ実クラウドへデプロイしていない。

## 1. ローカル検証

リポジトリの実装ブランチで、Node 24とPython 3.12を使う。

```sh
cd free_worker
npm ci --no-audit --no-fund
npm test
cd ../investment_mcp_deploy
python -m pip install --require-hashes -r requirements.lock
python -m unittest discover -v
```

Workerのbuildは`wrangler deploy --dry-run`であり公開しない。テストの価格・財務・JWTは使い捨てのローカルDB／エミュレーターだけに入る。

## 2. FreeのD1とHTTPS Worker

CloudflareのWorkers Freeアカウントを本人が作成する。カードや支払方法を登録しない。`free_worker/`で次を実行する。

```sh
npx wrangler login
npx wrangler d1 create investment-free
```

返された非秘密の`database_id`を`wrangler.jsonc`に設定する。既存DBを勝手に指定せず、専用の空D1を使う。テンプレートの`REPLACE_WITH_FREE_D1_DATABASE_ID`のままではデプロイしない。

```sh
npx wrangler d1 migrations apply investment-free --remote
```

`migrations/0001_investment.sql`は既存Pythonの追加型スキーマと互換。D1は永続保存先で、Actionsの一時ファイル領域を永続DBとして使わない。既存の実SQLiteファイルをD1へ自動移行する機能は含まない。実DBがある場合は元DBを残し、バックアップのコピーでスキーマとインポート手順を個別確認する。

## 3. 無料OAuthとシークレット

カードなしの無料OAuth/OIDCプロバイダーを本人が登録する。Auth0は候補の一つだが、必要機能と無料条件を登録時に確認する。必要条件：HTTPSのOIDC discoveryとJWKS、RS256 access token、Authorization Code + PKCE S256。callback URLはChatGPTの接続画面に表示された値をそのまま登録する。ユーザー登録・アプリ利用は本人に限定する。現行のJWKS取得先はissuer末尾の `/.well-known/jwks.json`（Auth0互換）なので、異なるJWKS URLのプロバイダーは追加対応が必要。

API Identifier/audienceを最終URL `https://investment-mcp-free.YOUR-SUBDOMAIN.workers.dev/mcp` にする。scopeは `investment:read` と `investment:update`。issuerは実際の`iss`と末尾`/`を含めて完全一致させる。ID tokenでAPIへアクセスしない。Auth0ならDefault Audience等でresourceとaudienceの対応を確認し、`resource`パラメーターが自動的にaudienceになると仮定しない。

以下はWranglerの対話プロンプトから登録する。値をコマンド引数・チャット・Git・ログに書かない。

```sh
npx wrangler secret put OAUTH_ISSUER
npx wrangler secret put MCP_RESOURCE_URL
npx wrangler secret put INVESTMENT_OWNER_SUB
npx wrangler secret put SYNC_KEY
```

`INVESTMENT_OWNER_SUB`は本人のアクセストークンの`sub`。`SYNC_KEY`はパスワードマネージャー等で生成した32〜128文字のランダム値で、後述のGitHub Secretと同じ値にする。同期用の所有者資格情報なので読み取りユーザーに渡さない。JWT認証を代替するプロキシフラグはない。

本人がWorkers Freeを確認済みなら公開する。

```sh
npx wrangler deploy
```

標準のworkers.dev URLを使い、ドメインは購入しない。`GET /healthz`で200、未認証の`POST /mcp`で401を確認する。

## 4. GitHub無料枠内の更新

先に変更ブランチを確認し、検証後にmainへマージする。GitHubのscheduleとworkflow_dispatchにはdefault branch上のworkflowが必要。リポジトリの公開範囲は変えない。

GitHub Repository SettingsのSecrets and variablesで設定する。

| 種別 | 名前 | 内容 |
|---|---|---|
| Secret | `JQUANTS_API_KEY` | 本人の無料契約で使えるv2キー |
| Secret | `SYNC_KEY` | Workerに登録した同期専用キー |
| Variable | `FREE_MCP_ORIGIN` | `https://investment-mcp-free.YOUR-SUBDOMAIN.workers.dev`（`/mcp`なし） |
| Variable | `INVESTMENT_SYMBOLS` | 本人の監視銘柄をカンマ区切りで1〜50件。最初は5〜10件 |
| Variable | `JQUANTS_DATA_DELAY_DAYS` | 任意。無料契約で確認した最新データの遅延日数。未設定0。プラン条件を推測して指定しない |
| Variable | `JQUANTS_AVAILABLE_FROM` | 任意。無料契約で確認した最古の日付 |
| Variable | `JQUANTS_AVAILABLE_THROUGH` | 任意。無料契約で確認した最新の日付。固定値なので更新が必要 |
| Variable | `FREE_SYNC_ENABLED` | 無料時間・残量と支出上限0を確認後だけ `true` |
| Variable | `FREE_CI_ENABLED` | 同じ無料枠確認後だけ `true` |

両ENABLE変数は未設定なら実行しない。skippedはテスト通過や同期成功を意味しない。支払方法なし・無料枠だけで動くことを確認できなければ有効化しない。CIや同期はartifactを作らず、一時DBをランナー終了時に破棄する。

更新workflowは平日18:15（日本時間）に無料契約で確認した最新利用可能日までの7暦日を再確認する。GitHubの遅延・無料枠枯渇・スケジュール無効化で停止する可能性がある。Actions画面で更新履歴を確認する。APIは既定12.2秒間隔・最大5試行、429/一時的5xxをretryする。GitHub concurrencyとD1の30分リースの両方で二重起動を防ぐ。期限切れリースの処理は書き込めない。

D1の現在値を一時SQLiteへ読み戻し、既存Python処理でAPIの各日付を全件検証してから差分だけをD1へ反映する。異常データは同じ単位全体をrollback。500行を超える差分は書く前に停止。完了チェックポイントはデータと同じD1 batch内で保存する。途中で止まっても次回は完了単位をスキップできる。財務訂正・株価改訂は履歴を残す。

無料期間外は理由付きでskipped、403はプラン／権限未確認として記録し、401はfailed。skippedがある更新はpartialとなる。これらを解決するための有料契約は行わない。

## 5. 所有者による即時更新（任意）

追加の資格情報を持たせたくなければこの節は省略できる。MCPの更新ツールはD1へqueuedジョブを保存し、次の無料スケジュールまたは本人の手動workflow実行が拾う。

即時にGitHub workflowを起動したい場合のみ、本人が対象repoだけのActions write権限を持つfine-grained tokenを作り、Workerへ`GITHUB_DISPATCH_TOKEN`として対話登録する。`GITHUB_REPOSITORY`は`ha-te/investment-mcp`、`GITHUB_REF`は通常`main`としてWorkerのVariablesに設定する。GitHub APIのリダイレクトにはトークンを転送しない。ディスパッチ失敗時もジョブはqueuedとして残る。

## 6. ChatGPT接続

本人の**現在のプラン**でカスタムMCPとOAuthが利用できるか確認する。追加料金が必要なら契約しない。接続できる場合、URLを `https://investment-mcp-free.YOUR-SUBDOMAIN.workers.dev/mcp`、認証をOAuthにする。事前登録したclient ID/secretを接続画面で指定する方式を使う。Dynamic Client Registrationを提供しないIdPを使う場合は、その手動登録方式がChatGPT画面で使えることが前提。

読み取りは`investment:read`、更新する本人だけは`investment:update`も発行する。最初に`database_status`と`search_instruments`を呼び、次に所有者の`run_update`で無料契約範囲内の1日だけを検証する。`update_job_status`で結果を確認する。無料版は1回最大7暦日。

ツールは銘柄検索、株価、財務訂正履歴、調整終値SMA/Cutler RSI、DB統計、所有者更新、所有者ジョブ照会の7つ。任意SQLやサーバーファイル読み取りは公開しない。

## 7. 運用と残る検証

実CloudflareのFree CPU・D1行数、実IdPの認可フロー、J-Quants無料API、ChatGPTからの呼び出しは未検証。ローカルエミュレーターの成功をこれらの成功とは扱わない。

D1 FreeのTime Travelは7日。重要な実データの退避は本人の安全な保存先へ行う。GitHub artifactやGitへ実DBをアップロードしない。保存容量が増えたらバックアップ後に保持範囲を見直す。枯渇時に有料プランへ変更しない。
