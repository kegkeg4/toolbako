# ツールバコ

個人がつくったAIツールを、個人や小さな事業者が発見・相談・購入できるFastAPI + Jinja2 + Supabase構成のSSRマーケットプレイスです。

## ローカル起動

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

`http://127.0.0.1:8000` を開いてください。Supabase未設定時はデモモードで動作し、ログイン画面からデモユーザーを利用できます。

## Supabase設定

1. Supabase SQL Editorで `supabase/schema.sql` と `supabase/migrations/` 内のSQLをファイル名順に実行します。`20260716_hybrid_sales.sql` まで適用されていることを確認します。
2. Authentication > Providers で Twitter と Google を有効化します。
3. Authentication > URL Configuration に `SITE_BASE_URL/auth/callback` を追加します。
4. `.env` に Supabase URL / anon key / service role keyを設定します。
5. 本番では `DEMO_MODE=false` とし、`SESSION_SECRET` を十分長いランダム値に変更します。
6. `/readyz` と管理画面 `/admin` の公開準備度が100になるまで実決済を有効化しません。

Twitter OAuthのSupabase provider名は `twitter` です。X Developer Portal側のCallback URLには Supabase Dashboardに表示されるcallback URLを指定してください。アプリ側はOAuthのアクセストークンをURL fragmentへ残さず、サーバー側PKCEで交換します。

## Railway

リポジトリをRailwayへ接続し、`.env.example` と同じ環境変数を設定します。`railway.json` と `Procfile` に起動・ヘルスチェック設定を含めています。現状の `STATE_DB_PATH` はデモ／ステージング用です。実決済を伴う公開では、注文・在庫・Webhook・売上を同一トランザクションで扱うPostgres repositoryへ置換してください。`PRIVATE_STORAGE_PATH` は永続ボリューム上の絶対パスに設定します。

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
- 77件の自動テスト、依存関係固定、`pip check`と`pip-audit`による整合性・既知脆弱性確認

デモモードでは実際の請求や送金は発生しません。本番モードではStripe Connect/Checkout/Identityと署名Webhookを使用する分岐を実装済みです。外部アカウントの契約、審査、鍵設定、法務情報は運営者作業です。ココナラとの機能比較は `COCONALA_PARITY_AUDIT.md`、最新の公開条件は `PRODUCTION_CHECKLIST.md`、厳格な再監査結果は `QA_REPORT.md` を参照してください。

## 本番接続時の補足

認証はSupabase Authへ接続し、デモ／単一プロセス検証では取引データをSQLiteスナップショットへ永続化できます。実カード決済を有効にする前にPostgres repositoryへ置換し、注文・Webhook・監査ログを同一トランザクションで処理してください。さらに分散レート制限、AAL2 MFA、追記専用監査ログが必要です。service role keyはサーバー内のみで使い、ブラウザへ返さないでください。
