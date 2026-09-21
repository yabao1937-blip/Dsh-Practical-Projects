"""使用 Codex 随附的 artifact-tool 生成本轮改动和后续计划。

运行：python backend/scripts/generate_optimization_plan_xlsx.py
可用 --runtime-root 指定 Codex dependencies 目录。生成器仅使用临时目录，
不安装仓库 npm 依赖；最终工作簿写入 docs，预览写入临时目录。
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, default=Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies")
    args = parser.parse_args()
    runtime = args.runtime_root.resolve()
    node = runtime / "node/bin/node.exe"
    modules = runtime / "node/node_modules"
    if not node.exists() or not (modules / "@oai/artifact-tool").exists():
        raise SystemExit("找不到 Codex Node/artifact-tool，请通过 --runtime-root 指定依赖目录")
    scripts = Path(__file__).resolve().parent
    work = Path(tempfile.mkdtemp(prefix="dmcs_plan_20260921_"))
    builder = work / "generate_optimization_plan_xlsx.mjs"
    shutil.copyfile(scripts / builder.name, builder)
    subprocess.run([str(node), "-e", "require('node:fs').symlinkSync(process.argv[1], process.argv[2], 'junction')",
                    str(modules), str(work / "node_modules")], check=True)
    out = scripts.parent.parent / "docs/项目优化与后续计划-20260921.xlsx"
    subprocess.run([str(node), str(builder), str(out), str(work)], check=True)
    print(f"Workbook: {out}")
    print(f"Previews: {work}")


if __name__ == "__main__":
    main()
