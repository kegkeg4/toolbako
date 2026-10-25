# Official Python image already used by the private worker.
FROM python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.txt \
    && python -m pip check
# Official release asset and SHA256SUMS verified 2026-10-25. No floating tag.
RUN python -c "import urllib.request,hashlib,bz2,pathlib; data=urllib.request.urlopen('https://github.com/restic/restic/releases/download/v0.19.1/restic_0.19.1_linux_amd64.bz2',timeout=60).read(); assert hashlib.sha256(data).hexdigest()=='f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c'; p=pathlib.Path('/usr/local/bin/restic'); p.write_bytes(bz2.decompress(data)); p.chmod(0o755)" \
    && restic version \
    && groupadd --gid 10001 backupworker \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin backupworker
COPY app/object_storage.py app/supabase_api.py ./app/
COPY tools/storage_backup.py ./tools/
USER 10001:10001
ENTRYPOINT ["python", "-m", "tools.storage_backup"]
CMD ["run"]
