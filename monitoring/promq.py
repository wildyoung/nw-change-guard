#!/usr/bin/env python3
"""Prometheus 에 PromQL 을 묻고 결과를 한 줄씩 찍는다 (확인용)  —  promq.py 'up' ['ALERTS' …]"""

import json
import sys
import urllib.parse
import urllib.request

for q in sys.argv[1:]:
    url = "http://127.0.0.1:9090/api/v1/query?" + urllib.parse.urlencode({"query": q})
    res = json.load(urllib.request.urlopen(url))["data"]["result"]
    print(f"# {q}")
    for r in res:
        labels = ", ".join(f"{k}={v}" for k, v in r["metric"].items() if k not in ("__name__", "instance", "job"))
        print(f"  {labels:<60} {float(r['value'][1]):.0f}")
    if not res:
        print("  (없음)")
