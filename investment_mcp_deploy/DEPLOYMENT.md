# 個人向けクラウド運用とChatGPT接続

## 選定構成・概算

GitHub → 手動起動の GitHub Actions → Fly.io Docker（1 Machine、512MB、`iad`）→ `/data` の永続Volume（初期1GB）。HTTPSはFly Proxy、OAuthは既存のOAuth/OIDC認可サーバー（設定例はAuth0）。PCの常時稼働は不要。

Fly公式料金資料を2026-10-31に公式GitHub原本で確認した基準額：512MB shared-cpu-1x **$3.69/月**、Volume **$0.15/GB/月**、合計 **約$3.84/月**。1GB Machineなら約$6.85/月。北米からの送信は$0.02/GB。スナップショットは最初の10GB/月が無料、それ以降$0.08/GB/月。税・為替・OAuthサービス・J-Quants契約・独立バックアップ先・CI超過料金は別。無料運用を保証する構成ではない。

単一Machine/Volumeは低コスト優先でありHAではない。Volumeはホスト内のローカル永続ストレージで、自動複製されない。Flyも冗長化と独立バックアップを推奨している。障害時に一時停止を許容できる個人向けの選択。可用性が必要ならLiteFS等の別設計が必要。

## 1. 本人が行う初回設定

以下は契約・課金・シークレット登録・外部公開を伴うため、この実装作業では実行していない。

1. Fly.ioの契約・支払設定を確認する。`fly.toml`の`app`を固有名に変更する。
2. OAuth認可サーバーを用意する。Auth0ならAPI Identifierを **`https://YOUR-APP.fly.dev/mcp`**、署名をRS256、scopeを`investment:read`と`investment:update`に設定する。所有者の`sub`を確認する。不要なユーザー登録を無効化し、アプリへのアクセスを自分に限定する。
3. ChatGPT用OAuthアプリを事前登録する。Authorization Code + PKCE（S256）、必要ならrefresh token rotationを有効化する。callback URLはChatGPTの接続画面に表示される値を**そのまま**登録する。値を推測しない。認可サーバーはHTTPSのOIDC discovery、JWKS、authorization/token endpointを提供する必要がある。
4. Auth0を使う場合、この個人用tenantの Default Audienceを上記API Identifierに設定するか、認可サーバーでRFC 8707 `resource`を同じaudienceへ対応付ける。MCPクライアントが`resource`を送ってもAuth0で自動的にaudienceになるとは仮定しない。アクセストークンに正しい`aud`、`iss`、`sub`、`scope`が入ることを確認する。ID tokenをAPI認証に使わない。
5. 読み取り接続には`investment:read`、更新する所有者接続には両scopeを発行する。署名済みsubject一致**かつ**update scopeが必要。scope付与だけで他人が所有者にはならない。
6. J-Quants公式ダッシュボードでAPIキーと契約範囲を確認する。キーをチャット・コマンド履歴・Gitに書かない。

このサーバーはOAuth **resource server**であり、認可サーバーやユーザー登録機能を自作しない。Dynamic Client Registrationを実装していないIdPでは、ChatGPT画面で事前登録したclient ID/secretを指定できる接続方式が必要。利用プラン・ワークスペースの接続機能を先に確認する。

## 2. Flyへのデプロイ

公式flyctlをインストールして本人がログインした後、リポジトリルートで実行する。アプリ名を設定済みであること。**Volume作成・デプロイは課金とHTTPS公開を伴う。**

```sh
fly apps create YOUR-APP
fly volumes create investment_data --app YOUR-APP --region iad --size 1
```

FlyダッシュボードのSecretsで以下を登録する。`INVESTMENT_OWNER_SUB`等は非秘密だが運用上同じ設定経路でよい。

| 設定 | 値・用途 |
|---|---|
| `JQUANTS_API_KEY` | 本人のAPIキー。必須（実データ取得） |
| `OAUTH_ISSUER` | 例 `https://YOUR-TENANT.auth0.com/`。末尾`/`を含め実際の`iss`と完全一致 |
| `MCP_RESOURCE_URL` | `https://YOUR-APP.fly.dev/mcp`。JWTのaudienceと完全一致 |
| `INVESTMENT_OWNER_SUB` | 所有者アクセストークンの`sub` |
| `JQUANTS_AVAILABLE_FROM` | 任意。契約で確認した最古の利用可能日 |
| `JQUANTS_AVAILABLE_THROUGH` | 任意。契約で確認した最新の利用可能日。固定値なので定期更新が必要 |

```sh
fly deploy investment_mcp_deploy --config "$PWD/fly.toml" --ha=false
fly status --app YOUR-APP
fly checks list --app YOUR-APP
```

1 Machineだけを維持する。独立したSQLiteを持つ2台を自動作成しない。Volumeがない場合やDBがVolume外の場合、HTTPサーバーは起動を拒否する。コンテナは起動時にVolumeルートの所有者をuid 10001に調整し、Pythonプロセスは非rootで動く。既存DBを移す場合は先にバックアップし、DB/WAL/SHMの権限をuid 10001で書けるようにする。元DBは削除しない。

GitHubからの再デプロイは `.github/workflows/deploy.yml` を手動実行する。GitHub Environment `production`を作り、必要なら承認者を設定する。そのEnvironmentのSecretsに対象アプリだけに限定した`FLY_API_TOKEN`を登録する。実行前にCIが緑であることを確認する。deploy workflow自身も全テストを実行する。Secretsがない状態では実行しない。

## 3. 起動・機能確認

`GET /healthz`は認証不要でDB readinessのみを返す（行数や秘密は返さない）。`POST /mcp`はBearer access tokenがない場合401、read scope不足は403。OAuth Protected Resource Metadataは `/.well-known/oauth-protected-resource/mcp`。

本番で最初に `database_status` と `search_instruments` を呼ぶ。空DBでは0件が正常。**テストデータを本番に投入しない。** 所有者トークンで`run_update`（開始・終了ISO日付、最大31日）を呼び、返されたjob IDを`update_job_status`で確認する。初回は契約範囲内の1日で確認する。履歴は期間を分割して取得する。

読み取りMCPツール：`search_instruments`、`lookup_price`、`financial_information`、`technical_indicators`、`database_status`。
所有者専用：`run_update`、`update_job_status`。
CSVファイル読み取りや任意SQLをリモートに公開しない。

## 4. ChatGPT接続

ChatGPTのアプリ／コネクタのカスタムMCP接続機能で、URL `https://YOUR-APP.fly.dev/mcp` とOAuth認証を指定する。必要なclient ID/secretは接続設定画面で入力する。認可画面で自分のアカウントを確認して接続する。

最初に「登録銘柄を検索して」「DBの更新状況を教えて」で読み取りを検証し、その後に所有者として小さな更新を試す。ChatGPT側のUI名、利用可否、事前登録clientの指定可否はプランに依存するため実アカウントでの確認が必要。この実装作業では実際のChatGPT OAuth接続を検証していない。

## 5. 自動更新・再開・プラン制限

Fly設定の`INVESTMENT_SCHEDULE_ENABLED=true`で同一アプリ内スケジューラーを有効化。日本時間の平日18時以降に1回、直近7暦日を再取得して遅延開示・直近訂正を反映する。祝日もAPIに問い合わせ、成功した空応答は完了として記録する。通常の営業日判定をローカルの祝日表で推測しない。

CLI・MCP・スケジューラーは同じDB横のOSファイルロックを共有。重複起動は拒否し、SQLiteはWAL＋busy timeoutで読み取りと更新を共存させる。再起動で未完ジョブが残った場合、次の更新時に`interrupted`へ変更。通常更新はAPI・日付ごとの完了単位をスキップし、途中のページは同じ日付を再取得する。全ページが揃う前に株価や財務をコミットしない。`refresh_completed=true`は完了済みの日付も再取得する。

古い訂正や分割後の過去調整株価は直近7日だけでは拾えない。必要に応じ所有者が過去期間を31日以内に分割し、refreshを実行する。テクニカル指標は登録された調整終値から計算するため、履歴更新後に再照会する。

APIはデフォルト12.2秒間隔、35秒timeout、最大5試行、429/一時的5xxにはRetry-Afterまたは指数バックオフ＋jitter。プランの正確な利用頻度に合わせ`JQUANTS_INTERVAL_SECONDS`を調整する。外部プログラムが同じキーを同時使用する場合、その通信分も契約上の制限に含まれる。

J-Quantsの利用可能API・遅延・履歴期間は[公式data-spec](https://jpx-jquants.com/spec/data-spec)と[rate-limits](https://jpx-jquants.com/spec/rate-limits)および本人の契約画面で確認する。この環境ではそのWebサイトがネットワークポリシーにより取得できず、現在のプラン別条件は未確認。古い「Freeプラン」の条件をv2にも適用したと断定しない。設定された範囲外は理由付きでスキップ。403は`access_denied_subscription_or_permission_unconfirmed`として記録し、プラン制限と断定しない。401は失敗。skippedがあるジョブは`partial`であり成功扱いにしない。失敗したスケジュールは最大1時間ごとに再試行する。

## 6. セキュリティの境界

Fly ProxyでTLSを終端し、アプリの8000番は公開HTTPサービスの内部ポートとして扱う。アプリは署名鍵を設定済みissuerのJWKS URLだけから取得し、RS256署名・issuer・audience・有効期限・subject・scopeを検証する。`X-Forwarded-*`や認証済みを示す任意ヘッダーは信用せず、`INVESTMENT_TRUSTED_AUTH_PROXY`も参照しない。OAuth管理者権限とFlyの組織アクセスも本人だけに制限する。

SQLはパラメータ化、コード・日付・件数を検証。MCPリクエストは64KiB、全体で120リクエスト/分に制限。個人用の全体制限で、分散DDoS防御ではない。APIレスポンスは16MB/ページ、48MB/取込単位・10万行に制限。APIキー付きリクエストのリダイレクトは禁止。リモートエラーに例外本文・DBパス・キー・JWTを返さない。SDKの詳細例外ログとHTTPアクセスログは本番起動で無効化する。

## 7. バックアップ・復旧

デプロイ・既存DBへの初回移行前にはSQLite online backup APIで整合性の取れたコピーを作る。稼働中のDBファイルを`cp`だけで複製しない。

```sh
# アプリ内の所有者シェルから。毎回、存在しない固有の保存名を使う。
python maintenance.py backup --destination /data/backups/investment-YYYYMMDD-HHMM.sqlite
python maintenance.py check
```

`maintenance.py backup`は新規ファイルを0600で作り、integrity_checkを行う。Volume内のコピーは同じディスクの障害に耐えない。本人がFlyのSSH/SFTP等で**別の安全な保存先**へ定期転送する。DBをGitHub artifactへ上げない。バックアップ先の契約・認証が未提供なので独立バックアップの自動転送は未設定。

Flyの自動Volume snapshots（既定毎日・5日保持）も補助として利用する。復旧はスケジューラーと更新処理を停止し、新しいVolumeへsnapshotまたは検証済みバックアップを復元し、uid 10001の権限と`maintenance.py check`を確認してから1 Machineだけを起動する。[公式snapshot手順](https://fly.io/docs/volumes/snapshots/)に従い、元Volumeを消す前に新しい環境で照会を確認する。実クラウドの復旧訓練は未実施。

## 公式資料

- [J-Quants公式SDK](https://github.com/J-Quants/jquants-api-client-python/tree/4a6ab360dbd43e763595f7ad362e749a25d6439d)：v2認証・エンドポイント・フィールドを照合済み。
- [MCP認証仕様](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-11-25/basic/authorization.mdx)：resource server / Protected Resource Metadata / audience検証。
- [Fly料金公式原本](https://github.com/superfly/docs/blob/e58f9d7cef54f42c2bb252e87aa08fd58b93667e/about/pricing.mdx)、[Volume公式原本](https://github.com/superfly/docs/blob/e58f9d7cef54f42c2bb252e87aa08fd58b93667e/volumes/overview.mdx)：料金・単一Volume制約を照合済み。
- [OpenAI公式MCP利用例](https://github.com/openai/openai-cookbook/blob/main/examples/mcp/mcp_tool_guide.ipynb)：remote MCPと認証ヘッダーの説明。ChatGPT製品UIの実接続を証明する資料ではない。
