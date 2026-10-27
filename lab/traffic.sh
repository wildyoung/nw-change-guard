#!/bin/sh
# 고객(r6) ↔ 데이터센터(r5) 사이에 계속 흐르는 트래픽 — 대시보드에서 어느 링크로 가는지 보이게 (약 1 Mbps)
for r in r6; do docker exec "lab-$r-1" pkill -f 'ping -i 0.01' 2>/dev/null; done
docker exec -d lab-r6-1 ping -q -i 0.01 -s 1200 -I 192.168.60.1 192.168.50.1
echo "트래픽 시작: r6 192.168.60.1 → r5 192.168.50.1"
