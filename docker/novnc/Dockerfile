FROM debian:bookworm-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        novnc \
        openbox \
        websockify \
        x11vnc \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY docker/novnc/entrypoint.sh /usr/local/bin/spu-novnc
RUN chmod 0755 /usr/local/bin/spu-novnc

EXPOSE 6080

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=6 \
    CMD test -S /tmp/.X11-unix/X99 || exit 1

ENTRYPOINT ["/usr/local/bin/spu-novnc"]
