# Official Python manifest, verified from Docker Hub on 2026-10-17.
# Build context is the repository root; do not copy .env, uploads or work/.
FROM python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHON_DOTENV_DISABLED=1 \
    APP_ENV=staging DEMO_MODE=false
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.txt \
    && python -m pip check \
    && groupadd --gid 10001 mailworker \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin mailworker
COPY app ./app
USER 10001:10001
ENTRYPOINT ["python", "-m", "app.mail_worker"]
CMD ["--dry-run"]
