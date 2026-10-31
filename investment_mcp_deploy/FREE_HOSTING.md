# 無料・カード登録なしを優先する運用方針

ユーザーの予算は月額0円。有料プラン、無料トライアル後の課金、従量課金への自動移行を前提にしない。Fly.ioには契約・デプロイしていない。Fly設定とデプロイworkflowは削除した。

## 検討中の無料構成

Cloudflare **Workers Free + D1 Free**。独自ドメインを購入せず、標準のworkers.dev HTTPS URLを使う。Freeプランのまま利用し、Workers PaidやR2など支払情報が必要な製品は有効にしない。

D1はSQLiteベースのマネージドDBであり、既存SQLiteファイルをそのままマウントするサービスではない。現行Python/FastMCPサーバーをそのままWorkersへデプロイできるとは扱わない。DBスキーマ・Python取込処理は再利用を検討できるが、D1接続とMCP公開部分の対応・再検証が必要。現在は方式変更の確認待ちで、無料版クラウド実装の完成を意味しない。

## 確認できた無料枠

2026-10-31、Cloudflare公式ドキュメントのGitHub原本を確認。

| 項目 | Freeの枠 |
|---|---|
| Workersリクエスト | 1日100,000回 |
| Workers CPU | 1リクエスト10ms |
| D1読み取り | 1日5,000,000行 |
| D1書き込み | 1日100,000行（インデックス更新も数える） |
| D1保存容量 | 1DB 500MB、アカウント合計5GB |
| D1復旧履歴 | 7日 |

D1公式FAQでは、Freeの読み書き上限に達するとクエリがエラーになり、保存上限では追加書き込み等ができなくなる。上限超過を理由に有料プランへ移行しない。全銘柄の長期株価・財務訂正履歴を無制限には保存できないため、対象銘柄・期間・更新量を無料枠内に制限する必要がある。

J-Quantsも**本人の無料契約で利用できるAPI・期間だけ**を使用する。現行v2の契約条件は未確認であり、無料で最新株価や全財務APIを利用できるとは保証しない。有料契約が必要な機能はスキップして理由を残す。ChatGPTのカスタムMCP接続についても、本人の現在のプランで利用可能か確認が必要。追加契約を自動で勧めたり実行したりしない。

OAuth認可サーバーやGitHub Actionsを使う場合も、カードなしの無料枠で提供されることを本人の登録画面で確認する。無料枠に収まらなければ処理を停止・縮小し、支払情報や有料プランを登録しない。

## 一次情報

- [Workers料金](https://developers.cloudflare.com/workers/platform/pricing/)
- [D1料金](https://developers.cloudflare.com/d1/platform/pricing/)
- [D1制限](https://developers.cloudflare.com/d1/platform/limits/)
- [D1公式料金FAQ原本](https://github.com/cloudflare/cloudflare-docs/blob/production/src/content/partials/d1/faq-pricing.mdx)
- [D1公式無料枠原本](https://github.com/cloudflare/cloudflare-docs/blob/production/src/content/partials/workers/d1-pricing.mdx)
