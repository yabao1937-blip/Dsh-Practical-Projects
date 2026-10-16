"""Package the standalone stdlib host reader; never include production data."""
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
output = ROOT/'outputs/offline-win10-x64'
settings = json.loads((output/'transfer-settings.json').read_text(encoding='utf-8'))
target = output/'extras/ProjectDiagnostics.zip'
with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
    for name in ('DiagnoseProject.cmd','diagnose_project.py'):
        archive.write(ROOT/'backend/scripts/windows_offline'/name,name)
    archive.writestr('diagnostic-config.json',json.dumps(dict(
        report_url=f'http://{settings["bind"]}:{settings["port"]}/{settings["token"]}/diagnostics')))
print(str(target),target.stat().st_size,'bytes')
