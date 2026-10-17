# Official image metadata checked 2026-10-17. Build with this directory as context.
FROM clamav/clamav:1.5.4_base@sha256:96b28b83eb72fb497c2400a94229844226e5fe4da9061fad1988092d9fcd88f3
COPY clamd.conf /etc/clamav/clamd.conf
COPY start.sh /usr/local/bin/toolbako-clamav
RUN chmod 755 /usr/local/bin/toolbako-clamav \
    && sed -i '/^TestDatabases /d' /etc/clamav/freshclam.conf \
    && printf '\nTestDatabases yes\n' >> /etc/clamav/freshclam.conf
# A persistent volume at /var/lib/clamav is required. Never expose TCP publicly.
ENTRYPOINT ["/usr/local/bin/toolbako-clamav"]
