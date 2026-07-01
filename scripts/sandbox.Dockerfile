# Preview/dev image for the Pixel app — built once by dev.sh, then reused for instant
# starts. It mirrors the proot-distro Debian guest on a Pixel (aarch64 glibc + Java 25 +
# signal-cli 0.14.x + the ARM libsignal native lib), so what you see here is what runs on
# the phone. Real config + link keys are NOT baked in (see .dockerignore); a fresh
# config.toml is seeded from the template and link keys live in a volume at runtime.
FROM debian:trixie-slim
ENV DEBIAN_FRONTEND=noninteractive SB_WEBUI_HOST=0.0.0.0
WORKDIR /root/signal-broadcast
COPY . /root/signal-broadcast
# setup-termux.sh installs deps, fetches Temurin JDK 25 + signal-cli 0.14.x, injects the
# aarch64 libsignal native lib, seeds config.toml, and smoke-tests signal-cli.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-flask qrencode git curl ca-certificates cron procps \
 && bash scripts/setup-termux.sh \
 && apt-get clean && rm -rf /var/lib/apt/lists/*
EXPOSE 8787
CMD ["python3", "webui.py"]
