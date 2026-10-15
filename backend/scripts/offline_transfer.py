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
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]


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
    prefix = '/'+settings['token']
    allowed = {settings['client'], settings['bind'], '127.0.0.1'}

    class Handler(BaseHTTPRequestHandler):
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
            elif path in (prefix, prefix+'/'):
                update = (f'<hr><h2>浏览器兼容更新</h2>'
                    f'<p><a href="{prefix}/browser-compat.zip">下载浏览器兼容更新包（无需重装系统）</a></p>'
                    f'<p>右键 ZIP → 全部解压，再双击 <b>UpdateBrowserCompat.cmd</b>。完成后回到系统页面按 Ctrl＋F5。</p>'
                    if compatibility.is_file() else '')
                browser = (f'<hr><h2>Google Chrome 浏览器</h2>'
                    f'<p><a href="{prefix}/chrome.msi">下载 Chrome 64位完整离线安装包（{chrome.stat().st_size/1048576:.0f} MB）</a></p>'
                    f'<p>下载完成后双击 MSI 文件安装，无需解压。安装时如出现 Windows 确认窗口，选择“是”。</p>'
                    f'<p>安装后用 Chrome 打开 <a href="http://127.0.0.1:8000/">http://127.0.0.1:8000/</a>。</p>'
                    if chrome.is_file() else '')
                content = (f'<!doctype html><meta charset="utf-8"><title>离线安装</title>'
                    f'<body style="font:22px sans-serif;padding:32px;line-height:1.7">'
                    f'<h2>Windows 10 64位 · 重介密控系统</h2>'
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
            if urlsplit(self.path).path != prefix+'/status':
                self.send_error(404)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 16384:
                    raise ValueError()
                report = json.loads(self.rfile.read(size))
                if not isinstance(report, dict):
                    raise ValueError()
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
    ThreadingHTTPServer((settings['bind'], settings['port']), Handler).serve_forever()


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
