#!/usr/bin/env python3
"""설정 변경 안전장치 — 변경 전 점검 → 문법 검사 → 사전 검증(Batfish) → 적용 → 변경 후 점검 → 실패하면 자동 롤백.

  nwguard.py check                         지금 망이 정상인지 점검
  nwguard.py change r1 new.conf [--allow-down r1-r4] [--no-rollback] [--reason "..."]
                                [--skip-prevalidate] [--apply-anyway]
  nwguard.py history                       지난 변경 기록

라우터는 lab/compose.yml 의 FRR 컨테이너 (lab-rN-1). 점검은 표준 라이브러리만,
사전 검증은 pybatfish (.venv) + Batfish 서버(컨테이너 batfish) — 없으면 건너뛴다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIGS = ROOT / "lab" / "configs"  # 기준 설정 (git) — 변경이 통과했을 때만 이 도구가 바꾼다
RUNTIME = ROOT / "lab" / "runtime"  # 컨테이너의 /etc/frr (FRR 이 frr.conf 를 직접 다시 씀)
RECORDS = ROOT / "changes" / "records"

# 망 설계 (compose.yml 과 같아야 한다) — 점검 기준
ROUTERS = ["r1", "r2", "r3", "r4", "r5", "r6"]
LINKS = ["r1-r2", "r1-r4", "r1-r5", "r2-r3", "r2-r4", "r2-r5", "r3-r4", "r3-r6", "r4-r6"]
LOOPBACK = {r: f"10.255.0.{r[1:]}" for r in ROUTERS}
SERVICES = {"r5": "192.168.50.0/24", "r6": "192.168.60.0/24"}  # 데이터센터 · 기업 고객
SERVICE_IP = {"r5": "192.168.50.1", "r6": "192.168.60.1"}
EDGES = ["r5", "r6"]  # 이중화(업링크 2개)를 지켜야 하는 라우터

DEAD_INTERVAL = 4  # frr.conf 의 ip ospf dead-interval
CONVERGE_MAX = 30


# ── 라우터에 묻기 ────────────────────────────────────────────


def sh(router: str, *cmd: str, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", f"lab-{router}-1", *cmd], capture_output=True, text=True, timeout=timeout
    )


def vtysh_json(router: str, command: str):
    out = sh(router, "vtysh", "-c", command)
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def running_config(router: str) -> str:
    # 머리 줄(Building configuration… · Current configuration:)이 남으면 frr-reload 가 파일 전체를 거부한다
    return "".join(
        l
        for l in sh(router, "vtysh", "-c", "show running-config").stdout.splitlines(keepends=True)
        if not l.startswith(("Building configuration", "Current configuration"))
    ).lstrip()


def collect_router(r: str) -> dict:
    nbrs = vtysh_json(r, "show ip ospf neighbor json")
    routes = vtysh_json(r, "show ip route ospf json")
    if nbrs is None or routes is None:
        return {"alive": False}
    rid_to_router = {v: k for k, v in LOOPBACK.items()}
    neighbors = {}
    for rid, entries in nbrs.get("neighbors", {}).items():
        for e in entries:
            peer = rid_to_router.get(rid, rid)
            neighbors[peer] = {"state": e.get("converged") or e.get("nbrState", "?"), "iface": e["ifaceName"].split(":")[0]}
    table = {}
    for prefix, entries in routes.items():
        for e in entries:
            if not e.get("selected"):
                continue
            hops = sorted(
                f"{h.get('interfaceName', '?')}" + (f" via {h['ip']}" if "ip" in h else "")
                for h in e.get("nexthops", [])
                if h.get("active")
            )
            table[prefix] = {"metric": e.get("metric"), "nexthops": hops}
    return {"alive": True, "neighbors": neighbors, "routes": table}


def ping(src_router: str, src_ip: str, dst_ip: str) -> dict:
    out = sh(src_router, "ping", "-c", "3", "-i", "0.2", "-W", "1", "-I", src_ip, dst_ip)
    received = 0
    for line in out.stdout.splitlines():
        if "packets received" in line:
            received = int(line.split(",")[1].split()[0])
    return {"from": src_router, "src": src_ip, "dst": dst_ip, "ok": received == 3, "received": received}


def ping_plan() -> list[tuple[str, str, str]]:
    plan = []
    for r in ["r1", "r2", "r3", "r4"]:
        for svc in SERVICE_IP.values():
            plan.append((r, LOOPBACK[r], svc))
    plan.append(("r6", SERVICE_IP["r6"], SERVICE_IP["r5"]))  # 고객 → 데이터센터
    plan.append(("r5", SERVICE_IP["r5"], SERVICE_IP["r6"]))
    return plan


def snapshot(with_pings: bool = True) -> dict:
    with ThreadPoolExecutor(max_workers=12) as pool:
        routers = dict(zip(ROUTERS, pool.map(collect_router, ROUTERS)))
        pings = list(pool.map(lambda p: ping(*p), ping_plan())) if with_pings else []
    return {"time": datetime.now().isoformat(timespec="seconds"), "routers": routers, "pings": pings}


def digest(snap: dict) -> str:
    return hashlib.sha1(json.dumps(snap["routers"], sort_keys=True).encode()).hexdigest()


def wait_converged() -> tuple[dict, float]:
    """적용 뒤 dead-interval 만큼 기다리고, 이웃 · 경로가 두 번 연속 같으면 수렴으로 본다."""
    start = time.monotonic()
    time.sleep(DEAD_INTERVAL + 1)
    prev = None
    while time.monotonic() - start < CONVERGE_MAX:
        cur = digest(snapshot(with_pings=False))
        if cur == prev:
            break
        prev = cur
        time.sleep(1)
    return snapshot(), time.monotonic() - start


# ── 점검 ────────────────────────────────────────────────────


def evaluate(snap: dict, pre: dict | None = None, allow_down: set[str] = frozenset()) -> list[dict]:
    """점검 결과 목록. level = FAIL (롤백 사유) · WARN · INFO"""
    res = []

    def add(level, name, detail=""):
        res.append({"level": level, "check": name, "detail": detail})

    routers = snap["routers"]
    dead = [r for r in ROUTERS if not routers[r]["alive"]]
    if dead:
        add("FAIL", "라우터 응답", ", ".join(dead) + " 응답 없음")
    else:
        add("OK", "라우터 응답", f"{len(ROUTERS)}대 모두 응답")

    # 링크마다 양쪽 OSPF 이웃이 Full 인가
    down = []
    for link in LINKS:
        a, b = link.split("-")
        ok = all(
            routers[x]["alive"] and routers[x]["neighbors"].get(y, {}).get("state") == "Full"
            for x, y in ((a, b), (b, a))
        )
        if not ok:
            down.append(link)
    unexpected = [l for l in down if l not in allow_down]
    if unexpected:
        add("FAIL", "OSPF 이웃", "끊긴 링크: " + ", ".join(unexpected))
    elif down:
        add("WARN", "OSPF 이웃", "계획된 단절만: " + ", ".join(down))
    else:
        add("OK", "OSPF 이웃", f"링크 {len(LINKS)}개 모두 Full")

    # 모든 라우터가 다른 라우터의 loopback 과 서비스 대역 경로를 갖고 있는가
    missing = []
    for r in ROUTERS:
        if not routers[r]["alive"]:
            continue
        want = [f"{LOOPBACK[o]}/32" for o in ROUTERS if o != r] + [p for o, p in SERVICES.items() if o != r]
        lost = [p for p in want if p not in routers[r]["routes"]]
        if lost:
            missing.append(f"{r}: {', '.join(lost)}")
    if missing:
        add("FAIL", "경로", " / ".join(missing))
    else:
        add("OK", "경로", "모든 라우터가 loopback · 서비스 대역 경로 보유")

    # 서비스 도달 (실제 패킷)
    bad = [f"{p['from']}({p['src']})→{p['dst']} {p['received']}/3" for p in snap["pings"] if not p["ok"]]
    if bad:
        add("FAIL", "서비스 도달", ", ".join(bad))
    else:
        add("OK", "서비스 도달", f"ping {len(snap['pings'])}개 경로 모두 응답")

    # 에지 이중화: 업링크 하나만 남으면 다음 장애에 서비스가 끊긴다
    for e in EDGES:
        if routers[e]["alive"]:
            full = [n for n, v in routers[e]["neighbors"].items() if v["state"] == "Full"]
            if len(full) < 2:
                add("WARN", "이중화", f"{e} 업링크 {len(full)}개 — 단일 경로")

    # 영향도: 변경 전과 비교해 바뀐 경로 (판정에는 안 씀)
    if pre:
        changes = []
        for r in ROUTERS:
            if not (routers[r]["alive"] and pre["routers"][r]["alive"]):
                continue
            before, after = pre["routers"][r]["routes"], routers[r]["routes"]
            for p in sorted(set(before) | set(after)):
                b, a = before.get(p), after.get(p)
                if b != a:
                    fmt = lambda x: "없음" if not x else f"{x['metric']} {'/'.join(x['nexthops'])}"
                    changes.append(f"{r} {p}: {fmt(b)} → {fmt(a)}")
        add("INFO", "경로 변화", f"{len(changes)}건" + ("".join("\n      " + c for c in changes[:12])) + ("\n      …" if len(changes) > 12 else ""))
    return res


def failed(results: list[dict]) -> bool:
    return any(r["level"] == "FAIL" for r in results)


MARK = {"OK": "\033[32m✔\033[0m", "FAIL": "\033[31m✘\033[0m", "WARN": "\033[33m!\033[0m", "INFO": "·"}


def report(title: str, results: list[dict]) -> None:
    print(f"\n[{title}]")
    for r in results:
        print(f"  {MARK[r['level']]} {r['check']:<8} {r['detail']}")


# ── 변경 ────────────────────────────────────────────────────


def frr_reload(router: str, conf: str, test: bool = False) -> subprocess.CompletedProcess:
    return sh(router, "/usr/lib/frr/frr-reload.py", "--test" if test else "--reload", "--stdout", conf, timeout=60)


def errors(proc: subprocess.CompletedProcess) -> str:
    lines = [l for l in (proc.stdout + proc.stderr).splitlines() if "ERROR" in l or "%" in l]
    return "\n  ".join(lines[-5:]) or f"종료 코드 {proc.returncode}"


def cmd_check(_args) -> int:
    snap = snapshot()
    results = evaluate(snap)
    report("현재 상태 점검", results)
    return 1 if failed(results) else 0


def cmd_change(args) -> int:
    r = args.router
    cand_src = Path(args.config)
    allow_down = {"-".join(sorted(l.split("-"))) for l in args.allow_down or []}
    rec_dir = RECORDS / f"{datetime.now():%Y%m%d-%H%M%S}-{r}"
    rec_dir.mkdir(parents=True)
    record = {"router": r, "reason": args.reason, "candidate": str(cand_src), "allow_down": sorted(allow_down), "start": datetime.now().isoformat(timespec="seconds")}
    # frr-reload 는 적용 뒤 frr.conf 를 새 설정으로 덮어쓴다 → 롤백 기준은 지금 돌고 있는 설정
    before = running_config(r)
    cand = RUNTIME / r / "candidate.conf"  # 컨테이너 안에서는 /etc/frr/candidate.conf
    rollback = RUNTIME / r / "rollback.conf"
    rollback.write_text(before)
    shutil.copy(rollback, rec_dir / "before.conf")
    shutil.copy(cand_src, cand)
    shutil.copy(cand_src, rec_dir / "candidate.conf")

    def finish(status: str, code: int) -> int:
        record["status"] = status
        record["end"] = datetime.now().isoformat(timespec="seconds")
        (rec_dir / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2))
        cand.unlink(missing_ok=True)
        rollback.unlink(missing_ok=True)
        print(f"\n결과: {status}  (기록 {rec_dir.relative_to(ROOT)})")
        return code

    print(f"변경 대상 {r}  ·  후보 {cand_src}" + (f"  ·  사유: {args.reason}" if args.reason else ""))

    # 1. 변경 전 점검 — 이미 문제가 있는 망에 손대지 않는다
    pre = snapshot()
    pre_res = evaluate(pre, allow_down=allow_down)
    report("1. 변경 전 점검", pre_res)
    (rec_dir / "pre.json").write_text(json.dumps(pre, ensure_ascii=False, indent=1))
    record["pre_check"] = pre_res
    if failed(pre_res) and not args.force:
        return finish("중단 — 변경 전 점검 실패 (--force 로 무시 가능)", 2)

    # 2. 문법 검사 · 바뀌는 줄
    syn = sh(r, "vtysh", "-C", "-f", "/etc/frr/candidate.conf")
    if syn.returncode != 0:
        print("\n[2. 문법 검사] ✘\n" + (syn.stdout + syn.stderr).strip())
        record["syntax_error"] = (syn.stdout + syn.stderr).strip()
        return finish("거부 — 문법 오류", 3)
    diff = frr_reload(r, "/etc/frr/candidate.conf", test=True)
    diff_text = diff.stdout.strip()
    (rec_dir / "diff.txt").write_text(diff_text + "\n")
    print("\n[2. 문법 검사 ✔ · 바뀌는 줄]\n" + "\n".join("  " + l for l in diff_text.splitlines() if l.strip()))

    # 2-1. 사전 검증: Batfish 가 설정 파일만으로 변경 뒤 망을 계산 — 라우터에는 아직 손대지 않는다
    if not args.skip_prevalidate:
        try:
            from bfmodel import prevalidate

            t = time.monotonic()
            pv = prevalidate(r, cand_src.read_text(), allow_down)
        except Exception as e:  # Batfish 가 없거나 꺼져 있으면 실제 점검만으로 진행
            print(f"\n[2-1. 사전 검증] 건너뜀 — Batfish 사용 불가 ({type(e).__name__}: {e})")
            record["prevalidate"] = {"skipped": str(e)}
        else:
            report(f"2-1. 사전 검증 — Batfish 예측 ({time.monotonic() - t:.1f}초)", pv["results"])
            for issue in pv["new_issues"]:
                print(f"  {MARK['WARN']} 모델 한계  Batfish 가 읽지 못한 줄: {issue}")
            predicted_fail = failed(pv["results"])
            record["prevalidate"] = {"results": pv["results"], "new_issues": pv["new_issues"], "predicted_fail": predicted_fail}
            (rec_dir / "model.json").write_text(json.dumps(pv["model"], ensure_ascii=False, indent=1))
            if predicted_fail and not args.apply_anyway:
                return finish("거부 — 사전 검증에서 장애 예측 (라우터에 적용 안 함)", 6)
            if predicted_fail:
                print("  --apply-anyway: 예측이 맞는지 보려고 그대로 적용")

    # 3. 적용 → 수렴 → 변경 후 점검
    t0 = time.monotonic()
    applied = frr_reload(r, "/etc/frr/candidate.conf")
    if applied.returncode != 0:
        record["apply_error"] = errors(applied)
        print("\n적용 중 오류 (일부만 적용됐을 수 있음 — 변경 후 점검으로 판정):\n  " + record["apply_error"])
    post, conv = wait_converged()
    post_res = evaluate(post, pre=pre, allow_down=allow_down)
    detect = time.monotonic() - t0
    report(f"3. 변경 후 점검 (수렴 {conv:.1f}초)", post_res)
    (rec_dir / "post.json").write_text(json.dumps(post, ensure_ascii=False, indent=1))
    record.update(post_check=post_res, converge_sec=round(conv, 1), detect_sec=round(detect, 1))

    if not failed(post_res):
        shutil.copy(cand_src, CONFIGS / r / "frr.conf")  # 후보가 새 기준 설정이 된다
        return finish("적용 완료", 0)
    if args.no_rollback:
        return finish("실패 — 롤백 안 함 (--no-rollback)", 4)

    # 4. 자동 롤백: 이전 설정으로 되돌리고 다시 점검
    print("\n변경 후 점검 실패 → 이전 설정으로 롤백")
    t1 = time.monotonic()
    undo = frr_reload(r, "/etc/frr/rollback.conf")
    if undo.returncode != 0:
        record["rollback_error"] = errors(undo)
        print("  롤백 설정 적용 실패:\n  " + record["rollback_error"])
        return finish("롤백 실패 — 사람이 확인해야 함 (이전 설정: before.conf)", 5)
    back, _ = wait_converged()
    back_res = evaluate(back, pre=pre, allow_down=allow_down)
    rb = time.monotonic() - t1
    report(f"4. 롤백 후 점검 (롤백 {rb:.1f}초)", back_res)
    (rec_dir / "rollback.json").write_text(json.dumps(back, ensure_ascii=False, indent=1))
    record.update(rollback_check=back_res, rollback_sec=round(rb, 1), impact_sec=round(detect + rb, 1))
    print(f"  영향 시간(적용 → 복구 확인) 약 {detect + rb:.1f}초")
    if failed(back_res):
        return finish("롤백 후에도 실패 — 사람이 확인해야 함", 5)
    return finish("롤백 완료 — 변경 전 상태로 복구", 1)


def cmd_history(_args) -> int:
    for d in sorted(RECORDS.glob("*"))[-20:]:
        f = d / "result.json"
        if f.exists():
            r = json.loads(f.read_text())
            extra = f"  영향 {r['impact_sec']}초" if "impact_sec" in r else ""
            print(f"{d.name}  {r['router']}  {r['status']}{extra}  {r.get('reason') or ''}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="설정 변경 안전장치")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    c = sub.add_parser("change")
    c.add_argument("router", choices=ROUTERS)
    c.add_argument("config")
    c.add_argument("--reason")
    c.add_argument("--allow-down", action="append", metavar="rA-rB", help="계획된 링크 단절 (작업 대상)")
    c.add_argument("--no-rollback", action="store_true")
    c.add_argument("--force", action="store_true", help="변경 전 점검 실패해도 진행")
    c.add_argument("--skip-prevalidate", action="store_true", help="Batfish 사전 검증 건너뛰기")
    c.add_argument("--apply-anyway", action="store_true", help="사전 검증이 장애를 예측해도 적용 (예측 확인용)")
    c.set_defaults(fn=cmd_change)
    sub.add_parser("history").set_defaults(fn=cmd_history)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
