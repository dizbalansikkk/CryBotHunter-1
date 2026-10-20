"""Read an audit ZIP and cache bounded public futures data for offline comparison."""
import argparse
import csv
from datetime import datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import sys
import time
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.exit_comparison import replay_exit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.archive) as archive:
        summary = json.loads(archive.read("summary.json"))
        positions = list(csv.DictReader(io.TextIOWrapper(archive.open("trade-journal.csv"), encoding="utf-8-sig")))
    end = datetime.fromisoformat(summary["generated_at"])
    selected = [p for p in positions if p["closed_at"]
                and datetime.fromisoformat(p["closed_at"]) >= end - timedelta(days=1)]
    results = []
    for p in selected:
        context = json.loads(p["entry_context_json"])
        scale = context.get("scale_out", {})
        if (scale.get("mode") != "MONOLITHIC" or scale.get("tp1_taken")
                or scale.get("tp2_taken") or context.get("dynamic_take_profit")):
            results.append({"position_id": p["position_id"], "excluded": "partial_or_dynamic_exit_history"})
            continue
        entered = datetime.fromisoformat(p["entered_at"])
        start_ms = int(entered.timestamp() * 1000)
        end_ms = int(min(end, entered + timedelta(hours=72)).timestamp() * 1000)
        symbol = p["symbol"].replace("/", "")
        filename = args.output / f"{symbol}-{start_ms}-{end_ms}-5m.json"
        url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=5m"
               f"&startTime={start_ms // 300000 * 300000}&endTime={end_ms}&limit=1000")
        if not filename.exists():
            # At most one request per position, no retry on rate limits / bans.
            with urllib.request.urlopen(url, timeout=20) as response:
                data = response.read()
            json.loads(data)
            filename.write_bytes(data)
            time.sleep(1)
        data = filename.read_bytes()
        bars = [[int(row[0]), *map(float, row[1:5])] for row in json.loads(data)]
        execution = context["entry_execution"]
        volume = float(execution["volume"])
        entry = float(p["entry_price"])
        risk = float(context["exit_plan"]["initial_risk"])
        fee = float(execution["fee"])
        common = dict(bars=bars, entry_time_ms=start_ms, end_time_ms=end_ms,
            entry=entry, volume=volume, side=p["side"], initial_risk=risk,
            target=float(context["scale_out"]["tp3_price"]), entry_fee=fee,
            exit_fee_rate=fee / (entry * volume), slippage_bps=2.0)
        results.append({"position_id": p["position_id"], "symbol": p["symbol"],
            "actual_pnl": float(p["pnl"]), "opened_at": p["entered_at"],
            "source": url, "sha256": hashlib.sha256(data).hexdigest(),
            "fixed": replay_exit(**common),
            "trailing_0_8_percent_1R": replay_exit(**common, trailing_percent=.8, activation_r=1.0)})
    report = {"as_of": end.isoformat(), "promotion_allowed": False,
        "assumptions": ["5-minute futures OHLC, entry candle omitted",
            "same entries, volumes, recorded entry fees; estimated exit fee uses entry fee rate",
            "2 bps exit slippage, funding excluded because actual payments unavailable",
            "trailing 0.8% activated at 1R; not proof of the historical live settings",
            "trailing updates apply on next candle; stop-first if both levels touched",
            "common horizon: min(72 hours after entry, archive time); open outcomes not booked as profit",
            "selection by actual closure in last 24h introduces selection bias; tiny diagnostic sample"],
        "positions": results}
    (args.output / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
