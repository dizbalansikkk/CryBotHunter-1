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


def cohort(positions, end, days=21, holdout_days=7):
    if not 0 < holdout_days < days:
        raise ValueError("Require 0 < holdout-days < days")
    start, split = end - timedelta(days=days), end - timedelta(days=holdout_days)
    # Select by entry, never by subsequent close or profit.
    return start, split, [p for p in positions if start <= datetime.fromisoformat(p["entered_at"]) < end]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--days", type=int, default=21)
    parser.add_argument("--holdout-days", type=int, default=7)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.archive) as archive:
        summary = json.loads(archive.read("summary.json"))
        positions = list(csv.DictReader(io.TextIOWrapper(archive.open("trade-journal.csv"), encoding="utf-8-sig")))
    end = datetime.fromisoformat(summary["generated_at"])
    if not 0 < args.holdout_days < args.days:
        parser.error("Require 0 < holdout-days < days")
    start, split, selected = cohort(positions, end, args.days, args.holdout_days)
    results = []
    for p in selected:
        context = json.loads(p["entry_context_json"])
        entered = datetime.fromisoformat(p["entered_at"])
        partition = "development" if entered < split else "holdout"
        base = {"position_id": p["position_id"], "partition": partition,
                "actual_status": p["status"], "opened_at": p["entered_at"]}
        execution = context.get("entry_execution", {})
        risk = context.get("exit_plan", {}).get("initial_risk")
        target = context.get("scale_out", {}).get("tp3_price")
        if not risk or not target or not execution.get("volume") or execution.get("fee") is None:
            results.append({**base, "excluded": "missing_original_risk_target_or_execution"})
            continue
        start_ms = int(entered.timestamp() * 1000)
        end_ms = int(min(split if partition == "development" else end, entered + timedelta(hours=72)).timestamp() * 1000)
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
        volume = float(execution["volume"])
        entry = float(p["entry_price"])
        risk = float(context["exit_plan"]["initial_risk"])
        fee = float(execution["fee"])
        common = dict(bars=bars, entry_time_ms=start_ms, end_time_ms=end_ms,
            entry=entry, volume=volume, side=p["side"], initial_risk=risk,
            target=float(context["scale_out"]["tp3_price"]), entry_fee=fee,
            exit_fee_rate=fee / (entry * volume), slippage_bps=2.0)
        results.append({**base, "symbol": p["symbol"],
            "actual_pnl": float(p["pnl"]), "opened_at": p["entered_at"],
            "source": url, "sha256": hashlib.sha256(data).hexdigest(),
            "fixed": replay_exit(**common),
            "trailing_0_8_percent_1R": replay_exit(**common, trailing_percent=.8, activation_r=1.0),
            "fixed_stress_10bps": replay_exit(**{**common, "slippage_bps": 10.0}),
            "trailing_stress_10bps": replay_exit(**{**common, "slippage_bps": 10.0},
                                               trailing_percent=.8, activation_r=1.0)})
    totals = {}
    for partition in ("development", "holdout"):
        candidates = [r for r in results if r["partition"] == partition]
        variants = ("fixed", "trailing_0_8_percent_1R", "fixed_stress_10bps", "trailing_stress_10bps")
        eligible = [r for r in candidates if all(r.get(v, {}).get("status") in
                    {"CLOSED", "OPEN_AT_HORIZON"} for v in variants)]
        totals[partition] = {"selected": len(candidates), "paired": len(eligible),
                            "excluded_or_incomplete": len(candidates) - len(eligible)}
        for variant in variants:
            closed = [r[variant]["net_pnl"] for r in eligible if r[variant]["status"] == "CLOSED"]
            opened = [r[variant]["liquidation_value_estimate"] for r in eligible if r[variant]["status"] == "OPEN_AT_HORIZON"]
            totals[partition][variant] = {"closed": len(closed), "open": len(opened),
                "realized_net": sum(closed), "open_liquidation_estimate": sum(opened),
                "combined_estimate": sum(closed) + sum(opened)}
    report = {"start": start.isoformat(), "split": split.isoformat(), "totals": totals, "as_of": end.isoformat(), "promotion_allowed": False,
        "assumptions": ["5-minute futures OHLC, entry candle omitted",
            "same entries, volumes, recorded entry fees; estimated exit fee uses entry fee rate",
            "2 bps exit slippage, funding excluded because actual payments unavailable",
            "trailing 0.8% activated at 1R; not proof of the historical live settings",
            "trailing updates apply on next candle; stop-first if both levels touched",
            "common horizon: min(72 hours after entry, archive time); open outcomes not booked as profit",
            "selection by entry time, includes actual OPEN positions; missing original metadata explicitly excluded",
            "development horizon censored at holdout boundary to avoid overlapping outcome data",
            "parameters fixed in advance; holdout is retrospective, not unseen prospective validation",
            "independent same-entry replays, not portfolio simulation; actual future entries depend on old exits",
            "production futures data may differ from sandbox execution; funding excluded",
            "open liquidation estimates are separate from booked realized PnL"],
        "positions": results}
    (args.output / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"start": report["start"], "split": report["split"], "totals": totals}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
