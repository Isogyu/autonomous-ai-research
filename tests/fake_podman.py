"""テスト用の podman の代役。受け取った引数を記録し、最小限の応答を返す（実物の隔離は何も確かめない）。"""
import json
import os
import sys
import time

args = sys.argv[1:]
state = os.environ["FAKE_PODMAN_STATE"]
with open(os.path.join(state, "calls.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps(args, ensure_ascii=False) + "\n")
if args and args[0] == "--connection":
    args = args[2:]
cmd = args[0] if args else ""
if cmd == "info":
    info_path = os.path.join(state, "info.json")
    default = {"version": {"Version": "5.4.0"}, "host": {"security": {"rootless": True}, "serviceIsRemote": False,
               "cgroupVersion": "v2", "cgroupControllers": ["cpu", "memory", "pids"], "os": "linux", "arch": "amd64",
               "kernel": "6.6", "ociRuntime": {"name": "crun"}}}
    print(open(info_path, encoding="utf-8").read() if os.path.exists(info_path) else json.dumps(default))
elif cmd == "image" and args[1] == "inspect":
    print("sha256:" + "ab" * 32)
elif cmd == "image" and args[1] == "exists":
    sys.exit(0)
elif cmd == "run":
    mode = os.environ.get("FAKE_PODMAN_RUN", "echo")
    if "--cidfile" in args:
        with open(args[args.index("--cidfile") + 1], "w", encoding="utf-8") as f:
            f.write("c" * 64)
    if mode == "sleep":
        time.sleep(120)
    data = sys.stdin.buffer.read() if "--interactive" in args else b""
    sys.stdout.write(os.environ.get("FAKE_PODMAN_STDOUT", "") or data.decode("utf-8"))
    sys.exit(int(os.environ.get("FAKE_PODMAN_EXIT", "0")))
elif cmd == "ps":
    left = os.path.join(state, "leftover")
    if os.path.exists(left):
        print(open(left, encoding="utf-8").read())
elif cmd == "rm":
    pass
