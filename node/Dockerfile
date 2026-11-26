FROM golang:1.26-bookworm AS build
ARG VERSION=dev
WORKDIR /node
COPY go.mod go.sum *.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags "-X main.version=${VERSION}" -o /frp-agent .

FROM fatedier/frps:v0.72.0 AS upstream-frps

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates nftables iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 node \
    && useradd --uid 10001 --gid 10001 --no-create-home node \
    && install -d -o node -g node /data \
    && install -d -m 0700 -o root -g root /var/lib/minecrafttools-firewall \
    && install -d -m 0750 -o root -g node /run/minecrafttools
COPY --from=build /frp-agent /usr/local/bin/frp-agent
COPY --from=upstream-frps /usr/bin/frps /usr/local/bin/frps
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/frp-agent"]
CMD ["agent"]
