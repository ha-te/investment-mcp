# 実装と検証範囲

## 調査時の状態

初期HEADは`38a321a`。README、Dockerfile、`server.py`、`jquants.py`、`remote_app.py`、requirements、13件のunittestがあった。GitHub Actionsやマイグレーションは存在せず、DEPLOYMENTの「CI syntax/compile check」という記述に対応するworkflowもなかった。実DBや既存全スキーマの定義は同梱されていない。既存テストに含まれる列定義を互換性の基準にした。実際の過去DBでの移行検証は別途必要。

既存宣言`mcp>=1,<3`はFastMCPを含まないMCP 2系も選ぶため起動失敗を再現した。MCP 1.30.0へ固定し、依存全体をSHA256ハッシュ付きrequirements.lockに固定した。

## ファイルと責務

| ファイル | 役割 |
|---|---|
| `server.py` | 既存CSV/SEC互換関数、ローカルstdio起動。実データを自動生成しない |
| `jquants.py` | 既存HTTPクライアントの拡張。キー認証、ページング、リトライ、リダイレクト拒否。旧単日プレビューは互換維持 |
| `database.py` | 追加型スキーマ、明示トランザクション、検索・株価・財務・調整終値指標 |
| `sync.py` | 銘柄・株価・財務の日付/API単位の原子的取込・チェックポイント |
| `jobs.py` | OSロック、永続ジョブ状態、再開、平日18時以降の更新 |
| `app.py` | 共通MCPツール、読み取り／所有者操作の分離 |
| `security.py` | JWKS/RS256検証、scope/所有者検証、本文サイズ・レート・受信timeout制限 |
| `remote_app.py` | 永続マウントの確認、HTTPアプリ、readiness |
| `maintenance.py` | SQLite online backupと整合性確認 |

## スキーマ互換・記録

`instruments`、`prices`、`sources`、`financial_facts`の既存列・IDを削除・変更しない。空DBには既存テストと互換の最小コアを作り、既存DBは必須列を事前検査する。旧`financial_facts`の詳細スキーマは不明なので、元のデータは維持し、v2財務は別の`jq_financial_disclosures`へ保存する。最小コアの`financial_facts(id)`に架空の財務値は入れない。

追加領域：銘柄スナップショット、財務開示リビジョン、株価改訂、API/日付チェックポイント、取込履歴、更新ジョブ、企業間関係、イベント、イベント波及仮説、テクニカル指標。企業間関係・イベント・波及分析は出典付きの保存構造を用意した段階であり、J-Quantsから自動推論・自動生成しない。

同じ開示番号の内容変更もSHA256が異なれば別バージョンとして保存する。株価は全観測リビジョンを保存し、現在値だけをUPDATEする。訂正の観測は取得時刻で記録し、過去に知られていた値と断定しない。JSONは元フィールド名を保つ。公表時刻がある財務は日本時間付きで保存する。

コアにもUNIQUEインデックスを追加し、既存DBに競合する重複があれば削除せず移行を停止する。欠落列・重複・外部キー不整合は別途バックアップ上で解決が必要。`BEGIN IMMEDIATE`、WAL、busy timeout、UNIQUE/UPSERT、外部キー検査、quick_checkを使用。1単位内の未知コードや異常値で全取込をrollbackする。ベーステーブルに追加のNOT NULL列がある実DBでは、その列の初期値を含む個別対応が必要となる可能性がある。

調整終値ベースのSMA 5/25/75と**Cutler RSI 14**を提供する。Wilder RSIとは異なる。欠損・履歴不足はnull/理由を返し、価格や財務を補間しない。更新後の最新指標を保存し、MCP照会は現時点の登録値から再計算する。

## 検証結果（現在のクラウド開発マシン）

- Python 3.12で既存テストと追加の回帰テストを実行。最新の無料版検証件数は末尾を参照。認証ヘッダー、ページング、429 retry、401 redaction、redirect拒否、繰返しcursor、重複・改訂、rollback、再開、プラン窓、ロック、スケジュール、SQL入力、移行、バックアップ、JWT、未認証拒否、所有者権限、本文・rate制限、OAuth metadata、HTTP MCPツールを検証。
- 実stdioサブプロセスでinitialize、tools/list、database_statusを呼び出す。HTTPは署名付きテストJWTとASGIクライアントを使用。実IdPのJWTは未使用。
- Dockerを実ビルドし、専用の使い捨てVolumeで実コンテナのhealthz=200、未認証MCP=401、再起動後のSQLite保持を確認。非rootプロセスのソース読み取り権限、同一device上のDocker bind mount検出の起動不具合を修正。
- CIはAPIキー不要。GitHub Actionsでは同じハッシュ固定依存・unittest・compileall・Dockerビルドを実行する。GitHub上の結果はPR/Actions画面で確認する。ローカル通過をリモートCI通過と同一視しない。GitHubへのブランチpushは成功したが、この開発環境からapi.github.comへのアクセスがForbiddenのため、PR作成とGitHub上のCI結果取得は未完了。
- 本番DBは作成していない。テストの架空データはTemporaryDirectoryまたは専用smoke Volumeだけで使用。

## 未検証・本人操作が必要な項目

1. **P0**：カードなしFreeのアカウント・外部OAuth登録、Secrets登録、HTTPSデプロイ。ChatGPTと実IdPの認可フロー・scope/audience・所有者更新を実機確認する。
2. **P0**：J-Quantsの契約プラン・v2利用可能APIと期間を公式契約画面で確認し、実APIの各1日分を取込検証する。キー未提供のためライブ認証・契約別制限は未検証。ネットワーク制限により公式Webの最新プラン表も未取得。
3. **P0（既存DBを移す場合）**：バックアップのコピーで実スキーマと移行を検証する。同梱テスト以上の元DB構造は未確認。
4. **P1**：独立バックアップ先と自動転送、実クラウド復旧訓練。Volume内バックアップやsnapshotだけでは十分な独立性はない。
5. **P1**：長期の価格調整・古い財務訂正の再走査頻度を本人の契約・利用目的に合わせる。日次更新は直近7暦日。
6. **P2**：無料枠内に収まるよう監視銘柄・保持期間・更新回数を見直す。有料リソースへの変更はしない。

## 無料版（最新の構成）

予算条件は0円・カードなし。D1への方式変更はユーザー確認済み。有料Fly設定とdeploy workflowは削除した。新しい公開MCPは `free_worker/` の公式TypeScript MCP SDK + Workers Free/D1 Free。API・検証・履歴・再開の中核はPythonを再利用する。

`free_sync.py` が同期用の所有者資格情報でD1リースを取得し、現在のデータを一時SQLiteに復元する。`sync.py` の日付/API単位の原子的取込と検証の後、主キーごとの変更だけを1回のD1 batchで反映する。チェックポイントも同じbatchに含める。リースは30分、コミットで更新し、期限切れの処理はSQL条件でも拒否する。GitHub側のconcurrencyも併用する。

監視銘柄は1〜50件、1更新7暦日、1原子的反映500変更行・2MiBまで。500行を超える変更を複数リクエストに分けて部分反映しない。無料枠やCPU制限で失敗しても有料プランへ変更しない。全銘柄の無制限保存ではない。実クラウドのCPU・容量・アカウント契約は未検証。

新たな検証：PythonからD1互換の実SQLiteへ、スナップショット復元・差分取込・watchlist絞込・再実行・財務異常時rollback・再開を確認する。Workerは本物のローカルWorkers/D1エミュレーターで、署名付きJWT、issuer/audience/scope、所有者権限、リース排他、D1 batch rollback、入力制限、MCP initialize/list、株価・財務・指標の呼び出しを検証する。PythonとWorkerのスキーマ互換もテストする。

GitHubのCIと同期は、本人が無料枠・残量・支出上限0を確認して `FREE_CI_ENABLED` / `FREE_SYNC_ENABLED` をtrueにするまで実行しない。skippedはテスト通過ではない。CI・同期でDB artifactは作らない。公開範囲は変更しない。

最優先の残作業は、本人によるカードなしFreeアカウント・OAuth・J-Quants無料APIの確認、Secrets登録、実デプロイとChatGPT接続。無料で利用できない外部機能を有料契約で補わない。具体的手順はDEPLOYMENT.md、上限と費用方針はFREE_HOSTING.md。

最新のローカル検証：Python 42件、Workers/D1 6件が通過。ロック固定からの再インストールとdry-run bundle作成も確認済み。互換用Dockerのビルド・非root起動・認証拒否・再起動永続化も通過。外部デプロイとリモートCIの通過は未確認。
