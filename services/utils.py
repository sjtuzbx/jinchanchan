import datetime
from pathlib import Path
import json
import numpy as np
from logger import Logger

BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8), name="CST")


def date2int(date_str):
    """将日期字符串转换为整数格式"""
    if date_str is None:
        return None
    return int(str(date_str).replace('-', ''))


def format_beijing_timestamp(ts):
    """将时间戳/ISO字符串转为北京时间字符串"""
    if not ts:
        return None
    try:
        if isinstance(ts, (int, float)):
            if ts > 1e12:
                ts = ts / 1000.0
            dt_obj = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        elif isinstance(ts, str):
            if ts.isdigit() and len(ts) == 8:
                dt_obj = datetime.datetime.strptime(ts, "%Y%m%d").replace(tzinfo=datetime.timezone.utc)
            else:
                dt_obj = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            return None
        return dt_obj.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S CST")
    except Exception as ex:
        Logger.error(f"timestamp format failed: {ex}")
        return None


def beijing_now():
    return datetime.datetime.now(datetime.timezone.utc).astimezone(BEIJING_TZ)


def convert_numpy_types(obj):
    """递归地将 numpy 类型转换为原生 Python 类型，便于 JSON 序列化"""
    if isinstance(obj, dict):
        return {k: convert_numpy_types(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_numpy_types(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(convert_numpy_types(v) for v in obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.generic):
        return obj.item()
    return obj


def load_json_if_exists(path: Path, default=None):
    if Path(path).exists():
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return default


def dump_json(path: Path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
