"""Safety contracts. Tests use synthetic data and never start SteamVR."""
from __future__ import annotations
import argparse
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('steamvr_wireless_fix', ROOT / 'steamvr_wireless_fix.py')
fix = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fix
spec.loader.exec_module(fix)


class TemporaryFiles(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='wireless-safety-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.file = self.root / 'integration.json'

    def write(self, data: bytes, mode=0o640):
        self.file.write_bytes(data)
        self.file.chmod(mode)
        return fix.snapshot(self.file)

    def record(self, before, expected, **extra):
        return {'path': str(self.file), 'before': before, 'expected': expected, **extra}


class VersionAndArchitectureTests(TemporaryFiles):
    def test_unknown_library_is_refused_before_any_mutation(self):
        location = self.root / 'SteamVR/bin/linux64'; location.mkdir(parents=True)
        nrf = location / 'libnrfserlib.so'; nrf.write_bytes(b'unsupported build')
        monitor = location / 'vrmonitor'; monitor.write_bytes(b'unsupported monitor')
        before = {p: p.read_bytes() for p in (nrf, monitor)}
        with self.assertRaises(fix.FixError): fix.verify_install(self.root / 'SteamVR')
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(set(location.iterdir()), set(before))

    def test_missing_install_is_refused_without_creating_it(self):
        missing = self.root / 'missing'
        with self.assertRaises(fix.FixError): fix.verify_install(missing)
        self.assertFalse(missing.exists())

    def test_symlinked_critical_library_is_refused(self):
        location = self.root / 'SteamVR/bin/linux64'; location.mkdir(parents=True)
        outside = self.root / 'outside'; outside.write_bytes(b'leave this alone')
        (location / 'libnrfserlib.so').symlink_to(outside)
        with self.assertRaises(fix.FixError): fix.verify_install(self.root / 'SteamVR')
        self.assertEqual(outside.read_bytes(), b'leave this alone')

    def test_unknown_patch_input_fails_closed(self):
        with self.assertRaises(fix.FixError): fix.patch_library(b'not an ELF file', bytes(227))

    def test_wrong_architecture_and_os_fail_before_build(self):
        for system, machine in [('Linux', 'aarch64'), ('Windows', 'x86_64')]:
            with self.subTest(system=system, machine=machine), mock.patch.object(fix.platform, 'system', return_value=system), mock.patch.object(fix.platform, 'machine', return_value=machine), mock.patch.object(fix.shutil, 'which') as which:
                with self.assertRaises(fix.FixError): fix.verify_dependencies()
                which.assert_not_called()

    def test_each_supported_component_is_independently_hash_gated(self):
        install = self.root / 'SteamVR'
        components = [
            ('NRF_SHA256', install / 'bin/linux64/libnrfserlib.so'),
            ('MONITOR_SHA256', install / 'bin/linux64/vrmonitor'),
            ('NORDIC_SHA256', install / 'bin/linux64/libpc_ble_driver_shared_sd_api_v6.so'),
            ('FIRMWARE_SHA256', install / fix.FIRMWARE_RELATIVE),
        ]
        with contextlib.ExitStack() as stack:
            for index, (constant, path) in enumerate(components):
                path.parent.mkdir(parents=True, exist_ok=True)
                data = f'known component {index}'.encode(); path.write_bytes(data)
                stack.enter_context(mock.patch.object(fix, constant, fix.digest(data)))
            self.assertEqual(len(fix.verify_install(install)), 4)
            for constant, path in components:
                with self.subTest(component=constant):
                    original = path.read_bytes(); path.write_bytes(original + b' changed')
                    with self.assertRaises(fix.FixError): fix.verify_install(install)
                    self.assertEqual(path.read_bytes(), original + b' changed')
                    path.write_bytes(original)

    def test_source_parent_symlink_cannot_escape_selected_install(self):
        install = self.root / 'SteamVR'; install.mkdir()
        outside = self.root / 'outside'; (outside / 'linux64').mkdir(parents=True)
        (outside / 'linux64/libnrfserlib.so').write_bytes(b'untouched external file')
        (install / 'bin').symlink_to(outside)
        with self.assertRaises(fix.FixError): fix.verify_install(install)
        self.assertEqual((outside / 'linux64/libnrfserlib.so').read_bytes(), b'untouched external file')

    def test_build_refuses_symlinked_output_directory(self):
        outside = self.root / 'outside-build'; outside.mkdir()
        sentinel = outside / 'adapter.o'; sentinel.write_bytes(b'user-owned output')
        build = self.root / 'build'; build.symlink_to(outside)
        with mock.patch.object(fix, 'command') as command:
            with self.assertRaises(fix.FixError): fix.build_artifacts(build)
            command.assert_not_called()
        self.assertEqual(sentinel.read_bytes(), b'user-owned output')

    def test_build_refuses_preexisting_output_directory(self):
        build = self.root / 'build'; build.mkdir()
        sentinel = build / 'adapter.bin'; sentinel.write_bytes(b'preserve prior artifact')
        with mock.patch.object(fix, 'command') as command:
            with self.assertRaises(fix.FixError): fix.build_artifacts(build)
            command.assert_not_called()
        self.assertEqual(sentinel.read_bytes(), b'preserve prior artifact')


class RestoreTests(TemporaryFiles):
    def test_restore_recovers_exact_original_bytes_and_mode(self):
        before = self.write(b'original\x00binary-safe\xff', 0o640)
        expected = self.write(b'temporary configuration', 0o600)
        self.assertTrue(fix.restore_file(self.record(before, expected)))
        self.assertEqual(self.file.read_bytes(), b'original\x00binary-safe\xff')
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o640)
        self.assertTrue(fix.restore_file(self.record(before, expected)))

    def test_newly_created_file_is_removed_only_when_unchanged(self):
        before = fix.snapshot(self.file)
        expected = self.write(b'temporary configuration')
        self.assertTrue(fix.restore_file(self.record(before, expected)))
        self.assertFalse(self.file.exists())

    def test_user_content_change_is_preserved(self):
        before = self.write(b'original')
        expected = self.write(b'temporary')
        self.file.write_bytes(b'user edited this during the session')
        self.assertFalse(fix.restore_file(self.record(before, expected)))
        self.assertEqual(self.file.read_bytes(), b'user edited this during the session')

    def test_user_permission_change_is_preserved(self):
        before = self.write(b'original', 0o600)
        expected = self.write(b'temporary', 0o600)
        self.file.chmod(0o640)
        self.assertFalse(fix.restore_file(self.record(before, expected)))
        self.assertEqual(self.file.read_bytes(), b'temporary')
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o640)

    def test_user_symlink_replacement_is_not_followed(self):
        before = self.write(b'original')
        expected = self.write(b'temporary')
        other = self.root / 'other'; other.write_bytes(b'user-owned target')
        self.file.unlink(); self.file.symlink_to(other)
        with self.assertRaises(fix.FixError): fix.restore_file(self.record(before, expected))
        self.assertTrue(self.file.is_symlink())
        self.assertEqual(other.read_bytes(), b'user-owned target')

    def test_modified_backup_fails_before_overwrite(self):
        before = self.write(b'original')
        expected = self.write(b'temporary')
        before['content_b64'] = base64.b64encode(b'corrupted backup').decode()
        with self.assertRaises(fix.FixError): fix.restore_file(self.record(before, expected))
        self.assertEqual(self.file.read_bytes(), b'temporary')

    def test_openvr_restore_preserves_unrelated_runtime_updates(self):
        stock, copied = '/test/stock/SteamVR', '/test/private/SteamVR'
        before = self.write(json.dumps({'runtime': [stock], 'external_drivers': ['original']}).encode())
        expected = self.write(json.dumps({'runtime': [copied], 'external_drivers': ['original']}).encode())
        self.file.write_text(json.dumps({'runtime': [copied], 'external_drivers': ['new-user-driver'], 'new_user_key': 42}))
        self.assertTrue(fix.restore_file(self.record(before, expected, kind='openvr', copy_runtime=copied)))
        self.assertEqual(json.loads(self.file.read_text()), {'runtime': [stock], 'external_drivers': ['new-user-driver'], 'new_user_key': 42})

    def test_openvr_third_party_runtime_selection_is_preserved(self):
        stock, copied = '/test/stock/SteamVR', '/test/private/SteamVR'
        before = self.write(json.dumps({'runtime': [stock]}).encode())
        expected = self.write(json.dumps({'runtime': [copied]}).encode())
        chosen = {'runtime': ['/test/user-selected/runtime'], 'new_user_key': True}
        self.file.write_text(json.dumps(chosen))
        self.assertFalse(fix.restore_file(self.record(before, expected, kind='openvr', copy_runtime=copied)))
        self.assertEqual(json.loads(self.file.read_text()), chosen)

    def test_snapshot_rejects_directory_and_symlink(self):
        with self.assertRaises(fix.FixError): fix.snapshot(self.root)
        self.file.symlink_to(self.root / 'absent')
        with self.assertRaises(fix.FixError): fix.snapshot(self.file)


class LifecycleGuards(TemporaryFiles):
    def test_prepare_and_restore_refuse_active_runtime_before_other_work(self):
        active = [{'pid': 12345, 'executable': '/test/SteamVR/bin/linux64/vrserver', 'name': 'vrserver'}]
        with mock.patch.object(fix, 'running_vr_processes', return_value=active), mock.patch.object(fix, 'verify_dependencies') as deps, mock.patch.object(fix, 'load_manifest') as manifest:
            with self.assertRaises(fix.FixError): fix.prepare(argparse.Namespace(), self.root / 'state')
            with self.assertRaises(fix.FixError): fix.restore_state(self.root / 'state')
            deps.assert_not_called(); manifest.assert_not_called()
        self.assertFalse((self.root / 'state').exists())

    def test_launch_requires_single_station_acknowledgement(self):
        with mock.patch.object(fix, 'load_manifest') as manifest, mock.patch.object(fix.subprocess, 'Popen') as popen:
            with self.assertRaises(fix.FixError): fix.launch(argparse.Namespace(single_station=False), self.root)
            manifest.assert_not_called(); popen.assert_not_called()

    def test_state_symlink_is_refused(self):
        other = self.root / 'other'; other.mkdir()
        linked = self.root / 'linked'; linked.symlink_to(other)
        with self.assertRaises(fix.FixError), fix.state_lock(linked): pass
        self.assertEqual(list(other.iterdir()), [])

    def test_world_writable_state_is_refused(self):
        state = self.root / 'state'; state.mkdir(); state.chmod(0o777)
        with self.assertRaises(fix.FixError), fix.state_lock(state): pass
        self.assertEqual(list(state.iterdir()), [])

    def test_existing_state_lock_is_exclusive(self):
        state = self.root / 'state'
        with fix.state_lock(state):
            with self.assertRaises(fix.FixError), fix.state_lock(state): pass

    def test_restore_rejects_moved_state_directory(self):
        state = self.root / 'state'; state.mkdir()
        (state / 'manifest.json').write_text(json.dumps({'schema': fix.SCHEMA, 'state_dir': str(self.root / 'other')}))
        with self.assertRaises(fix.FixError): fix.load_manifest(state)

    def test_symlinked_lock_file_is_refused(self):
        state = self.root / 'state'; state.mkdir()
        outside = self.root / 'outside'; outside.write_bytes(b'owned data')
        (state / 'session.lock').symlink_to(outside)
        with self.assertRaises(fix.FixError), fix.state_lock(state): pass
        self.assertEqual(outside.read_bytes(), b'owned data')

    def test_launch_refuses_active_runtime_before_integration_writes(self):
        with mock.patch.object(fix, 'running_vr_processes', return_value=[{'pid': 12}]), mock.patch.object(fix, 'load_manifest') as manifest, mock.patch.object(fix.subprocess, 'Popen') as popen:
            with self.assertRaises(fix.FixError): fix.launch(argparse.Namespace(single_station=True), self.root)
            manifest.assert_not_called(); popen.assert_not_called()

    def test_root_execution_is_refused_before_state_creation(self):
        state = self.root / 'state'
        with mock.patch.object(fix.os, 'geteuid', return_value=0), contextlib.redirect_stderr(io.StringIO()):
            result = fix.main(['prepare', '--state-dir', str(state)])
        self.assertEqual(result, 1)
        self.assertFalse(state.exists())

    def test_manifest_cannot_restore_an_unrelated_path(self):
        state = self.root / 'state'; state.mkdir()
        outside = self.root / 'unrelated'; outside.write_bytes(b'user data')
        manifest = {'schema': fix.SCHEMA, 'state_dir': str(state), 'copy': str(state / 'SteamVR'),
                    'files': [{'path': str(outside), 'backup': str(state / 'backup')}]}
        (state / 'manifest.json').write_text(json.dumps(manifest))
        with mock.patch.object(fix, 'integration_paths', return_value=[('desktop', self.file)]):
            with self.assertRaises(fix.FixError): fix.load_manifest(state)
        self.assertEqual(outside.read_bytes(), b'user data')

    def test_manifest_artifact_escape_is_refused(self):
        state = self.root / 'state'; state.mkdir()
        outside = self.root / 'outside'; outside.mkdir()
        (state / 'escape').symlink_to(outside)
        for artifact in ('../outside/data', str(outside / 'data'), 'escape/data'):
            with self.subTest(artifact=artifact):
                manifest = {'schema': fix.SCHEMA, 'state_dir': str(state), 'copy': str(state / 'SteamVR'),
                            'artifacts': {artifact: 'unused'}, 'files': []}
                (state / 'manifest.json').write_text(json.dumps(manifest))
                with self.assertRaises(fix.FixError): fix.load_manifest(state)

    def test_manifest_backup_escape_is_refused(self):
        state = self.root / 'state'; state.mkdir()
        manifest = {'schema': fix.SCHEMA, 'state_dir': str(state), 'copy': str(state / 'SteamVR'),
                    'files': [{'path': str(self.file), 'backup': str(self.root / 'outside-backup')}]}
        (state / 'manifest.json').write_text(json.dumps(manifest))
        with mock.patch.object(fix, 'integration_paths', return_value=[('desktop', self.file)]):
            with self.assertRaises(fix.FixError): fix.load_manifest(state)

    def test_prepare_state_cannot_overlap_installed_tree(self):
        install = self.root / 'installed'; install.mkdir()
        for state in (install / 'private-state', self.root):
            with self.subTest(state=state), mock.patch.object(fix, 'installation_paths', return_value=(install, self.root / 'sniper', self.root / 'steam')), mock.patch.object(fix, 'state_lock') as lock, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(fix.main(['prepare', '--state-dir', str(state)]), 1)
                lock.assert_not_called()
        self.assertFalse((install / 'private-state').exists())

    def test_launch_only_starts_runtime_and_never_drives_firmware(self):
        state = self.root / 'state'; state.mkdir()
        copied = state / 'SteamVR'
        manifest = {'install': str(self.root / 'installed'), 'copy': str(copied),
                    'sniper': str(self.root / 'sniper'), 'steam': str(self.root / 'steam'),
                    'status': 'prepared', 'files': []}
        copied_process = {'pid': 123, 'executable': str(copied / 'bin/linux64/vrserver'), 'name': 'vrserver'}
        child = mock.Mock(); child.poll.return_value = 0
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(fix, 'running_vr_processes', side_effect=[[], [copied_process], [], [], []]))
            stack.enter_context(mock.patch.object(fix, 'load_manifest', return_value=manifest))
            stack.enter_context(mock.patch.object(fix, 'verify_prepared'))
            stack.enter_context(mock.patch.object(fix, 'monitor_ready', return_value=True))
            stack.enter_context(mock.patch.object(fix, 'plan_integration', return_value=[]))
            stack.enter_context(mock.patch.object(fix, 'save_manifest'))
            restore = stack.enter_context(mock.patch.object(fix, 'restore_state', return_value=True))
            popen = stack.enter_context(mock.patch.object(fix.subprocess, 'Popen', return_value=child))
            stack.enter_context(mock.patch.object(fix.time, 'monotonic', side_effect=[0, 1, 2, 18]))
            stack.enter_context(mock.patch.object(fix.time, 'sleep'))
            stack.enter_context(mock.patch.object(fix.signal, 'signal'))
            stack.enter_context(mock.patch.dict(fix.os.environ, {'LD_PRELOAD': '/unrelated.so', 'LD_LIBRARY_PATH': '/unrelated'}))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertTrue(fix.launch(argparse.Namespace(single_station=True), state))
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertEqual(command, [str(self.root / 'sniper/run-in-sniper'), '--filesystem=' + str(state), '--', str(copied / 'bin/vrstartup.sh')])
        env = popen.call_args.kwargs['env']
        self.assertNotIn('LD_PRELOAD', env); self.assertNotIn('LD_LIBRARY_PATH', env)
        self.assertEqual(env['VR_OVERRIDE'], str(copied))
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        child.terminate.assert_not_called(); child.kill.assert_not_called(); child.send_signal.assert_not_called()
        restore.assert_called_once_with(state)


class ReadinessTests(TemporaryFiles):
    def mapping(self, path):
        return f'1000-2000 r-xp 00000000 00:00 1 {path}\n'

    def test_readiness_requires_exact_private_monitor_and_both_libraries(self):
        copy = self.root / 'SteamVR'; shim = self.root / 'build/libnrf_handoff_timing.so'
        process = {'pid': 12, 'executable': str(copy / 'bin/linux64/vrmonitor.real')}
        library = copy / 'bin/linux64/libnrfserlib.so'
        for mappings, expected in [
            (self.mapping(library) + self.mapping(shim), True),
            (self.mapping(library), False),
            (self.mapping(shim), False),
            (self.mapping(str(library) + '.backup') + self.mapping(shim), False),
            (self.mapping(library) + self.mapping(str(shim) + '.backup'), False),
            (self.mapping(str(library) + ' (deleted)') + self.mapping(shim), False),
        ]:
            with self.subTest(mappings=mappings), mock.patch.object(Path, 'read_text', return_value=mappings):
                self.assertEqual(fix.monitor_ready(process, copy, shim), expected)
        process['executable'] = '/other/vrmonitor.real'
        with mock.patch.object(Path, 'read_text') as read:
            self.assertFalse(fix.monitor_ready(process, copy, shim))
            read.assert_not_called()

    def test_unreadable_process_maps_is_not_ready(self):
        copy = self.root / 'SteamVR'
        process = {'pid': 12, 'executable': str(copy / 'bin/linux64/vrmonitor.real')}
        with mock.patch.object(Path, 'read_text', side_effect=PermissionError):
            self.assertFalse(fix.monitor_ready(process, copy, self.root / 'shim.so'))

    def test_preload_ambiguous_state_paths_are_rejected_before_writes(self):
        for name in ('state with spaces', 'state:separator', 'state\tseparator', 'state\nseparator'):
            with self.subTest(name=name), contextlib.redirect_stderr(io.StringIO()), mock.patch.object(fix, 'state_lock') as lock:
                state = self.root / name
                self.assertEqual(fix.main(['prepare', '--state-dir', str(state)]), 1)
                lock.assert_not_called(); self.assertFalse(state.exists())


if __name__ == '__main__':
    unittest.main()
