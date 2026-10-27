# 설정 변경 안전장치 (nw-change-guard)

라우터 설정을 바꾸기 **전에** 영향을 계산하고, 바꾼 **뒤** 자동으로 점검해서, 이상이 있으면 **이전 설정으로 되돌린다.**
장비 없이 클라우드 VM 한 대에 가상 백본(FRR 라우터 6대)을 띄워 만들었다.

> 2021년 전국 인터넷 장애는 라우팅 설정 한 줄에서 시작됐다.
> 사람이 아무리 조심해도 오타와 착오는 생긴다 — 그래서 **절차를 도구로** 만든다.

## 망 구성

```
            r5 (데이터센터 192.168.50.0/24)
           /       \
         r1 ------- r2        r1 ~ r4 백본 (OSPF, 경로 이중화 + r2-r4 대각선)
         |   ` . .  |         r6 기업 고객 192.168.60.0/24
         r4 ------- r3        링크 9개 · hello 1초 / dead 4초
           \       /
            r6 (기업 고객)
```

- 라우터: FRR 10.2 컨테이너 6대, 링크마다 Docker 네트워크 하나 (`lab/compose.yml`)
- 고객 ↔ 데이터센터 트래픽 약 1 Mbps가 계속 흐른다 (`lab/traffic.sh`)

## 변경 절차 (`tools/nwguard.py change`)

| 단계 | 하는 일 | 실패하면 |
| --- | --- | --- |
| 1. 변경 전 점검 | 망이 이미 정상인지 | 시작하지 않음 |
| 2. 문법 검사 | FRR 로 후보 설정 검사 · 실제로 바뀌는 줄 표시 | 거부 |
| 2-1. 사전 검증 | **Batfish** 가 설정 파일만으로 변경 뒤 망을 계산 (라우터는 그대로) | 거부 |
| 3. 적용 · 변경 후 점검 | 수렴을 기다린 뒤 라우터 응답 · OSPF 이웃 · 경로 · **실제 ping** · 이중화 | 자동 롤백 |
| 4. 롤백 · 재점검 | 변경 직전 **돌고 있던** 설정으로 되돌리고 다시 점검 | 사람이 확인 |

모든 변경은 `changes/records/` 에 이전 · 후보 설정, 바뀐 줄, 예측, 점검 결과, 영향 시간이 남는다.

## 결과

| 시나리오 (`changes/scenarios/`) | 결과 |
| --- | --- |
| 01 링크 비용 조정 | 적용 — 경로 영향 8건 표시 |
| 02 `network` 오타 (/16 → /24) | **사전 검증에서 거부** (링크 4개 단절 예측) · 강제 적용 시 탐지 → 롤백, 영향 약 23초 |
| 03 "안 쓰는 줄" 삭제 | **이웃은 모두 정상인데 서비스만 끊김** → 경로 · ping 점검이 잡음 → 거부 / 롤백 |
| 04 문법 오류 | 적용 전 거부 |
| 05 계획된 링크 차단 | 적용 — 계획된 단절은 경고로만 |

**Batfish 예측 vs 실제** (`tools/accuracy.py`): 점검 항목 **12/12**, 경로 표 **283/283** 일치 (비용 · 다음 홉까지).

## 모니터링

Prometheus + Grafana (`monitoring/`). 지표 수집기 `tools/exporter.py` 가 5초마다 모은다.

- 경보: 라우터 다운 · 링크 다운 · 서비스 도달 실패 · 에지 단일 경로 · 수집기 다운
- 대시보드: 링크 · 서비스 상태 흐름, 링크별 트래픽(장애 때 우회 경로로 옮겨 가는 모습), 서비스까지 OSPF 비용, 설정 변경 시각(보라색 점선), 롤백 영향 시간

링크 r4–r6 을 끊었을 때: `LinkDown` + `EdgeSingleHomed r6` 경보, 트래픽 r6→r4→r2→r5 에서 r6→r3→r2→r5 로, 서비스는 유지.

## 만들면서 찾은 것

1. **FRR 적용 도구는 적용 직후 설정 파일을 덮어쓴다** → "이전 파일을 다시 적용"하는 롤백은 새 설정을 다시 넣을 뿐이었다. 롤백 기준을 변경 직전 *실행 중인 설정*으로 바꿨다.
2. **롤백 파일 머리 줄 하나 때문에 파일 전체가 거부됐다** — 그대로 진행됐다면 라우터 설정을 모두 지울 뻔했다. 롤백 적용 자체가 실패하면 멈추고 사람을 부른다.
3. **경로는 있는데 패킷이 사라졌다** — 리눅스 역경로 검사(rp_filter)가 비대칭 경로를 버렸다. 경로 표만 보는 점검으로는 못 찾고, 실제 ping 이 잡았다.
4. **Batfish 는 FRR 설정 일부(`ip ospf passive`, 타이머)를 읽지 못한다.** 이 망에서는 결과가 같음을 실제 망과 비교해 확인했고, 후보에서 모델이 못 읽는 줄이 새로 생기면 경고한다. 모델은 판단 자료일 뿐 — 적용 후 실제 점검은 계속 필요하다.

## 폴더

```
lab/          가상 망: compose.yml · configs/(기준 설정, 이 도구만 바꾼다) · up.sh · traffic.sh
tools/        nwguard.py (점검 · 변경 · 롤백) · bfmodel.py (Batfish 사전 검증) · accuracy.py · exporter.py · bfprobe.py
changes/      scenarios/ (시연 변경 5개) · records/ (실행 기록, git 밖)
monitoring/   Prometheus · Grafana 설정 · install.sh · promq.py
demo.sh       시연 (VM) · demo.ps1 (Windows 에서 VM 시연 실행)
```

## 실행

Ubuntu 24.04 VM (2 vCPU · 8 GB), 이 폴더를 `/opt/nwlab` 에 둔다.

```sh
# 한 번
sudo apt-get install -y docker.io docker-compose-v2 python3-venv && sudo usermod -aG docker $USER
python3 -m venv .venv && .venv/bin/pip install pybatfish
docker run -d --name batfish --restart unless-stopped -p 127.0.0.1:9996:9996 -p 127.0.0.1:9997:9997 batfish/batfish:latest
sh monitoring/install.sh          # 지표 수집기 서비스 + Prometheus · Grafana

# 망
sh lab/up.sh [--reset]            # --reset: 기준 설정으로 다시 시작 (수렴 약 20초)
.venv/bin/python tools/nwguard.py check
.venv/bin/python tools/nwguard.py change r5 changes/scenarios/03-service-withdraw-r5.conf --reason "..."
#   --allow-down r1-r4   계획된 링크 단절   --apply-anyway   예측이 장애여도 적용(예측 확인용)
.venv/bin/python tools/nwguard.py history
.venv/bin/python tools/accuracy.py
```

Grafana 는 VM 의 localhost 에서만 열린다 → SSH 터널 `-L 3000:127.0.0.1:3000` 후 http://localhost:3000
