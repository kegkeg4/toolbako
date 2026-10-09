# 2026-09-22：本番保存・決済障害対策の実装記録

## 判定

**実カード決済を伴う公開は、まだ不可。今回のコードはローカルで実装・検証済みで、Railwayへの反映は未実施。**

「鍵を設定すれば完成」ではありません。同日追記で、単発JPYの台帳・分配／振込worker・銀行振込前の分配取消までローカル実装しました。実サービスでの検証、月額課金の台帳、結果不明の復旧・銀行振込後の回収運用は未完了です。本番Stripeキーによる書き込み停止は解除していません。

## 同日追記：売上・振込・返金

- 決済明細・振込申請・明細割当・追記イベントの4テーブルを追加。runtimeスキーマをversion 2へ更新。
- 申請トークンで二重送信を防止し、申請済み残高を再申請可能額から除外。管理画面も同じ計算に統一。
- Sandbox限定workerが分配→銀行振込→結果確認を別段階で実行。成功後の保存前クラッシュ、通信タイムアウト、資金未確定、運営保留、外部で発生した返金、振込失敗を検証。
- Connect Webhookで口座・金額・通貨・振込IDを照合。作成APIの200応答だけで振込済みにしない。
- 管理者が銀行振込前の申請・分配を取消可能。Transfer reversal確認後に予約を解除。
- 本体と追加支払いの全額返金確認を分離。未完了を全額返金済みと誤表示しない。
- 本人確認Webhookの結果を既存セッションへ反映。振込照合中の退会による口座情報削除を防止。
- PC1280pxとスマートフォン390pxで売上・振込画面の表示を確認。手数料説明の詰まりを修正。
- 通常の画面移動・セッション更新では、変更のない金融履歴のDB再同期を省略。注文・振込の変更と初回ロードは再検証する。
- 「120日後に自動振込」は未確定のデモ案。Stripe手動振込の90日ルールと整合する規約・処理期限の確定が必要。

詳細なコマンド・状態・未完了事項は [PAYOUT_OPERATIONS.md](PAYOUT_OPERATIONS.md)。

## 今回実装したもの

- `DATABASE_URL` を指定すると、既存の画面／ルートがPostgres互換repositoryを実際に使用。会員、サーバーセッション、商品、注文、Webhook処理済みID、通知等を同じDBの状態として保存。
- 専用スキーマ `toolbako_runtime` と明示的なマイグレーションコマンド。Supabaseの公開PostgRESTテーブル群とは別物。既存の `supabase/*.sql` の適用だけではこのruntimeは作成されない。
- 本番／DB接続モードにサンプル商品・デモ会員・架空の取引・レビュー・認定バッジを持ち込まない。デモログインも無効化。
- 本番DB未設定・接続不可・保存失敗では成功応答を返さない。本番でSQLiteへの自動フォールバックは行わない。
- リクエストごとにDBの最新状態を読み、同じPostgres上の複数worker間で排他制御。保存前の成功応答、再起動による注文消失、古い状態による上書きを防止。
- 状態更新と監査イベントを同じトランザクションで保存。監査テーブルのUPDATE／DELETE／TRUNCATE拒否と既存イベント内容の変更検出。
- Stripe呼び出し前に、注文等の予約と操作ID／要求ハッシュを同時保存。結果不明は予約を保持し、別IDでの再課金を自動実行しない。成功応答は再利用し、引数の変わった同一IDは拒否。
- Stripe応答保存直後のプロセス停止から、Checkout URL・Webhook照合を回復。テスト／本番イベントの混在を拒否。同じ支払いについて別イベントIDが届いても重複通知しない。
- 追加支払いのCheckout期限切れを元の購入と分離。
- DBモードのメールは注文等と同時にoutboxへ確定し、別workerで送信。worker競合を行ロックで防ぎ、再送に同じResendキーを利用。23時間超の結果不明・10回以上失敗はreviewへ停止。
- 商品の出品日がサーバー起動日に固定されていた不具合を修正。トップの固定表示「今週38件」を廃止し、商品ゼロ件の案内を追加。
- 実Postgresを使うCI設定と回帰テストを追加。

## 検証

Python 3.14.5、PostgreSQL 17.11、psycopg 3.3.6。検証は `/tmp/toolbako-pg-20260922` のローカル専用DBで実施。テストごとに固有のDBを作成・削除し、既存DB・本番DBは変更していない。Stripeとメール配信はMockで、実資金・実メールは送信していない。

会員登録→出品→テスト用の公開条件設定→Checkout予約→購入状況表示→別repositoryからの復元を、実際のFastAPIルートと実Postgresで確認。Supabase Auth／Stripe実サービスの成功を意味しない。

全テストの最新結果はQA_REPORT.mdの追記を参照。

## この保存方式の制限（重要）

これは既存コードを安全に移行する**互換層**であり、注文ごとに正規化された最終的な金融repositoryではない。全業務状態をJSONBで保存し、動的リクエストを直列化する。外部API呼び出しの間もロックするため、高負荷時は503／待ち時間が増える。待機は5秒、競合workerは再試行可能な503。大規模運用の合格とは扱わない。

- direct接続または **session pooler** が必要。transaction poolerは不可。Supabaseの代表的なtransaction poolerポート6543は拒否するが、独自pooler設定は運用者が確認する。
- 本番DSNはTLS必須。`verify-full` が望ましく、証明書設定も接続先に合わせて確認する。
- 状態には個人情報とサーバー用認証トークンを含む。DB・バックアップ・管理接続を非公開にし、権限・暗号化・保持期間を設定する。SQLのRLSはanon/authenticatedには許可しないが、サーバー所有者権限の侵害対策まで保証しない。
- ファイル本体は別の永続ストレージが必要。DB保存だけでは画像や納品ファイルは永続化されない。
- Stripe `pending/unknown` の汎用的な自動突合・二者承認復旧、銀行振込後の回収、部分返金後の再分配、定期課金台帳は別途実装・検証が必要。単発の分配／振込／銀行振込前の取消workerはローカル検証済み。
- メールworkerの配備・監視・review対応・メール保持削除運用が必要。

## 接続後の順序

1. 空の**ステージング**用DBとSecretsを用意（本番や既存デモDBを上書きしない）。
2. `DATABASE_URL` を環境変数として安全に設定。パスワードをコマンド引数・チャット・gitへ記載しない。
3. 同じ環境で `python -m app.database migrate` → `python -m app.database check`。
4. `APP_ENV=staging`、`DEMO_MODE=false`、Stripeはtestキー。Supabase AuthのURL／鍵、リダイレクトURLを設定。
5. `python -m app.mail_worker --limit 20` をメール送信可能な環境の定期workerとして設定。現段階ではコードのみで、外部配信は未実行。
6. 実サービス接続試験、ファイル永続化、バックアップ復元、MFA／Redis障害試験。
7. `PAYOUT_OPERATIONS.md` に従いSandboxの分配・銀行振込・取消を検証。残る照合・回収処理と本番口座試験を完了してから、本番決済ガード解除を別変更として審査。

`/readyz` は今もサービス公開判定なので、未完了項目があれば503になる。Railwayの本番healthcheckを緩めて通過したことにしない。

## 外部作業の現在地

2026-09-22に既存のGitHub認証でRailway・Supabaseへログインできた。以下は画面で確認した事実。

- Railway `rare-manifestation` / `web` / `production` はOnline。ただしActiveは1か月前の `Improve homepage onboarding and signup conversion`。今回のコードは反映されていない。
- サービス環境変数は `ALLOWED_HOSTS`、`SESSION_SECRET`、`SITE_BASE_URL` の3件のみ。値は表示・取得していない。DB・Supabase・Stripe・メール等の設定は未登録。
- Supabaseプロジェクト `xnjzgfaasbemffgorvnj`（表示名 `kegkeg4@gmail.com's Project`、東京リージョン）は、ユーザーがツールバコ用と確認済み。9/22に Resume を実行し、**Restoration complete! / back online** を確認。Table Editor・Authenticationが使用可能になった。外部アプリからの接続試験はまだ行っていない。
- Table Editorのpublicスキーマには `posts`、`replies` の2テーブルが表示される。行データは読んでいない。必要な `profiles`、`creator_badges` は一覧になく、スキーマ選択肢にも `toolbako_runtime` はない。既存テーブルを消さず、アプリ用の不足スキーマを適用する必要がある。
- AuthのSite URLは `http://localhost:3000`、Redirect URLsは未登録。読み取りのみで、まだ変更していない。
- Stripeの既存タブはログイン画面。対象のツールバコSandbox／本番アカウントを認証後に再確認する必要がある。

秘密鍵の取得・登録、外部DBマイグレーション、デプロイ、本番審査／決済／送金は今回実施していない。DBの再開だけで公開可能になるわけではなく、上記の残実装と実サービス試験が必要。

既存データを初期データで上書きしない。RailwayのIPv6外向き接続は無効のため、接続にはSupabaseのSession poolerを使用する。Connect画面で確認した接続先は `aws-0-ap-northeast-1.pooler.supabase.com:5432`、database `postgres`、user `postgres.xnjzgfaasbemffgorvnj`。TLSを使用する。DBパスワードは `[YOUR-PASSWORD]` のプレースホルダーであり、取得していない。秘密鍵・DB接続情報のRailway登録は、対象サービスと権限を明示してユーザーへ確認中。鍵を公開する／本番決済を有効化する許可ではない。

Railway設定画面ではNixpacksがDeprecated、Config as Codeも2026-12-01までの既存設定サポートと表示されている。稼働版はcommit `3d3f6aebebf144f470b88d2edda81c41824f4e55`。次回配備前に公式のInfrastructure as Code／ビルド移行手順を確認する。今回、この設定は変更していない。

## 参照した公式仕様

- [PostgreSQL advisory lock](https://www.postgresql.org/docs/current/explicit-locking.html)
- [Psycopg transaction管理](https://www.psycopg.org/psycopg3/docs/basic/transactions.html)
- [Stripe idempotency](https://docs.stripe.com/api/idempotent_requests)
- [Resend idempotency（24時間）](https://resend.com/docs/dashboard/emails/idempotency-keys)
