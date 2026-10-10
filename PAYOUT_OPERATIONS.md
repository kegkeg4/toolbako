# 売上・振込運用（2026-10-10、Sandbox限定）

## 公開判定

実資金の決済・銀行振込はまだ開始しない。`sk_live_` / `rk_live_` での書き込みは停止中。
今回確認したのはローカルPostgres＋Mock Stripe。実Stripe Sandbox、日本の販売者口座、銀行入金を通した証拠ではない。

## 実装範囲

- JPY単発購入・カスタマイズ購入・追加支払いの決済明細を `finance_receipts` に記録。
- 確定売上のうち、返金・紛争・結果不明ではない明細だけを申請対象にする。
- 申請番号による重複防止。申請額を古い売上から割当し、明細ごとの予約を保存。
- 販売手数料控除後の売上から振込手数料を引き、残額をConnectへ分配する。例：1,000円売上、販売手数料150円、850円申請、振込手数料160円 → Connect分配／銀行振込690円。
- 管理画面・販売者画面で共通の残高計算。申請予約中と銀行振込済みを分離。
- Stripe操作前の業務状態＋journal保存、結果不明の保留、成功応答記録からの復元。
- 接続先アカウント、通貨、金額、元決済、手動振込、本人確認・送金能力を確認してから分配。
- 銀行振込は作成API成功で完了扱いにしない。署名Webhookまたは認証付き取得APIで結果を確認。
- 管理者による銀行振込開始前の申請取消。既に分配済みならTransfer reversalを確認してから予約を解除。
- 元決済と追加支払いの両方を返金対象にし、全額の確認前に「返金完了」と表示しない。
- 振込結果不明・保留・失敗中は残高を予約し続ける。退会処理も保留する。

これは販売者補助台帳で、合同会社ONEの総勘定元帳・税務会計そのものではない。

## 構成

専用の `toolbako_runtime` スキーマ、version 2。

| テーブル | 内容 |
| --- | --- |
| finance_receipts | PaymentIntentごとの売上・手数料。上書き・削除不可 |
| finance_payouts | 申請金額、販売者・口座、予定日、銀行振込の状態 |
| finance_allocations | 振込申請と決済明細の割当。上書き・削除不可 |
| finance_entries | 売上、予約、分配、取消、返金、銀行振込の追記記録 |
| stripe_operations | API要求のハッシュ・結果。別アカウントへの同一キー再利用も拒否 |

業務JSONBと台帳は同一トランザクションで保存する。現段階では全業務がDB advisory lockで直列化され、workerの外部API待ちも含めてロックする。高負荷向けの実装ではなく、負荷試験・粒度分割は別途必要。

## Sandboxへの接続手順

1. 専用ステージングDBを作る。既存デモDBや他サービスのDBは流用しない。
2. `APP_ENV=staging`、`DEMO_MODE=false`、`DATABASE_URL`（direct/session接続）、`STRIPE_CHARGE_MODE=separate`、Stripeのtestキーを安全な環境変数に設定する。キーをチャット、コマンド引数、gitへ書かない。
3. `python -m app.database migrate`、`python -m app.database check` を実行する。
4. JP / JPY / transfers有効 / 本人確認完了のConnect口座を用意する。新規separate方式のExpress口座は手動振込で作成する。既存口座の設定は勝手に変更しない。
5. 通常の購入→Webhook→納品承諾→本人確認→振込申請を通す。
6. `python -m app.payout_worker --payout-id <申請ID>` を予定日以後に実行。1回で最大1件の分配・振込・取消を進める。`--payout-id` を省略すると、対象の最も古い申請を1段階進める。
7. 実行結果の `transferring` → `bank_pending` → `paid` と、Stripe側の金額・アカウント・IDを照合する。資金未確定なら `awaiting_funds`。署名付きConnect Webhookでも振込結果を反映する。

このworkerは本番環境・liveキー・destination方式では起動を拒否する。アカウントをまたぐ分配方式の切替はデータ移行と実機検証が必要。既存destination決済をseparate決済として再分配しない。

## 要対応状態

- **review**：結果不明・不一致。新しいキーで再送しない。管理画面の「結果不明の振込を照合する」から、GETだけでStripe記録と台帳を照合する。分配／銀行振込の成功記録が一意・完全一致した場合に限り、別の管理者が30分以内に再照合して承認できる。両者とも直近MFAが必要。0件・複数件・不一致・不完全な一覧では状態を変えず予約を保持する。記録がないことを再送許可と解釈しない。Checkout／返金は下記の別画面で照合する。分配取消の結果不明はまだ対象外。
- **bank_failed**：銀行口座情報・返戻金をStripeで確認。元の申請は予約状態を保つ。別申請を作って残高を二重に渡さない。再振込の承認・試験は未完了。
- **held**：運営による販売者保留。保留解除はこの理由だけを再開する。返金・紛争・結果不明は解除しない。
- **reversing**：運営が申請取消を要求。workerが分配を取り消し、資金回収確認後にcancelledへ進める。銀行振込開始済みはこの処理の対象外。
- **refund partial/review**：本体と追加支払いの返金を照合する。返金未完了の売上を解放しない。

## 結果不明の購入・返金の復旧

管理画面「結果不明の購入・追加支払い・返金を照合する」は、永続DB・Stripe testキー・separate方式・別々の管理者2名を登録したstagingだけで操作できる。本番やliveキーでは操作不可。確認者・承認者とも直近MFAが必要。

1. `stripe_operations`のpending／unknownを100件まで表示する。件数超過は警告し、注文と対応できない記録は操作ボタンを出さない。プロバイダーの応答JSONや秘密値は表示しない。
2. 最初の管理者がGETだけで完全な一覧を取得する（最大10ページ・全照合30秒）。元要求のhash、注文／追加明細、JPY・金額・作成日時・戻りURL等を照合する。0件・複数件・不完全な一覧では保留継続。
3. 別の管理者が30分以内に再取得し、照合結果が変更されていないことを承認する。購入完了の復旧にはPaymentIntent／元Chargeのテスト環境・支払済み全額・注文・送金グループ・返金／異議申立てなしも確認する。Refundにはlivemode属性がないため、元PaymentIntentとChargeで環境を確認する。
4. open／unpaidのCheckoutはStripeの有効な購入URLだけを復元し、ツールを提供しない。expired／unpaidは期限切れにする。complete／paidの証拠がそろった場合だけ通常の購入状態に戻す。返金はsucceededの全額が確認できた場合だけ累計確定額へ反映し、pending／requires_actionは待機、failed／canceledは運営確認のままとする。
5. journal・注文・追記監査・通知outboxを同じDBトランザクションで保存する。失敗時はまとめてrollback。復旧後のWebhookは販売件数・通知を重複させない。他の結果不明明細があれば振込保留を解除しない。

**この処理は再請求・再返金を行わない。** Stripeで作成記録がない場合も自動再送しない。複数決済の全額取消で、最初の返金が不明になったためまだ要求していない残りの返金は、自動的に新規作成しない。全明細の照合と、未要求分を再開する明示的な運用／実機試験は別途必要。返金途中の売上は保留する。

## 本番前に残る事項

- Stripe再認証、対象環境・アカウントの確定、Secrets登録、配備、実サービスE2E。Supabaseへのスキーマ追加・権限確認は9/23に完了。Railwayの接続変数と配備前チェックは保存済み・反映待ち。Auth URLと実接続の最新状態は `RELEASE_PROGRESS_2026-10-09.md` を参照。
- 月額課金のinvoiceごとの売上・返金台帳。separate方式の月額Checkoutは受付停止し、売上だけ回収して分配できない状態を避ける。
- 結果不明の分配取消の復旧、未要求の残りの返金の安全な再開、銀行振込失敗後の再振込、振込後の返金・債権回収の運用。分配／銀行振込／単発Checkout／全額Refundの二者照合はローカル実装済みで、実Stripe試験は未完了。
- 部分返金後の残額再配分、紛争終了後の残高復元。現状は対象注文を保留する。
- 未申請売上の自動振込、保有期限アラート、銀行休業日・処理遅延の運用と規約。
- workerの配備・定期実行・停止監視・バックアップ復元・負荷試験。今回、外部の定期ジョブは作成していない。

日本のStripe手動振込は原則90日以内の払出しが必要。従来の「120日後」案をそのまま本番採用しない。プラットフォーム残高からの送金時期も含め、Stripeと専門家にモデルを確認して規約を確定する。Stripeを「法的なエスクロー」と表示しない。

参照：[手動振込・保有期限](https://docs.stripe.com/connect/manual-payouts)、[分離した支払いと送金](https://docs.stripe.com/connect/separate-charges-and-transfers)、[銀行振込API](https://docs.stripe.com/api/payouts/create)、[Transfer reversal](https://docs.stripe.com/api/transfer_reversals/create)、[Checkout一覧](https://docs.stripe.com/api/checkout/sessions/list)、[返金一覧](https://docs.stripe.com/api/refunds/list)、[PaymentIntent](https://docs.stripe.com/api/payment_intents/object)、[冪等キーの保持期限](https://docs.stripe.com/api/idempotent_requests)。
