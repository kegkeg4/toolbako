"""Disposable Docker checks; no cloud credentials, public ports or customer data.

This is explicitly run by the infrastructure workflow, not normal pytest.
ClamAV definitions and cloud connectivity must still be verified at deployment.
"""
import os
import subprocess
import time
from uuid import uuid4


def docker(*args, env=None, check=True):
    result = subprocess.run(["docker", *args], env=env, capture_output=True, text=True, timeout=60)
    if check and result.returncode:
        raise AssertionError(f"Docker command failed: {args[:2]}; {result.stderr}")
    return result


def main():
    for image, script in (("toolbako-clamav-check", "toolbako-clamav"), ("toolbako-redis-check", "toolbako-redis")):
        docker("run", "--rm", "--entrypoint", "/bin/sh", image, "-n", f"/usr/local/bin/{script}")
    version = docker("run", "--rm", "--entrypoint", "clamd", "toolbako-clamav-check", "--version")
    assert "ClamAV 1.5.4" in version.stdout
    config = docker("run", "--rm", "--entrypoint", "clamconf", "toolbako-clamav-check", "-n")
    assert "ERROR" not in config.stdout + config.stderr

    name = "toolbako-redis-check-" + uuid4().hex
    secret = "synthetic_ci_only_" + uuid4().hex
    env = {**os.environ, "REDIS_PASSWORD": secret, "REDISCLI_AUTH": secret}
    try:
        docker("run", "--detach", "--name", name, "--memory=256m", "--cpus=1",
               "--env", "REDIS_PASSWORD", "toolbako-redis-check", env=env)

        def client(*args, authenticated=True, password=None):
            flags = ["--env", "REDISCLI_AUTH"] if authenticated else []
            options = ["--user", "toolbako"] if authenticated else []
            active_env = env if password is None else {**env, "REDISCLI_AUTH": password}
            return docker("exec", *flags, name, "redis-cli", "--raw", *options, *args,
                          env=active_env, check=False).stdout.strip()

        for _ in range(30):
            if client("PING") == "PONG":
                break
            time.sleep(1)
        else:
            raise AssertionError("Redis did not start with the restricted application user")
        assert "NOAUTH" in client("PING", authenticated=False)
        assert client("PING", password="wrong-synthetic-password") != "PONG"
        key = "toolbako:ratelimit:127.0.0.1:auth"
        script = "local n = redis.call('INCR', KEYS[1]); if n == 1 or redis.call('TTL', KEYS[1]) < 0 then redis.call('EXPIRE', KEYS[1], 60) end; return n"
        assert client("EVAL", script, "1", key) == "1"
        assert client("EVAL", script, "1", key) == "2"
        assert 0 < int(client("TTL", key)) <= 60
        assert "NOPERM" in client("EVAL", script, "1", "unrelated:key")
        for command in (("SET", key, "0"), ("GET", key), ("FLUSHALL",), ("CONFIG", "GET", "*"), ("ACL", "LIST")):
            assert "NOPERM" in client(*command), command[0]
        assert client("EXPIRE", key, "1") == "1"
        time.sleep(2)
        assert client("TTL", key) == "-2"
        assert client("EVAL", script, "1", key) == "1"
        print("Container checks passed: ClamAV version/config/shell, Redis authentication, restricted ACL, shared counters and expiry.")
    finally:
        docker("rm", "--force", name, check=False)


if __name__ == "__main__":
    main()
