"""Local read-only SQLite browser; uses Python's built-in SQLite and Tk widgets."""
import argparse
import json
from pathlib import Path
import sqlite3
import tkinter as tk
from tkinter import ttk


def connect(path):
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    connection.execute("PRAGMA query_only=ON")
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
    connection.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY)
    return connection


def main():
    parser = argparse.ArgumentParser(description="只读浏览项目 SQLite 数据库")
    parser.add_argument("database", nargs="?", type=Path, default=Path(__file__).resolve().parent.parent / "data/dense_medium.db")
    args = parser.parse_args()
    path = args.database.resolve(strict=True)
    connection = connect(path)
    try:
        tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    finally:
        connection.close()

    root = tk.Tk()
    root.title("项目 SQLite 数据库 · 只读")
    root.geometry("1280x820")
    root.minsize(800, 550)
    style = ttk.Style(root)
    style.configure("Treeview", rowheight=28)
    ttk.Label(root, text=str(path), font=("Microsoft YaHei", 11)).pack(anchor="w", padx=14, pady=(14, 4))
    ttk.Label(root, text="只读连接 · 不修改数据库 · 查询最多显示 200 行，选中一行可查看完整字段").pack(anchor="w", padx=14)
    toolbar = ttk.Frame(root)
    toolbar.pack(fill="x", padx=14, pady=10)
    ttk.Label(toolbar, text="数据表").pack(side="left")
    chosen = tk.StringVar(value="telemetry_points" if "telemetry_points" in tables else tables[0] if tables else "")
    picker = ttk.Combobox(toolbar, textvariable=chosen, values=tables, state="readonly", width=30)
    picker.pack(side="left", padx=10)
    status = tk.StringVar()
    ttk.Label(toolbar, textvariable=status).pack(side="right")
    query = tk.Text(root, height=3, font=("Consolas", 11), wrap="word")
    query.pack(fill="x", padx=14)
    panes = ttk.Panedwindow(root, orient="vertical")
    panes.pack(fill="both", expand=True, padx=14, pady=(10, 14))
    grid = ttk.Frame(panes)
    tree = ttk.Treeview(grid, show="headings")
    vertical = ttk.Scrollbar(grid, orient="vertical", command=tree.yview)
    horizontal = ttk.Scrollbar(grid, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vertical.grid(row=0, column=1, sticky="ns")
    horizontal.grid(row=1, column=0, sticky="ew")
    grid.columnconfigure(0, weight=1)
    grid.rowconfigure(0, weight=1)
    details = tk.Text(panes, height=10, font=("Consolas", 10), wrap="word", state="disabled")
    panes.add(grid, weight=3)
    panes.add(details, weight=1)
    visible = []
    columns = []

    def run():
        nonlocal visible, columns
        connection = connect(path)
        try:
            cursor = connection.execute(query.get("1.0", "end").strip())
            rows = cursor.fetchmany(201)
            columns = [c[0] for c in cursor.description]
        except sqlite3.Error as exc:
            status.set("查询失败：" + str(exc))
            return
        finally:
            connection.close()
        visible = rows[:200]
        tree.delete(*tree.get_children())
        tree.configure(columns=[str(i) for i in range(len(columns))])
        for i, name in enumerate(columns):
            tree.heading(str(i), text=name)
            tree.column(str(i), width=190, minwidth=80, stretch=False)
        for i, row in enumerate(visible):
            tree.insert("", "end", iid=str(i), values=["NULL" if value is None else str(value)[:140] for value in row])
        status.set(f"显示 {len(visible)} 行" + ("（还有更多）" if len(rows) > 200 else ""))

    def table(_event=None):
        name = chosen.get()
        if not name:
            return
        quoted = '"' + name.replace('"', '""') + '"'
        query.delete("1.0", "end")
        query.insert("1.0", f"SELECT * FROM {quoted} ORDER BY rowid DESC LIMIT 201;")
        run()

    def selection(_event):
        selected = tree.selection()
        if not selected:
            return
        record = dict(zip(columns, visible[int(selected[0])]))
        for key, value in record.items():
            if isinstance(value, str) and value[:1] in {"{", "["}:
                try:
                    record[key] = json.loads(value)
                except ValueError:
                    pass
        details.configure(state="normal")
        details.delete("1.0", "end")
        details.insert("1.0", json.dumps(record, ensure_ascii=False, indent=2, default=str))
        details.configure(state="disabled")

    ttk.Button(toolbar, text="刷新 / 执行查询", command=run).pack(side="left", padx=4)
    picker.bind("<<ComboboxSelected>>", table)
    tree.bind("<<TreeviewSelect>>", selection)
    table()
    root.after(200, lambda: print(f"sqlite_window_ready mapped={root.winfo_ismapped()} table={chosen.get()} rows={len(visible)} read_only=True", flush=True))
    root.mainloop()


if __name__ == "__main__":
    main()
