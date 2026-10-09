# 2026-10-09：リリース準備・配備の実績

## 現在の判定

**コードの回帰試験は合格。本番決済はNO-GO。** 本記録の接続・配備確認は、有料サービス開始の承認ではない。未検証を完了扱いにするための採点やチェックの解除は行わない。

運営主体は合同会社ONE。既存のRailway `rare-manifestation` / `web`を使用し、新しい有料サービスは作成しない。会員・取引用のSupabaseはユーザー確認済みのプロジェクトを使用。秘密値はソース・記録・チャットに載せず、封印済み変数は取得しない。

## ローカルで完了

- Python 3.12.13と実ローカルPostgreSQL 17で **248 passed / 2 warnings / 30.55秒**。Stripe・Supabase Auth・メール等のHTTP APIはMock。
- 固定依存39パッケージの互換性確認、`pip-audit`の公開DB照会（既知脆弱性なし）、全差分の`git diff --check`に合格。警告2件はテストクライアントの非推奨API。未知の脆弱性がないことは保証しない。
- Sandboxの結果不明Transfer／銀行Payoutについて、元のリクエスト・金額・口座・source・metadataをGETで照合する復旧処理を追加。別管理者、MFA、30分の有効期限、二重送金拒否、監査記録・DB原子性を検証。残高は確認中も予約し、再送しない。Checkout・返金・reversalの結果不明は未対応。
- stagingにも認証・Origin検証・エラー秘密情報抑制を適用。公開判定`/readyz`とは別の`/deploymentz`と、読取専用`python -m app.deployment_check`を配備に使用する。本番StripeキーによるPOSTはコードで禁止したまま。
- デモSQLite保存が既存親ディレクトリをchmodする不具合を修正。新規専用ディレクトリ・DBは非公開権限にし、symlink／hardlinkを拒否する。
- GitHub ActionsはPython 3.12・PostgreSQL 17の回帰試験を追加。checkout／setup-pythonは検証済みcommit SHAに固定。
- ローカルのトップ（1440px／390px）、管理画面（390px）で横はみ出し・ブラウザーerrorなし。トップはh1とmain各1件、ロゴ表示を確認。実認証・実決済・全画面の目視・負荷試験ではない。

## 実Supabaseで完了

- プロジェクトをINACTIVEから復帰し、ACTIVE_HEALTHYを確認。キーの発行・変更、パスワード再設定はしていない。
- Site URL：`https://web-production-556d.up.railway.app`
- Redirect URLsは同ホストの`/auth/callback`と`/auth/recovery`の2件だけ。ワイルドカードを許可していない。保存後の画面を確認済み。
- runtime version 2、9テーブルすべてRLS有効、anon／authenticatedにruntime schema USAGEなし。anonにprofiles SELECTなし、authenticatedにprofiles／creator_badges書込なしを再確認。Authプロフィール登録トリガーは1件。
- `finance_allocations(receipt_id)`、`finance_entries(receipt_id)`、`finance_entries(payout_id)`のインデックスを追加し、実DBの定義で確認。既存データ・権限を変更せず、Performance Advisorの未索引FK警告は解消。使用前の索引をunusedという理由で削除していない。

### 同じDBに残る旧機能の注意

`public.report_post`、`public.report_reply`、`public.resolve_post`は既存の投稿・返信機能。匿名・一般ユーザーに実行権限があるSECURITY DEFINERで、特にresolveは呼出者の所有権検査なしに更新する。別サイトへの影響が不明なため、データ・関数・権限を勝手に削除／変更していない。ユーザーへ利用状況と公開権限停止の可否を確認中。

[Supabaseの匿名SECURITY DEFINER警告と対処](https://supabase.com/docs/guides/database/database-linter?lint=0028_anon_security_definer_function_executable)、[認証済みユーザーの警告と対処](https://supabase.com/docs/guides/database/database-linter?lint=0029_authenticated_security_definer_function_executable)を参照。private runtimeの「RLS有効・policyなし」は意図したdenyであり、一般ユーザーにschema USAGEもない。

実DBはPostgreSQL **17.6**。2026-09-25のSupabase公式changelogに新しいセキュリティ修正版があるため、[DBアップグレード手順](https://supabase.com/docs/guides/platform/upgrading)に沿った拡張機能・互換性確認と更新が必要。稼働データのあるDBを無断アップグレードしていない。

## GitHub・Railwayの配備状況

GitHub `kegkeg4/toolbako`への反映権限を確認。既存mainは保持し、公開用ブランチへ検証済み変更をまとめてからCI・配備を行う。

配備前の状態：旧版が稼働中。Railwayの変数10件と配備設定4件、計14件が反映待ち。新コードなしで先に反映しない。

- `APP_ENV=staging`、`DEMO_MODE=false`、HTTPS許可ホスト、MFA必須、separate charge modeを設定。
- DBはパスワードを含まないSession pooler URIと封印済みPGPASSWORD、TLSを使用。Supabase service_roleも封印済み。値は読み出さない。
- Pre-deploy：`python -m app.deployment_check`、timeout 120秒。
- Healthcheck：`/deploymentz`、timeout 120秒。
- Stripeキーは未登録。本番決済有効化はこの配備の対象外。

**GitHub公開・外部CI・Railway配備・実接続結果は、実行後にこの項目へ追記する。現時点で成功とは扱わない。**

## 有料開始までの残作業

1. 新コードの公開・CI・Railway Pre-deployによる実DB／Auth API接続確認。
2. 実Supabase会員登録、メール確認、ログイン、パスワード再設定、MFA。管理者2人の実アカウントID登録と復旧運用確認。
3. Stripe SandboxのCheckout・Webhook・JPY Connect分配・銀行Payout・返金・異議申立てE2E。本番実行ガードを外す前に証拠を保存する。
4. Checkout／返金／reversalの結果不明、銀行振込後の回収、銀行失敗後の再申請、部分返金・定期課金台帳・異議申立て終了後の復元。
5. 振込期限のStripe条件・日本での運用確認。120日の旧ルールを未確認のまま適用しない。
6. Redisの実複数worker試験、非公開ファイル永続化・ウイルス検査、メール送信worker、監視・バックアップ復元。
7. 実環境の速度・想定負荷、DB直列化によるボトルネックの対処。ローカル単体試験の速さを本番性能としない。
8. 合同会社ONEの正式な連絡先・法務レビュー、旧投稿RPCの整理、PostgreSQLのセキュリティ更新。

外部のログイン・秘密値・会社情報・契約確認はユーザーにしか確定できない。追加権限が必要な変更は、その影響を明記して確認する。詳細な運用は`PAYOUT_OPERATIONS.md`と`PRODUCTION_CHECKLIST.md`。
