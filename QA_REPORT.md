# ツールバコ 厳格QAレポート

## 2026-10-10 購入・返金の結果不明を二者照合で復旧・配備成功（最新）

- 単発Checkout・追加支払い・全額Refundについて、GETだけの二者照合を実装。元要求hash・注文／追加明細・JPY全額・環境・戻り先・PaymentIntent／Chargeの支払済み証拠を確認。0件・複数件・不完全な一覧・不一致・未入金では保留し、再POSTしない。
- 別の管理者2名、両者の直近MFA、30分期限、承認時再取得、live／本番の拒否を検証。未入金Checkoutに利用権を出さず、未成功Refundを完了と表示しない。他の不明明細があれば振込は保留。
- Webhookと復旧の状態更新を共通化。journal・業務状態・監査・通知outboxの原子的rollback、後着Webhookによる二重件数・通知の防止、信頼できない購入URL拒否、会員連絡先の維持、提案からの秘密値・payer情報除外を検証。
- 77件追加。専用実ローカルPostgres 17・Python 3.12.13で **450 passed / 2 warnings / 31.34秒**、DB skipなし、Stripe／メールはMock。最終案内文の「再購入せず」が欠ける回帰1件を検出・修正後、全450件を再実行した。[Linux外部CI](https://github.com/kegkeg4/toolbako/actions/runs/38033999477)も **450 passed / 2 warnings / 16.51秒、success**、依存ハッシュ・`pip check`合格。全tree一致を確認した同コードの配備はSUCCESS、DB／Authの読取専用Pre-deployは全true。新しいサービス・秘密変数・スキーマは変更していない。
- 匿名15リクエストで、トップ／一覧／登録／ログイン／health／deployment200、管理GET・復旧POST403、会員／販売者／メッセージ／出品303、demo／schedules404。未設定SNSなし、live_payments_enabled=false、`/readyz`は503／14/24を維持。実登録・メール・Stripe操作はしていない。ローカル試験Postgresは停止済み。
- 実Stripe Sandbox、分配取消の結果不明、未要求の残りの返金の再開、失敗後の再振込、振込後回収、月額台帳等は未完了。本番決済NO-GO継続。

## 2026-10-10 不要な閲覧時DB書込の削減・配備成功（履歴）

- 最新状態の読込・DBロックを維持したまま、不変な閲覧でのUPDATE／COMMITを省略。GETの期限処理・セッション・監査・通知・注文の変更、outboxだけの追加、worker初回／他workerの金融変更は省略しない。決済予約の原子性と失敗時rollbackも維持。
- schema／stateの読込を1回にまとめるが、ロックSQLとは分離。異なるworkerのset配列順、未来／欠落schema、監査改ざん、クロスworker状態更新も追加検証。
- 18件追加し、専用実ローカルPostgreSQL 17・Python 3.12.13で **373 passed / 2 warnings / 25.72秒**、DB skipなし、外部APIはMock。[Linux外部CI](https://github.com/kegkeg4/toolbako/actions/runs/38025584059)も **373 passed / 2 warnings / 16.37秒、success**、ハッシュ照合と`pip check`合格。公開treeはローカルと一致。
- 既存webのcode commit変更だけを反映し、配備SUCCESS、DB／Authの読取専用Pre-deployは全true。逐次匿名HTTPの変更前6件は1499〜1807ms、変更後6件は1015〜2112ms（すべて200）、中央値 **1562→1169ms**。初回は遅く、通信・コールド接続・他アクセス等を含む少数サンプル。CWV・p95・高負荷の合格ではない。
- `/healthz`・`/deploymentz`200／live_payments_enabled=false、`/readyz`503／14/24、未ログイン管理403・会員／販売者／メッセージ／出品303、登録200・未設定SNSなし、demo／schedules404を再確認。実登録・メール送信・実決済は未検証のまま。試験用ローカルPostgreSQLは停止済み。
- Chrome DevTools MCPが未接続のため、web-perf指針によるCWV監査は保留。HTTP値をCWVや高負荷の合格証拠にしない。本番決済NO-GOは継続。

## 2026-10-10 DB実接続・新版配備成功（履歴）

- Railwayの新版はSUCCESS。Pre-deployでPostgres runtime schema・Auth／profiles／badge API・匿名profiles拒否はすべて合格。`/healthz`・`/deploymentz`は200、`/readyz`は意図した503／ready=false／ **24項目中14項目**。本番Stripe書込停止とstagingを維持。
- 公開URLの27ルートで予期しない5xxなし。未ログイン管理画面403、会員／販売者／メッセージ／出品303、デモログインと`/schedules`404。不正登録422、異なるOrigin403、長すぎるログイン401を確認。実会員作成やメール送信はしていない。
- PC1440px／スマートフォン390pxのトップ、390pxの登録画面で横はみ出しなし。ロゴ読込とモバイルメニューの登録導線、ブラウザーerrorなしを確認。全画面・負荷の合格ではない。3並列HTTP確認で約3〜4秒の応答を観測し、速度の追加改善が必要。
- 未設定のGoogle／Xが登録・ログイン画面に表示され、実Supabaseが400を返す不具合を発見。`OAUTH_PROVIDERS`の明示allowlist、未設定ボタン非表示／外部redirect拒否、既存PKCE／safe next維持、実DEMO_MODEのみのデモ案内へ修正。外部SNSログインはまだ未設定・未検証。
- 20件を追加し、専用実ローカルPostgreSQL 17・Python 3.12.13で **355 passed / 2 warnings / 20.70秒**。DB skipなし、外部Auth／Stripe／メールはMock。最初のsandboxによるローカルTCP拒否は環境setup errorで、合格に数えず全件再実行。
- [Linux外部CI](https://github.com/kegkeg4/toolbako/actions/runs/37967040779)も **355 passed / 2 warnings / 15.00秒、success**、公開treeはローカルと一致。修正版deploymentはSUCCESS、実DB／AuthのPre-deployも合格維持。実ログイン／登録画面200、未設定SNSボタン・デモログイン案内なし、未設定SNS直接URL503／外部redirectなしを再確認。
- **本番決済NO-GO継続**。会員登録・確認メール・ログイン・MFA、Stripe Sandbox、メール／ファイル／運営／法務等は残る。詳細は`RELEASE_PROGRESS_2026-10-09.md`。

## 2026-10-10 DBパスワード入力後の検証（履歴）

- Railway実行時の`PGPASSWORD`値の存在はtrueに改善。ただし実DBプローブはfalseでPre-deploy停止、新版は未稼働。API接続と匿名profiles拒否は引き続き合格。パスワードの値は取得していない。
- Supabaseの実Connect画面でSession poolerの正式な接続先を照合。接続先変更・パスワードreset・キー再発行・スキーマ／権限変更なし。
- 接続／スキーマ失敗へ固定原因コードだけを追加する41件を検証。プロバイダー例外本文・接続先・資格情報はログへ出さず、公開判定を緩めない。
- Python 3.12.13・専用実ローカルPostgreSQL 17で **335 passed / 2 warnings / 22.41秒**、DB試験skipなし。外部Auth・Stripe・メールはMock。[Linux外部CI](https://github.com/kegkeg4/toolbako/actions/runs/37962678920)も **335 passed / 2 warnings / 14.59秒、success**。公開treeはローカルと一致。
- 診断版の実Pre-deployで固定原因コード **`authentication_failed`** を確認。パスワード未注入は解消したが、現在の値ではDB認証に失敗。正しい既存DBパスワードの本人による直接再入力が必要。不明なら既存接続への影響を確認したうえで本人がresetする。値の取得・無断resetはしない。
- **本番決済NO-GO継続**。実Auth登録・決済・その他の公開条件も未完了。以下の未注入／入力待ちは前段階の履歴。最新の実配備結果は`RELEASE_PROGRESS_2026-10-09.md`。

## 2026-10-10 サーバー用キー入力後の実接続（履歴）

- Railway Pre-deployでサーバー用キー注入、Auth API、profiles／creator_badges API、匿名profilesアクセス拒否が実接続で合格。DB接続／スキーマ確認のみfalseで、新版はまだ稼働していない。MCPの読取専用SQLでは必要なruntime version 2とmarketplace_stateの存在を確認。
- DB構成の非秘密boolean-only診断を追加し、DSN・パスワード・例外本文を一切出さずに受け渡しと接続方式を切り分ける12件を追加。既存の公開判定を緩めない。
- 全回帰試験はPython 3.12.13と専用実ローカルPostgreSQL 17で **294 passed / 2 warnings / 15.18秒**。DB試験skipなし。外部Auth・Stripe・メールはMock。実会員登録・実決済の合格ではない。
- 診断コードの[Linux外部CI](https://github.com/kegkeg4/toolbako/actions/runs/37960210124)も **294 passed / 2 warnings / 14.19秒、success**。診断版の実Pre-deployでURI解析・TLS・対象プロジェクト一致がtrue、transaction pooler不使用、URI内パスワードと`PGPASSWORD`の実行時存在がfalseと判明。API接続は合格のまま。
- **本番決済NO-GOを継続**。既存sealed `PGPASSWORD`のユーザー入力待ち。キーの再発行・DBパスワード再設定・スキーマ変更は行っていない。詳細な履歴と残課題は`RELEASE_PROGRESS_2026-10-09.md`。

## 2026-10-10 依存固定・監視情報保護の再検証（履歴）

同日の追加：Supabase新旧APIキー互換対応後の最新ローカル証拠は **282 passed / 2 warnings / 15.51秒**。空の環境から構成した同じPython 3.12.13・実PostgreSQL 17を使用。opaque APIキーを誤ってBearer JWTとして送らないこと、ユーザーJWTの分離、新変数名の優先順、キー種類の取り違えを通信前に拒否する14件を追加。キー自体の発行・失効・実プロジェクトでの新キー疎通は行っていない。下記268件はこの追加前の確認済み証拠。

最新コードの[Linux外部CI](https://github.com/kegkeg4/toolbako/actions/runs/37956850480)も **282 passed / 2 warnings / 11.79秒、success**。ハッシュ付きインストール・`pip check`・実PostgreSQL 17を使用し、外部Auth／Stripe／メール等はMock。

- 空のPython 3.12.13環境へ、間接依存も含むハッシュ付き固定requirementsから39パッケージを新規インストール。実ローカルPostgreSQL 17で **268 passed / 2 warnings / 14.48秒**。Auth・Stripe・メールはMockで、実サービスE2Eではない。
- `uv pip check`で39パッケージの互換性確認に合格。ハッシュ付きrequirementsへの`pip-audit --disable-pip --no-deps --strict`は公開DB上の既知脆弱性なし。未知の脆弱性・未試験プラットフォームの動作を保証しない。
- 外部Sentryへ送るイベントをallowlistから再構築。例外本文、URL、Query、Cookie、Header、ユーザー情報、scope extras、breadcrumb、変数、ソース行、添付を除外し、自動integration／trace／profiling／log／metrics／sessionを停止。実SDKのシリアライズ済みenvelopeを含む15件を追加。実Sentryへの通信・通知は未試験。
- 呼出者指定の問い合わせ番号を廃止し、サーバーのUUIDを使用。監視障害が安全な500応答を壊さないことと、設定エラーがDSNをログへ出さないことを検証。
- OAuth codeなどのURL情報が標準access logに残らないようRailway／Procfileへ`--no-access-log`を追加。配備先の上書きStart Commandとproxyログは実環境での反映・確認が必要。
- 直接依存とlockの一致、全パッケージのバージョン／ハッシュ、本番とテストlockの一致、CIハッシュ必須設定の3件を追加。[Linuxの外部CI](https://github.com/kegkeg4/toolbako/actions/runs/37955305762)もハッシュ付きインストール・`pip check`・実PostgreSQL 17で **268 passed / 2 warnings / 14.46秒、success**。
- **公開NO-GOを継続**。Railwayへ検証済みcommit `a92c9a74e4acc53cfbed7791f0eabfb79299d0d0`と安全なStart CommandをSTAGEDで準備。パッチは5フィールドだが、有効な値変更はcommitSha／Start Commandの2件、他は同じrepo／branchと空imageの再設定。未反映で、再配備はしていない。前回配備の実行時service_roleキーは空で、ユーザーの再入力・実DB／Auth試験を待つ。キー以外の金融復旧・実決済等の残課題も変更なし。

## 2026-10-09 リリース準備の再検証（履歴）

- Python 3.12系、実ローカルPostgreSQL 17で全回帰テスト **249 passed / 2 warnings / 15.37秒**。ローカルは3.12.13、Railway buildは3.12.15。Stripe・Auth・メールAPIはMockであり、実決済の合格ではない。ログ抑制修正前の248件も合格。
- 直接依存を固定した検証環境の39パッケージの互換性確認に合格。推移的依存の完全なlockfileは未整備。`pip-audit`の公開脆弱性DB照会は既知脆弱性なし。全作業差分の`git diff --check`に合格。
- Sandbox限定で結果不明のTransfer／銀行PayoutをGET照合し、別の管理者がMFA付きで30分以内に承認する復旧を追加。二重送金、金額・口座・metadata不一致、期限切れ、不完全な一覧、DB rollbackをテスト。Checkout・返金・reversalの結果不明は対象外。
- stagingにもOrigin検証・認証制限・秘密情報抑制を適用。配備用`/deploymentz`を追加し、厳格な公開判定`/readyz`と本番決済禁止を維持。
- staging／productionの通常サーバーログに例外本文・tracebackを出さず、問い合わせ番号と例外種別だけ記録する修正を追加。外部監視SDKのscrub設定は別途確認が必要。
- SQLiteデモ保存が既存親ディレクトリの権限を変更する不具合を修正。保存先のsymlink／hardlinkを拒否する4件のテストを追加。
- 実Supabaseを復帰し、AuthのSite URLと2つのコールバックURLをRailway URLに設定。runtime 9テーブルのRLSと一般ユーザーの書込拒否を再確認。金融FK用インデックス3件を実DBへ追加。
- トップ（PC1440px／スマートフォン390px）と管理画面（390px）で横はみ出し・ブラウザーerrorなし、トップのh1／mainは各1件。全画面・実環境CWVの合格を意味しない。
- [GitHub Actions](https://github.com/kegkeg4/toolbako/actions/runs/37945152540)でも **248 passed / 2 warnings / 11.38秒、success**。公開ブランチのtreeはローカルの検証済みtreeと完全一致。GitHubのmainは維持。
- 追加の非秘密キー存在診断を含む[最新外部CI](https://github.com/kegkeg4/toolbako/actions/runs/37949695876)は **250件合格、10.65秒**。配備チェック単体22件も合格。
- **本番決済NO-GOを継続**。Railway Pre-deployで公開キーは注入成功、sealed service_roleの実行時値が空と判明。ユーザーの既存変数再入力待ち。DB／Auth API試験はまだ走っていない。実認証・Stripe Sandbox E2Eは未完了。既存の投稿用公開RPCと実DBの旧バージョンにも追加対処が必要。実績・最新の配備結果は`RELEASE_PROGRESS_2026-10-09.md`。

## 2026-09-23 接続準備の検証（履歴）

- 全回帰テスト：**223 passed / 2 warnings / 13.69秒**。Python 3.14.5、実ローカルPostgreSQL 17.11。外部Auth API・Stripe・メール等はMock。
- 現行runtime構成向けの最小Auth bootstrap SQLと6件のテストを追加。既存テーブル保持、既存会員がいる場合の中断、長いメタデータ・ユーザー名重複、本人だけの読み取り、ブラウザーからの本人確認／認定バッジ変更拒否を確認。
- 承認後、実Supabaseにprofiles・creator_badges・Auth登録トリガー・toolbako_runtime version 2（9テーブル）を追加。RLS、匿名／一般ユーザーのruntimeアクセス不可、プロフィール／バッジへの直接更新不可を実DBのメタデータで確認。既存のposts/repliesは変更していない。実Auth会員登録は未検証。
- RailwayへSupabase・DB接続設定5件を保存（反映待ち）。PGPASSWORDとservice_roleをSeal。パスワードの値は取得していない。Railwayからの実接続・新コードのデプロイは未実施。
- 読み取り専用の配備前接続チェックを追加し、設定不備、APIエラー・リダイレクト・タイムアウト、匿名アクセス許可、秘密情報を出さないエラー処理など17件を追加検証。実環境での実行は未完了。
- **本番決済NO-GOは継続**。認証URLの変更は確認待ち。詳細は `RELEASE_PROGRESS_2026-09-23.md`。

## 2026-09-22 再確認（履歴）

- 全回帰テスト：**200 passed / 2 warnings**（33.65秒）。警告はテストクライアントの非推奨APIに関するもの。
- 実PostgreSQL 17.11で、保存・再起動復元・別worker競合・トランザクションrollback・追記専用監査・Stripe操作journal・メールoutboxを検証。
- 実FastAPIルートで会員登録→出品→Checkout予約→購入状況→振込申請の重複送信→再起動相当の復元を検証。外部Stripe／メールはMock。実Supabase認証・カード課金・送金成功の証拠ではない。
- 単発JPYの金融台帳、残高予約、Connect分配と銀行振込の分離、タイムアウト・成功後クラッシュからの復元、口座／金額照合、銀行振込前の分配取消、本体＋追加支払いの返金完了判定を検証。通常の画面移動で変更のない金融履歴を再同期しないことも確認。
- `git diff --check` とPython構文検査は合格。
- 分離した検証環境の39パッケージは `uv pip check` で互換性確認。今回、脆弱性DBの再監査は未実施。
- 商品0件のトップ画面と、PC1280px／スマートフォン390pxの売上・振込画面をローカルブラウザで表示確認。手数料説明の詰まりを修正。
- 現在の判定：**本番決済NO-GO**。単発向け台帳・Sandbox workerはローカル実装済み。結果不明の復旧運用、銀行振込後の資金回収、定期課金台帳、期限管理、本番相当E2Eが残る。Postgres互換層は直列処理で、高負荷の合格を意味しない。
- 詳細と本番接続待ちは `RELEASE_PROGRESS_2026-09-22.md`。下記の旧レポートの採点・性能値は今回の証拠ではない。

## 2026-09-07 再確認（履歴）

本番公開は引き続き **NO-GO** です。以下に残す旧評価の「97点」は現在の完成度・安全性を示す数値として使用しません。実際に、本番MFAが設定フラグだけだった箇所、未接続台帳、振込計算の境界・GETによる申請生成を確認しました。修正・実行テスト・外部接続の未確認範囲は `RELEASE_PROGRESS_2026-09-07.md` に記録しています。

## 旧レポート（2026-07-22時点の記録・今回の検証結果ではありません）

更新日: 2026-07-22

## 結論

- プロダクト実装品質: **97 / 100**
- 本番公開判定: **NO-GO**

97点は「ローカル／ステージングで確認できるコード、画面、主要業務フロー」の評価です。実カード決済を受ける本番公開には、Postgres repository、複数台共通レート制限、永続監査ログ、管理者・販売者MFA、および実サービスE2Eが必要です。外部設定が未完了なだけの項目と、追加実装が必要な項目を混同していません。

## 7担当の最終採点

| 視点 | 点数 | 最終判定 |
|---|---:|---|
| セキュリティ | 96 | PKCE、fail-closed認証、サーバーSID、厳格CSP、Origin検証、RLS列権限、Webhook署名・金額・通貨・対象照合、危険ZIP検査、案件単位の双方NDAと独占譲渡専用審査を確認。本番DB・MFA・分散制限は公開ブロッカー。 |
| パフォーマンス | 97 | gzip、1年immutable静的キャッシュ、匿名HTMLの短期CDNキャッシュ、画像最適化、同期保存のスレッド退避。主要10 URL・300リクエストのローカルp95は1.89ms。実環境CWVと負荷試験は未実施。 |
| 境界値 | 99 | 同時購入上限、重複契約・振込・問い合わせ、案件別NDA、譲渡再審査、順不同／重複Webhook、決済不一致、危険パス、期限・金額・状態遷移を自動検証。 |
| UI/UX | 97 | release24を実ブラウザで再監査。トップ、商品、独占譲渡、販売管理、運営管理で横はみ出し・未ラベル入力・コンソールエラーは0。空状態、確認ダイアログ、エラー画面も確認。 |
| 保守性 | 91 | security/webhooks/files/integrations/persistence/webを分離し79テストを整備。route定義が大きいため、次の大規模追加前にrouter分割を推奨。 |
| 依存関係 | 99 | 全依存を固定し`pip check`は正常、`pip-audit`は既知脆弱性0件。Python 3.14上でFastAPI経由の上流deprecation warningは残る。 |
| ビジネスロジック | 98 | 購入・提案・募集・納品・返金・定期課金・振込に加え、販売者別の売上・手数料・口座保留、追記型金融台帳、通常販売を止めない独占譲渡の独立審査を検証。 |

単純平均は **96.7点** です。

## 機械検証

- `pytest`: **79 passed**（固定済み本番依存 `fastapi 0.139.0` / `starlette 1.3.1` の隔離環境でも合格）
- `pip check`: 固定済み本番依存の不整合 **0件**
- Python構文検査: 合格
- Jinjaテンプレート: **58件すべてコンパイル合格**
- `pip check`: broken requirements 0
- `pip-audit`: **既知脆弱性0件**
- `/healthz`: HTTP 200、demoモード正常
- `/readyz`: demoモードではHTTP 200で `ready: false`（8 / 24通過）。本番未接続を正しく可視化
- 匿名クロール: **209 HTML画面**、ログイン後クロール: **231 HTML画面**。HTTPエラー、main／h1欠落、重複ID、画像alt漏れ0
- 双方向ブロック、即時提供、再開待ち、案件別双方NDA、独占譲渡審査、退会・出力、販売者クーポンの専用回帰テスト: 合格
- ローカルTestClient処理時間: 主要10 URL・300リクエスト、median **1.30ms**／p95 **1.89ms**／max **4.17ms**
- ブラウザコンソール: error / warning 0（トップ、商品、独占譲渡、販売管理、運営管理）
- 既存の390px／800px／1280pxレスポンシブ監査: 横スクロール、主要入力ラベル漏れ、main／h1重複、主要操作の重なり0
- release24実ブラウザ回帰: 640pxでトップ、商品、独占譲渡、販売管理、運営管理の横はみ出し0
- 404画面: 説明、ホーム導線、mainを確認
- 販売休止操作: POSTと具体的な確認ダイアログを確認（状態は変更せず）

ローカル処理時間はネットワークや実DBを含みません。LCP／INP／CLSは実デプロイURLとChrome DevTools計測環境が揃うまで未評価です。

## 公開を止める残課題

1. `DemoStore`／SQLiteスナップショットをPostgres repositoryへ置換し、注文・在庫・Webhook claim・売上・監査を同一トランザクションで処理する。
2. RedisまたはエッジWAFで、全インスタンス共通のレート制限を実装する。
3. Supabase AAL2を検証するMFA登録・チャレンジ・重要操作時再認証を実装する。
4. 追記専用の永続監査ログ、Webhook outbox／retry／dead-letterを実装する。
5. Stripe Connectの分配・Payout方式を確定し、納品承諾前の銀行出金を防止する。Stripe・Supabase・メール・ClamAV・Storageを本番相当環境へ接続し、返金・dispute・定期失敗を含むE2Eを通す。
6. Stripe Balance Transactionの決済手数料と、Transfer・Payout・返金・異議申立てを追記型金融台帳へ照合する。
7. 実デプロイ環境でCWV、想定ピーク2倍の負荷、バックアップ復元を確認する。

運営者が行う契約・鍵・法務作業は [PRODUCTION_CHECKLIST.md](PRODUCTION_CHECKLIST.md) に分離しています。
