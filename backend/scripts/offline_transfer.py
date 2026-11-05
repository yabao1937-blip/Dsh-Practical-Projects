"""Serve an offline bundle and optional Chrome installer to the deployment PC.

Installation reports are data, never commands. No directory listing, arbitrary
file access, application database access or remote shell is exposed here.
"""
import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]

# 2026-11-05 安全审计：未鉴权连接此前可无限占用线程（线程在 IP 白名单 check() 之前
# 就已生成，且无 socket 超时）。读超时 + 并发线程封顶共同封住资源占用。
HANDLER_TIMEOUT = 30
MAX_HANDLER_THREADS = 8


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """每连接一线程，但线程总数封顶：未鉴权连接也占不满门户。"""
    daemon_threads = True
    max_handler_threads = MAX_HANDLER_THREADS

    def __init__(self, address, handler):
        self._slots = threading.BoundedSemaphore(self.max_handler_threads)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self._reject_busy(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    @staticmethod
    def _reject_busy(request):
        try:
            request.sendall(b'HTTP/1.1 503 Service Unavailable\r\n'
                            b'Content-Length: 0\r\nConnection: close\r\n\r\n')
        except OSError:
            pass
        finally:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            request.close()


def serve(output):
    latest = json.loads((output/'latest.json').read_text(encoding='utf-8'))
    settings = json.loads((output/'transfer-settings.json').read_text(encoding='utf-8'))
    bundle = Path(latest['bundle']).resolve()
    if not bundle.is_relative_to(output.resolve()) or bundle.suffix != '.zip':
        raise ValueError('Bundle must be a ZIP inside the configured output directory')
    chrome = (output/'extras'/'GoogleChromeStandaloneEnterprise64.msi').resolve()
    if not chrome.is_relative_to(output.resolve()):
        raise ValueError('Chrome installer must stay inside the configured output directory')
    compatibility = (output/'extras'/'BrowserCompatibilityUpdate.zip').resolve()
    if not compatibility.is_relative_to(output.resolve()):
        raise ValueError('Compatibility update must stay inside the configured output directory')
    project_update = (output/'extras'/'ProjectUpdate.zip').resolve()
    diagnostics = (output/'extras'/'ProjectDiagnostics.zip').resolve()
    readonly_bridge = (output/'extras'/'ReadOnlyBridge.zip').resolve()
    storage_move = (output/'extras'/'MoveStorageToD.zip').resolve()
    advice_backfill = (output/'extras'/'BackfillHourlyAdvice.zip').resolve()
    yl_points = (output/'extras'/'UpdateYlPoints.zip').resolve()
    runs_points = (output/'extras'/'AddRunsPoints.zip').resolve()
    if not runs_points.is_relative_to(output.resolve()):
        raise ValueError('RUNS point tool must stay inside the configured output directory')
    if not storage_move.is_relative_to(output.resolve()):
        raise ValueError('Storage migration must stay inside the configured output directory')
    if not advice_backfill.is_relative_to(output.resolve()):
        raise ValueError('Advice backfill must stay inside the configured output directory')
    if not yl_points.is_relative_to(output.resolve()):
        raise ValueError('Press-state point update must stay inside the configured output directory')
    prefix = '/'+settings['token']
    allowed = {settings['client'], settings['bind'], '127.0.0.1'}

    class Handler(BaseHTTPRequestHandler):
        # 见模块顶 HANDLER_TIMEOUT 说明。
        timeout = HANDLER_TIMEOUT

        def check(self):
            if self.client_address[0] not in allowed:
                self.send_error(403)
                return False
            return True

        def do_GET(self):
            if not self.check():
                return
            path = urlsplit(self.path).path
            if path == '/':
                self.send_response(302)
                self.send_header('Location', prefix+'/')
                self.end_headers()
            elif path == '/runs-points.zip' and runs_points.is_file():
                self.send_response(302)
                self.send_header('Location', prefix+'/runs-points.zip')
                self.end_headers()
            elif path in (prefix, prefix+'/'):
                storage = (f'<h2>C 盘空间：迁移数据到 D 盘</h2>'
                    f'<p><a href="{prefix}/storage-move.zip">下载 D 盘数据迁移工具</a></p>'
                    f'<p>全部解压后运行 <b>MoveStorageToD.cmd</b>。停服复制并校验，程序入口保留在 C 盘。</p>'
                    f'<p>原 C 盘副本暂时保留；迁移成功后按 README 中的校验清理步骤释放空间。</p><hr>'
                    if storage_move.is_file() else '')
                readonly = (f'<h2>笔记本测试服 · 主机只读数据接入</h2>'
                    f'<p><a href="{prefix}/readonly-bridge.zip">下载只读数据接入包</a></p>'
                    f'<p>在主机全部解压到新文件夹，双击 <b>EnableReadOnlyTest.cmd</b>。'
                    f'仅短暂重启网页服务，保留数据库、采集、模型与原配置。</p><hr>'
                    if readonly_bridge.is_file() else '')
                backfill = (f'<h2>密度趋势 · 小时建议密度补档</h2>'
                    f'<p><a href="{prefix}/advice-backfill.zip">下载小时建议补档工具</a></p>'
                    f'<p>在主机全部解压到新文件夹，双击 <b>BackfillHourlyAdvice.cmd</b>。'
                    f'用已存储的小时证据按系统当前算法回补近 7 天缺失的小时建议密度归档；'
                    f'不停服务、不改代码，只向 gpt_hourly_density_advice 增加带 origin 标记的行，'
                    f'已有归档不改动。完成后在密度趋势对比页按 Ctrl＋F5。</p><hr>'
                    if advice_backfill.is_file() else '')
                points = (f'<h2>采集点位 · 压滤机 A416/A417 运行状态新地址</h2>'
                    f'<p><a href="{prefix}/yl-points.zip">下载点位地址更新工具</a></p>'
                    f'<p>在主机全部解压到新文件夹，双击 <b>UpdateYlPoints.cmd</b>。'
                    f'把 A416/A417 压滤机运行状态两点改到新 OPC 地址（XMZX-YM.YM4_2），'
                    f'自动备份配置、只重启本机采集器并验证新点位采样正常；'
                    f'可先加 --dry-run 预览。建议在项目更新包安装完成后再运行。</p><hr>'
                    if yl_points.is_file() else '')
                project = (f'<h2>已有主机：本次项目更新</h2>'
                    f'<p><a href="{prefix}/project-update.zip">下载本次项目更新包（保留主机数据库）</a></p>'
                    f'<p>右键 ZIP → 全部解压，进入文件夹，双击 <b>UpdateProject.cmd</b>。成功后按 Ctrl＋F5。</p>'
                    f'<p>本次更新密度趋势三图，并将脱粉 473/474 切到 RUNS。按包内 README 先预检再分组件安装；'
                    f'只备份本次受影响的源码、配置与迁移元数据，保留原始化验、历史数据和模型。</p><hr>'
                    if project_update.is_file() else '')
                update = (f'<hr><h2>浏览器兼容更新</h2>'
                    f'<p><a href="{prefix}/browser-compat.zip">下载浏览器兼容更新包（无需重装系统）</a></p>'
                    f'<p>右键 ZIP → 全部解压，再双击 <b>UpdateBrowserCompat.cmd</b>。完成后回到系统页面按 Ctrl＋F5。</p>'
                    if compatibility.is_file() else '')
                browser = (f'<hr><h2>Google Chrome 浏览器</h2>'
                    f'<p><a href="{prefix}/chrome.msi">下载 Chrome 64位完整离线安装包（{chrome.stat().st_size/1048576:.0f} MB）</a></p>'
                    f'<p>下载完成后双击 MSI 文件安装，无需解压。安装时如出现 Windows 确认窗口，选择“是”。</p>'
                    f'<p>安装后用 Chrome 打开 <a href="http://127.0.0.1:8000/">http://127.0.0.1:8000/</a>。</p>'
                    if chrome.is_file() else '')
                runs = (f'<h2>本次新增：脱粉 RUNS 诊断点</h2>'
                    f'<p><a href="{prefix}/runs-points.zip">下载新增脱粉 RUNS 点位包（AddRunsPoints.zip）</a></p>'
                    f'<p>全部解压后双击 <b>AddRunsPoints.cmd</b>。只追加 473、474 两路 RUNS，保留旧 if_prod 和模型绑定；'
                    f'仅采集器短暂重启，网页保持运行。设备含义待明日现场对照。</p><hr>'
                    if runs_points.is_file() else '')
                content = (f'<!doctype html><meta charset="utf-8"><title>离线安装</title>'
                    f'<body style="font:22px sans-serif;padding:32px;line-height:1.7">'
                    f'{runs}{storage}{project}{readonly}{backfill}{points}'+(f'<h2>主机故障诊断</h2><p><a href="{prefix}/diagnostics.zip">下载一键诊断包</a>：全部解压后运行 DiagnoseProject.cmd，不停止服务、不修改数据库。</p><hr>' if diagnostics.is_file() else '')+
                    f'<h2>首次安装 · Windows 10 64位 · 重介密控系统</h2>'
                    f'<p><a href="{prefix}/download.zip">下载离线安装包（{bundle.stat().st_size/1048576:.0f} MB）</a></p>'
                    f'<ol><li>下载完成后，右键 ZIP → 全部解压。</li>'
                    f'<li>进入解压文件夹，双击 <b>Install.cmd</b>。</li>'
                    f'<li>安装完成会自动打开本机系统，桌面生成 DshDensityControl 快捷方式。</li></ol>'
                    f'<p>安装到当前用户目录，不覆盖其他项目。无需互联网或预装Python。</p>'
                    f'<p>采集数据需要之后接通 KEPServer 所在生产网络。</p>{browser}{update}</body>').encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            elif path == prefix+'/download.zip':
                self.download(bundle, 'application/zip')
            elif path == prefix+'/chrome.msi' and chrome.is_file():
                self.download(chrome, 'application/x-msi')
            elif path == prefix+'/browser-compat.zip' and compatibility.is_file():
                self.download(compatibility, 'application/zip')
            elif path == prefix+'/project-update.zip' and project_update.is_file():
                self.download(project_update, 'application/zip')
            elif path == prefix+'/diagnostics.zip' and diagnostics.is_file():
                self.download(diagnostics, 'application/zip')
            elif path == prefix+'/readonly-bridge.zip' and readonly_bridge.is_file():
                self.download(readonly_bridge, 'application/zip')
            elif path == prefix+'/advice-backfill.zip' and advice_backfill.is_file():
                self.download(advice_backfill, 'application/zip')
            elif path == prefix+'/yl-points.zip' and yl_points.is_file():
                self.download(yl_points, 'application/zip')
            elif path == prefix+'/runs-points.zip' and runs_points.is_file():
                self.download(runs_points, 'application/zip')
            elif path == prefix+'/storage-move.zip' and storage_move.is_file():
                self.download(storage_move, 'application/zip')
            else:
                self.send_error(404)

        def download(self, file, content_type):
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Disposition', 'attachment; filename="'+file.name+'"')
            self.send_header('Content-Length', str(file.stat().st_size))
            self.end_headers()
            with file.open('rb') as stream:
                shutil.copyfileobj(stream, self.wfile, length=1024*1024)

        def do_POST(self):
            if not self.check():
                return
            if self.client_address[0] != settings['client']:
                self.send_error(403)
                return
            report_path = urlsplit(self.path).path
            if report_path not in (prefix+'/status',prefix+'/diagnostics'):
                self.send_error(404)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= (65536 if report_path.endswith('/diagnostics') else 16384):
                    raise ValueError()
                report = json.loads(self.rfile.read(size))
                if not isinstance(report, dict):
                    raise ValueError()
                if report_path.endswith('/diagnostics'):
                    report = {k:report[k] for k in ('at','disk_free_mb','files','processes','http','database','logs') if k in report}
                    (output/'target-diagnostics.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                    self.send_response(204)
                    self.end_headers()
                    return
                if report.get('stage') not in ('verifying', 'copying', 'dependencies', 'starting', 'installed', 'failed'):
                    raise ValueError()
                # Keep a small, known schema. Never interpret a report as code.
                report = {k:report[k] for k in ('stage', 'message', 'healthy', 'revision', 'at') if k in report}
                report['received_at'] = datetime.now().astimezone().isoformat()
                report['client'] = self.client_address[0]
                (output/'target-install-status.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                with (output/'target-install-events.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(report, ensure_ascii=False)+'\n')
            except (ValueError, TypeError):
                self.send_error(400)
                return
            self.send_response(204)
            self.end_headers()

    print(f'http://{settings["bind"]}:{settings["port"]}{prefix}/', flush=True)
    BoundedThreadingHTTPServer((settings['bind'], settings['port']), Handler).serve_forever()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT/'outputs/offline-win10-x64')
    parser.add_argument('--daemon', action='store_true')
    args = parser.parse_args()
    if args.daemon:
        with (args.output/'transfer.log').open('ab') as log:
            child = subprocess.Popen([sys.executable, '-X', 'utf8', '-u', str(Path(__file__).resolve()), '--output', str(args.output.resolve())],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
                close_fds=True)
        (args.output/'transfer.pid').write_text(str(child.pid), encoding='ascii')
        print('Transfer process started: '+str(child.pid))
    else:
        serve(args.output)


if __name__ == '__main__':
    main()
