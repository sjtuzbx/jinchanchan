import json
from pathlib import Path
from datetime import datetime


class StrategyStore:
    def __init__(self, file_path):
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            self.file_path.write_text("[]", encoding="utf-8")

    def _load(self):
        with self.file_path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _save(self, data):
        with self.file_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def list_strategies(self):
        return self._load()

    def get(self, name):
        for item in self._load():
            if item["name"] == name:
                return item
        return None

    def save(self, name, payload):
        data = self._load()
        payload["name"] = name
        payload["updated_at"] = datetime.utcnow().isoformat()
        for idx, item in enumerate(data):
            if item["name"] == name:
                data[idx] = payload
                break
        else:
            data.append(payload)
        self._save(data)

