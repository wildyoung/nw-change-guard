#!/usr/bin/env python3
"""가상 망 지표를 Prometheus 형식으로 내보낸다 — http://127.0.0.1:9105/metrics

5초마다 라우터(OSPF 이웃 · 경로 · 인터페이스 바이트)와 서비스 ping 을 모으고, 변경 기록(changes/records)을 센다.
점검 기준(LINKS · SERVICES …)은 nwguard 와 같은 것을 쓴다.
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nwguard import LINKS, LOOPBACK, RECORDS, ROUTERS, SERVICES, collect_router, ping_plan, sh

PORT = 9105
INTERVAL = 5

# 변경 결과 → 짧은 이름 (result.json 의 status 앞부분)
STATUS = [
    ("적용 완료", "applied"),
    ("롤백 완료", "rolled_back"),
    ("거부 — 문법", "rejected_syntax"),
    ("거부 — 사전", "rejected_prevalidate"),
    ("중단", "aborted"),
    ("롤백", "rollback_failed"),  # 롤백 실패 · 롤백 후에도 실패
    ("실패", "failed_no_rollback"),
]


def esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


class Metrics:
    def __init__(self):
        self.lines: list[str] = []
        self.meta: dict[str, tuple[str, str]] = {}

    def add(self, name: str, value, kind: str = "gauge", help_: str = "", **labels):
        self.meta.setdefault(name, (kind, help_))
        lab = ",".join(f'{k}="{esc(v)}"' for k, v in labels.items())
        self.lines.append(f"{name}{{{lab}}} {value}")

    def render(self) -> str:
        out, seen = [], set()
        for line in self.lines:
            name = line.split("{", 1)[0]
            if name not in seen:
                seen.add(name)
                kind, help_ = self.meta[name]
                out += [f"# HELP {name} {help_}", f"# TYPE {name} {kind}"]
            out.append(line)
        return "\n".join(out) + "\n"


def iface_bytes(router: str) -> dict[str, tuple[int, int]]:
    out = sh(router, "sh", "-c", "for d in /sys/class/net/to-*; do echo $(basename $d) $(cat $d/statistics/rx_bytes) $(cat $d/statistics/tx_bytes); done")
    res = {}
    for line in out.stdout.splitlines():
        name, rx, tx = line.split()
        res[name] = (int(rx), int(tx))
    return res


def ping_once(src_router: str, src: str, dst: str) -> tuple[bool, float | None]:
    out = sh(src_router, "ping", "-c", "1", "-W", "1", "-I", src, dst)
    m = re.search(r"time=([\d.]+) ms", out.stdout)
    return (m is not None, float(m.group(1)) / 1000 if m else None)


def collect() -> str:
    t0 = time.monotonic()
    m = Metrics()
    with ThreadPoolExecutor(max_workers=16) as pool:
        routers = dict(zip(ROUTERS, pool.map(collect_router, ROUTERS)))
        counters = dict(zip(ROUTERS, pool.map(iface_bytes, ROUTERS)))
        plan = ping_plan()
        pings = list(pool.map(lambda p: ping_once(*p), plan))

    for r, st in routers.items():
        m.add("nwlab_router_up", int(st["alive"]), help_="라우터(FRR) 응답 1/0", router=r)
        if not st["alive"]:
            continue
        full = [n for n, v in st["neighbors"].items() if v["state"] == "Full"]
        m.add("nwlab_ospf_neighbors_full", len(full), help_="Full 상태 OSPF 이웃 수", router=r)
        m.add("nwlab_ospf_routes", len(st["routes"]), help_="설치된 OSPF 경로 수", router=r)
        for owner, prefix in SERVICES.items():
            if owner == r:
                continue
            route = st["routes"].get(prefix)
            m.add("nwlab_service_route_present", int(route is not None), help_="서비스 대역 경로 있음 1/0", router=r, service=owner)
            if route:
                m.add("nwlab_service_route_metric", route["metric"], help_="서비스 대역까지 OSPF 비용", router=r, service=owner)
                m.add("nwlab_service_route_paths", len(route["nexthops"]), help_="서비스 대역 경로 수(ECMP)", router=r, service=owner)

    for link in LINKS:
        a, b = link.split("-")
        up = all(routers[x]["alive"] and routers[x]["neighbors"].get(y, {}).get("state") == "Full" for x, y in ((a, b), (b, a)))
        m.add("nwlab_ospf_link_up", int(up), help_="링크 양쪽 OSPF 이웃이 Full 1/0", link=link)

    for r, ifaces in counters.items():
        for iface, (rx, tx) in ifaces.items():
            peer = iface.removeprefix("to-")
            link = "-".join(sorted([r, peer]))
            m.add("nwlab_iface_rx_bytes_total", rx, "counter", "인터페이스 수신 바이트", router=r, iface=iface, link=link)
            m.add("nwlab_iface_tx_bytes_total", tx, "counter", "인터페이스 송신 바이트", router=r, iface=iface, link=link)

    names = {v: k for k, v in LOOPBACK.items()}
    for (src_router, src, dst), (ok, rtt) in zip(plan, pings):
        dst_owner = next((o for o, p in SERVICES.items() if dst.startswith(p.rsplit(".", 1)[0])), names.get(dst, dst))
        m.add("nwlab_service_reachable", int(ok), help_="서비스 ping 응답 1/0", src=src_router, dst=dst_owner)
        if rtt is not None:
            m.add("nwlab_service_rtt_seconds", rtt, help_="서비스 ping 왕복 시간", src=src_router, dst=dst_owner)

    # 변경 기록
    counts = {name: 0 for _, name in STATUS}
    last = None
    recent = []
    for d in sorted(RECORDS.glob("*")):
        f = d / "result.json"
        if not f.exists():
            continue
        rec = json.loads(f.read_text())
        name = next((n for prefix, n in STATUS if rec.get("status", "").startswith(prefix)), "other")
        counts[name] = counts.get(name, 0) + 1
        last = (rec, name, f.stat().st_mtime)
        recent.append((d.name, rec, name, f.stat().st_mtime))
    # 대시보드 주석(보라색 표시)용: 최근 변경마다 끝난 시각
    for rid, rec, name, ts in recent[-20:]:
        m.add("nwguard_change_end_timestamp_seconds", ts, help_="변경이 끝난 시각 (최근 20건)", id=rid, router=rec["router"], result=name, reason=(rec.get("reason") or "")[:40])
    for name, n in counts.items():
        m.add("nwguard_changes_total", n, "counter", "설정 변경 시도 수 (결과별)", result=name)
    if last:
        rec, name, ts = last
        m.add("nwguard_last_change_timestamp_seconds", ts, help_="마지막 변경이 끝난 시각", router=rec["router"], result=name)
        if "impact_sec" in rec:
            m.add("nwguard_last_impact_seconds", rec["impact_sec"], help_="마지막 롤백의 영향 시간(적용 → 복구 확인)", router=rec["router"])

    m.add("nwlab_collect_seconds", round(time.monotonic() - t0, 3), help_="지표를 모으는 데 걸린 시간")
    return m.render()


class State:
    body = "# 아직 모으는 중\n"


def loop():
    while True:
        start = time.monotonic()
        try:
            State.body = collect()
        except Exception as e:  # 한 번 실패해도 계속 돈다 — 다음 수집에서 회복
            State.body = f"# 수집 오류: {e}\nnwlab_collect_error 1\n"
        time.sleep(max(0, INTERVAL - (time.monotonic() - start)))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = State.body.encode()
        self.send_response(200 if self.path.startswith("/metrics") else 404)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
