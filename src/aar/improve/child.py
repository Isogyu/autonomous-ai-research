"""評価対象のコードを動かす側（隔離した別プロセスで実行する）。

`python -I -S child.py` として起動し、標準入力から {"module": ファイルのパス, "entry": 関数名, "cases": [{"id", "inputs"}]} を読み、
各課題の出力を標準出力に JSON で返す。受け取るのは課題の入力だけで、正解や採否の基準は渡されない。
標準ライブラリだけで動く（-S で site-packages を外すので、aar も第三者のパッケージも import できない）。
"""

import importlib.util
import json
import sys
import time


def main() -> int:
    job = json.loads(sys.stdin.read())
    out = {"error": "", "results": []}
    try:
        spec = importlib.util.spec_from_file_location("candidate_module", job["module"])
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = getattr(mod, job["entry"])
    except BaseException as e:  # noqa: BLE001 - 評価対象の失敗は、すべて結果として返す
        out["error"] = f"{type(e).__name__}: {e}"[:500]
        sys.stdout.write(json.dumps(out))
        return 0
    for case in job["cases"]:
        res = {"id": case["id"], "output": None, "again": None, "error": "", "ms": 0.0}
        try:
            started = time.perf_counter()
            res["output"] = fn(**json.loads(json.dumps(case["inputs"])))
            res["ms"] = round((time.perf_counter() - started) * 1000, 3)
            res["again"] = fn(**json.loads(json.dumps(case["inputs"])))      # 同じ入力で同じ出力になるか
            if not isinstance(res["output"], str) or not isinstance(res["again"], str):
                res["error"] = "文字列を返していない"
                res["output"] = res["again"] = None
        except BaseException as e:  # noqa: BLE001
            res["error"] = f"{type(e).__name__}: {e}"[:500]
            res["output"] = res["again"] = None
        out["results"].append(res)
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
