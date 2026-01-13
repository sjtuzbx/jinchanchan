import argparse
import datetime
import time
from pathlib import Path

import pandas as pd

import sys
sys.path.append('/home/zbx/jinchanchan')
import app
from app import pro, compute_cn_sentiment, persist_cn_sentiment, CN_BSE_HISTORY_CSV


def parse_args():
    parser = argparse.ArgumentParser(description="Rebuild BSE sentiment history with resume support.")
    parser.add_argument("--start", default="20230101", help="Start date YYYYMMDD")
    parser.add_argument("--end", default=None, help="End date YYYYMMDD (default: today)")
    parser.add_argument("--reset", action="store_true", help="Delete existing history before rebuild")
    parser.add_argument("--sleep", type=float, default=0.6, help="Base sleep seconds between days")
    return parser.parse_args()


def is_rate_limited(msg: str) -> bool:
    if not msg:
        return False
    return "最多访问" in msg or "频率" in msg or "limit" in msg.lower()


def load_existing_dates(path: Path):
    if not path.exists():
        return set()
    df = pd.read_csv(path)
    if df.empty or "trade_date" not in df.columns:
        return set()
    return set(df["trade_date"].astype(str).tolist())


def sort_history(path: Path):
    if not path.exists():
        return
    df = pd.read_csv(path)
    if df.empty or "trade_date" not in df.columns:
        return
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.drop_duplicates(subset=["trade_date"], keep="last")
    df = df.sort_values("trade_date")
    df.to_csv(path, index=False)


def main():
    args = parse_args()
    end_date = args.end or datetime.datetime.now().strftime("%Y%m%d")

    if args.reset and CN_BSE_HISTORY_CSV.exists():
        CN_BSE_HISTORY_CSV.unlink()

    cal = pro.trade_cal(exchange="SSE", start_date=args.start, end_date=end_date, is_open="1")
    cal = cal.sort_values("cal_date")
    trade_dates = cal["cal_date"].astype(str).tolist()

    existing = load_existing_dates(CN_BSE_HISTORY_CSV)
    total = len(trade_dates)
    done = 0

    for idx, d in enumerate(trade_dates, start=1):
        if d in existing:
            done += 1
            continue

        for attempt in range(6):
            try:
                res = compute_cn_sentiment(d, market="bse")
                if res:
                    persist_cn_sentiment(res["trade_date"], res["main"], res["subs"], history_csv=CN_BSE_HISTORY_CSV)
                    existing.add(d)
                break
            except Exception as exc:
                msg = str(exc)
                if is_rate_limited(msg):
                    time.sleep(2.0 + attempt * 2.0)
                    continue
                time.sleep(0.5)
                break

        done += 1
        if done % 20 == 0 or done == total:
            print(f"progress {done}/{total} ({d})")
        time.sleep(args.sleep)

    sort_history(CN_BSE_HISTORY_CSV)
    print(f"completed {done}/{total} -> {CN_BSE_HISTORY_CSV}")


if __name__ == "__main__":
    main()
