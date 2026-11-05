"""Read exactly four installed source files; never import application code or data."""
import argparse
import base64
from datetime import datetime
import hashlib
import json
from pathlib import Path
import stat
from urllib.request import Request, urlopen
import zlib


SOURCE_FILES = (
    'backend/app/telemetry/derived.py',
    'backend/app/hourly_brief/inputs.py',
    'backend/app/services/decision_sources.py',
    'backend/app/decision_hourly/history.py',
)
MAX_SOURCE_BYTES = 128 * 1024
MAX_TOTAL_SOURCE_BYTES = 256 * 1024
MAX_REPORT_BYTES = 60 * 1024
PURPOSE = 'runs-runtime-source-baseline-20261103-v1'


def unredirected(path):
    path = path.absolute()
    for part in reversed((path,*path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('Redirected paths are not allowed for source capture')
    return path.resolve()


def collect(root):
    root = unredirected(root)
    if not (root/'offline-install.json').is_file():
        raise ValueError('Recognized installation marker was not found')
    d_marker = (Path('D:/DshDensityControl')/'offline-install.json').is_file()
    files = {}
    total_bytes = 0
    for name in SOURCE_FILES:
        path = root/name
        unredirected(path)
        if not path.exists():
            files[name] = dict(capture=PURPOSE,install_root=str(root),d_installation_marker_present=d_marker,exists=False)
            continue
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError('Source path redirects outside the installation: '+name)
        with resolved.open('rb') as source:
            data = source.read(MAX_SOURCE_BYTES+1)
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError('Source file exceeds the bounded capture limit: '+name)
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_SOURCE_BYTES:
            raise ValueError('The four source files exceed the bounded total capture limit')
        crlf = data.count(b'\r\n')
        files[name] = dict(capture=PURPOSE,install_root=str(root),d_installation_marker_present=d_marker,exists=True,bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),utf8_bom=data.startswith(b'\xef\xbb\xbf'),
            crlf=crlf,lone_lf=data.count(b'\n')-crlf,lone_cr=data.count(b'\r')-crlf,
            encoding='zlib+base64',content=base64.b64encode(zlib.compress(data,9)).decode('ascii'))
    return dict(at=datetime.now().astimezone().isoformat(),files=files)


def encode_report(report):
    payload = json.dumps(report,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    if len(payload) > MAX_REPORT_BYTES:
        raise ValueError('The bounded upload is too large; retain the local source-capture.json for review')
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--install-dir',type=Path,required=True)
    parser.add_argument('--no-upload',action='store_true',help='Save the local capture without uploading it')
    args = parser.parse_args()
    try:
        root = unredirected(args.install_dir)
        package = unredirected(Path(__file__).absolute().parent)
        if package.is_relative_to(root) or package.is_relative_to(Path('D:/DshDensityControl').absolute()):
            raise ValueError('Extract the capture ZIP outside the installed application directory')
        target = package/'source-capture.json'
        unredirected(target)
        if target.exists() and target.stat().st_nlink > 1:
            raise ValueError('The capture output must not be a linked file')
        report = collect(root)
        target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Read-only source capture saved. No service or installed file was changed.',flush=True)
        for name,info in report['files'].items():
            print(name+': '+(info['sha256']+' ('+str(info['bytes'])+' bytes)' if info['exists'] else 'MISSING'),flush=True)
        payload = encode_report(report)
        if args.no_upload:
            print('Local capture only: '+str(target),flush=True)
            return 0
        config_path = unredirected(package/'capture-settings.json')
        config = json.loads(config_path.read_text(encoding='utf-8'))
        try:
            request = Request(config['report_url'],data=payload,headers={'Content-Type':'application/json'})
            with urlopen(request,timeout=15) as response:
                if response.status != 204:
                    raise OSError('Unexpected diagnostic upload response')
        except (OSError,ValueError):
            print('Upload did not complete. Keep source-capture.json; no installed file was changed.',flush=True)
            return 1
        print('SOURCE CAPTURE UPLOADED. No restart or production write was performed.',flush=True)
        return 0
    except Exception as exc:
        print('SOURCE CAPTURE FAILED: '+str(exc),flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
