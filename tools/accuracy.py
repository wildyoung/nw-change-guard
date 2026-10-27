"""사전 검증(Batfish 예측)과 실제 변경 후 점검을 항목마다 비교한다 — 변경 기록(changes/records)을 읽는다.

  accuracy.py [기록 폴더 이름 앞부분 …]     없으면 예측 · 실제가 둘 다 있는 기록 전부
"""

import json
import sys

from nwguard import RECORDS

CHECKS = ["OSPF 이웃", "경로", "서비스 도달"]


def level(results, check):
    hit = [r["level"] for r in results if r["check"] == check]
    return "FAIL" if "FAIL" in hit else ("WARN" if "WARN" in hit else "OK")


rows, agree, total = [], 0, 0
for d in sorted(RECORDS.glob("*")):
    if sys.argv[1:] and not any(d.name.startswith(p) for p in sys.argv[1:]):
        continue
    f = d / "result.json"
    if not f.exists():
        continue
    r = json.loads(f.read_text())
    pv, post = r.get("prevalidate", {}), r.get("post_check")
    if "results" not in pv or not post:
        continue
    cells = []
    for c in CHECKS:
        p, a = level(pv["results"], c), level(post, c)
        total += 1
        agree += p == a
        cells.append(f"{p}/{a}" + ("" if p == a else " ≠"))
    verdict = ("장애" if pv["predicted_fail"] else "정상") + " / " + ("장애" if any(x["level"] == "FAIL" for x in post) else "정상")
    # 경로 표 전체: 모델이 계산한 경로(model.json)와 실제 변경 후 경로(post.json)
    model, live = json.loads((d / "model.json").read_text()), json.loads((d / "post.json").read_text())
    n_routes = n_same = 0
    for rt in model["routers"]:
        m, l = model["routers"][rt]["routes"], live["routers"][rt].get("routes", {})
        for p in set(m) | set(l):
            n_routes += 1
            n_same += m.get(p) == l.get(p)
    rows.append((r.get("reason") or d.name, verdict, *cells, f"{n_same}/{n_routes}"))

print(f"{'변경':<28} {'판정 예측/실제':<14} " + " ".join(f"{c:<12}" for c in CHECKS) + "경로 일치")
for row in rows:
    print(f"{row[0]:<28} {row[1]:<14} " + " ".join(f"{c:<12}" for c in row[2:-1]) + row[-1])
print(f"\n항목 일치 {agree}/{total}")
