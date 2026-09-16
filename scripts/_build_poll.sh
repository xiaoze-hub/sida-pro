#!/bin/bash
# 轮询 build 直到完成或超时
for i in $(seq 1 60); do
  if grep -q "BUILD_BG_DONE" /tmp/build_v066.log 2>/dev/null; then
    echo "DONE"
    tail -15 /tmp/build_v066.log
    exit 0
  fi
  if grep -q "DOCKER_BUILD_EXIT=" /tmp/build_v066.log 2>/dev/null; then
    echo "EXIT_SEEN"
    tail -20 /tmp/build_v066.log
    exit 0
  fi
  # show progress hint
  LAST=$(tail -1 /tmp/build_v066.log 2>/dev/null | tr -d '\r' | tail -c 120)
  echo "[$i] $LAST"
  sleep 20
done
echo "POLL_TIMEOUT"
tail -20 /tmp/build_v066.log
exit 1
