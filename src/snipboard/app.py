"""Tk desktop board over the tested read-only source and independent library."""
from __future__ import annotations

import math
import logging
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .library import Library
from .live import detect_current
from .runtime import thumbnail_dir
from .source import inspect_source
from .windows import DesktopIntegration


class BoardApp:
    SYNC_MS = 2500

    def __init__(self, source: Path, library_root: Path):
        self.source = source.resolve()
        self.library = Library(library_root)
        logging.getLogger(__name__).info(
            "desktop starting source=%s library=%s", self.source, self.library.root
        )
        self.thumbnail_root = thumbnail_dir(self.library.root)
        self.thumbnail_root.mkdir(parents=True, exist_ok=True)
        self.root = tk.Tk()
        self.root.report_callback_exception = self._report_exception
        self.root.title("SnipBoard")
        self.root.geometry("1280x820")
        self.root.minsize(900, 600)
        self.board_id: int | None = None
        self.item_id: int | None = None
        self.photos: dict[int, tk.PhotoImage] = {}
        self.canvas_items: dict[int, tuple[int, int, int]] = {}
        self.drag: tuple[int, float, float] | None = None
        self.sync_after: str | None = None
        self.poll_after: str | None = None
        self.sync_thread: threading.Thread | None = None
        self.sync_results: queue.Queue[dict] = queue.Queue()
        self.closing = threading.Event()
        self._build()
        try:
            self.desktop = DesktopIntegration(self.root, self._open_live_group)
            self.status.set(f"{self.desktop.status}（实验性识别，尚未发布验收）")
        except (OSError, ValueError) as exc:
            self.desktop = None
            self.status.set(str(exc))
        self._refresh_boards()
        self._poll_sync()
        self._schedule_sync()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build(self) -> None:
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(self.root, padding=8)
        toolbar.grid(row=0, column=0, columnspan=3, sticky="ew")
        toolbar.columnconfigure(1, weight=1)
        ttk.Label(toolbar, text="搜索").grid(row=0, column=0, padx=(0, 6))
        self.query = tk.StringVar()
        search = ttk.Entry(toolbar, textvariable=self.query)
        search.grid(row=0, column=1, sticky="ew")
        search.bind("<KeyRelease>", lambda _event: self._render())
        ttk.Button(toolbar, text="添加图组", command=self._add_group).grid(row=0, column=2, padx=6)
        ttk.Button(toolbar, text="立即同步", command=self._sync).grid(row=0, column=3)
        ttk.Button(toolbar, text="暂停/继续", command=self._toggle_subscription).grid(
            row=0, column=4, padx=(6, 0))
        ttk.Button(toolbar, text="备份", command=self._backup).grid(row=0, column=5, padx=(6, 0))

        left = ttk.Frame(self.root, padding=(8, 0, 4, 8))
        left.grid(row=1, column=0, sticky="ns")
        ttk.Label(left, text="画板").pack(anchor="w")
        self.boards = tk.Listbox(left, width=26, exportselection=False)
        self.boards.pack(fill="y", expand=True, pady=(6, 0))
        self.boards.bind("<<ListboxSelect>>", self._select_board)

        center = ttk.Frame(self.root)
        center.grid(row=1, column=1, sticky="nsew")
        center.rowconfigure(0, weight=1)
        center.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(center, background="#202124", highlightthickness=0)
        xbar = ttk.Scrollbar(center, orient="horizontal", command=self.canvas.xview)
        ybar = ttk.Scrollbar(center, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=xbar.set, yscrollcommand=ybar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)

        side = ttk.Frame(self.root, padding=(8, 0, 8, 8), width=260)
        side.grid(row=1, column=2, sticky="nsew")
        ttk.Label(side, text="图片信息").pack(anchor="w")
        ttk.Label(side, text="备注").pack(anchor="w", pady=(10, 2))
        self.note = tk.Text(side, width=30, height=12, wrap="word")
        self.note.pack(fill="x")
        ttk.Label(side, text="标签（逗号分隔）").pack(anchor="w", pady=(10, 2))
        self.tags = ttk.Entry(side, width=30)
        self.tags.pack(fill="x")
        ttk.Button(side, text="保存信息", command=self._save_metadata).pack(fill="x", pady=(10, 0))
        self.status = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status, padding=(8, 4)).grid(
            row=2, column=0, columnspan=3, sticky="ew")

    def _refresh_boards(self, select_id: int | None = None) -> None:
        rows = self.library.boards()
        current = select_id if select_id is not None else self.board_id
        self.board_rows = rows
        self.boards.delete(0, "end")
        selected_index = None
        for index, board in enumerate(rows):
            marker = "●" if board["subscribed"] else "○"
            self.boards.insert("end", f"{marker} {board['name']}  ({board['item_count']})")
            if board["id"] == current:
                selected_index = index
        if selected_index is not None:
            self.boards.selection_set(selected_index)
            self.boards.see(selected_index)
        elif rows and self.board_id is None:
            self.boards.selection_set(0)
            self._select_board()

    def _select_board(self, _event=None) -> None:
        selection = self.boards.curselection()
        if not selection:
            return
        self.board_id = self.board_rows[selection[0]]["id"]
        self.item_id = None
        self._clear_metadata()
        self._render()

    def _render(self) -> None:
        self.canvas.delete("all")
        self.photos.clear()
        self.canvas_items.clear()
        if self.board_id is None:
            self.canvas.create_text(40, 40, anchor="nw", fill="white", text="点击“添加图组”创建画板")
            return
        for item in self.library.items(self.board_id, self.query.get()):
            try:
                photo = self._thumbnail(item)
            except (OSError, tk.TclError):
                logging.getLogger(__name__).exception("thumbnail failed: %s", item["asset"])
                continue
            self.photos[item["id"]] = photo
            x, y = item["x"], item["y"]
            tags = (f"asset:{item['id']}", "asset")
            image_id = self.canvas.create_image(x, y, anchor="nw", image=photo, tags=tags)
            border = self.canvas.create_rectangle(
                x - 2, y - 2, x + photo.width() + 2, y + photo.height() + 2,
                outline="#8ab4f8", width=2, tags=tags)
            label = self.canvas.create_text(
                x, y + photo.height() + 8, anchor="nw", fill="#e8eaed",
                text=item["source_name"], tags=tags)
            self.canvas_items[item["id"]] = (image_id, border, label)
        self.canvas.configure(scrollregion=self.canvas.bbox("all") or (0, 0, 1200, 800))

    def _thumbnail(self, item: dict) -> tk.PhotoImage:
        width = max(40, min(4000, round(item["width"])))
        cached = self.thumbnail_root / f"{item['digest']}-{width}.png"
        if cached.is_file():
            try:
                return tk.PhotoImage(file=str(cached))
            except tk.TclError:
                cached.unlink(missing_ok=True)
        original = tk.PhotoImage(file=str(item["asset"]))
        factor = max(1, math.ceil(original.width() / width))
        photo = original.subsample(factor, factor)
        temporary = cached.with_suffix(".tmp.png")
        photo.write(str(temporary), format="png")
        temporary.replace(cached)
        return photo

    def _item_at(self) -> int | None:
        current = self.canvas.find_withtag("current")
        if not current:
            return None
        for tag in self.canvas.gettags(current[0]):
            if tag.startswith("asset:"):
                return int(tag.split(":", 1)[1])
        return None

    def _drag_start(self, event) -> None:
        item_id = self._item_at()
        if item_id is None:
            return
        self._load_metadata(item_id)
        self.drag = (item_id, self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))

    def _drag_move(self, event) -> None:
        if not self.drag:
            return
        item_id, old_x, old_y = self.drag
        new_x, new_y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        for canvas_id in self.canvas_items[item_id]:
            self.canvas.move(canvas_id, new_x - old_x, new_y - old_y)
        self.drag = (item_id, new_x, new_y)

    def _drag_end(self, _event) -> None:
        if not self.drag or self.board_id is None:
            return
        item_id = self.drag[0]
        x, y = self.canvas.coords(self.canvas_items[item_id][0])
        row = next(item for item in self.library.items(self.board_id) if item["id"] == item_id)
        self.library.save_positions(self.board_id, {item_id: (x, y, row["width"])})
        self.drag = None
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _load_metadata(self, item_id: int) -> None:
        if self.board_id is None:
            return
        row = next(item for item in self.library.items(self.board_id) if item["id"] == item_id)
        self.item_id = item_id
        self.note.delete("1.0", "end")
        self.note.insert("1.0", row["note"])
        self.tags.delete(0, "end")
        self.tags.insert(0, ", ".join(row["tags"]))

    def _clear_metadata(self) -> None:
        self.note.delete("1.0", "end")
        self.tags.delete(0, "end")

    def _save_metadata(self) -> None:
        if self.item_id is None:
            self.status.set("请先选择一张图片")
            return
        tags = [tag.strip() for tag in self.tags.get().split(",")]
        self.library.annotate(self.item_id, self.note.get("1.0", "end-1c"), tags)
        self.status.set("备注和标签已保存")
        self._render()

    def _add_group(self) -> None:
        groups = inspect_source(self.source)["groups"]
        if not groups:
            messagebox.showinfo("SnipBoard", "未发现含持久化 PNG 的图组目录")
            return
        choices = "\n".join(f"{g['id']}  {g['name_candidate'] or '(未命名)'}" for g in groups)
        group_id = simpledialog.askstring("添加图组", f"输入六位图组 ID：\n\n{choices}", parent=self.root)
        if not group_id:
            return
        known = {group["id"]: group for group in groups}
        if group_id not in known:
            messagebox.showerror("SnipBoard", "图组 ID 不在只读目录中")
            return
        self._start_collect(
            [(group_id, known[group_id]["name_candidate"])], select_group=group_id)

    def _sync(self) -> None:
        tasks = [
            (board["group_id"], board["name"])
            for board in self.library.boards()
            if board["subscribed"] and Path(board["source"]).resolve() == self.source
        ]
        if tasks:
            self._start_collect(tasks)

    def _start_collect(
        self, tasks: list[tuple[str, str | None]], select_group: str | None = None
    ) -> None:
        if self.sync_thread and self.sync_thread.is_alive():
            self.status.set("同步正在进行")
            return
        self.status.set("后台同步中…")

        def work() -> None:
            added = errors = 0
            try:
                with Library(self.library.root) as worker:
                    for group_id, name in tasks:
                        if self.closing.is_set():
                            break
                        result = worker.collect(
                            self.source, group_id, name, cancelled=self.closing.is_set
                        )
                        added += result["added"]
                        errors += len(result["errors"])
                self.sync_results.put({
                    "added": added, "errors": errors, "select_group": select_group
                })
            except Exception as exc:
                logging.getLogger(__name__).exception("background sync failed")
                self.sync_results.put({"exception": str(exc)})

        self.sync_thread = threading.Thread(
            target=work, name="snipboard-sync", daemon=True
        )
        self.sync_thread.start()

    def _poll_sync(self) -> None:
        try:
            while True:
                result = self.sync_results.get_nowait()
                if "exception" in result:
                    self.status.set(f"同步失败：{result['exception']}")
                    continue
                selected = None
                if result["select_group"]:
                    selected = next(
                        (row["id"] for row in self.library.boards()
                         if row["group_id"] == result["select_group"]
                         and Path(row["source"]).resolve() == self.source),
                        None,
                    )
                    if selected is not None:
                        self.board_id = selected
                self._refresh_boards(selected)
                self._render()
                self.status.set(
                    f"同步完成：新增 {result['added']}，错误 {result['errors']}"
                )
        except queue.Empty:
            pass
        if not self.closing.is_set():
            self.poll_after = self.root.after(100, self._poll_sync)

    def _toggle_subscription(self) -> None:
        if self.board_id is None:
            self.status.set("请先选择画板")
            return
        board = next(row for row in self.library.boards() if row["id"] == self.board_id)
        enabled = not bool(board["subscribed"])
        self.library.subscribe(self.board_id, enabled)
        self._refresh_boards(self.board_id)
        self.status.set("已继续自动同步" if enabled else "已暂停自动同步")

    def _schedule_sync(self) -> None:
        self._sync()
        self.sync_after = self.root.after(self.SYNC_MS, self._schedule_sync)

    def _open_live_group(self) -> None:
        state = detect_current(self.source)
        if state.get("status") != "observed" or not state.get("id"):
            self.status.set(f"无法可靠识别当前图组：{state.get('reason', '无完整证据')}")
            return
        self._start_collect(
            [(state["id"], state.get("name"))], select_group=state["id"])
        self.status.set(f"实验性识别：{state.get('name') or state['id']}；后台同步中…")

    def _backup(self) -> None:
        target = filedialog.asksaveasfilename(
            parent=self.root, title="备份收藏库", defaultextension=".zip",
            filetypes=[("ZIP 备份", "*.zip")])
        if not target:
            return
        try:
            self.library.backup(Path(target))
            self.status.set(f"备份完成：{target}")
        except (OSError, ValueError) as exc:
            messagebox.showerror("备份失败", str(exc))

    def close(self) -> None:
        logging.getLogger(__name__).info("desktop closing")
        self.closing.set()
        if self.sync_after:
            self.root.after_cancel(self.sync_after)
        if self.poll_after:
            self.root.after_cancel(self.poll_after)
        if self.desktop:
            self.desktop.close()
        self.library.close()
        self.root.destroy()

    def _report_exception(self, kind, value, traceback) -> None:
        logging.getLogger(__name__).exception(
            "Tk callback failed", exc_info=(kind, value, traceback)
        )
        messagebox.showerror("SnipBoard 错误", str(value), parent=self.root)

    def run(self) -> None:
        self.root.mainloop()


def run(source: Path, library_root: Path) -> None:
    BoardApp(source, library_root).run()
