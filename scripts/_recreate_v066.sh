#!/bin/bash
# 备份容器 → 用 panwatch:v0.6.6 重建(保留 env/volumes/network)
set -euo pipefail
CT=panwatch
IMG=panwatch:v0.6.6
echo "=== 1) inspect current ==="
docker inspect $CT --format 'Image={{.Config.Image}}'
docker inspect $CT --format 'Restart={{.HostConfig.RestartPolicy.Name}}'
docker inspect $CT --format 'Ports={{json .HostConfig.PortBindings}}'
docker inspect $CT --format 'Networks={{json .NetworkSettings.Networks}}' | head -c 400; echo
docker inspect $CT --format 'Binds={{json .HostConfig.Binds}}'
docker inspect $CT --format 'Memory={{.HostConfig.Memory}} Cpus={{.HostConfig.NanoCpus}}'
# env (redact values length only for secrets-like keys)
docker inspect $CT --format '{{range .Config.Env}}{{println .}}{{end}}' | sed 's/\(PASSWORD\|TOKEN\|SECRET\|KEY\)=.*/\1=***/' 

echo "=== 2) backup /app code + TQ private files ==="
BAK=/root/app_backup_pre_image_v066_$(date +%Y%m%d_%H%M%S).tar.gz
docker exec -u root $CT sh -c "cd /app && tar czf /tmp/bak.tgz --exclude=data --exclude=static-data --exclude=downloads --exclude=node_modules --exclude=__pycache__ ."
docker cp $CT:/tmp/bak.tgz $BAK
docker exec -u root $CT rm -f /tmp/bak.tgz
ls -lh $BAK

# 抽出可能的 TQ 私有文件以防新镜像丢失
mkdir -p /root/tq_private_backup
for f in \
  /usr/local/lib/python3.11/site-packages/marketdata/vendors/tq.py \
  /usr/local/lib/python3.11/site-packages/marketdata/registry.py \
  /app/packages/marketdata/src/marketdata/vendors/tq.py \
  /app/packages/marketdata/src/marketdata/registry.py
do
  if docker exec $CT test -f "$f" 2>/dev/null; then
    base=$(echo "$f" | tr '/' '_')
    docker cp "$CT:$f" "/root/tq_private_backup/$base" 2>/dev/null || true
    echo "saved $f"
  fi
done
ls -la /root/tq_private_backup | head

echo "=== 3) recreate with new image ==="
# dump env to file
docker inspect $CT --format '{{range .Config.Env}}{{println .}}{{end}}' > /tmp/panwatch_env.txt
# network name
NET=$(docker inspect $CT --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' | head -1)
echo "NET=$NET"

docker stop $CT
docker rm $CT

# build env args
ENVARGS=()
while IFS= read -r line; do
  [ -z "$line" ] && continue
  ENVARGS+=("-e" "$line")
done < /tmp/panwatch_env.txt

docker run -d --name $CT \
  --restart=unless-stopped \
  -p 8000:8000 \
  --network "$NET" \
  -v panwatch_data:/app/data \
  -v panwatch_tck:/app/data/tck \
  "${ENVARGS[@]}" \
  $IMG

echo "=== 4) wait healthy ==="
for i in $(seq 1 36); do
  ST=$(docker inspect --format '{{.State.Health.Status}}' $CT 2>/dev/null || echo unknown)
  echo "[$i] $ST"
  [ "$ST" = "healthy" ] && break
  sleep 5
done

echo "=== 5) verify ==="
docker exec $CT cat /app/VERSION
docker inspect $CT --format 'Image={{.Config.Image}}'
curl -sS http://127.0.0.1:8000/api/version; echo
curl -sS -o /dev/null -w "health=%{http_code}\n" http://127.0.0.1:8000/api/health
# thsdk still present?
docker exec $CT python -c "import thsdk; print('thsdk ok')"
# theme-mood board
TOKEN=$(cat /mnt/c/Users/tianxiang/sida-work/data/admin_token.txt | tr -d '\r\n')
curl -sS -o /tmp/tm.json -H "Authorization: Bearer $TOKEN" -w "board=%{http_code}\n" \
  "http://127.0.0.1:8000/api/theme-mood/board?window=20&top=5"
echo RECREATE_DONE
