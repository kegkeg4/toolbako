# Official image metadata checked 2026-10-17. Build with this directory as context.
FROM redis:8.2-alpine@sha256:b51665e66f00759be7c3152ad5ac3c66fb2f619c13ef62dea7cc1f9914524635
COPY --chown=redis:redis start.sh /usr/local/bin/toolbako-redis
RUN chmod 755 /usr/local/bin/toolbako-redis
USER redis
ENTRYPOINT ["/usr/local/bin/toolbako-redis"]
