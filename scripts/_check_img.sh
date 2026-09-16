#!/bin/bash
echo "=== running ==="
docker ps --filter name=panwatch --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
echo
echo "=== VERSION in container ==="
docker exec panwatch cat /app/VERSION
echo
echo "=== images ==="
docker images --format "table {{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.CreatedSince}}\t{{.Size}}" | grep -E "panwatch|stock-intelligent|thsdk" | head -10
echo
echo "=== inspect image of panwatch ==="
docker inspect panwatch --format 'Image={{.Config.Image}} ImageID={{.Image}}'
echo
echo "=== theme_mood has ohlc batch fix? ==="
docker exec panwatch grep -c "BETWEEN :d0" /app/src/web/api/theme_mood.py || true
echo "=== ladder sticky? (frontend is static, not in py) ==="
docker exec panwatch grep -c "sticky left-0" /app/static/assets/*.js 2>/dev/null | grep -v ":0" | head -3 || true
