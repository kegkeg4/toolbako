import pytest

import app.files as files


@pytest.mark.parametrize("phase", ["send", "receive"])
def test_scanner_cannot_extend_total_deadline_with_small_progress(monkeypatch, phase):
    elapsed = [0.0]
    closed = []
    timeouts = []
    received = []

    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): closed.append(True)
        def settimeout(self, seconds): timeouts.append(seconds)
        def sendall(self, data):
            if phase == "send": elapsed[0] += 6
        def recv(self, size):
            received.append(True)
            elapsed[0] += 6
            return b"s"

    monkeypatch.setattr(files, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(files.socket, "create_connection", lambda *a, **kw: Socket())
    with pytest.raises(TimeoutError, match="time limit"):
        files.clamav_scan(b"x" * (65536 * 5), "fixture-scanner", 3310)
    assert elapsed[0] == 24
    assert closed == [True]
    assert all(0 < seconds <= 20 for seconds in timeouts)
    assert timeouts[-1] == 2
    assert bool(received) == (phase == "receive")


def test_scanner_connection_time_consumes_the_same_budget(monkeypatch):
    elapsed = [0.0]
    closed = []

    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): closed.append(True)
        def settimeout(self, seconds): pytest.fail("Expired connection must not send")
        def sendall(self, data): pytest.fail("Expired connection must not send")

    def connect(*args, **kwargs):
        elapsed[0] = 21
        return Socket()

    monkeypatch.setattr(files, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(files.socket, "create_connection", connect)
    with pytest.raises(TimeoutError):
        files.clamav_scan(b"fixture", "fixture-scanner", 3310)
    assert closed == [True]
