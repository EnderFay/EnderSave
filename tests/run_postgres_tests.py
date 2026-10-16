"""Создаёт изолированный PostgreSQL, запускает тесты и удаляет лишь свой контейнер."""

import os
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    name = "ytttbot-test-" + uuid.uuid4().hex[:10]
    env = dict(os.environ, POSTGRES_PASSWORD=secrets.token_urlsafe(24), PYTHONUTF8="1")
    created = False
    try:
        subprocess.run(["docker", "run", "--rm", "-d", "--name", name,
            "--tmpfs", "/var/lib/postgresql/data:rw",
            "-e", "POSTGRES_PASSWORD", "-e", "POSTGRES_DB=ytttbot_test",
            "-p", "127.0.0.1::5432", "postgres:17"], check=True, capture_output=True, env=env)
        created = True
        for _ in range(40):
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres", "-d", "ytttbot_test"], capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Test PostgreSQL did not become ready")
        port = subprocess.check_output(["docker", "port", name, "5432"], text=True).strip().rsplit(":", 1)[1]
        env.update(TEST_POSTGRES_PORT=port, TEST_POSTGRES_PASSWORD=env["POSTGRES_PASSWORD"])
        print("Running tests against isolated PostgreSQL 17; working bot is unchanged", flush=True)
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=root, env=env)
        return result.returncode
    finally:
        if created:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)


if __name__ == "__main__":
    raise SystemExit(main())
