#!/bin/sh
# 발표용 시연: 지금 상태 점검 → "안 쓰는 줄 정리" 변경 → 탐지 · 자동 롤백 → 변경 기록
cd "$(dirname "$0")"
g=".venv/bin/python tools/nwguard.py"
step() { printf '\n\033[1;36m━━ %s ━━\033[0m\n' "$1"; sleep 2; }

step "지금 망 상태"
$g check

step "변경 요청: r5 에서 '안 쓰는 줄' 하나 삭제"
diff lab/configs/r5/frr.conf changes/scenarios/03-service-withdraw-r5.conf
sleep 3
$g change r5 changes/scenarios/03-service-withdraw-r5.conf --reason "시연: 안 쓰는 줄 정리"

step "변경 기록"
$g history
