# 2026-10-09〜10-10：リリース準備・配備の実績

## 現在の判定

**コードの回帰試験は合格。本番決済はNO-GO。** 本記録の接続・配備確認は、有料サービス開始の承認ではない。未検証を完了扱いにするための採点やチェックの解除は行わない。

運営主体は合同会社ONE。既存のRailway `rare-manifestation` / `web`を使用し、新しい有料サービスは作成しない。会員・取引用のSupabaseはユーザー確認済みのプロジェクトを使用。秘密値はソース・記録・チャットに載せず、封印済み変数は取得しない。

## 10/10：サーバー用キー入力後の実接続（最新）

- ユーザーの入力完了後、反映待ちは既存webのコードcommit、`--no-access-log`、封印済み`SUPABASE_SERVICE_ROLE_KEY`の3件だけで、削除・別サービス変更なしと確認して配備した。キー値は取得していない。
- deployment `67ecbb98-12d7-4164-bc01-9493eb31c9a0`はビルド後、Pre-deployで停止。実際のbuildはbranch最新`90c8528e2d4785b4c1712502635712261949462c`（コードは検証済みa92c9a74と同一、ドキュメントだけ追加）だった。[同commitの外部CI](https://github.com/kegkeg4/toolbako/actions/runs/37957421402)もsuccessを確認。
- 非秘密診断で`supabase_public_key_present`／`supabase_server_key_present`／`supabase_key_roles`はtrue。**Auth API、profiles API、creator_badges API、匿名profilesアクセス拒否はすべて実接続で合格**。サーバー用キーの受け渡し不備は解消。
- `database_schema`だけfalse。Supabase MCPの読取専用SQLではruntime version 2、marketplace_stateテーブルの存在を再確認した。再マイグレーション・権限変更・DBパスワード変更はしていない。RailwayのDBパスワード・接続先・TLS・ネットワークを切り分ける必要がある。現時点でパスワード不正と断定しない。
- Pre-deployへDB構成のboolean-only診断を追加。URIの解析、TLS、transaction poolerのポート、URI／PGPASSWORDの値の存在、対象Supabase一致だけを出す。DSN、ホスト、ユーザー、パスワード、値の長さ・prefix・例外本文は出さない。追加観察は公開許可や接続成功の代用にせず、既存fail-closed判定を維持。
- 診断・既存配備確認の単体試験 **48 passed / 2 warnings / 0.89秒**。全回帰試験はPython 3.12.13・専用ローカル実PostgreSQL 17で **294 passed / 2 warnings / 15.18秒**、DB試験のskipなし。外部Auth・Stripe・メールはMock。初回のDB用環境変数誤指定では60件skipとなったため、その結果はDB合格の証拠にせず、正しい`TEST_POSTGRES_ADMIN_DSN`で全件再実行した。
- 新版はまだ稼働していない。旧deploymentが稼働中で、実会員登録・決済試験・その他の有料公開条件は未完了。本番Stripeの書込禁止は維持。

以下の「service_role再入力待ち」「実API接続未確認」はこの更新前の履歴。

## ローカルで完了

### 10/10の追加仕上げ

- 同日の最終追加としてSupabase新旧APIキーに互換対応し、最新ローカル回帰試験は **282 passed / 2 warnings / 15.51秒**。14件を追加し、設定の優先順、公開／サーバーキー取り違えの通信前拒否、opaqueキーのBearer誤送信防止、ユーザーJWT分離、読取専用preflight、認定バッジ保存ヘッダーをMockで検証。以下の268件はこの修正前の履歴。
- Supabaseスキルに従って公式changelogと[APIキー移行案内](https://supabase.com/docs/guides/getting-started/migrating-to-new-api-keys)を再確認。旧キーは2026年末の非推奨化が案内されているため、`SUPABASE_PUBLISHABLE_KEY`／`SUPABASE_SECRET_KEY`を優先し、既存変数名の互換性も保持。既存`SUPABASE_SERVICE_ROLE_KEY`へ新しい`sb_secret_...`を保存する場合も対応する。キーの取得・新規発行・削除・失効・旧キー停止はしていない。実キーを使った疎通は未確認。
- preflightの個別存在フラグは新旧共通の`supabase_public_key_present`／`supabase_server_key_present`にした。値やprefixをログへ出さず、構成チェックを緩めない。2026-10-30の[Data API明示grant変更](https://supabase.com/changelog/45329-breaking-change-tables-not-exposed-to-data-and-graphql-api-automatically)も確認し、現行bootstrapがprofile／badgeのservice_role権限を明示grantしていることをソースで再確認。既存DBの権限は変更しない。
- 最新版を空のPython 3.12.13環境へハッシュ照合付きでインストールし、実ローカルPostgreSQL 17で **268 passed / 2 warnings / 14.48秒**。39パッケージの互換性確認も合格。外部Auth・Stripe・メールはMock。
- `requirements.in`／`requirements-dev.in`を編集元とし、間接依存を含むrequirementsをバージョン・SHA256で固定。[uvの公式手順](https://docs.astral.sh/uv/pip/compile/)を使用し、CIも`--require-hashes`と`pip check`を追加。macOS以外のOS markerも含むが、Python 3.12のmacOS／Linux以外は未検証。
- `pip-audit`で生成済みlockを公開脆弱性DBへ照会し、既知脆弱性なし。未知の問題がない保証ではない。
- [Sentryの公式送信前フック](https://getsentry.github.io/sentry-python/api.html)を利用し、例外種別・app内の発生箇所・サーバー生成UUIDだけを送る。URL、Query、Cookie、Header、ユーザー情報、例外本文、変数、ソース行、scope、breadcrumb、添付等は保持しない。自動integration／trace／profiling／log／metrics／sessionも停止。偽transportで実SDKの送信envelopeを検査する15件を追加し、秘密情報が含まれないことを確認。実監視先との通信・通知は未確認。
- 任意の文字を含められるクライアント指定の問い合わせ番号を使わず、UUIDをサーバーで生成。監視先障害でも安全な500を維持する。
- Uvicorn標準access logはOAuth code等を含むURL全体を記録するため、Railway／Procfileへ`--no-access-log`を追加。Railwayの上書きStart Commandとホスティング側proxyログの実確認は未完了。
- 依存編集元／lock／CIの一致を維持する3件のテストを追加。公開判定は緩めず、本番Stripe POST禁止は維持。

10/10の開始時にGitHubの前回最新commit `56a535c4e4603b189a689c104d7339b7a227ddac`のCI successを確認。Railwayには新しい配備・反映待ち変更がなく、service_roleキー不足による停止は継続していた。以下の249件等は10/9時点の履歴であり、上記268件が現在のローカル証拠。

10/10の追加変更は既存PR #1へ反映。GitHubのコードcommitは`9b8e786b72594164e75b6c088e7971ffb0b866e4`、全tree `6e08e35e4ed6c74e03e960b0f3d9989a72c27ea1`はローカルと一致。[外部CI](https://github.com/kegkeg4/toolbako/actions/runs/37955305762)ではLinux・Python 3.12・実PostgreSQL 17で **268 passed / 2 warnings / 14.46秒**、ハッシュ付きインストールと`pip check`もsuccess。Auth・Stripe・メール等の外部APIはMock。

RailwayはStart Commandが管理画面側で上書きされていたため、コードcommit更新と`--no-access-log`追加の2件を反映待ちにした。patch `59114231-c90d-4d1d-81ee-f3c40c021703`はSTAGED、対象は既存webのみ、destructive=false、変数変更・データ削除・新規有料リソースなし。**accept-deployは実行しておらず、旧版が稼働したまま**。既存service_roleの再入力後、パッチに想定外の変更がないことを確認し、配備と実接続試験を再開する。実Stripeを有効にするパッチではない。

今回起動したローカル試験用PostgreSQLは試験後に停止した。ユーザーの8000番開発サーバーは操作していない。

Supabaseキー互換修正のコードcommitは`a92c9a74e4acc53cfbed7791f0eabfb79299d0d0`、全tree `a0d387f21cdcd7ebb61e49711a920fd1cb720c57`はローカルと一致。[最新のLinux外部CI](https://github.com/kegkeg4/toolbako/actions/runs/37956850480)は **282 passed / 2 warnings / 11.79秒、success**。上記パッチのcommitShaをこの版へ更新。APIのsource再設定により表示件数は5フィールドとなったが、有効な値変更はcommitShaとStart Commandの2件で、他3件は同一branch／repoと空imageの再設定。既存webだけ、destructive=false、変数変更なし。キー未入力でaccept-deployは実行していない。サーバー用キーは、既存sealed `SUPABASE_SERVICE_ROLE_KEY`へ旧service_roleまたは新しいsecret keyを直接再入力できる。新変数名を使う場合は`SUPABASE_SECRET_KEY`も対応するが、サーバーの秘密変数として保護すること。チャット・公開コードへ貼らない。

- Python 3.12.13と実ローカルPostgreSQL 17で **249 passed / 2 warnings / 15.37秒**。Stripe・Supabase Auth・メール等のHTTP APIはMock。ログ抑制修正前の版も248件合格。
- 直接依存をバージョン固定した検証環境の39パッケージの互換性確認、`pip-audit`の公開DB照会（既知脆弱性なし）、全差分の`git diff --check`に合格。推移的依存の完全なlockfileはまだない。警告2件はテストクライアントの非推奨API。未知の脆弱性がないことは保証しない。
- Sandboxの結果不明Transfer／銀行Payoutについて、元のリクエスト・金額・口座・source・metadataをGETで照合する復旧処理を追加。別管理者、MFA、30分の有効期限、二重送金拒否、監査記録・DB原子性を検証。残高は確認中も予約し、再送しない。Checkout・返金・reversalの結果不明は未対応。
- stagingにも認証・Origin検証・エラー秘密情報抑制を適用。公開判定`/readyz`とは別の`/deploymentz`と、読取専用`python -m app.deployment_check`を配備に使用する。本番StripeキーによるPOSTはコードで禁止したまま。
- 予期しない例外の本文・tracebackが通常サーバーログに出る問題を修正。staging／productionは問い合わせ番号と例外種別だけ記録する。外部監視SDKへの例外送信の詳細なscrub確認は別途必要。
- デモSQLite保存が既存親ディレクトリをchmodする不具合を修正。新規専用ディレクトリ・DBは非公開権限にし、symlink／hardlinkを拒否する。
- GitHub ActionsはPython 3.12・PostgreSQL 17の回帰試験を追加。checkout v7.0.1／setup-python v7.0.0は公式のtag・action.ymlでNode 24を確認し、commit SHAに固定。
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

### GitHub公開と外部CIの結果

- 公開用ブランチ：`codex/release-20261009`。既存mainは変更していない。
- [変更PR #1](https://github.com/kegkeg4/toolbako/pull/1)を作成し、このCodexタスクに添付。
- GitHub commit：`842db278fd2d43276fae3038c0fd2e661f516ac1`。
- GitHubとローカルのtree SHAはともに`42e1ce794dace91cc51ab60a40b70246ed6618f1`。59ファイルの変更を含む全treeが一致。
- [GitHub Actionsの回帰試験](https://github.com/kegkeg4/toolbako/actions/runs/37945152540)は **success、248 passed / 2 warnings / 11.38秒**。Ubuntu・Python 3.12・実PostgreSQL 17。外部Auth・Stripe・メールはMock。
- 初回CIのcheckout v4／setup-python v5にNode 20の非推奨警告が出たため、[公式checkout v7.0.1](https://github.com/actions/checkout/releases/tag/v7.0.1)と[公式setup-python v7.0.0](https://github.com/actions/setup-python/releases/tag/v7.0.0)のNode 24対応を確認して更新。更新版の[外部CI](https://github.com/kegkeg4/toolbako/actions/runs/37946293580)は249件合格、14.22秒。Node 20警告は解消。
- キー値を出さず、公開キー／service_roleの存在を個別に表示する診断を追加。配備チェック単体22件合格。追加後の[外部CI](https://github.com/kegkeg4/toolbako/actions/runs/37949695876)は **250 passed / 2 warnings / 10.65秒、success**。
- 最新のコードcommitは`fc4ce45b319270042737d593ef383c61f01df2a7`、treeは`f231ff417885c67943438ca24e52b10a1b054c19`でローカルと一致。

### Railway反映結果の確認中

配備元を上記commitへ固定する変更を追加し、合計18件の反映待ち変更を再確認した。対象は既存webサービスだけ、データ削除なし、APP_ENVはstaging、Stripe本番書込は停止のまま。

初回CIの成功後に配備操作を実行したが、応答中に通信エラーが発生。復旧後の読み取りで、**18件はSTAGEDのまま・新しいdeploymentなし**を確認し、未反映であることが分かった。ログ抑制・CI更新版を追加検証し、その新しいcommitへ配備元を更新してから反映する。Pre-deploy／DB接続・新アプリのHTTP成功は、まだ確認できていない。

10/10 00:04 JSTに18件の設定反映・配備が実行された。deployment `e59cc0cf-fbf5-4692-b3d6-0ca9af4129c1`は**ビルド成功後、PRE_DEPLOY_COMMANDで停止**。Railwayの配備ログ画面で`supabase_keys_present: false`を確認。設定名が存在することと、実行時に非空の値が渡ることは別だった。キー検査より後のDB／Auth試験は実行されていないため、DBパスワードの不正とは断定しない。

Supabaseから既存の有効なlegacy anon公開キーだけを確認し、Railwayの同名変数へ再保存した。キーの新規発行・無効化、sealed service_role／PGPASSWORDの変更はしていない。[Railway公式のsealed variable仕様](https://docs.railway.com/variables#sealed-variables)では配備時に値が提供されるが、UI／APIからは取得できない。値を読み取るための回避は行わない。

個別の存在診断を含む最新commitと公開キー再保存の2件を反映した。deployment `bebfdff5-7dd1-4c9b-971a-e75791d24c2d`もPre-deployで停止。構造化ログの非秘密チェックは以下だった。

- `supabase_anon_key_present: true`：既存公開キーの受け渡しは解消。
- `supabase_service_role_key_present: false`：サーバー用キーが実行時に空。
- server environment、demo disabled、HTTPS、許可host、session secret、Supabase endpoint、DB設定、live keyなし：true。
- 構成不備があるため、DB接続・Auth APIへの通信は実行していない。DBパスワードの正否は未確認。

Railway APIではservice_roleの変数名とSeal状態は存在する。**名前があるだけで値の注入成功とは扱わない。** 封印済み値を読む／再作成する操作はしていない。ユーザーがRailwayの既存`SUPABASE_SERVICE_ROLE_KEY`の「編集」から、対象Supabaseの既存service_roleキーを直接再入力する必要がある。チャットへキーを貼らず、削除・新規発行・ローテーションは不要。保存後に再配備・読取専用チェックを再実行する。

最新配備失敗後も旧版の成功deploymentが残るため、新版公開成功とは扱わない。Railway管理画面のブラウザー認証は作業途中で期限切れになり、再ログインが必要。Railwayのbuild runtimeはPython 3.12.15、ローカルは3.12.13で同じ3.12系だがpatch versionは異なる。

## 有料開始までの残作業

1. ユーザーによる既存sealed service_role値の再入力と、再配備・Pre-deployによる実DB／Auth API接続確認。GitHub公開・CIは完了。DBパスワードは未検証。
2. 実Supabase会員登録、メール確認、ログイン、パスワード再設定、MFA。管理者2人の実アカウントID登録と復旧運用確認。
3. Stripe SandboxのCheckout・Webhook・JPY Connect分配・銀行Payout・返金・異議申立てE2E。本番実行ガードを外す前に証拠を保存する。
4. Checkout／返金／reversalの結果不明、銀行振込後の回収、銀行失敗後の再申請、部分返金・定期課金台帳・異議申立て終了後の復元。
5. 振込期限のStripe条件・日本での運用確認。120日の旧ルールを未確認のまま適用しない。
6. Redisの実複数worker試験、非公開ファイル永続化・ウイルス検査、メール送信worker、監視・バックアップ復元。
7. 実環境の速度・想定負荷、DB直列化によるボトルネックの対処。ローカル単体試験の速さを本番性能としない。
8. 合同会社ONEの正式な連絡先・法務レビュー、旧投稿RPCの整理、PostgreSQLのセキュリティ更新。

外部のログイン・秘密値・会社情報・契約確認はユーザーにしか確定できない。追加権限が必要な変更は、その影響を明記して確認する。詳細な運用は`PAYOUT_OPERATIONS.md`と`PRODUCTION_CHECKLIST.md`。
