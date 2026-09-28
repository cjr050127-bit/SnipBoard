from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .library import Library
from .live import detect_current
from .runtime import configure_logging, default_library_dir
from .source import discover, inspect_source


def _source(value: Path | None) -> Path:
    if value:
        path = value.expanduser().resolve()
        if not (path / "history").is_dir():
            raise ValueError(f"不是 Snipaste LocalState 目录：{path}")
        return path
    candidates = discover()
    if len(candidates) != 1:
        found = "\n".join(f"  {path}" for path in candidates) or "  （未找到）"
        raise ValueError(f"需要恰好一个 Snipaste 数据目录；请使用 --source 指定。\n{found}")
    return candidates[0]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="snipboard", description="Snipaste 独立收藏画板")
    parser.add_argument("--source", type=Path, help="Snipaste LocalState 目录")
    parser.add_argument("--library", type=Path, help="独立收藏库目录（默认位于 LocalAppData）")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("discover", help="列出可用 Snipaste 数据目录")
    commands.add_parser("inspect", help="只读检查图组与恢复态证据")
    collect = commands.add_parser("collect", help="收藏指定图组的新 PNG")
    collect.add_argument("groups", nargs="+", help="六位图组 ID，或 all")
    search = commands.add_parser("search", help="按标签和备注搜索")
    search.add_argument("query", nargs="?", default="")
    observe = commands.add_parser("observe", help="实验性观测实时图组信号")
    observe.add_argument("--seconds", type=float, default=60)
    observe.add_argument("--interval", type=float, default=0.2)
    backup = commands.add_parser("backup", help="创建一致性收藏库备份")
    backup.add_argument("destination", type=Path)
    commands.add_parser("desktop", help="打开桌面画板")
    return parser


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    configure_logging()
    library_root = (args.library or default_library_dir()).expanduser().resolve()
    try:
        if args.command == "discover":
            _print([str(path) for path in discover()])
            return 0
        if args.command == "desktop":
            from .qt_app import run
            run(args.source, library_root)
            return 0
        source = _source(args.source)
        if args.command == "inspect":
            _print(inspect_source(source))
            return 0
        if args.command == "collect":
            snapshot = inspect_source(source)
            known = {group["id"]: group for group in snapshot["groups"]}
            selected = list(known) if "all" in args.groups else args.groups
            unknown = sorted(set(selected) - set(known))
            if unknown:
                raise ValueError(f"未知图组：{', '.join(unknown)}")
            with Library(library_root) as library:
                _print([library.collect(source, group_id, known[group_id]["name_candidate"])
                        for group_id in selected])
            return 0
        if args.command == "search":
            with Library(library_root) as library:
                _print(library.search(args.query))
            return 0
        if args.command == "backup":
            destination = args.destination.expanduser().resolve()
            with Library(library_root) as library:
                library.backup(destination)
            _print({"backup": str(destination)})
            return 0
        if args.command == "observe":
            deadline = time.monotonic() + max(0.0, args.seconds)
            previous = object()
            while time.monotonic() < deadline:
                state = detect_current(source)
                if state != previous:
                    print(json.dumps(state, ensure_ascii=False), flush=True)
                    previous = state
                time.sleep(max(0.05, args.interval))
            return 0
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
