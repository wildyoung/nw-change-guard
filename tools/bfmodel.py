"""Batfish 로 설정 파일만 보고 망 상태를 계산한다 (실제 라우터에 손대지 않음).

Batfish 는 FRR 설정을 Cumulus 형식(호스트 이름 + /etc/network/interfaces + frr.conf 를 이은 파일)으로 읽는다.
링크 주소는 Docker 가 붙이므로 frr.conf 에 없다 → compose.yml 과 같은 규칙으로 interfaces 부분을 만든다.
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path

from pybatfish.client.session import Session
from pybatfish.datamodel import HeaderConstraints

from nwguard import LINKS, LOOPBACK, ROUTERS, SERVICES, evaluate, ping_plan, running_config

logging.getLogger("pybatfish").setLevel(logging.WARNING)
NETWORK = "nwlab"


def link_addresses() -> dict[str, list[tuple[str, str]]]:
    """라우터 → [(인터페이스, 주소/29)] — 링크 10.0.XY.0/29, 번호가 작은 쪽 .2 · 큰 쪽 .3"""
    out: dict[str, list[tuple[str, str]]] = {r: [] for r in ROUTERS}
    for link in LINKS:
        a, b = link.split("-")
        net = f"10.0.{a[1]}{b[1]}"
        out[a].append((f"to-{b}", f"{net}.2/29"))
        out[b].append((f"to-{a}", f"{net}.3/29"))
    return out


def cumulus_file(router: str, frr_conf: str) -> str:
    ifaces = ["# This file describes the network interfaces available on your system", "", "auto lo", "iface lo inet loopback", f"    address {LOOPBACK[router]}/32", ""]
    if router in SERVICES:
        ip = SERVICES[router].replace(".0/24", ".1/24")
        ifaces += ["auto svc", "iface svc", f"    address {ip}", ""]
    for name, addr in link_addresses()[router]:
        ifaces += [f"auto {name}", f"iface {name}", f"    address {addr}", ""]
    frr = frr_conf
    if not re.search(r"^frr version", frr, re.M):
        frr = "frr version 10.2.1\n" + frr
    return "\n".join([router, *ifaces, "# ports.conf --", "", frr, ""])


def build_snapshot(dst: Path, configs: dict[str, str]) -> None:
    (dst / "configs").mkdir(parents=True, exist_ok=True)
    for r, conf in configs.items():
        (dst / "configs" / f"{r}.cfg").write_text(cumulus_file(r, conf))


class Model:
    def __init__(self, host: str = "localhost"):
        self.bf = Session(host=host)
        self.bf.set_network(NETWORK)

    def load(self, name: str, configs: dict[str, str]) -> None:
        with tempfile.TemporaryDirectory() as d:
            build_snapshot(Path(d), configs)
            self.bf.init_snapshot(d, name=name, overwrite=True)

    def issues(self, name: str) -> list[str]:
        """파싱하지 못한 줄 · 경고 — 모델이 설정을 다 이해했는지"""
        out = []
        ps = self.bf.q.fileParseStatus().answer(snapshot=name).frame()
        for _, row in ps.iterrows():
            if row["Status"] != "PASSED":
                out.append(f"{row['File_Name']}: {row['Status']}")
        ii = self.bf.q.initIssues().answer(snapshot=name).frame()
        for _, row in ii.iterrows():
            nodes = ",".join(row["Nodes"] or [])
            text = str(row.get("Line_Text") or row.get("Details") or "")
            out.append(f"{nodes} {row['Type']}: {text.strip()[:120]}")
        return out

    def snapshot(self, name: str) -> dict:
        """nwguard.snapshot() 과 같은 모양 — 같은 점검(evaluate)을 그대로 쓴다"""
        bf = self.bf
        routers = {r: {"alive": True, "neighbors": {}, "routes": {}} for r in ROUTERS}

        sess = bf.q.ospfSessionCompatibility().answer(snapshot=name).frame()
        for _, row in sess.iterrows():
            a, b = row["Interface"].hostname, row["Remote_Interface"].hostname
            state = "Full" if row["Session_Status"] == "ESTABLISHED" else str(row["Session_Status"])
            routers[a]["neighbors"][b] = {"state": state, "iface": row["Interface"].interface}

        rt = bf.q.routes(protocols="ospf").answer(snapshot=name).frame()
        for _, row in rt.iterrows():
            r = row["Node"]
            nh = row["Next_Hop"]
            ip = getattr(nh, "ip", None)
            hop = str(getattr(nh, "interface", "?")) + (f" via {ip}" if ip else "")
            e = routers[r]["routes"].setdefault(row["Network"], {"metric": int(row["Metric"]), "nexthops": []})
            e["nexthops"] = sorted(e["nexthops"] + [hop])

        pings = []
        for src_router, src, dst in ping_plan():
            ans = bf.q.bidirectionalTraceroute(
                startLocation=src_router, headers=HeaderConstraints(srcIps=src, dstIps=dst, ipProtocols=["ICMP"])
            ).answer(snapshot=name).frame()
            ok = len(ans) > 0 and all(
                t.disposition == "ACCEPTED"
                for _, row in ans.iterrows()
                for t in list(row["Forward_Traces"]) + list(row["Reverse_Traces"] or [])
            ) and all(len(row["Reverse_Traces"] or []) > 0 for _, row in ans.iterrows())
            pings.append({"from": src_router, "src": src, "dst": dst, "ok": ok, "received": 3 if ok else 0})
        return {"time": name, "routers": routers, "pings": pings}


def prevalidate(router: str, candidate: str, allow_down=frozenset()) -> dict:
    """지금 돌고 있는 설정(base)과 router 만 후보로 바꾼 설정(candidate)을 계산해 비교한다."""
    live = {r: running_config(r) for r in ROUTERS}
    m = Model()
    m.load("base", live)
    m.load("candidate", {**live, router: candidate})
    base, cand = m.snapshot("base"), m.snapshot("candidate")
    # 모델이 읽지 못한 줄이 후보에서 새로 생겼으면 예측을 믿기 어렵다
    new_issues = sorted(set(m.issues("candidate")) - set(m.issues("base")))
    return {"results": evaluate(cand, pre=base, allow_down=allow_down), "new_issues": new_issues, "model": cand}
