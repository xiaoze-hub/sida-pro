#!/bin/bash
docker logs panwatch --since 30m 2>&1 | grep -A 40 -E "theme.mood|theme_mood|ThemeMood" | tail -80
echo "==== direct exec traceback ===="
TOKEN=$(cat /mnt/c/Users/tianxiang/sida-work/data/admin_token.txt | tr -d '\r\n')
curl -sS -o /dev/null "http://127.0.0.1:8000/api/theme-mood/ladder?window=20" -H "Authorization: Bearer $TOKEN"
sleep 1
docker logs panwatch --since 2m 2>&1 | tail -60
