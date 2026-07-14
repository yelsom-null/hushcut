FROM python:3.12-slim

# ffmpeg for muting; curl/unzip to fetch deno (yt-dlp's YouTube extractor needs a JS runtime)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/*

# deno (x86_64 — use deno-aarch64-unknown-linux-gnu.zip on ARM)
RUN curl -fsSL https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip -o /tmp/deno.zip \
    && unzip /tmp/deno.zip -d /usr/local/bin \
    && rm /tmp/deno.zip \
    && chmod +x /usr/local/bin/deno

# bgutil-ytdlp-pot-provider is the yt-dlp plugin side of the PO token
# provider (see the bgutil-provider service in docker-compose.yml)
RUN pip install --no-cache-dir --upgrade yt-dlp pyyaml bgutil-ytdlp-pot-provider

WORKDIR /app
COPY server/main.py /app/main.py

EXPOSE 8788
CMD ["python", "-u", "/app/main.py"]
