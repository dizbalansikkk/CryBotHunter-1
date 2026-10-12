"""Public-data-only reproducible pilot; never reads credentials or places orders.

Run from backend: python course_research_report.py --output ../work/course/pilot.json
"""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from app.services.course_research import ResearchBar, ResearchParameters, evaluate_sweeps


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    now = int(time.time() * 1000)
    interval = 3600000
    report = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "purpose": "fixed_parameter_pilot_not_strategy_approval",
              "venue": "binance_usdm", "market": "perpetual", "timeframe": "1h",
              "promotion_allowed": False, "symbols": {}}
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        url = "https://fapi.binance.com/fapi/v1/klines?" + urlencode(
            {"symbol": symbol, "interval": "1h", "limit": 1500, "endTime": now})
        with urlopen(url, timeout=30) as response:
            raw = json.load(response)
        bars = [ResearchBar(int(r[0]), *map(float, r[1:5])) for r in raw if int(r[0]) + interval <= now]
        digest = hashlib.sha256(json.dumps([asdict(b) for b in bars], sort_keys=True).encode()).hexdigest()
        split = int(len(bars) * 0.6)
        results = []
        # No tuning: identical parameters on chronological train and holdout.
        for name, start, end in (("train", 100, split), ("holdout", split, len(bars))):
            for filtered in (False, True):
                result = evaluate_sweeps(bars, interval, start=start, end=end,
                                         parameters=ResearchParameters(), trend_filter=filtered)
                result["slice"] = name
                results.append(result)
        report["symbols"][symbol] = {"endpoint": url, "data_sha256": digest,
            "count": len(bars), "first_open_ms": bars[0].timestamp,
            "last_open_ms": bars[-1].timestamp, "results": results}
        data_path = Path(args.output).with_name(symbol + "_bars.json")
        data_path.parent.mkdir(parents=True, exist_ok=True)
        data_path.write_text(json.dumps([asdict(b) for b in bars]), encoding="utf-8")
    output = Path(args.output)
    output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    for symbol, item in report["symbols"].items():
        for result in item["results"]:
            print(symbol, result["slice"], result["hypothesis"], result["trades_count"], round(result["net_r"], 3))


if __name__ == "__main__":
    main()
