import argparse
import datetime
import time
import pandas as pd
from pathlib import Path

from app import pro, compute_cn_sentiment, persist_cn_sentiment, CN_HISTORY_CSV


def get_trading_days(start_date: str, end_date: str):
    cal = pro.trade_cal(
        exchange="SSE",
        start_date=start_date,
        end_date=end_date,
        is_open="1",
        fields="cal_date,is_open"
    )
    return list(cal.sort_values("cal_date")["cal_date"])


def load_done_dates():
    if CN_HISTORY_CSV.exists():
        df = pd.read_csv(CN_HISTORY_CSV)
        return set(df["trade_date"].astype(str).tolist())
    return set()


def backfill(start_date: str, end_date: str, sleep_sec: float = 0.35):
    trading_days = get_trading_days(start_date, end_date)
    done = load_done_dates()
    print(f"Total trading days: {len(trading_days)}, already done: {len(done)}")

    for idx, d in enumerate(trading_days, 1):
        if d in done:
            continue
        try:
            res = compute_cn_sentiment(d)
            if not res:
                print(f"[{idx}/{len(trading_days)}] {d} skip (no data)")
                continue
            persist_cn_sentiment(res["trade_date"], res["main"], res["subs"])
            print(f"[{idx}/{len(trading_days)}] {d} ok, score={res['main']['score']}")
        except Exception as e:
            print(f"[{idx}/{len(trading_days)}] {d} failed: {e}")
        time.sleep(sleep_sec)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill A-share sentiment history")
    parser.add_argument("--start", help="Start date YYYYMMDD, default 10y ago")
    parser.add_argument("--end", help="End date YYYYMMDD, default today")
    parser.add_argument("--sleep", type=float, default=0.35, help="Sleep seconds between requests")
    args = parser.parse_args()

    today = datetime.date.today()
    default_start = (today - datetime.timedelta(days=365 * 10)).strftime("%Y%m%d")
    start = args.start or default_start
    end = args.end or today.strftime("%Y%m%d")
    backfill(start, end, args.sleep)
