#!/bin/bash
for i in $(seq 1 90); do
  if grep -q "BUILD_BG_DONE" /tmp/build_v067.log 2>/dev/null; then
    echo DONE
    tail -20 /tmp/build_v067.log
    exit 0
  fi
  LAST=$(tail -1 /tmp/build_v067.log 2>/dev/null | tr -d '\r' | tail -c 100)
  echo "[$i] $LAST"
  sleep 15
done
echo TIMEOUT
tail -20 /tmp/build_v067.log
exit 1
