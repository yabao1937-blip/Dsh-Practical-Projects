"""Execute the installer's actual sidecar-copy function in isolated folders."""
import os
from pathlib import Path
import subprocess

import pytest

from point_metadata import Catalog, MARKER


INSTALL = Path(__file__).parents[1]/'scripts/windows_offline/Install.ps1'


def copy_with_installer(source, target, tmp_path):
    runner = tmp_path/'copy-test.ps1'
    runner.write_text('''param($Installer,$Source,$Destination)
$ErrorActionPreference='Stop'
$tokens=$null; $errors=$null
$tree=[Management.Automation.Language.Parser]::ParseFile($Installer,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'Installer syntax error' }
$function=$tree.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Copy-PointMetadata'}, $true)
. ([ScriptBlock]::Create($function.Extent.Text))
Copy-PointMetadata -Source $Source -Destination $Destination
''', encoding='utf-8-sig')
    return subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(runner),
        str(INSTALL),str(source),str(target)],capture_output=True,timeout=30)


@pytest.mark.skipif(os.name != 'nt', reason='Windows offline installer')
def test_fresh_and_resumed_install_preserve_metadata_versions(tmp_path):
    source, target = tmp_path/'snapshot'/'point-metadata', tmp_path/'installed'/'point-metadata'
    original = {'item_id':'channel.device.original'}
    packed = Catalog(source).pack(original)
    assert copy_with_installer(source,target,tmp_path).returncode == 0
    assert Catalog(target).unpack(packed) == original
    installed = target/(packed[MARKER]+'.json')
    timestamp = installed.stat().st_mtime_ns
    assert copy_with_installer(source,target,tmp_path).returncode == 0
    assert installed.stat().st_mtime_ns == timestamp
    installed.write_text('{}', encoding='utf-8')
    assert copy_with_installer(source,target,tmp_path).returncode != 0
    assert installed.read_text() == '{}'


@pytest.mark.skipif(os.name != 'nt', reason='Windows offline installer')
def test_corrupt_snapshot_is_rejected_before_database_publish(tmp_path):
    source, target = tmp_path/'source', tmp_path/'target'
    packed = Catalog(source).pack({'item_id':'channel.device.original'})
    (source/(packed[MARKER]+'.json')).write_text('{}', encoding='utf-8')
    assert copy_with_installer(source,target,tmp_path).returncode != 0
    assert not list(target.glob('*.json'))
    text = INSTALL.read_text(encoding='utf-8-sig')
    assert text.index('Copy-PointMetadata -Source') < text.index("Copy-Item -LiteralPath (Join-Path $taskRoot 'snapshot\\dense_medium.db')")
