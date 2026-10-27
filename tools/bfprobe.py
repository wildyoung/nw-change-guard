"""확인용: Batfish 가 계산한 망과 실제 라우터를 같은 점검 · 같은 모양으로 비교한다"""

from bfmodel import Model
from nwguard import evaluate, report, snapshot

m = Model()
m.load("base")
model = m.snapshot("base")
live = snapshot()
report("Batfish 모델 (기준 설정)", evaluate(model))
diffs = []
for r in model["routers"]:
    a, b = model["routers"][r]["routes"], live["routers"][r]["routes"]
    for p in sorted(set(a) | set(b)):
        if a.get(p) != b.get(p):
            diffs.append(f"{r} {p}: 모델 {a.get(p)} / 실제 {b.get(p)}")
print(f"\n경로 표 — 모델과 실제가 다른 곳 {len(diffs)}건")
print("\n".join(diffs[:15]))
