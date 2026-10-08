#!/usr/bin/env python3
"""Opt-in, hardware-isolated checks using the user's own matching Valve files.

No Valve files are distributed with these tests. All generated code, extracted
payloads, and copied libraries live in a TemporaryDirectory and are removed.
This program does not start SteamVR or call any Bluetooth API.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
NATIVE = PROJECT / 'tests' / 'native'
FIRMWARE_RELATIVE = Path('tools/lighthouse/firmware/lighthouse_tx_vader/archive/428/lighthouse_tx_vader_radio_full_dfu.v2_9_2004771.zip')
FIRMWARE_SHA256 = '1e0885a02844a36f38ec356915d48345c17d68c7f686dfcf86d71350155a80d0'

def load_tool():
    spec = importlib.util.spec_from_file_location('steamvr_wireless_fix', PROJECT / 'steamvr_wireless_fix.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

def run(command, **kwargs):
    return subprocess.run([str(item) for item in command], check=True, text=True,
                          capture_output=True, timeout=30, **kwargs)

def sandbox(work: Path, command: list[str], valve_root: Path | None = None,
            env: dict[str, str] | None = None, readonly_work: bool = False):
    args = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session',
            '--ro-bind', '/usr', '/usr', '--symlink', 'usr/lib', '/lib',
            '--symlink', 'usr/lib', '/lib64', '--dev', '/dev', '--proc', '/proc',
            '--tmpfs', '/tmp', '--clearenv', '--setenv', 'PATH', '/usr/bin',
            '--setenv', 'LC_ALL', 'C', '--ro-bind' if readonly_work else '--bind',
            str(work), '/work', '--chdir', '/work']
    if valve_root:
        args += ['--ro-bind', str(valve_root / 'bin' / 'linux64'), '/valve',
                 '--setenv', 'LD_LIBRARY_PATH', '/valve']
    for key, value in (env or {}).items():
        args += ['--setenv', key, value]
    return subprocess.run(args + command, capture_output=True, text=True, timeout=30)

def timing_tests(work: Path, shim: Path):
    fake = work / 'fake'; fake.mkdir()
    common = ['gcc', '-O2', '-Wall', '-Wextra', '-Werror']
    run(common + ['-shared', '-fPIC', NATIVE / 'fake_backend.c',
        '-Wl,-soname,libpc_ble_driver_shared_sd_api_v6.so', '-o', fake / 'libpc_ble_driver_shared_sd_api_v6.so'])
    link = ['-L' + str(fake), '-lpc_ble_driver_shared_sd_api_v6', '-Wl,-rpath,$ORIGIN']
    run(common + [NATIVE / 'timing.c'] + link + ['-o', fake / 'timing'])
    run(common + ['-shared', '-fPIC', '-Dmain=run_test', NATIVE / 'timing.c'] + link + ['-o', fake / 'timing_local.so'])
    run(common + [NATIVE / 'local_loader.c', '-ldl', '-o', fake / 'local_loader'])
    names = sorted(line.split()[-1] for line in run(['nm', '-D', '--defined-only', shim]).stdout.splitlines())
    if names != ['sd_ble_gap_disconnect', 'sd_ble_gattc_hv_confirm']:
        raise RuntimeError('Unexpected timing shim exports')
    shim_in_work = '/work/' + str(shim.relative_to(work))
    results = []
    for mode, command in [
        ('global', ['/work/fake/timing']),
        ('local', ['/work/fake/local_loader', '/work/fake/timing_local.so']),
        ('expiry', ['/work/fake/timing']),
    ]:
        env = {'LD_PRELOAD': shim_in_work, 'LD_LIBRARY_PATH': '/work/fake'}
        if mode == 'expiry': env['TEST_EXPIRY'] = '1'
        result = sandbox(work, command, env=env, readonly_work=True)
        if result.returncode or result.stderr.count('delaying matching disconnect 500ms once') != 1:
            raise RuntimeError(f'Timing test {mode} failed: {result.stdout}\n{result.stderr}')
        if 'every backend called exactly once' not in result.stdout:
            raise RuntimeError(f'Missing timing assertions: {mode}')
        results.append({'mode': mode, 'status': 'PASS'})
    return results

def zip_reader_tests(work: Path, valve_root: Path, tool, adapter: Path):
    source = valve_root / 'bin' / 'linux64' / 'libnrfserlib.so'
    original = source.read_bytes()
    if hashlib.sha256(original).hexdigest() != tool.NRF_SHA256:
        raise RuntimeError('Valve library changed after initial validation')
    firmware = valve_root / FIRMWARE_RELATIVE
    archive = firmware.read_bytes()
    if hashlib.sha256(archive).hexdigest() != FIRMWARE_SHA256:
        raise RuntimeError('Unsupported firmware archive; no native code will run')
    (work / 'firmware.zip').write_bytes(archive)
    patched = tool.patch_library(original, adapter.read_bytes())
    if hashlib.sha256(patched).hexdigest() != tool.PATCHED_SHA256:
        raise RuntimeError('Unexpected copied-library SHA256')
    (work / 'patched.so').write_bytes(patched)
    run(['gcc', '-O2', '-Wall', '-Wextra', '-Werror', NATIVE / 'zip_reader.c', '-ldl', '-o', work / 'zip_reader'])
    records = []
    for mode, lib in [('original', '/valve/libnrfserlib.so'), ('patched', '/work/patched.so')]:
        result = sandbox(work, ['/work/zip_reader', lib, '/work/firmware.zip', mode], valve_root)
        if mode == 'original':
            if result.returncode not in (-6, 134, -11, 139) or 'MODE\toriginal' not in result.stdout:
                raise RuntimeError(f'Original reader did not reproduce the supported bug (exit {result.returncode}); stop and inspect locally: {result.stdout}\n{result.stderr}')
            records.append({'mode': mode, 'expected_memory_fault_reproduced': True,
                            'signal': 'SIGSEGV' if result.returncode in (-11, 139) else 'SIGABRT',
                            'bad_alloc_reported': 'std::bad_alloc' in result.stderr})
        else:
            if result.returncode or 'PASS: actual firmware ZIP reader' not in result.stdout:
                raise RuntimeError(f'Patched reader failed: {result.stdout}\n{result.stderr}')
            verified = []
            with zipfile.ZipFile(work / 'firmware.zip') as z:
                if z.testzip() is not None: raise RuntimeError('ZIP integrity check failed')
                for round_number in range(2):
                    for index, name in enumerate(z.namelist()):
                        value = (work / f'full-{round_number}-entry-{index}.bin').read_bytes()
                        if value != z.read(name): raise RuntimeError('Native decoder differs from Python zipfile')
                        verified.append({'round': round_number, 'entry': name, 'bytes': len(value),
                                         'sha256': hashlib.sha256(value).hexdigest()})
            records.append({'mode': mode, 'status': 'PASS', 'byte_identical_payloads': verified})
    if source.read_bytes() != original or firmware.read_bytes() != archive:
        raise RuntimeError('Source installation changed during the test')
    return records

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--steamvr', type=Path, help='Local matching SteamVR installation; omit for timing-only checks')
    args = parser.parse_args()
    for program in ('gcc', 'ld', 'objcopy', 'nm', 'bwrap'):
        if not shutil.which(program): parser.error(f'Missing required program: {program}')
    tool = load_tool()
    valve_root = args.steamvr.expanduser().resolve() if args.steamvr else None
    if valve_root: tool.verify_install(valve_root)
    with tempfile.TemporaryDirectory(prefix='steamvr-wireless-offline-tests-') as temp:
        work = Path(temp)
        artifacts = tool.build_artifacts(work / 'build')
        report = {'scope': 'Hardware-free isolated tests; SteamVR was not started',
                  'timing': timing_tests(work, artifacts['shim'])}
        if valve_root:
            report['archive_reader'] = zip_reader_tests(work, valve_root, tool, artifacts['adapter'])
        print(json.dumps(report, indent=2))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
