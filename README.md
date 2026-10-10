# ツールバコ

個人がつくったAIツールを、個人や小さな事業者が発見・相談・購入できるFastAPI + Jinja2 + Supabase構成のSSRマーケットプレイスです。

## ローカル起動

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

`http://127.0.0.1:8000` を開いてください。Supabase未設定時はデモモードで動作し、ログイン画面からデモユーザーを利用できます。

Python 3.12を使用します。`requirements.in`／`requirements-dev.in`は直接依存の編集元、`requirements.txt`／`requirements-dev.txt`は間接依存も含むハッシュ付き固定ファイルです。生成済みファイルを手で更新せず、[uvの依存固定手順](https://docs.astral.sh/uv/pip/compile/)に沿って再生成・全テスト・脆弱性監査を行います。

```bash
uv pip compile requirements.in --python-version 3.12 --universal --generate-hashes --no-annotate -o requirements.txt
uv pip compile requirements-dev.in -c requirements.txt --python-version 3.12 --universal --generate-hashes --no-annotate -o requirements-dev.txt
```

CIもハッシュ照合を必須にして同じ固定ファイルを使用します。OS用markerは含みますが、動作保証範囲は検証済みのPython 3.12／macOSとLinuxです。

## Supabase設定

1. 現行のruntime保存方式では、新規の会員0件プロジェクトに `supabase/bootstrap_runtime_auth.sql`、続いて `python -m app.database migrate` を適用します。既存会員・プロフィールがあるDBではbootstrapを実行せず、別の移行／バックフィルをレビューしてください。旧 `supabase/schema.sql` と公開commerceテーブル向けのmigrationsを混ぜて一括実行しないでください。
2. 外部OAuthを使う場合だけ、Authentication > Providers で必要なプロバイダーを設定します。メール認証とは別の準備です。
3. Authentication > URL Configuration のSite URLを公開先へ設定し、`SITE_BASE_URL/auth/callback` と `SITE_BASE_URL/auth/recovery` を個別に許可します。ワイルドカードは使いません。
4. サーバーの非公開環境変数に Supabase URL / publishable key / secret keyを設定します。推奨変数名は`SUPABASE_PUBLISHABLE_KEY`／`SUPABASE_SECRET_KEY`、既存`SUPABASE_ANON_KEY`／`SUPABASE_SERVICE_ROLE_KEY`も互換用に使用できます。旧変数名へ新しいopaqueキーを保存した場合も対応します。新変数名に非空の値があれば優先し、勝手に旧キーを削除・無効化しません。DBはSession poolerとTLSを利用し、`DATABASE_URL` にパスワードを含めない場合は `PGPASSWORD` を別の秘密変数として設定できます。
5. 本番では `DEMO_MODE=false` とし、`SESSION_SECRET` を十分長いランダム値に変更します。
6. `/readyz` と管理画面 `/admin` は設定・実装条件の確認です。100%になっても実機決済・負荷・復元試験の代替にはなりません。

Twitter OAuthのSupabase provider名は `twitter` です。X Developer Portal側のCallback URLには Supabase Dashboardに表示されるcallback URLを指定してください。アプリ側はOAuthのアクセストークンをURL fragmentへ残さず、サーバー側PKCEで交換します。

[Supabaseの公式キー移行手順](https://supabase.com/docs/guides/getting-started/migrating-to-new-api-keys)に沿い、新しい`sb_publishable_...`／`sb_secret_...`は`apikey`ヘッダーだけに送信します。ユーザーのログインJWTは引き続き別の`Authorization`に指定し、サーバー用APIキーで置き換えません。旧キーは2026年末の非推奨化が案内されています。新旧キーの送信・設定優先順はMockで検証済みであり、実プロジェクトでの新キー発行・疎通は別途必要です。

## Railway

リポジトリをRailwayへ接続し、`.env.example` を参照して対象環境の変数を設定します。`railway.json` と `Procfile` に起動・ヘルスチェック設定を含めています。`STATE_DB_PATH` のSQLite保存はデモ用で、本番は `DATABASE_URL` によるPostgres保存を使用します。現在のPostgres実装は全状態JSONB＋排他制御の互換層で、金融台帳のみ別テーブルに正規化しています。実サービスE2E・負荷・障害復旧試験の合格までは本番決済を開始できません。`PRIVATE_STORAGE_PATH` は永続ボリューム上の絶対パスに設定します。

配備前の接続確認は、対象サーバーの秘密変数が使える環境で `python -m app.deployment_check` を実行します。DBのスキーマversion、Auth API、サーバー専用プロフィールAPI、匿名アクセス拒否を読み取りだけで確認し、不備があれば終了コード1になります。パスワードやAPIレスポンスは表示しません。**この合格は公開・課金開始の承認ではなく、`/readyz` の判定も緩めません。**

`railway.json` にPre-deploy Commandと `/deploymentz` ヘルスチェックを設定済みです。`/deploymentz` は安全な接続試験環境の起動確認であり、本番決済の開始判定ではありません。`/readyz` は引き続き全公開条件を確認し、決済未検証なら503を返します。既存の旧版へ接続設定だけを先に反映しないでください。最新の作業状況は [リリース作業記録](RELEASE_PROGRESS_2026-10-09.md) を参照してください。

## 秘密情報を送らないエラー監視

`SENTRY_DSN`設定時は`app.monitoring`で明示的に初期化します。通常ログ／Sentryには例外本文を送らず、サーバー生成の問い合わせ番号・例外種別・app内のファイル名と行番号だけを使います。[Sentryの送信前フック](https://getsentry.github.io/sentry-python/api.html)を使い、未知の将来フィールドを含め、URL、Query、Cookie、Header、ユーザー情報、DM、納品ファイル、ローカル変数、ソース行、添付、breadcrumb、scope extrasを送信イベントから除外します。自動integration・trace・profiling・ログ・metrics・session報告も停止しています。実SDKの送信envelopeを偽transportで検査し、外部サービスへはテストデータを送りません。

Railway／Procfileの起動には`--no-access-log`が必要です。標準access logはOAuth code等を含むURL全体を記録するためです。RailwayでStart Commandを上書きしている場合も同じ引数を設定してください。ホスティング側のproxyログ・実監視先の通知と保持期間は、別途実環境で確認が必要です。

## 実装済み範囲

- SSRトップ、一覧、検索、カテゴリ・価格・AIフィルタ、人気/急上昇ソート
- 詳細、Markdown、関連ツール、OGP画像生成、Xシェア、セッション単位閲覧
- OAuth導線、デモ認証、出品フォーム、いいね、通報、プロフィール・設定
- 管理画面、静的法務ページ、sitemap、robots、Railway設定
- Supabaseスキーマ、RLS、Storageポリシー、Authプロフィール作成、カウンタtrigger
- 自由価格の買い切り販売、相談価格、オプション、販売者クーポン、販売手数料計算
- 完成済みツールの即時提供と、カスタマイズ商品の取引ルーム納品
- 通常購入・カスタマイズ相談・独占譲渡の3販売方式
- 独占譲渡の本人確認、専用審査、案件ごとの双方NDA、当事者限定交渉画面
- 購入確認、デモ決済、購入履歴、販売者ダッシュボード
- 商品単位の購入前・購入後DM
- 見積もり相談、出品者からの提案、提案内容での購入
- 専用取引ルーム、正式な納品、承諾、1回の差し戻し
- キャンセルリクエスト、合意・差し戻し、取引ステータス管理
- 取引完了後の5段階評価、本人確認、確定売上・取引中売上の分離
- 売り手・買い手のblind相互評価、10日間の評価期限、取引ルーム自動終了
- 見積書、発注書、納品書、領収書の取引書類
- AIツール診断、最大3商品の比較、AIツールパスポート
- バージョン履歴、更新フォロー、カテゴリ別サイト内通知と通知設定
- クリエイター検索、受付状況、経歴・スキル・ポートフォリオ、直接相談
- 双方向ブロック、ブロック管理、受付休止／満枠商品の再開通知
- 本人確認を前提とする共通NDA電子同意とプロフィール信頼バッジ
- お気に入りフォルダ、購入前注意、FAQ、商品別の実購入口コミ
- 公開募集、応募比較、採用から取引ルームへの移行
- 商品画像のアップロード・検証・WebP最適化
- 本番設定診断、セキュリティヘッダー、Origin検証、レート・容量制限
- 監査ログ、通報解決、問い合わせ・紛争対応、データ出力・退会予約
- SQLite永続化オプション、Stripe署名Webhook、Connect販売者登録
- Stripe Checkout・返金・定期解約・Identityの接続コード
- 非公開納品ファイル、危険ZIP・偽装形式検査、ClamAV接続
- 認証アプリの登録・コード検証・重要操作時の10分間MFA確認（Supabase実機試験は未完了）
- Redis原子的カウンタと障害時のアクセス停止（複数台での実機試験は未完了）
- 単発JPYの売上・振込予約・分配・返金台帳とSandbox限定の分配／振込／取消worker
- 結果不明のSandbox分配／振込の読み取り専用照合と、別管理者2名・MFAによる復旧承認
- 自動テスト、依存関係固定（最新の検証範囲は `RELEASE_PROGRESS_2026-10-09.md` を参照）

デモモードでは実際の請求や送金は発生しません。単発購入の台帳・分配／振込worker、結果不明の分配／分配取消／銀行振込／Checkout／返金の二者照合、未要求の全額返金の明示再開をローカルPostgres＋Mock Stripeで検証しました。実Stripe接続、銀行振込失敗後の再振込・振込後の回収、月額台帳、保有期限対応は未完了です。`sk_live_` / `rk_live_` キーによるAPI書き込みはコードで停止しています。解除には設定変更ではなく、残る実装と実機検証が必要です。外部アカウントの契約、審査、鍵設定、法務情報は運営者による確認が必要です。最新の状態は `RELEASE_PROGRESS_2026-10-09.md` を参照してください。

## 本番接続時の補足

認証はSupabase Authへ接続します。`DATABASE_URL` がある場合、Postgres互換repositoryを全業務ルートで利用し、注文・Webhook処理済みID・監査ログ・メールoutboxを同一トランザクションで保存します。本番ではDB未設定時にアクセスを停止し、SQLiteへフォールバックしません。`python -m app.database migrate` で専用runtimeスキーマを作成し、`python -m app.database check` で確認します。デモデータは移行されません。

この方式は全状態JSONB＋リクエスト排他制御の互換層です。単発購入の金融台帳は別の4テーブルに正規化し、業務状態と同時に保存します。staging実接続は確認済みですが、実負荷試験・本番決済運用は未完了で、本番決済ガードは維持しています。Supabaseのdirect接続またはsession poolerを使い、transaction poolerは使わないでください。DBモードのメールは `python -m app.mail_worker --limit 20` で送信します。分配／振込workerの手順と制限は [売上・振込運用](PAYOUT_OPERATIONS.md)、保存方式・検証結果は [2026-09-22の実装記録](RELEASE_PROGRESS_2026-09-22.md) を参照してください。service role keyやprovider tokenはサーバー内だけで扱い、ログ・公開バックアップへ出さないでください。

stagingのDB／Auth実接続と新版配備は確認済みです（最新の証拠は[配備記録](RELEASE_PROGRESS_2026-10-09.md)）。DB境界は全リクエストで最新状態をロック下で読み、不変の状態・空のoutbox・検証済み金融状態の場合だけ保存を省きます。GETの期限処理・認証・監査等の変更や決済予約の保存は省略しません。[SupabaseのSession pooler指針](https://supabase.com/docs/guides/database/connecting-to-postgres)に従い、session lockに必要な接続モードを維持します。これは直列化の解消や本番負荷の合格ではありません。
