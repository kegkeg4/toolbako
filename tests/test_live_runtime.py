"""Secret-safe child launch; no OS exec, real DB or provider connection."""
import pickle
import sys

import pytest
from psycopg.conninfo import conninfo_to_dict

from app.live_runtime import RuntimeLaunchError, prepare_runtime_launch


def fixture_env(**changes):
    return {
        "LIVE_RUNTIME_CONFIG": "isolated_v1", "STRIPE_CONNECTION_MODE": "live_read_only",
        "APP_ENV": "staging", "DEMO_MODE": "false", "STRIPE_CHARGE_MODE": "separate",
        "STRIPE_PLATFORM_ACCOUNT_ID": "acct_1Ty4WnIogL39waUY",
        "STRIPE_LEDGER_ID": "56387c08-fdb4-4dd7-b63f-aeae34b6de54",
        "SUPABASE_URL": "https://xnjzgfaasbemffgorvnj.supabase.co", "STORAGE_PROVIDER": "supabase",
        "SUPABASE_PRIVATE_BUCKET": "private", "SUPABASE_IMAGE_BUCKET": "images",
        "SUPABASE_SECRET_KEY": "synthetic-auth-unchanged",
        "DATABASE_URL": "postgresql://old-source.invalid/postgres", "PGPASSWORD": "old-synthetic-password",
        "STRIPE_SECRET_KEY": "rk_test_synthetic", "STRIPE_LIVE_PENDING_SECRET_KEY": "rk_live_synthetic",
        "TOOLBAKO_LIVE_MIGRATION_PASSWORD": "Synthetic '\\: $pass with spaces",
        "STRIPE_WEBHOOK_SECRET": "whsec_oldtest", "STRIPE_CONNECT_WEBHOOK_SECRET": "whsec_oldconnect",
        "PORT": "8080", **changes,
    }


@pytest.mark.parametrize("action", ["web", "preflight"])
def test_existing_launch_is_unchanged_when_off(action, monkeypatch):
    env = fixture_env(LIVE_RUNTIME_CONFIG="off")
    calls = []
    monkeypatch.setattr('os.execve', lambda exe, argv, child: calls.append((exe, argv, child)))
    prepare_runtime_launch(action, env).execute()
    assert calls[0][2] == env
    assert calls[0][1][:3] == [sys.executable, "-m", "uvicorn" if action == "web" else "app.deployment_check"]
    if action == "web": assert calls[0][1][-1] == '--no-access-log'


@pytest.mark.parametrize("action", ["web", "preflight"])
def test_both_commands_use_same_isolated_target_without_secret_argv(action, monkeypatch, capsys):
    original = fixture_env()
    calls = []
    monkeypatch.setattr('os.execve', lambda exe, argv, child: calls.append((exe, argv, child)))
    plan = prepare_runtime_launch(action, original)
    assert repr(plan) == 'RuntimeLaunch(<redacted>)'
    with pytest.raises(TypeError): pickle.dumps(plan)
    plan.execute()
    _, argv, child = calls[0]
    config = conninfo_to_dict(child['DATABASE_URL'])
    assert config['host'] == 'aws-0-ap-northeast-1.pooler.supabase.com'
    assert config['user'] == 'postgres.xijdnqnsuhuciqyygnkg'
    assert config['password'] == original['TOOLBAKO_LIVE_MIGRATION_PASSWORD']
    assert config['sslmode'] == 'verify-full'
    assert child['STRIPE_SECRET_KEY'] == 'rk_live_synthetic'
    assert child['STRIPE_WEBHOOK_SECRET'] == child['STRIPE_CONNECT_WEBHOOK_SECRET'] == ''
    assert not any(key.startswith('PG') for key in child)
    assert 'STRIPE_LIVE_PENDING_SECRET_KEY' not in child and 'TOOLBAKO_LIVE_MIGRATION_PASSWORD' not in child
    assert child['SUPABASE_SECRET_KEY'] == original['SUPABASE_SECRET_KEY']
    assert original == fixture_env()
    assert not any(value in ' '.join(argv) for value in (config['password'], child['STRIPE_SECRET_KEY']))
    assert capsys.readouterr().out == ''


@pytest.mark.parametrize("changes", [
    {"LIVE_RUNTIME_CONFIG": "unknown"}, {"LIVE_RUNTIME_CONFIG": "ISOLATED_V1"},
    {"STRIPE_CONNECTION_MODE": "sandbox"}, {"STRIPE_CONNECTION_MODE": "live"},
    {"STRIPE_LIVE_SELLER_SETUP_ENABLED": "true"}, {"APP_ENV": "development"},
    {"DEMO_MODE": "true"}, {"STRIPE_CHARGE_MODE": "destination"},
    {"STRIPE_PLATFORM_ACCOUNT_ID": "acct_another"}, {"STRIPE_LEDGER_ID": "invalid"},
    {"SUPABASE_URL": "https://xijdnqnsuhuciqyygnkg.supabase.co"}, {"STORAGE_PROVIDER": "local"},
    {"SUPABASE_PRIVATE_BUCKET": ""}, {"SUPABASE_IMAGE_BUCKET": ""},
    {"PGHOST": "other.example"}, {"pgpassword": "mixed-case"}, {"PGOPTIONS": "-c role=other"},
    {"TOOLBAKO_LIVE_MIGRATION_PASSWORD": ""}, {"STRIPE_LIVE_PENDING_SECRET_KEY": "rk_test_synthetic"},
    {"STRIPE_LIVE_PENDING_SECRET_KEY": "sk_live_synthetic"}, {"STRIPE_LIVE_PENDING_SECRET_KEY": "rk_live_with_underscore"},
    {"STRIPE_LIVE_PENDING_WEBHOOK_SECRET": "wrong"}, {"PORT": "$(secret)"}, {"PORT": "0"}, {"PORT": "65536"},
])
def test_bad_projection_never_executes_and_errors_do_not_echo_input(changes, monkeypatch):
    monkeypatch.setattr('os.execve', lambda *args: pytest.fail('No launch on invalid config'))
    with pytest.raises(RuntimeLaunchError) as error:
        prepare_runtime_launch('web', fixture_env(**changes)).execute()
    assert str(error.value) == 'runtime_launch_configuration_unverified'


def test_setup_projects_separate_signed_receivers_and_keeps_money_off(monkeypatch):
    env = fixture_env(STRIPE_CONNECTION_MODE='live_setup_only', STRIPE_LIVE_SELLER_SETUP_ENABLED='true',
                      STRIPE_LIVE_PENDING_WEBHOOK_SECRET='whsec_platform',
                      STRIPE_LIVE_PENDING_CONNECT_WEBHOOK_SECRET='whsec_connect')
    calls = []
    monkeypatch.setattr('os.execve', lambda exe, argv, child: calls.append(child))
    prepare_runtime_launch('preflight', env).execute()
    child = calls[0]
    assert child['STRIPE_WEBHOOK_SECRET'] == 'whsec_platform'
    assert child['STRIPE_CONNECT_WEBHOOK_SECRET'] == 'whsec_connect'
    assert 'STRIPE_LIVE_PENDING_WEBHOOK_SECRET' not in child
    assert child['STRIPE_CONNECTION_MODE'] == 'live_setup_only'


@pytest.mark.parametrize('changes', [
    {}, {'STRIPE_LIVE_PENDING_WEBHOOK_SECRET':'whsec_one', 'STRIPE_LIVE_PENDING_CONNECT_WEBHOOK_SECRET':'whsec_one'},
    {'STRIPE_LIVE_PENDING_WEBHOOK_SECRET':'whsec_one', 'STRIPE_LIVE_PENDING_CONNECT_WEBHOOK_SECRET':'whsec_two',
     'STRIPE_API_VERSION':'different'},
])
def test_setup_requires_signature_pair_and_version(changes):
    with pytest.raises(RuntimeLaunchError):
        prepare_runtime_launch('preflight', fixture_env(STRIPE_CONNECTION_MODE='live_setup_only',
            STRIPE_LIVE_SELLER_SETUP_ENABLED='true', **changes))


@pytest.mark.parametrize('action', ['', 'shell', 'python -c secret', 'web --reload'])
def test_no_arbitrary_command(action):
    with pytest.raises(RuntimeLaunchError): prepare_runtime_launch(action, fixture_env())


def test_repository_deploy_commands_both_use_reviewed_wrapper():
    import json
    from pathlib import Path
    project = Path(__file__).resolve().parents[1]
    config = json.loads((project/'railway.json').read_text(encoding='utf-8'))
    assert config['deploy']['startCommand'] == 'python -m app.live_runtime web'
    assert config['deploy']['preDeployCommand'] == ['python -m app.live_runtime preflight']
    assert (project/'Procfile').read_text(encoding='utf-8').strip() == 'web: python -m app.live_runtime web'


@pytest.mark.parametrize("changes,reason", [
    ({"LIVE_RUNTIME_CONFIG": "private-invalid-selection"}, "runtime_selection_invalid"),
    ({"STRIPE_LIVE_REFUNDS_ENABLED": "private-not-bool"}, "operation_flags_invalid"),
    ({"STRIPE_LIVE_SELLER_SETUP_ENABLED": "false"}, "connection_stage_invalid"),
    ({"STRIPE_LIVE_REFUNDS_ENABLED": "true"}, "connection_stage_invalid"),
    ({"APP_ENV": "private-development"}, "deployment_environment_invalid"),
    ({"STRIPE_API_VERSION": "private-version"}, "stripe_source_configuration_invalid"),
    ({"SUPABASE_URL": "https://private-source.invalid"}, "auth_storage_configuration_invalid"),
    ({"STRIPE_LEDGER_ID": "private-ledger"}, "ledger_invalid"),
    ({"PGOPTIONS": "private-options"}, "inherited_database_options"),
    ({"TOOLBAKO_LIVE_MIGRATION_PASSWORD": ""}, "database_secret_missing"),
    ({"TOOLBAKO_LIVE_MIGRATION_PASSWORD": "private-password\x00suffix"}, "database_secret_invalid"),
    ({"STRIPE_LIVE_PENDING_SECRET_KEY": ""}, "stripe_key_missing"),
    ({"STRIPE_LIVE_PENDING_SECRET_KEY": "private-key"}, "stripe_key_invalid"),
    ({"STRIPE_LIVE_PENDING_WEBHOOK_SECRET": "private-platform"}, "webhook_platform_secret_invalid"),
    ({"STRIPE_LIVE_PENDING_CONNECT_WEBHOOK_SECRET": "private-connect"}, "webhook_connect_secret_invalid"),
    ({"STRIPE_LIVE_PENDING_WEBHOOK_SECRET": ""}, "webhook_secret_pair_missing"),
    ({"STRIPE_LIVE_PENDING_CONNECT_WEBHOOK_SECRET": ""}, "webhook_secret_pair_missing"),
    ({"STRIPE_LIVE_PENDING_CONNECT_WEBHOOK_SECRET": "whsec_platform"}, "webhook_secrets_not_distinct"),
    ({"STRIPE_WEBHOOK_API_VERSION": "private-hook-version"}, "webhook_version_unsupported"),
    ({"PORT": "private-port"}, "web_port_invalid"),
])
def test_fixed_failure_categories_preserve_generic_error_and_never_print_inputs(changes, reason, monkeypatch, capsys):
    from app import live_runtime
    env = fixture_env(STRIPE_CONNECTION_MODE="live_setup_only", STRIPE_LIVE_SELLER_SETUP_ENABLED="true",
        STRIPE_LIVE_PENDING_WEBHOOK_SECRET="whsec_platform", STRIPE_LIVE_PENDING_CONNECT_WEBHOOK_SECRET="whsec_connect",
        STRIPE_WEBHOOK_API_VERSION="2026-06-24.dahlia")
    env.update(changes)
    monkeypatch.setattr(live_runtime.os, "environ", env)
    monkeypatch.setattr(sys, "argv", ["app.live_runtime", "web"])
    monkeypatch.setattr(live_runtime.os, "execve", lambda *args: pytest.fail("Invalid configuration must not launch"))
    with pytest.raises(RuntimeLaunchError) as error:
        prepare_runtime_launch("web", env)
    assert str(error.value) == "runtime_launch_configuration_unverified"
    assert error.value.reason == reason
    with pytest.raises(SystemExit) as exit_error:
        live_runtime.main()
    assert exit_error.value.code == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "runtime_launch_configuration_unverified\nruntime_launch_reason=" + reason + "\n"
    assert "private-" not in output.err and "whsec_" not in output.err and "rk_live_" not in output.err
    assert not any(name in output.err for name in env)


def test_ca_or_database_builder_failure_is_fixed_and_does_not_echo_original_exception(monkeypatch, capsys):
    from app import live_runtime
    def fail(*args, **kwargs):
        raise ValueError("private-CA-path and private-password")
    monkeypatch.setattr(live_runtime, "build_live_database_config", fail)
    monkeypatch.setattr(live_runtime.os, "environ", fixture_env())
    monkeypatch.setattr(sys, "argv", ["app.live_runtime", "preflight"])
    with pytest.raises(SystemExit) as error:
        live_runtime.main()
    assert error.value.code == 1
    assert capsys.readouterr().err == (
        "runtime_launch_configuration_unverified\nruntime_launch_reason=database_ca_or_config_invalid\n")


@pytest.mark.parametrize("reason", ["private-secret-reason", {"private": "value"}, None])
def test_arbitrary_reason_is_not_an_output_channel(reason):
    error = RuntimeLaunchError(reason)
    assert str(error) == "runtime_launch_configuration_unverified"
    assert error.reason == "configuration_unverified"


def test_unexpected_exec_failure_never_prints_original_exception(monkeypatch, capsys):
    from app import live_runtime
    def fail(*args, **kwargs):
        raise OSError("private-executable-path private-password")
    monkeypatch.setattr(live_runtime.os, "execve", fail)
    monkeypatch.setattr(live_runtime.os, "environ", fixture_env())
    monkeypatch.setattr(sys, "argv", ["app.live_runtime", "preflight"])
    with pytest.raises(SystemExit) as error:
        live_runtime.main()
    assert error.value.code == 1
    assert capsys.readouterr().err == (
        "runtime_launch_configuration_unverified\nruntime_launch_reason=unexpected_launch_failure\n")


def test_setup_dahlia_receivers_preserve_clover_api_and_financial_stop(monkeypatch, capsys):
    env = fixture_env(STRIPE_CONNECTION_MODE="live_setup_only", STRIPE_LIVE_SELLER_SETUP_ENABLED="true",
        STRIPE_LIVE_PENDING_WEBHOOK_SECRET="whsec_platform", STRIPE_LIVE_PENDING_CONNECT_WEBHOOK_SECRET="whsec_connect",
        STRIPE_API_VERSION="2026-02-25.clover", STRIPE_WEBHOOK_API_VERSION="2026-06-24.dahlia",
        STRIPE_LIVE_REFUNDS_ENABLED="false", STRIPE_LIVE_PAYOUTS_ENABLED="false")
    calls = []
    monkeypatch.setattr("os.execve", lambda exe, argv, child: calls.append(child))
    prepare_runtime_launch("preflight", env).execute()
    child = calls[0]
    assert child["STRIPE_API_VERSION"] == "2026-02-25.clover"
    assert child["STRIPE_WEBHOOK_API_VERSION"] == "2026-06-24.dahlia"
    assert child["STRIPE_LIVE_REFUNDS_ENABLED"] == child["STRIPE_LIVE_PAYOUTS_ENABLED"] == "false"
    assert capsys.readouterr().out == ""
