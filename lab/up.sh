#!/bin/sh
# 라우터 시작. runtime/rN 은 FRR 이 쓰는 폴더 (컨테이너가 소유) — 없거나 --reset 이면 기준 설정(configs/)으로 채운다
set -e
cd "$(dirname "$0")"
for r in r1 r2 r3 r4 r5 r6; do
  if [ "$1" = "--reset" ] || [ ! -f runtime/$r/frr.conf ]; then
    sudo rm -rf runtime/$r
    mkdir -p runtime/$r
    cp configs/$r/frr.conf configs/$r/vtysh.conf runtime/$r/
  fi
done
docker compose up -d ${1:+--force-recreate}
sleep 5
sh traffic.sh
