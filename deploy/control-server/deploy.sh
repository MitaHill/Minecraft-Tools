#!/bin/sh
# 切换 Control Server 镜像标签，健康检查失败时退回上一版。
# 用法：./deploy.sh 2026-11-26-2 [--no-rollback]
# --no-rollback 用于单向数据库迁移：失败时保留新镜像并提示恢复备份，避免旧镜像读到不兼容的库。
set -eu

tag=${1:?usage: deploy.sh <image-tag> [--no-rollback]}
rollback=yes
case ${2:-} in
    "") ;;
    --no-rollback) rollback=no ;;
    *) echo "invalid option: $2" >&2; exit 2 ;;
esac
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

case $tag in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]-[1-9]*) ;;
    *) echo "invalid image tag: $tag" >&2; exit 2 ;;
esac

container=minecrafttools-integration-control-1
image=kindmitaishere/minecrafttools-control
compose="docker compose -p minecrafttools-integration --env-file deploy.env"
files="-f compose.yaml -f compose.node-hub.yaml -f node-https.yaml -f compose.releases.yaml -f public-control.yaml"
previous=$(sed -n 's/^CONTROL_IMAGE_TAG=//p' deploy.env)

docker pull "$image:$tag"
sed -i "s/^CONTROL_IMAGE_TAG=.*/CONTROL_IMAGE_TAG=$tag/" deploy.env
$compose $files up -d --no-build control

status=
for _ in $(seq 1 30); do
    status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || true)
    [ "$status" = healthy ] && break
    [ "$status" = unhealthy ] && break
    sleep 2
done

if [ "$status" = healthy ]; then
    echo "control is healthy on $tag"
    docker image prune -f >/dev/null
    exit 0
fi

echo "health check failed on $tag" >&2
$compose $files logs --tail 50 --no-color control >&2 || true
if [ "$rollback" = yes ] && [ -n "$previous" ] && [ "$previous" != "$tag" ]; then
    echo "rolling back to $previous" >&2
    sed -i "s/^CONTROL_IMAGE_TAG=.*/CONTROL_IMAGE_TAG=$previous/" deploy.env
    $compose $files up -d --no-build control
else
    echo "rollback disabled; restore the SQLite backup before reverting to $previous" >&2
fi
exit 1
