"""Run the existing suite against isolated local demo state, without provider keys."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-bin", type=Path, help="Official portable PostgreSQL bin directory; creates only a temporary local cluster")
    parser.add_argument("--restore-drill-only", action="store_true", help="Run the synthetic backup/restore checks in the temporary PostgreSQL cluster only")
    parser.add_argument("--test-path", action="append", help="Run selected test paths in the same isolated environment (repeatable)")
    args = parser.parse_args()
    if args.restore_drill_only and not args.postgres_bin:
        parser.error("--restore-drill-only requires --postgres-bin")
    if args.restore_drill_only and args.test_path:
        parser.error("--restore-drill-only cannot be combined with --test-path")
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    names = re.findall(r'os\.getenv\("([A-Z0-9_]+)"', (root / "app/config.py").read_text(encoding="utf-8"))
    for name in names:
        environment.pop(name, None)
    for name in ("TEST_POSTGRES_ADMIN_DSN", "TOOLBAKO_DRILL_ADMIN_DSN", "TOOLBAKO_DRILL_PG_BIN", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        environment.pop(name, None)
    environment.update(
        APP_ENV="development", DEMO_MODE="true", PYTHON_DOTENV_DISABLED="1",
        PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    work = root / "work"
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="windows-tests-", dir=work) as temporary:
        environment["PRIVATE_STORAGE_PATH"] = str(Path(temporary) / "uploads")
        bootstrap = ("import asyncio,sys; "
                     "asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()) if sys.platform == 'win32' else None; "
                     "import pytest; sys.exit(pytest.main(sys.argv[1:]))")
        test_targets = ["tests/test_restore_drill.py"] if args.restore_drill_only else (args.test_path or ["tests"])
        report_name = "windows-restore-drill.xml" if args.restore_drill_only else "windows-tests.xml"
        command = [sys.executable, "-c", bootstrap, "-q", *test_targets, "-ra",
                   "--basetemp", str(Path(temporary) / "pytest"),
                   "--junitxml", str(work / report_name)]
        if not args.postgres_bin:
            return subprocess.call(command, cwd=root, env=environment)
        binaries = args.postgres_bin.resolve()
        for filename in ("initdb.exe", "pg_ctl.exe", "postgres.exe"):
            if not (binaries / filename).is_file():
                parser.error(f"Missing PostgreSQL executable: {filename}")
        cluster = Path(temporary) / "postgres-data"
        password_file = Path(temporary) / "postgres-password"
        password = secrets.token_urlsafe(32)
        password_file.write_text(password, encoding="ascii")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        # No services, machine PATH changes, existing databases or remote hosts.
        db_environment = environment.copy()
        for name in tuple(db_environment):
            if name.startswith("PG"):
                db_environment.pop(name)
        start_attempted = False
        try:
            subprocess.run([str(binaries / "initdb.exe"), "-D", str(cluster), "-U", "postgres",
                            "--encoding=UTF8", "--locale=C", "--auth=scram-sha-256",
                            f"--pwfile={password_file}"], env=db_environment, check=True)
            password_file.unlink()
            start_attempted = True
            subprocess.run([str(binaries / "pg_ctl.exe"), "-D", str(cluster), "-l",
                            str(Path(temporary) / "postgres.log"), "-w", "-t", "30", "-o",
                            f"-h 127.0.0.1 -p {port}", "start"], env=db_environment, check=True)
            environment["TEST_POSTGRES_ADMIN_DSN"] = f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres"
            # Never reuse an inherited drill DSN; this cluster is ours to discard.
            environment["TOOLBAKO_DRILL_ADMIN_DSN"] = environment["TEST_POSTGRES_ADMIN_DSN"]
            environment["TOOLBAKO_DRILL_PG_BIN"] = str(binaries)
            return subprocess.call(command, cwd=root, env=environment)
        finally:
            if start_attempted and subprocess.run(
                [str(binaries / "pg_ctl.exe"), "-D", str(cluster), "status"],
                env=db_environment, capture_output=True,
            ).returncode == 0:
                subprocess.run([str(binaries / "pg_ctl.exe"), "-D", str(cluster), "-m", "fast",
                                "-w", "-t", "30", "stop"], env=db_environment, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
