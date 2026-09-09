"""P1 迁移冒烟测试：验证 preview/apply 映射正确"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import migrate  # noqa: E402

store = {
    "coarseCoal": [{"timestamp": "2026-06-16 09:31:00", "system": "合并", "ash_content": 13.86,
                    "coal_amount": 927.0, "level": 55.0, "raw_ash": 31.82, "sysA": 1, "source": "import"}],
    "floatCoal": [{"timestamp": "2026-05-21 07:23:00", "system": "合并", "ash_content": 8.38,
                   "coal_amount": 27.0, "source": "import"}],
    "calcLogs": [
        {"timestamp": "2026-05-21 08:23:00", "calc_type": "ash_density",
         "input_json": "{\"system\":\"A\",\"belt\":\"502\",\"ash_content\":7.74,\"density\":1.473}", "output_json": "{}"},
        {"timestamp": "2026-05-21 08:30:00", "calc_type": "density_meter",
         "input_json": "{\"value\":1.45}", "output_json": "{}"},
    ],
    "ashTarget": 8.50, "ashTargetTol": 0.1, "guideScheme": "total", "totalAshManualOn": False,
    "densityGuide": {"maxStep": 0.01, "deadband": 0.05}, "densityAutoOn": False,
    "coarseTolerance": 0.8, "coarseTrainRange": "jun_jul",
    "amountInputs": {"totalAmount": {"mode": "manual", "manual": 450}},
    "instrumentInputs": {"density": {"manual": 1.45}},
    "heavyAshInput": {"manual": 8.50},
    "autoState": {"totalAmount": {"v": 503.7, "t": 1700000000000}},
    "coarseModel": {"production": "pls",
                    "mlr": {"intercept": 1.0, "coefs": [1, 2], "metrics": {"r2": 0.5}},
                    "pls": {"intercept": 2.0, "coefs": [3, 4], "A": 2, "metrics": {"r2": 0.52}},
                    "n": 113, "tolerance": 0.8, "range": "jun_jul", "trainedAt": "2026-07-14 10:00:00"},
    "magneticTail": [{"timestamp": "x", "level": 55}],
    "unknownKey": {"a": 1},
}

print("PREVIEW:", json.dumps(migrate.preview(store), ensure_ascii=False))
r = migrate.apply(store)
print("APPLY:", json.dumps(r, ensure_ascii=False))

# 验证落库
from app.database import SessionLocal  # noqa: E402
from app.models import CoalRecord, CalcLog, Setting, AutoState  # noqa: E402

db = SessionLocal()
try:
    print("coal_records:", db.query(CoalRecord).count())
    print("calc_logs:", db.query(CalcLog).count())
    print("settings:", db.query(Setting).count())
    print("auto_state:", db.query(AutoState).count())
finally:
    db.close()
