"""Owned localhost fixtures only; no actual source connection or email send."""
from copy import deepcopy
from dataclasses import replace
import pickle
from uuid import uuid4

import psycopg
import pytest

from app import live_source_drain as drain
from app import live_source_freeze as freeze
from app import live_dataset_import as local
from app.live_dataset import value_hash
from app.live_dataset_review import prepare_reviewed_candidate
from test_live_source_drain import source, read, held


def rows(source):
    with local._connect(source.target._config, source.target.database_name) as conn:
        conn.execute("set timezone='UTC'")
        return drain._all_rows(conn)


@pytest.fixture
def mail_source(source):
    for _ in range(15):
        read(source, """insert into toolbako_runtime.email_outbox(id,recipient,sender,subject,body)
            values(%s,'retained@example.invalid','sender@example.invalid','RETAINED_SUBJECT','RETAINED_BODY')""", (str(uuid4()),))
    # Only committed, all-column canonical observation is accepted as review.
    outbox = rows(source)["email_outbox"]
    source.mail_review = freeze.RetainedMailReview(
        source.fingerprint, value_hash(outbox), 15, 1, "a"*64)
    return source


def frozen(source, **kwargs):
    options = dict(expected_fingerprint=source.fingerprint,
                   expected_schema_sha256=source.target.schema_signature,
                   mail_review=source.mail_review, external_writer_evidence_sha256="e"*64)
    options.update(kwargs)
    return freeze.held_source_freeze("synthetic-local-only", "synthetic-local-only", **options)


@pytest.mark.parametrize("change", [
    {"source_fingerprint": "b"*64}, {"outbox_sha256": "bad"}, {"evidence_sha256": ""},
    {"pending_count": -1}, {"pending_count": True}, {"sent_count": "1"},
    {"disposition": "send"}, {"disposition": "delete"}, {"disposition": "copy_to_target"},
])
def test_review_claim_requires_exact_disposition_and_valid_fields(change):
    review = freeze.RetainedMailReview("f"*64, "a"*64, 15, 1, "e"*64)
    with pytest.raises(freeze.SourceFreezeError, match="review_required"):
        freeze._validate_review(replace(review, **change), "f"*64)


def test_arbitrary_dict_or_handle_is_not_a_reviewed_boundary():
    with pytest.raises(freeze.SourceFreezeError): freeze._validate_review({"approved": True}, "f"*64)
    with pytest.raises(freeze.SourceFreezeError): freeze.FrozenSource(None, {"password": "SECRET"}, (), None, None)


@pytest.mark.parametrize("timeout", [0, -1, 6, float("nan"), float("inf"), True, "1"])
def test_invalid_timeout_is_rejected_before_connection(monkeypatch, timeout):
    monkeypatch.setattr(drain, "_connect", lambda *_: pytest.fail("No connection expected"))
    review = freeze.RetainedMailReview("f"*64, "a"*64, 0, 0, "e"*64)
    with pytest.raises(freeze.SourceFreezeError, match="configuration_invalid"):
        with freeze.held_source_freeze("SECRET_DSN", "SECRET_PASSWORD", expected_fingerprint="f"*64,
                                      expected_schema_sha256="c"*64, mail_review=review,
                                      external_writer_evidence_sha256="e"*64, lock_timeout_seconds=timeout):
            pass


def test_reviewed_pending_mail_stays_unchanged_and_is_not_exposed_to_export(mail_source, capsys):
    source = mail_source
    before = rows(source)
    seen = []
    with frozen(source) as handle:
        proof = handle.verify()
        assert dict(proof.table_counts)["email_outbox"] == 16
        def consume(state):
            assert "email_outbox" not in state and "RETAINED_BODY" not in repr(state)
            seen.append(True)
            return prepare_reviewed_candidate(state, source.manifest)
        candidate = handle.export(consume, manifest=source.manifest)
        assert candidate is not None and seen == [True]
        assert "RETAINED" not in repr(proof) + repr(handle)
        assert "SECRET" not in repr(handle)
        with pytest.raises(TypeError): pickle.dumps(handle)
        with local._connect(source.target._config, source.target.database_name, autocommit=True) as other:
            assert other.execute("select pg_try_advisory_lock(%s)", (drain.LOCK_ID,)).fetchone() == (False,)
    assert rows(source) == before
    assert all(conn.closed for conn in source.connections)
    with pytest.raises(freeze.SourceFreezeError, match="boundary_inactive"): handle.verify()
    # The ordinary read-only drain was not changed to allow pending messages.
    with pytest.raises(drain.SourceDrainError, match="unsettled_work"):
        with held(source): pytest.fail("Old drain must remain strict")
    assert capsys.readouterr().out == ""


def writer_statement(source, table):
    if table == "schema_version": return "update toolbako_runtime.schema_version set version=2", ()
    if table == "marketplace_state": return "update toolbako_runtime.marketplace_state set revision=43", ()
    if table == "audit_events": return "insert into toolbako_runtime.audit_events values(%s,null,'writer','writer','writer',now(),'writer')", (str(uuid4()),)
    if table == "stripe_operations": return "update toolbako_runtime.stripe_operations set request_hash='writer'", ()
    if table == "email_outbox": return "update toolbako_runtime.email_outbox set body='writer' where id=%s", (source.ids["mail"],)
    if table == "finance_receipts": return "insert into toolbako_runtime.finance_receipts(payment_intent,order_id,seller_id,amount,fee) values('pi_writer','new','seller',20,2)", ()
    if table == "finance_payouts": return "update toolbako_runtime.finance_payouts set request_key='writer'", ()
    if table == "finance_allocations": return "insert into toolbako_runtime.finance_allocations(id,payout_id,receipt_id,amount,transfer_amount) values(%s,%s,'pi_other',20,20)", (str(uuid4()), source.ids["payout"])
    if table == "finance_entries": return "insert into toolbako_runtime.finance_entries(id,kind,amount,content_hash) values('writer','writer',2,'writer')", ()
    raise AssertionError("Uncovered runtime table")


@pytest.mark.parametrize("table", drain.TABLES)
def test_each_of_nine_tables_blocks_actual_writer_and_allows_it_after_release(mail_source, table):
    source = mail_source
    if table == "finance_allocations":
        read(source, "insert into toolbako_runtime.finance_receipts(payment_intent,order_id,seller_id,amount,fee) values('pi_other','other','seller',20,0)")
    statement, params = writer_statement(source, table)
    before = rows(source)
    with frozen(source) as handle:
        with local._connect(source.target._config, source.target.database_name, autocommit=True) as writer:
            writer.execute("set lock_timeout='100ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable): writer.execute(statement, params)
        handle.verify()
    # Actual DML succeeds only after release. Roll back this synthetic assertion
    # so even test writer values do not become the source's persistent state.
    with local._connect(source.target._config, source.target.database_name) as writer:
        writer.execute("set local lock_timeout='100ms'")
        assert writer.execute(statement, params).rowcount == 1
        writer.rollback()
    assert rows(source) == before


@pytest.mark.parametrize("field,value", [("pending_count", 14), ("sent_count", 0), ("outbox_sha256", "f"*64)])
def test_exact_mail_counts_and_full_hash_are_required(mail_source, field, value):
    before = rows(mail_source)
    with pytest.raises(freeze.SourceFreezeError, match="mail_review_mismatch"):
        with frozen(mail_source, mail_review=replace(mail_source.mail_review, **{field: value})):
            pytest.fail("Must not yield")
    assert rows(mail_source) == before


@pytest.mark.parametrize("column", ["body", "recipient", "attempts", "claim_token", "available_at"])
def test_review_hash_detects_every_outbox_column_not_only_status(mail_source, column):
    statements = {"body": "body='different'", "recipient": "recipient='other@example.invalid'",
                  "attempts": "attempts=attempts+1", "claim_token": "claim_token='11111111-1111-4111-8111-111111111111'::uuid",
                  "available_at": "available_at=available_at+interval '1 second'"}
    read(mail_source, "update toolbako_runtime.email_outbox set " + statements[column])
    before = rows(mail_source)
    with pytest.raises(freeze.SourceFreezeError, match="mail_review_mismatch"):
        with frozen(mail_source): pytest.fail("Must reject stale reviewed mail hash")
    assert rows(mail_source) == before


@pytest.mark.parametrize("fault", ["sending", "review", "lease", "pending_sent_at", "journal_pending", "journal_unknown"])
def test_unresolved_work_remains_rejected_even_with_exact_new_review_hash(mail_source, fault):
    source = mail_source
    if fault.startswith("journal_"):
        read(source, "update toolbako_runtime.stripe_operations set status=%s", (fault.removeprefix("journal_"),))
    elif fault in {"sending", "review"}:
        read(source, "update toolbako_runtime.email_outbox set status=%s where status='pending'", (fault,))
    elif fault == "lease": read(source, "update toolbako_runtime.email_outbox set lease_until=now()-interval '1 hour'")
    else: read(source, "update toolbako_runtime.email_outbox set sent_at=now() where status='pending'")
    before = rows(source)
    review = replace(source.mail_review, outbox_sha256=value_hash(before["email_outbox"]))
    with pytest.raises(freeze.SourceFreezeError, match="unsettled_work"):
        with frozen(source, mail_review=review): pytest.fail("Review cannot override unsettled work")
    assert rows(source) == before


def test_relation_lock_timeout_rolls_back_partial_freeze_and_does_not_change_data(mail_source):
    source = mail_source
    before = rows(source)
    with local._connect(source.target._config, source.target.database_name) as writer:
        writer.execute("lock table toolbako_runtime.email_outbox in row exclusive mode")
        with pytest.raises(freeze.SourceFreezeError, match="lock_busy"):
            with frozen(source, lock_timeout_seconds=0.05): pytest.fail("Must not yield partial freeze")
        # audit_events sorts before email_outbox and would have been acquired
        # first. It must already be released by the failed transaction rollback.
        with local._connect(source.target._config, source.target.database_name) as verifier:
            verifier.execute("set local lock_timeout='100ms'")
            verifier.execute("lock table toolbako_runtime.audit_events in row exclusive mode")
            assert verifier.execute("select pg_try_advisory_lock(%s)", (drain.LOCK_ID,)).fetchone() == (True,)
            verifier.execute("select pg_advisory_unlock(%s)", (drain.LOCK_ID,))
        writer.rollback()
    assert rows(source) == before and all(c.closed for c in source.connections)


@pytest.mark.parametrize("fault", ["closed", "rollback", "unlock", "repeatable_read", "callback_error", "callback_mutates", "accidental_dml"])
def test_connection_loss_or_invalid_export_rolls_back_and_sanitizes_errors(mail_source, fault, capsys):
    source = mail_source
    before = rows(source)
    with pytest.raises(freeze.SourceFreezeError) as error:
        with frozen(source) as handle:
            conn = source.connections[-1]
            if fault == "closed": conn.close()
            elif fault == "rollback": conn.execute("rollback")
            elif fault == "unlock": conn.execute("select pg_advisory_unlock(%s)", (drain.LOCK_ID,))
            elif fault == "repeatable_read":
                # PostgreSQL itself also rejects changing isolation after reads.
                conn.execute("set default_transaction_isolation='repeatable read'")
                conn.execute("rollback")
            elif fault == "accidental_dml": conn.execute("update toolbako_runtime.marketplace_state set revision=99")
            else:
                def consumer(state):
                    if fault == "callback_error": raise RuntimeError("SECRET_CALLBACK")
                    state["tools"].clear()
                handle.export(consumer, manifest=source.manifest)
            handle.verify()
    assert "SECRET" not in str(error.value)
    assert rows(source) == before and all(c.closed for c in source.connections)
    assert capsys.readouterr().out == ""


def test_stale_manifest_never_calls_consumer(mail_source):
    manifest = deepcopy(mail_source.manifest)
    manifest["source_revision"] += 1
    with pytest.raises(freeze.SourceFreezeError, match="manifest_stale"):
        with frozen(mail_source) as handle:
            handle.export(lambda _: pytest.fail("No stale export"), manifest=manifest)


def test_normal_success_always_rolls_back_the_source_transaction(mail_source, monkeypatch):
    # Spy on the real psycopg transaction setting, not a substitute transaction.
    calls = []
    original = psycopg.Connection.transaction
    def transaction(conn, *args, **kwargs):
        if conn in mail_source.connections:
            calls.append(kwargs.get("force_rollback"))
        return original(conn, *args, **kwargs)
    monkeypatch.setattr(psycopg.Connection, "transaction", transaction)
    before = rows(mail_source)
    with frozen(mail_source) as handle: handle.verify()
    assert calls == [True]
    assert rows(mail_source) == before


def test_statement_timeout_aborts_freeze_and_releases_all_locks_without_changes(mail_source):
    before = rows(mail_source)
    with pytest.raises(freeze.SourceFreezeError, match="freeze_failed"):
        with frozen(mail_source) as handle:
            conn = mail_source.connections[-1]
            assert conn.execute("show statement_timeout").fetchone() == ("5s",)
            assert conn.execute("show lock_timeout").fetchone() == ("1s",)
            assert conn.execute("show idle_in_transaction_session_timeout").fetchone() == ("30s",)
            # Shorten only the disposable fixture's timeout to prove the actual
            # PostgreSQL abort/rollback path without a five-second test sleep.
            conn.execute("set local statement_timeout='20ms'")
            conn.execute("select pg_sleep(0.05)")
            pytest.fail("Statement should have timed out")
    assert rows(mail_source) == before
    with local._connect(mail_source.target._config, mail_source.target.database_name) as writer:
        writer.execute("set local lock_timeout='100ms'")
        writer.execute("lock table toolbako_runtime.email_outbox in row exclusive mode")
        assert writer.execute("select pg_try_advisory_lock(%s)", (drain.LOCK_ID,)).fetchone() == (True,)
        writer.execute("select pg_advisory_unlock(%s)", (drain.LOCK_ID,))
