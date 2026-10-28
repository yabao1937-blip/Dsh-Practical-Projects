"""Point the A416/A417 press-state collection at their renamed OPC addresses.

The plant renamed the KEPServer channel for these two tags: the existing points
(kept identity: key + item_id ``TIA_4_2.TIA_4_2.A41x/YL_STATE``) must read from
``ns=2;s=XMZX-YM.YM4_2.A41x/YL_STATE`` instead. Adding duplicate points would
break the chain (two enabled points with one item_id disable each other), so
this edits the two points in place, restarts this installation's collector and
verifies fresh good-quality samples for both keys.
"""
import argparse
import json
import shutil
import subprocess
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

MOVES = {
    'ns=2;s=TIA_4_2.TIA_4_2.A416/YL_STATE': 'ns=2;s=XMZX-YM.YM4_2.A416/YL_STATE',
    'ns=2;s=TIA_4_2.TIA_4_2.A417/YL_STATE': 'ns=2;s=XMZX-YM.YM4_2.A417/YL_STATE',
}
NEW_DEVICE = 'XMZX-YM.YM4_2'


def parse_utc(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp lacks timezone')
    return parsed.astimezone(timezone.utc)


def load(config):
    data = json.loads(config.read_text(encoding='utf-8-sig'))
    points = data.get('points') or []
    old = {node: [p for p in points if p.get('node_id') == node] for node in MOVES}
    new = {node: [p for p in points if p.get('node_id') == node] for node in MOVES.values()}
    return data, old, new


def patch(config, apply):
    data, old, new = load(config)
    for node, matches in {**old, **new}.items():
        if len(matches) > 1:
            raise SystemExit(f'配置中 {node} 出现 {len(matches)} 次，请先人工核对，未做任何修改')
    pending = [node for node, matches in old.items() if matches]
    done = [node for node, matches in new.items() if matches]
    if not pending and not done:
        raise SystemExit('配置中找不到 A416/A417 压滤机运行状态点位（旧或新地址均无），未做任何修改')
    if not pending:
        print('两个点位已经指向新地址，无需修改。')
        return data, []
    if not apply:
        for node in pending:
            print(f'将修改 {node} -> {MOVES[node]}')
        print('dry-run：未写入任何修改。')
        return data, []
    backup = config.with_name(config.name + f'.backup-{datetime.now().strftime("%Y%m%d-%H%M%S")}')
    shutil.copy2(config, backup)
    for point in data['points']:
        node = point.get('node_id')
        if node in MOVES and node in pending:
            point['node_id'] = MOVES[node]
            point['device'] = NEW_DEVICE
    config.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\r\n', encoding='utf-8')
    for node in pending:
        print(f'已修改 {node} -> {MOVES[node]}（key/item_id 保持不变）')
    print('原配置已备份为 ' + backup.name)
    return data, pending


def powershell(script, *args, cwd):
    subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script), *args],
                   cwd=cwd, check=True, timeout=180)


def collector_status(database, source_id):
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT owner,lease_until,state,last_sample_at FROM telemetry_sources WHERE source_id=?',
                           (source_id,)).fetchone()
        return dict(row) if row else {}


def wait_lease_expired(database, source_id, timeout=300):
    """Start.ps1 skips launching while an unexpired lease exists; let the old
    collector's lease lapse first (same rule as the source updater)."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        status = collector_status(database, source_id)
        lease = status.get('lease_until')
        remaining = 0
        if lease and status.get('owner'):
            try:
                remaining = max(0, (parse_utc(lease) - datetime.now(timezone.utc)).total_seconds())
            except ValueError:
                remaining = 0
        if remaining == 0:
            print('旧采集租约已过期，可以启动。')
            return
        print(f'等待旧采集租约自然过期（剩余 {remaining:.0f} 秒）…')
        time.sleep(2)
    raise SystemExit('旧采集租约长时间未过期；请稍后重跑本工具或用启动脚本拉起采集。')


def wait_collector(database, source_id, timeout=300):
    started, last = time.monotonic(), None
    while time.monotonic() - started < timeout:
        status = collector_status(database, source_id)
        sample = status.get('last_sample_at')
        try:
            lease_ok = (status.get('owner') and status.get('lease_until')
                        and parse_utc(status['lease_until']) > datetime.now(timezone.utc))
            advanced = sample and (last is None or parse_utc(sample) > parse_utc(last))
            if status.get('state') == 'connected' and lease_ok and advanced and last is not None:
                print(f'采集器已重连并推进采样（最新 {sample}）。')
                return True
            last = sample if sample else last
        except (ValueError, TypeError):
            pass
        time.sleep(2)
    raise SystemExit('等待采集器重连超时；请查看采集日志，配置修改与备份已保留。')


def verify_samples(database, keys, restarted, expected_nodes):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        rows = {}
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
            for key in keys:
                row = conn.execute("SELECT sample FROM telemetry_readings WHERE point_key=?"
                                   " AND received_at >= ? ORDER BY id DESC LIMIT 1", (key, restarted)).fetchone()
                rows[key] = json.loads(row[0]) if row and row[0] else None
        if all(rows.values()):
            for key in keys:
                sample = rows[key]
                node = (sample.get('node_id') or '').rsplit('/', 1)[-1]
                print(f"{key}: quality={sample.get('quality')} value={sample.get('value')}"
                      f" node={sample.get('node_id')} received={sample.get('received_at')}")
                if sample.get('quality') != 'good':
                    raise SystemExit(f'{key} 新样本质量不是 good；可还原备份后重试。')
                if sample.get('node_id') not in expected_nodes:
                    raise SystemExit(f'{key} 新样本仍来自旧地址 {sample.get("node_id")}；请核对配置后重跑。')
            return
        time.sleep(5)
    raise SystemExit('等待两个点位的新样本超时；采集可能未恢复，请查看采集日志。')


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--config', type=Path, help='直接指定 kepserver.local.json（默认由 --install-dir 推导）')
    parser.add_argument('--install-dir', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--no-restart', action='store_true', help='只改配置，不重启采集器')
    args = parser.parse_args()
    root = args.install_dir.resolve() if args.install_dir else None
    config = args.config or (root / 'backend/config/kepserver.local.json')
    if not config.is_file():
        raise SystemExit('未找到采集配置文件：' + str(config))
    data, _ = patch(config, apply=not args.dry_run)
    keys = [p['key'] for p in data['points'] if p.get('node_id') in MOVES.values()]
    if args.dry_run or args.no_restart:
        return 0
    if root is None:
        raise SystemExit('重启采集器需要 --install-dir')
    if not (root / 'offline-install.json').is_file():
        raise SystemExit('未识别的安装目录：' + str(root))
    # StopOwnedCollector.ps1 ships beside this script (same layout as update
    # packages); installations do not keep a copy at their root.
    stop_script = Path(__file__).resolve().parent / 'StopOwnedCollector.ps1'
    if not stop_script.is_file():
        raise SystemExit('工具包缺少 StopOwnedCollector.ps1，请重新下载完整 ZIP。')
    restarted = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
    powershell(stop_script, '-InstallDir', str(root), cwd=root)
    wait_lease_expired(root / 'backend/data/dense_medium.db', data['source_id'])
    restarted = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
    powershell(root / 'Start.ps1', '-NoBrowser', cwd=root)
    wait_collector(root / 'backend/data/dense_medium.db', data['source_id'])
    time.sleep(15)  # two poll cycles at the 5-second cadence
    verify_samples(root / 'backend/data/dense_medium.db', keys, restarted, set(MOVES.values()))
    print('完成：A416/A417 压滤机运行状态已改用新地址采集并验证。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
