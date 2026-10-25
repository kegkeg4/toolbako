# Toolbako 現状分析・改善計画

調査日：2026-10-25。既存チェックアウトを直接調査。法的適合性を認定する文書ではない。今回の公開規約改定、外部契約、本番決済接続、破壊的DB変更は行わない。

## 1. 結論と完成度

購入・相談・納品・運営審査の主要機能は実装済み。ただし「コードがある」「Sandboxで確認」「一般向け本番運用可能」は別である。完成率の数値は算出しない。一般販売開始には金融E2E、実商品、事業者情報の登録運用、専門家確認、負荷・復旧・監視の残作業がある。

当日読み取り確認：`/healthz` 200、`/deploymentz` 200・live_payments_enabled=false、`/readyz` 503。設定チェック21/24で未充足はproduction_mode/payments/monitoring。監視サービス自体は過去の当日検証で通知到達済みなので、monitoring未充足を「一切監視なし」とも評価しない。Sentry DSNの有無だけで運用の完成を判定しない。

既存エビデンス：当日記録ではSandbox購入1,000円→納品→受取→販売者残高800円→振込申請まで成功。資金利用可能日10/27、アプリ予定日10/29のため、Stripe Transfer/Payoutの完了は未確認。過去の記録を今回の再実行結果と混同しない。

## 2. 技術構成とファイル

- Python 3.12、FastAPI、Jinja2 SSR、既存CSS/JavaScript。PHPを使う構成ではない。ハッシュ付き依存ロックあり。
- Supabase Auth（メール確認・回復・PKCE・MFA）、サーバー側セッション。管理者は環境設定IDで判定。購入者/販売者/管理者の認可と重要操作MFA、二名承認を維持。
- Supabase Postgresのprivate `toolbako_runtime`：marketplace_stateはJSONB全体の互換保存・排他制御、金融のreceipts/entries/allocations/payoutsは別テーブル。auditは追記専用。`supabase/schema.sql` は旧構成であり一括再適用しない。
- Supabase Storage：公開商品画像と非公開納品を分離。納品ダウンロードは参加者認可後の短期署名URL。公開画像は秘密情報不可。
- Railway web、ClamAV、Redis、メールworker/監視、暗号化ファイルバックアップ。Resend取引メールとSupabase Auth SMTPは別の経路。
- Stripe Checkout/Connect/Identity、Webhook署名・重複・順不同対応、結果不明操作の停止と照合を備える。別々の管理者による返金/振込復旧承認。
- 対象ルート：`app/main.py`、ドメイン状態：`app/data.py`、DB：`app/database.py`, `app/runtime_schema.sql`、金融：`app/finance.py`, `app/integrations.py`, `app/webhooks.py`。
- このチェックアウト・対象サブディレクトリ・確認可能な親階層にAGENTS.md/DESIGN.mdは見つからなかった。既存テンプレート、色、余白、カード、ロゴをデザイン基準とする。今回新しいデザインシステムは導入しない。

## 3. 既存ページ・機能と不足

|領域|存在するページ/処理|評価・不足|
|---|---|---|
|登録・認証|signup/login/confirm/recovery/security/mfa|実装済み。確認メールと認証アプリを区別する既存説明を維持|
|発見|tools、categories/AI/price_type検索、match、compare、creators|実装済み。数値価格範囲/用途による細かな絞り込みは拡張候補|
|商品|tool_detail、tool_form、tool_manage、demo_url、FAQ、passport|フォームからpassport条件を編集できず、未確定の既定値も混在。今回専用条件欄を追加。実ファイル・動作実証は別|
|購入|checkout、purchases、mypage、receipt|単発JPY。今回通常購入の注文作成時の条件保存を追加。既存注文は遡及補完しない|
|納品|transaction、files、承諾/差戻し/キャンセル|個別納品は実装済み。instantは完了文言・ライセンス発行だけで実配布物がないケースを別途解消する必要あり|
|利用権|library、subscriptions、subscription_state|コード/デモあり。外部SaaSへ認可を強制する仕組みと本番更新/解約E2Eは未完成。月額は非demoで閉じている|
|相談開発|messages/proposals、requests/applications|相談・見積・注文・取引ルームあり。通常条件と見積固有条件の優先順位・版保存を拡張する|
|信頼|プロフィール、verification、通報、listing_review|手動掲載審査とClamAV検査は別。一般販売者の正式事業者情報と本人確認必須範囲の設計が必要|
|レビュー・保存|購入後相互評価、お気に入り/フォルダ|実装済み。サンプル販売数/評価を一般販売の実績に流用しない|
|ヘルプ|support、usage guide|あり。今回権利侵害カテゴリ/公開窓口案内を追加。自動判定・自動停止はしない|
|法務|terms/privacy/tokushoho（static_page.html）|3ページあり。出品者規約・返金独立ページ・禁止ガイド・権利通報ポリシーはドラフトとして整理|
|運営|admin、finance、operations、reports、cases、users|人数、審査、通報、売上・保留・返金等あり。月間GMV、定義付き返金率、月間人気商品の集計は後述の設計|

## 4. 競合比較（当日公式サイト調査）

|参考|公式情報で確認した仕組み|Toolbakoへの適用|
|---|---|---|
|CodeCanyon|ライセンスFAQ、コード商品の提供、商品サポート範囲|商用利用と再配布、OSSとソースコードの権利を分けて表示。独自のライセンスを無断でコピーしない|
|Gumroad|購入ライブラリ、会員契約、ソフトウェアのライセンスキー検証|ライセンス文字列の発行と、期限/返金後のアクセス制御は別。サーバー側entitlement確認を設計|
|ココナラ|見積相談、非公開トークルーム、正式納品・承諾、相互評価|既存Toolbakoで大部分を実装済み。手順の分かりやすさ、状態表示、追加費用の透明性を優先|

一次情報：[CodeCanyon](https://codecanyon.net/help/getting_started)、[ライセンスFAQ](https://codecanyon.net/licenses/faq)、[Envatoサポート範囲](https://help.market.envato.com/hc/en-us/articles/206171030-Item-Support-FAQ)、[Gumroadライセンスキー](https://gumroad.com/help/article/76-license-keys)、[会員契約](https://gumroad.com/help/article/278-guide-to-memberships)、[ココナラ取引フロー](https://coconala.com/pages/guide_market)。競合の規約・手数料をToolbakoへそのまま適用しない。

## 5. A/B/C販売方式の拡張設計

既存の通常販売/カスタマイズ/独占譲渡と、依頼書の完成品/利用権/オーダーメイドは異なる分類。独占譲渡は既存機能として残す。

|販売方式|既存対応|追加設計・公開判定|
|---|---|---|
|A 完成品買い切り|通常購入、ファイル納品、参加者DL認可、library|ProductVersion→検査済みArtifact→OrderSnapshot→DownloadGrant。即時提供は必須実ファイル/提供先が準備・検査済みであること。ファイルなしで自動完了させない仕様の確定がA|
|B 利用権|one_time/subscription、ライセンス表示、契約状態|Entitlement(user, order, version, start/end, status, revoked_reason)。利用先サーバーで検証。更新invoice支払確定だけ延長、期間末解約、失敗猶予/返金時失効を規約と整合。買い切りでもサービス提供期間を明記。初回の月額公開はしない|
|C オーダーメイド|募集/応募、DM、見積、取引/検収|ProposalRevisionに成果物、納期、追加費、知財、検収条件を固定して注文へ複製。変更はChangeOrderとして双方確認後に決済。分割納品/マイルストーンは将来|

想定追加テーブルは `seller_business_profiles`（非公開正本）/`seller_public_disclosures`（公開項目）/`product_versions`/`artifacts`/`entitlements`/`proposal_revisions`。owner参照、状態遷移、created/updated/revision、監査を持たせる。Auth IDを外部キーの基準とし、metadataによる権限付与をしない。マイグレーションは今回作成・適用していない。正規化は影響評価、バックフィル、比較読取り、切替、復帰手順を承認してから行う。

## 6. 出品者管理と審査

登録→連絡先確認→事業形態/正式名称/代表者/住所/電話/窓口登録→確認→Connect受取条件確認→商品審査→販売者が公開、を設計する。住所・電話はprivate情報と公開/請求時開示設定を分け、一般プロフィールへ混在させない。運営法人の情報で他の販売者を代用しない。本人確認と販売権限確認は別である。

商品審査は、提供物の存在、操作手順、動作環境、費用、権利、外部データ送信、サポート、返金、販売事業者表示を証跡付きで確認。ClamAV陰性はAIの挙動/情報漏えい/権利の保証ではない。非公開化、修正依頼、再審査、理由通知、異議申立ての手順を残す。新規権利確認は版/時刻/会員/商品を保存し、過去の出品者が同意済みだったと補完しない。

## 7. 法務・決済設計

詳細ドラフトは `LEGAL_DRAFTS_2026-10-25.md`。契約当事者、債権者、収納権限、購入者の弁済時点、売上保留と返金を整理して専門家に提出する。

既存構成に合わせStripe Checkout + Connect separate charges and transfersを推奨候補とする。購入→プラットフォーム決済→署名済Webhook/支払確定→取引保留→納品・承諾→分配可能判定→Transfer→Connected AccountのPayout→銀行結果確認。Transferと銀行着金を同じ「振込完了」にしない。20%は今回の計画でも設計値として維持し、既存の通常/カスタマイズ20%を変更しない。

例：税込11,000円→手数料2,200円→販売者分8,800円（銀行振込費等の未確定条件を別途適用する前）。通常カード決済費込みの既存方針なのでStripe実費はプラットフォーム側の費用として別記録する。実費を手数料売上と混ぜない。通貨最小単位の整数・注文時料率固定・追加決済/割引/返金配賦・丸めの仕様をテストする。

返金：申請/証拠保全→新規振込保留→元決済・総額・既返金・Transfer・Payout照合→別担当者の承認→元決済への返金→必要なら分配取消→Webhook/照合→アクセス状態調整→購入者案内。結果不明は再送せず照合。銀行払出後の残高不足/回収不能は運営の損失・留保方針が必要。separate方式のrefundはTransferを自動で戻さない（[Stripe公式](https://docs.stripe.com/connect/separate-charges-and-transfers?platform=web&integration=checkout&ui=stripe-hosted)）。

弁護士/Stripe確認事項：収納代行・為替取引該当性、資金保全/保有期限、倒産時の帰属、国外販売者/購入者・越境の扱い、任意残高移転の禁止、相殺・差押え・未請求残高、AML/KYC、チャージバック/負残高責任、法的なエスクローを提供すると表示してよいか。Connect採用だけで資金決済法の整理が終わったとは判断しない。

## 8. 管理ダッシュボードの設計

|指標|定義・データ源|現状/優先|
|---|---|---|
|登録者|Auth同期済み会員、退会済みを区別|既存人数はruntime側。Auth件数との突合はB|
|出品者|商品保有者/審査済み事業者/販売実績ありを分離|既存finance.sellersは支払済注文ベースであり全出品者数ではない。B|
|商品数|下書き・審査待ち・公開・停止別|既存あり|
|月間GMV|JST暦月の確定paid日時、一次/追加決済receiptを一度ずつ合算。未払・Sandboxを除外|通算あり。月間read modelはB|
|販売件数|その月に初回支払成功したorderのdistinct数|追加決済は件数を増やさない。B|
|手数料売上|完成取引に対応する注文時fee。返金戻し/実費を別表示|既存あり。月別会計定義はB|
|返金額/率|返金確定月の実額、同月返金額÷同月paid額。分母0は—。購入月コホート率を別表示|一部集計あり。部分返金・追加決済も含むledger起点でB|
|人気商品|期間内支払済注文数順、同数はGMV。返金除外、Sandbox除外|表示回数/いいねの既存人気と分ける。B|
|審査待ち|status=pending、滞留時間別|既存あり。滞留警告B|
|通報/問い合わせ|未対応/調査中/完了・カテゴリ・SLA超過|既存あり。今回copyrightカテゴリ追加|

これらは読取専用集計。表示のために台帳を書き換えない。金融事実のpaid_at/refunded_at・livemodeが欠けた過去データは不明として除外数を表示し、推測で本番売上へ含めない。

## 9. 優先順位と受入条件

### A：公開前必須

1. 実商品の提供物と権利/動作環境/費用を確定。A112はユーザー申告でファイル未用意。一般購入可能にする前に実ファイル・導入説明・動作検証を用意する。
2. 今回実装：購入前条件、権利確認履歴、条件変更時の再審査、通常購入時の条件スナップショット、著作権通報導線。既存商品への虚偽補完なし。
3. 事業者登録・本人確認必須範囲・販売停止基準の承認。ONEの情報と第三者販売者を分離。正式な法務ドラフト承認/再同意/表示の整合。
4. 単発決済のSandbox E2EをTransfer→Payout確定まで終え、返金/銀行失敗/結果不明/二重送信の統合検証。本番接続は別承認。
5. 即時納品のArtifact必須条件と購入者限定アクセス保証。単にライセンスキーを発行しただけで納品済みにしない。
6. 通知workerの実運用開始範囲、独立監視、バックアップ初回定刻実行、別環境Auth/Storageを含む復旧。現在の実ファイル暗号化復元4件成功は維持。
7. DB短時間読み取り8並列p95約5.8秒の当日記録。持続/書込混合負荷の許容量、運用SLOを決めて合格させる。排他JSONB全状態保存の制約を明示。

### B：公開後早期

数値価格/用途フィルタ、動画案内、月間管理指標、出品者情報更新ワークフロー、購入後の統一ガイド、規約文書レジストリと既存会員への再同意、見積固有の契約スナップショット、通知再試行・滞留監視、段階的DB正規化。

### C：将来

月額利用権と失効/更新/解約、外部SaaS entitlement API、法人席数ライセンス、マイルストーン開発、検証済み自動コード解析の補助審査。AIの判断だけで法的権利・本人確認・安全性を確定しない。既存独占譲渡は維持。

## 10. 今回の変更と適用範囲

`app/product_disclosures.py`（入力検証・権利証跡・注文スナップショット）、`app/main.py`（既存ルート連携・copyright受付）、`app/data.py`（通常購入の条件保存）、`app/listing_review.py`（条件を審査fingerprintへ追加）、共通partial2件、商品登録/管理/詳細/checkout/transaction/審査/support/baseテンプレート、限定CSS、回帰テスト。

JSONBの既存tools/orders/legal_consentsへ追加項目を保存する互換変更。新規スキーマ/権限/外部接続なし。旧API送信で条件欄が省略された場合は既存値を保持。空欄やunknownは許可とみなさない。新条件を編集するときだけ改めて権利確認を要求する。既存商品を一括非公開にはしない。

既存passportに明示された条件は編集画面にも引き継ぎ、無変更の保存では同意履歴を捏造せず再審査にも戻さない。明示的に空欄・未記載へ変更した場合は旧条件を復活させない。注文時点の複製は通常購入が対象で、見積・追加発注への拡張や、購入画面表示後に条件が変わった場合の再確認トークンは追加設計対象。

実画面検証で見つけた小さな不具合も修正：新商品の未評価をNoneと表示しない、未確認の既定品質点を実検査の結果のように表示しない、プレビューから販売管理へ戻るURL、スマートフォンの管理フォームの横はみ出し、プレビューボタンの文字色。既存のロゴ・配色・画面構成は維持した。

検証結果と反映範囲は `work/marketplace-improvement-result-20261025.json` に記録する。今回の規約ドラフトは非公開で、公開ページへ適用しない。
