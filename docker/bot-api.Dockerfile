# Сборка из официального исходного кода Telegram, без стороннего образа сервера.
FROM debian:bookworm-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git cmake g++ make gperf libssl-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
ARG BOT_API_REF=master
RUN git clone --recursive --depth 1 --shallow-submodules --branch "$BOT_API_REF" \
    https://github.com/tdlib/telegram-bot-api.git /src
RUN cmake -S /src -B /build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/out \
    && cmake --build /build --target install -j2

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libssl3 zlib1g libstdc++6 libatomic1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /out/bin/telegram-bot-api /usr/local/bin/telegram-bot-api
WORKDIR /var/lib/telegram-bot-api
ENTRYPOINT ["telegram-bot-api"]
CMD ["--local", "--http-port=8081", "--dir=/var/lib/telegram-bot-api", "--temp-dir=/tmp"]
