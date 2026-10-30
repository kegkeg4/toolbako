# 2026-09-23：Supabase構築・接続準備の実績

## 判定

**Supabaseの会員・取引保存テーブルは追加・権限確認済み。Railwayの接続情報は保存済み・反映待ち。RailwayからのDB接続成功・実会員登録・本番公開・決済開始はまだ未確認／未実施。**

ユーザーが、ツールバコ用SupabaseのAPIキー・DB接続情報をRailwayの非公開設定へ登録すること、および会員・認定バッジ・取引保存用テーブルを既存データに触れず追加することを承認。本番決済の有効化は対象外。

## 今回完了した作業

- Supabaseプロジェクト `xnjzgfaasbemffgorvnj` の管理画面で Healthy を確認。
- Railway `rare-manifestation` / `web` / `production` に、既存キーを使用して以下の5変数を保存。
  - `SUPABASE_URL`
  - `SUPABASE_ANON_KEY`
  - `SUPABASE_SERVICE_ROLE_KEY`
  - `DATABASE_URL`（パスワードを含まないSession poolerの接続先）
  - `PGPASSWORD`（ユーザーがRailway画面で直接入力）
- 現行コードに合わせて既存のlegacy anon / service_roleキーを使用。新しいキーの発行・ローテーションはしていない。
- 管理用の `SUPABASE_SERVICE_ROLE_KEY` と `PGPASSWORD` はRailwayのSeal機能で再表示できない設定にした。保存後に表示・コピーの操作がなくなったことを確認。パスワード値は取得していない。
- Railway画面で **8 Service Variables / Apply 5 changes** を確認。5件は反映待ちであり、**Deployは押していない**。稼働中のアプリにはまだ反映されていない。
- キーの値はこの記録・チャット・ソースコードに記載していない。

## 実DBへの追加と権限確認

追加前にSupabaseのSQL Editorで、テーブル存在・会員数・トリガー数のみを確認。個々のユーザーや投稿内容は読んでいない。

- `public.profiles`：なし
- `public.creator_badges`：なし
- `toolbako_runtime`：なし
- `auth.users`：あり、会員数0
- `auth.users` のユーザー定義トリガー：0件
- publicの既存テーブル：`posts`、`replies`。変更していない。

`supabase/bootstrap_runtime_auth.sql` を新規作成。旧来の全公開テーブル方式ではなく、現行runtime方式で必要なプロフィール・認定バッジだけを作成する。一般ユーザーは自分の非停止プロフィールとバッジを読めるだけで、ブラウザーからの更新・権限昇格は不可。サーバー専用service_roleで更新する。Auth登録時には長さ制限・一意制約付きでプロフィールを自動生成し、信用・本人確認状態をユーザーメタデータから採用しない。

SQLはトランザクションで実行し、既存プロフィール・既存会員・既存Authトリガーがあれば中断。既存データの自動バックフィルや上書きはしない。再実行用マイグレーションではなく、新規構築専用。**承認後に実Supabaseへ適用し、成功を確認した。**

続いて `app/runtime_schema.sql` version 2を、トランザクション・advisory lock・既存runtimeスキーマがある場合の中断ガード付きで適用した。適用前の確認で管理テーブル `schema_version` にもRLSを追加し、ローカルで再検証した。注文などのデモデータは投入していない。

追加後のメタデータ照会で以下を確認した。

- `public.profiles` と `public.creator_badges`：作成済み、両方RLS有効。
- Auth登録時のプロフィール作成トリガー：1件。
- `toolbako_runtime`：version 2、9テーブル、全テーブルRLS有効。
- `anon` / `authenticated`：runtimeスキーマへのUSAGEなし。
- `anon`：profilesへのSELECT権限なし。
- `authenticated`：profiles / creator_badgesへのINSERT・UPDATE・DELETE権限なし。
- 既存 `posts` / `replies`：存在を確認。テーブル・内容とも変更していない。

runtimeの9テーブルは `schema_version`、`marketplace_state`、`audit_events`、`stripe_operations`、`email_outbox`、`finance_receipts`、`finance_payouts`、`finance_allocations`、`finance_entries`。SQL Editorでの適用・メタデータ確認であり、Railwayからの接続や実Auth会員登録の成功とは別である。

6件の追加テストが実ローカルPostgresで合格（6 passed / 30 deselected / 2 warnings）。既存テーブル保持、既存会員がいる場合の拒否、長いメタデータ、ユーザー名の重複、RLS、ブラウザーからの不正更新拒否、サーバー更新を検証。ローカル用のAuthテーブル・ロールであり、実Supabase AuthやRailwayからの接続試験ではない。

## 配備前チェックと回帰テスト

読み取り専用コマンド `python -m app.deployment_check` と17件のテストを追加した。環境・HTTPS・許可ホスト・秘密変数の有無を確認後、TLS付きDBのスキーマversion、Auth設定API、service_roleでのプロフィール／バッジAPI、匿名アクセス拒否を検査する。HEADリクエストで列・関連・権限を確認し、プロフィール本文は取得しない。DBへの書き込み、会員作成、決済、マイグレーションは行わない。秘密値・例外本文・APIレスポンスをログへ出さず、不備があれば終了コード1とする。

このコマンドはまだRailway上で実行・Pre-deploy Command登録していない。接続チェック合格は公開承認ではなく、`/readyz` と本番決済ガードは維持している。

全回帰テストは **223 passed / 2 warnings / 13.69s**（Python 3.14.5、実ローカルPostgreSQL 17.11）で合格。接続チェックのHTTP API、Stripe・メール等の外部呼び出しはMock。警告はStarlette/HTTPX・Anyioの非推奨APIで、実サービス接続成功や脆弱性監査済みを意味しない。

隔離した検証環境の `pip check` は依存関係の不整合なし。今回編集したREADME・QAレポート・環境変数例・gitignoreの `git diff --check` は合格。作業ツリー全体の差分検査は応答が返らず中断したため、全ファイルの合格とは扱わない。テスト専用ローカルPostgresは検証後に停止した。ユーザーの開発サーバーには触れていない。

既存 `.venv-release` の依存読み込みが進まなかったため、そのテストは中断。`/tmp/toolbako-release-qa-20260923` にrequirements-dev.txtから検証環境を作成した。既存仮想環境は変更していない。

## 未完了・次の操作

1. **DB接続試験**：保存済みパスワードの正否は未検証。RailwayからSession poolerへの実接続を試験する必要がある。設定値が保存されただけで接続成功とは扱わない。
2. **認証URL**：9/23にもSite URLが `http://localhost:3000`、Redirect URLsが未登録であることを確認。現在のRailway URLをSite URLに設定し、`/auth/callback` と `/auth/recovery` だけ許可する変更についてユーザー確認待ち。まだ設定は変更していない。
3. **配備前接続チェック**：新しいコマンドを含むコードがRailwayの配備対象になった後、同環境で実行する。封印済みの秘密値をローカルへ持ち出さない。
4. **新しいコードの配備**：今回のローカル実装はGitHubへpush・Railwayへdeployしていない。旧版が稼働中のため、不完全な設定を先に反映しない。
5. **実サービス試験**：会員登録・認証・出品・ファイル永続化・Sandbox決済と売上分配・メール・障害復旧を実環境で確認する必要がある。ローカルのMockテスト合格は代わりにならない。

DBは確認済みのSession pooler（`aws-0-ap-northeast-1.pooler.supabase.com:5432`、DB `postgres`、ユーザー `postgres.xnjzgfaasbemffgorvnj`）とTLS `sslmode=require` を使用する。transaction poolerは使用しない。libpqはpasswordを含まないDATABASE_URLをPGPASSWORDで補完する。パスワードのURIエンコードは不要。両設定をセットで管理し、パスワードをチャット・git・コマンド履歴へ書かない。

Stripeの設定変更・本番キー登録・課金・送金・パスワード再設定は実施していない。スキーマのみ実Supabaseへ適用済み。アプリの変更・テスト・手順書はGitHubへpushしていない。

## 参照

- [Railway Variables](https://docs.railway.com/variables)：変数の変更はstaged changeとして保存し、Deployで反映。Sealした値はUI/APIから取得できない。
- [Supabase API keys](https://supabase.com/docs/guides/getting-started/api-keys)：service_roleはサーバー専用の秘密キーとして扱う。
- [Supabase User Management](https://supabase.com/docs/guides/auth/managing-user-data)：プロフィールテーブルと登録トリガー。トリガー失敗は会員登録を阻害するため事前検証が必要。
- [PostgreSQL 接続パラメータの環境変数](https://www.postgresql.org/docs/current/libpq-envars.html)：PGPASSWORDによる補完。環境変数が他プロセスから見えるOSもあるため、Railwayの権限を制限し、ログや公開設定へ出力しない。
- [Railway Pre-deploy Command](https://docs.railway.com/deployments/pre-deploy-command)：配備前に環境変数・プライベートネットワークを使えるコマンドを実行し、失敗時はデプロイを停止する。
- 前日の実装・検証と残課題は [RELEASE_PROGRESS_2026-09-22.md](RELEASE_PROGRESS_2026-09-22.md) を参照。
