# 無料運用への移行と接続手順

**予算は0円・カード登録なしを優先する。** 有料のFly.ioデプロイ設定とworkflowは削除した。外部サービスの契約・デプロイ・課金設定は一切実行していない。

現行Pythonサーバーはローカル／Dockerで動作確認済みだが、無料クラウドへのデプロイは未完了。無料候補と制限は [FREE_HOSTING.md](FREE_HOSTING.md) を参照。Cloudflare Workers Free + D1 Freeへ変更する場合は、当初のSQLiteファイル方式からの変更と、D1/MCP接続部分の実装・再検証が必要になる。

以下は現行実装のMCP・OAuth仕様であり、無料版のクラウド接続が既に使えることを意味しない。URLの `YOUR-APP` は、採用した無料サービスのHTTPS URLへ置き換える。独自ドメイン購入は不要。

## OAuthの設定条件

外部OAuth認可サーバーはカード登録なしのFree契約だけを使う。無料条件を登録画面で確認できるまでは登録しない。API Identifierは実際のMCP URL、署名RS256、scopeは `investment:read` と `investment:update`。Authorization Code + PKCE S256、HTTPS discovery/JWKSが必要。callback URLはChatGPTの接続画面に表示された実値を事前登録する。

`OAUTH_ISSUER`は実際のissuer、`MCP_RESOURCE_URL`は実際のAPI audienceと完全一致させる。`INVESTMENT_OWNER_SUB`は所有者のsubject。ID tokenをアクセストークン代わりに使わない。認可サーバーで `resource` とaudienceの対応を確認し、MCPクライアントが送るresourceが自動でaudienceになると仮定しない。不要なユーザー登録と他人へのアクセスを無効化する。

## 3. 起動・機能確認

`GET /healthz`は認証不要でDB readinessのみを返す（行数や秘密は返さない）。`POST /mcp`はBearer access tokenがない場合401、read scope不足は403。OAuth Protected Resource Metadataは `/.well-known/oauth-protected-resource/mcp`。

本番で最初に `database_status` と `search_instruments` を呼ぶ。空DBでは0件が正常。**テストデータを本番に投入しない。** 所有者トークンで`run_update`（開始・終了ISO日付、最大31日）を呼び、返されたjob IDを`update_job_status`で確認する。初回は契約範囲内の1日で確認する。履歴は期間を分割して取得する。

読み取りMCPツール：`search_instruments`、`lookup_price`、`financial_information`、`technical_indicators`、`database_status`。
所有者専用：`run_update`、`update_job_status`。
CSVファイル読み取りや任意SQLをリモートに公開しない。

## 4. ChatGPT接続

ChatGPTのアプリ／コネクタのカスタムMCP接続機能で、URL `https://YOUR-FREE-HOST/mcp` とOAuth認証を指定する。必要なclient ID/secretは接続設定画面で入力する。認可画面で自分のアカウントを確認して接続する。

最初に「登録銘柄を検索して」「DBの更新状況を教えて」で読み取りを検証し、その後に所有者として小さな更新を試す。ChatGPT側のUI名、利用可否、事前登録clientの指定可否はプランに依存するため実アカウントでの確認が必要。この実装作業では実際のChatGPT OAuth接続を検証していない。


## 現行実装のセキュリティと更新

未認証のMCPアクセスは拒否し、JWT署名・issuer・audience・有効期限・subject・scopeを検証する。更新には所有者subject一致とupdate scopeの両方が必要。任意のプロキシヘッダーや `INVESTMENT_TRUSTED_AUTH_PROXY` では認証できない。

現行Pythonの更新はAPI/日付単位のトランザクションとOSロックを使う。無料サーバーレス環境では、このロック・起動・永続化の仕組みをそのまま使えないため、クラウド側の実装方式を確定してから再検証する。

J-Quantsの最新プラン条件は未確認。本人の無料契約で利用可能なAPIと期間のみを使い、利用できない機能は理由付きでスキップする。追加料金の発生する契約はしない。既存DBを移す際は `maintenance.py backup` でコピーを作り、コピー上でスキーマ互換と整合性を確認する。元DBを削除・上書きしない。
