"""Exercise the exact standalone workflow program with no network or app import."""
import json
from pathlib import Path
import subprocess
import textwrap
from types import SimpleNamespace

import pytest


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/availability-monitor.yml'
PRIVATE = 'PRIVATE-secret https://example.invalid/private?token=SECRET 10.0.0.7'
DEFAULT_TIMES = ('0.010', '0.040', '0.090', '0.091', '0.190', '0.200')


def wire(status=200, times=DEFAULT_TIMES):
    return ('\n'.join((f'{status:03}', *times))).encode('ascii')


@pytest.fixture
def monitor(monkeypatch):
    script = textwrap.dedent(WORKFLOW.read_text(encoding='utf-8').split("python3 - <<'PY'\n", 1)[1].rsplit('\n          PY', 1)[0])
    namespace = {'__name__': 'availability_monitor_test'}
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('Unexpected real subprocess'))
    exec(compile(script, '<availability-monitor-workflow>', 'exec'), namespace)
    return namespace


def mock_requests(monkeypatch, monitor, responses):
    calls = []
    entries = iter(responses)
    def execute(command, **kwargs):
        calls.append((command, kwargs))
        item = next(entries)
        if isinstance(item, Exception):
            raise item
        status, body, code, output = item
        Path(command[command.index('--output') + 1]).write_bytes(body)
        return SimpleNamespace(returncode=code, stdout=wire(status) if output is None else output,
                               stderr=PRIVATE.encode())
    monkeypatch.setattr(monitor['subprocess'], 'run', execute)
    return calls


OK = (200, b'{"status":"ok"}', 0, None)


def test_healthy_json_and_phase_differences_without_response_echo(monkeypatch, monitor, capsys):
    calls = mock_requests(monkeypatch, monitor, [(200, json.dumps({'status': 'ok', 'private': PRIVATE}).encode(), 0, None)])
    row = monitor['check_endpoint']('/healthz')
    assert row['healthy'] is True and row['status'] == 200 and row['category'] == 'ok'
    assert row['timings'] == {'dns_ms': 10.0, 'tcp_ms': 30.0, 'tls_ms': 50.0,
                              'first_byte_ms': 99.0, 'body_ms': 10.0, 'total_ms': 200.0}
    assert PRIVATE not in json.dumps(row) + capsys.readouterr().out
    command, kwargs = calls[0]
    assert command[:2] == ['curl', '--disable']
    for flag, value in {'--proto': '=https', '--retry': '0', '--max-redirs': '0',
                        '--connect-timeout': '15', '--max-time': '15', '--max-filesize': '8192'}.items():
        assert command[command.index(flag) + 1] == value
    assert command[-1] == 'https://toolbako.onellc-ai.com/healthz'
    assert not {'--location', '-L', '--insecure', '-k', '--netrc', '--cookie', '--verbose', '-v'} & set(command)
    assert kwargs['stderr'] == subprocess.DEVNULL and kwargs['stdin'] == subprocess.DEVNULL
    assert kwargs['stdout'] == subprocess.PIPE and kwargs['timeout'] == 20 and kwargs['check'] is False
    assert set(kwargs['env']) == {'PATH', 'LANG', 'LC_ALL'}
    assert not Path(command[command.index('--output') + 1]).exists()


@pytest.mark.parametrize('status,body,code,category', [
    (301, b'{"status":"ok"}', 0, 'redirect_rejected'),
    (302, PRIVATE.encode(), 0, 'redirect_rejected'),
    (401, b'{"status":"ok"}', 0, 'http_status_unexpected'),
    (503, b'{"status":"ok"}', 0, 'http_status_unexpected'),
    (200, b'{"status":"down"}', 0, 'health_json_mismatch'),
    (200, b'{}', 0, 'health_json_mismatch'),
    (200, b'[]', 0, 'health_json_mismatch'),
    (200, b'null', 0, 'health_json_mismatch'),
    (200, b'"ok"', 0, 'health_json_mismatch'),
    (200, b'not json ' + PRIVATE.encode(), 0, 'health_json_invalid'),
    (200, b'{"status":"ok"}' + b' ' * 8192, 0, 'response_too_large'),
    (200, b'{"status":"ok"}', 63, 'response_too_large'),
    (200, b'{"status":"ok"}', 18, 'response_incomplete'),
    (200, b'{"status":"ok"}', 56, 'response_receive_failed'),
])
def test_http_body_and_transport_must_all_be_healthy(monkeypatch, monitor, status, body, code, category, capsys):
    calls = mock_requests(monkeypatch, monitor, [(status, body, code, None)])
    row = monitor['check_endpoint']('/deploymentz')
    assert row['healthy'] is False and row['category'] == category
    assert len(calls) == 1  # no redirect or hidden curl retry
    assert PRIVATE not in json.dumps(row) + capsys.readouterr().out


@pytest.mark.parametrize('code,category', [(6, 'dns_resolution_failed'), (7, 'tcp_connect_failed'),
    (35, 'tls_handshake_failed'), (60, 'tls_verification_failed'), (77, 'tls_ca_unavailable'),
    (52, 'empty_response'), (55, 'request_send_failed'), (99, 'transport_failed')])
def test_fixed_transport_categories(monkeypatch, monitor, code, category):
    mock_requests(monkeypatch, monitor, [(0, b'', code, wire(0, ('0',) * 6))])
    row = monitor['check_endpoint']('/healthz')
    assert row['category'] == category and row['healthy'] is False
    assert all(row['timings'][key] is None for key in ('dns_ms', 'tcp_ms', 'tls_ms', 'first_byte_ms', 'body_ms'))


@pytest.mark.parametrize('status,times,category', [
    (0, ('0', '0', '0', '0', '0', '15'), 'connection_phase_timeout'),
    (0, ('0.1', '0', '0', '0', '0', '15'), 'tcp_phase_timeout'),
    (0, ('0.1', '0.2', '0', '0', '0', '15'), 'tls_phase_timeout'),
    (0, ('0.1', '0.2', '0.3', '0.31', '0', '15'), 'request_or_first_byte_timeout'),
    (200, ('0.1', '0.2', '0.3', '0.31', '0.4', '15'), 'body_transfer_timeout'),
])
def test_timeout_stage_is_inferred_without_inventing_completed_phases(monkeypatch, monitor, status, times, category):
    mock_requests(monkeypatch, monitor, [(status, b'{"status":"ok"}', 28, wire(status, times))])
    row = monitor['check_endpoint']('/healthz')
    assert row['category'] == category and row['healthy'] is False
    if not status:
        assert row['timings']['first_byte_ms'] is None and row['timings']['body_ms'] is None
    if times[1] == '0':
        assert row['timings']['tcp_ms'] is None and row['timings']['tls_ms'] is None


@pytest.mark.parametrize('output', [b'', PRIVATE.encode(), b'200\nNaN\n0\n0\n0\n0\n0',
    b'200\ninf\n0\n0\n0\n0\n0', b'200\n-1\n0\n0\n0\n0\n0', b'200\n99\n0\n0\n0\n0\n0',
    b'999\n0\n0\n0\n0\n0\n0', b'20\n0\n0\n0\n0\n0\n0', b'200\n0\n0\n0\n0\n0\n0\nPRIVATE', b'\xff'])
def test_non_whitelisted_metrics_cannot_be_logged_or_accepted(monkeypatch, monitor, output, capsys):
    mock_requests(monkeypatch, monitor, [(200, b'{"status":"ok"}', 0, output)])
    row = monitor['check_endpoint']('/healthz')
    assert row['healthy'] is False and row['category'] == 'monitor_metrics_invalid'
    assert row['status'] is None and row['timings'] is None
    assert PRIVATE not in json.dumps(row) + capsys.readouterr().out


@pytest.mark.parametrize('error,category', [
    (subprocess.TimeoutExpired(PRIVATE, 20, output=PRIVATE.encode(), stderr=PRIVATE.encode()), 'monitor_process_timeout'),
    (FileNotFoundError(PRIVATE), 'monitor_dependency_or_output_missing'),
    (RuntimeError(PRIVATE), 'monitor_error'),
])
def test_process_exceptions_never_echo_arguments_output_or_private_paths(monkeypatch, monitor, error, category, capsys):
    mock_requests(monkeypatch, monitor, [error])
    row = monitor['check_endpoint']('/healthz')
    assert row['category'] == category and row['healthy'] is False
    assert PRIVATE not in json.dumps(row) + capsys.readouterr().out


def test_recovery_keeps_failed_round_and_original_maximum_budget(monkeypatch, monitor, tmp_path, capsys):
    bad = (0, b'', 7, wire(0, ('0.01', '0', '0', '0', '0', '0.2')))
    calls = mock_requests(monkeypatch, monitor, [bad, OK, OK, OK])
    sleeps = []
    monkeypatch.setattr(monitor['time'], 'sleep', sleeps.append)
    summary = tmp_path / 'summary'
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(summary))
    assert monitor['main']() == 0
    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    report = outputs[-1]
    assert outputs[0]['checks'][0]['category'] == 'tcp_connect_failed'
    assert report['attempts'] == 2 and report['failed_checks'] == 1 and report['recovered'] is True
    assert report['history'][0] == outputs[0] and report['history'][1] == outputs[1]
    assert len(calls) == 4 and sleeps == [20]
    assert 'Failed checks retained: 1' in summary.read_text()


def test_three_failed_rounds_stop_at_six_requests_no_extra_probe(monkeypatch, monitor, capsys):
    bad = (503, PRIVATE.encode(), 0, None)
    calls = mock_requests(monkeypatch, monitor, [bad, OK] * 3)
    sleeps = []
    monkeypatch.setattr(monitor['time'], 'sleep', sleeps.append)
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    assert monitor['main']() == 1
    output = capsys.readouterr().out
    report = json.loads(output.splitlines()[-1])
    assert report['attempts'] == 3 and len(report['history']) == 3 and report['failed_checks'] == 3
    assert report['healthy'] is False and report['recovered'] is False
    assert len(calls) == 6 and sleeps == [20, 20]
    assert PRIVATE not in output
    assert all(c[0][-1] in {'https://toolbako.onellc-ai.com/healthz', 'https://toolbako.onellc-ai.com/deploymentz'} for c in calls)


def test_first_healthy_round_stops_without_sleep(monkeypatch, monitor, capsys):
    calls = mock_requests(monkeypatch, monitor, [OK, OK])
    monkeypatch.setattr(monitor['time'], 'sleep', lambda _: pytest.fail('Unexpected sleep'))
    report = monitor['run_checks']()
    assert report['healthy'] is True and report['attempts'] == 1 and report['failed_checks'] == 0
    assert len(calls) == 2 and report['recovered'] is False
    assert json.loads(capsys.readouterr().out)['attempt'] == 1


@pytest.mark.parametrize('path', ['/login', '/healthz?token=private', 'https://example.invalid', '//other'])
def test_unknown_endpoint_never_reaches_subprocess(monitor, path):
    with pytest.raises(ValueError, match='^endpoint_not_allowed$'):
        monitor['check_endpoint'](path)


def test_workflow_schedule_permissions_and_secrets_contract_remains_small():
    source = WORKFLOW.read_text(encoding='utf-8')
    assert "cron: '17,47 * * * *'" in source
    assert 'contents: read' in source and 'timeout-minutes: 4' in source
    assert 'secrets.' not in source and 'uses:' not in source
    assert 'urllib.request' not in source
    assert 'remote_ip' not in source and 'url_effective' not in source
