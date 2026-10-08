#!/usr/bin/env python3
"""Experimental, exact-build SteamVR wireless-update repair. No firmware is bundled."""
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time

NRF_SHA256 = "840fcca06c14ecdec68567ac41fc347f2dfa0ae26d425283f45006599ff7d49a"
MONITOR_SHA256 = "6aa3f55de3d80e5299ddbf26afce6dc48ca34edab6896969d70175a3a1144e3e"
PATCHED_SHA256 = "060d7ef2ffded261738068f959ecdb8d7f3ee906af8018fb0ca72e92c5955310"
NORDIC_SHA256 = "924ed4f0c7d8e9baa8b3b6ea70a5a579de3e1c40294529a32776cc5240fec6a2"
FIRMWARE_SHA256 = "1e0885a02844a36f38ec356915d48345c17d68c7f686dfcf86d71350155a80d0"
FIRMWARE_RELATIVE = "tools/lighthouse/firmware/lighthouse_tx_vader/archive/428/lighthouse_tx_vader_radio_full_dfu.v2_9_2004771.zip"
SNIPER_VERSION = "3.0.20260805.254768"
SCHEMA = 1
VR_NAMES = {"vrmonitor", "vrmonitor.real", "vrserver", "vrcompositor", "vrstartup", "vrwebhelper", "vrdashboard", "vrcompositor-launcher"}


class FixError(ValueError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise FixError(message)


def within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def verify_install(root: Path) -> dict:
    root = Path(root).resolve()
    expected = {"libnrfserlib.so": NRF_SHA256, "vrmonitor": MONITOR_SHA256, "libpc_ble_driver_shared_sd_api_v6.so": NORDIC_SHA256, FIRMWARE_RELATIVE: FIRMWARE_SHA256}
    for name, wanted in expected.items():
        file = root/name if name == FIRMWARE_RELATIVE else root/"bin/linux64"/name
        require(file.is_file() and not file.is_symlink() and within(file.resolve(), root), f"Expected an ordinary file contained in the installation: {file}")
        actual = digest(file.read_bytes())
        require(actual == wanted, f"Unsupported {name} SHA-256: {actual}. This experiment supports only the documented 2.18.2 build; no patch was applied.")
    return expected


def patch_library(original: bytes, adapter: bytes) -> bytes:
    require(digest(original) == NRF_SHA256, "Unsupported original libnrfserlib.so")
    require(len(adapter) == 227, "Unexpected adapter size")
    blob = bytearray(original)
    require(blob[:6] == b"\x7fELF\x02\x01", "Expected little-endian ELF64")
    phoff, = struct.unpack_from("<Q", blob, 32)
    phsize, phnum = struct.unpack_from("<HH", blob, 54)
    require((phsize, phnum) == (56, 11), "Unexpected program headers")
    headers = [list(struct.unpack_from("<IIQQQQQQ", blob, phoff+i*phsize)) for i in range(phnum)]
    loads = [h for h in headers if h[0] == 1]
    align = lambda n: (n+4095) & ~4095
    vaddr = align(max(h[3]+h[6] for h in loads))
    offset = align(len(blob))
    require((vaddr, offset) == (0xed000, 0xe8000), "Unexpected ELF layout")
    for site, wanted in {0x5ec7d: bytes.fromhex("e8 0e 8d 01 00"), 0x5ed18: bytes.fromhex("e8 73 8c 01 00")}.items():
        segment = next(h for h in loads if h[3] <= site < h[3]+h[5])
        pos = segment[2] + site-segment[3]
        require(blob[pos:pos+5] == wanted, "Unexpected original call instruction")
        blob[pos:pos+5] = b"\xe8" + struct.pack("<i", vaddr-site-5)
    require(sum(h[0] == 4 for h in headers) == 1, "Expected one PT_NOTE descriptor")
    headers = [h for h in headers if h[0] != 4]
    headers.append([1, 5, offset, vaddr, vaddr, len(adapter), len(adapter), 4096])
    for i, header in enumerate(headers):
        struct.pack_into("<IIQQQQQQ", blob, phoff+i*phsize, *header)
    blob.extend(b"\0"*(offset-len(blob)))
    blob.extend(adapter)
    require(digest(blob) == PATCHED_SHA256, "Generated library differs from the successfully tested patch")
    return bytes(blob)


def command(args, **kwargs):
    try:
        return subprocess.run([str(x) for x in args], check=True, **kwargs)
    except (OSError, subprocess.CalledProcessError) as error:
        raise FixError(f"Command failed: {args[0]}: {error}") from error


def build_artifacts(build_dir: Path, source_dir: Path | None = None) -> dict:
    build_dir = Path(build_dir)
    source_dir = Path(source_dir) if source_dir else Path(__file__).resolve().parent / "native"
    require(not os.path.lexists(build_dir), "Build directory already exists; use a fresh directory")
    build_dir.mkdir(parents=True)
    for name in ("adapter.o", "adapter.elf", "adapter.bin", "libnrf_handoff_timing.so"):
        require(not os.path.lexists(build_dir/name), "Build output already exists; use a fresh build directory")
    command(["gcc", "-c", source_dir/"adapter.S", "-o", build_dir/"adapter.o"])
    command(["ld", "-e", "compact_getzipitem", "-Ttext=0xed000", "--defsym=original_getzipitem=0x77990", build_dir/"adapter.o", "-o", build_dir/"adapter.elf"])
    command(["objcopy", "-O", "binary", "--only-section=.text", build_dir/"adapter.elf", build_dir/"adapter.bin"])
    command(["gcc", "-Wall", "-Wextra", "-Werror", "-O2", "-shared", "-fPIC", "-fvisibility=hidden", source_dir/"handoff_timing.c", "-pthread", "-ldl", "-o", build_dir/"libnrf_handoff_timing.so"])
    return {"adapter": build_dir/"adapter.bin", "shim": build_dir/"libnrf_handoff_timing.so"}


def snapshot(path: Path) -> dict:
    path = Path(path)
    require(not path.is_symlink(), f"Refusing to overwrite a symlink: {path}")
    if not path.exists():
        return {"exists": False}
    require(path.is_file(), f"Expected ordinary file: {path}")
    data = path.read_bytes()
    return {"exists": True, "sha256": digest(data), "content_b64": base64.b64encode(data).decode(), "mode": stat.S_IMODE(path.stat().st_mode)}


def snapshot_bytes(state: dict) -> bytes:
    data = base64.b64decode(state["content_b64"], validate=True)
    require(digest(data) == state["sha256"], "Backup content does not match its hash")
    return data


def same_file(left: dict, right: dict) -> bool:
    return left["exists"] == right["exists"] and (not left["exists"] or (left["sha256"], left["mode"]) == (right["sha256"], right["mode"]))


def atomic_write(path: Path, data: bytes, mode=0o600):
    path = Path(path)
    require(not path.is_symlink(), f"Refusing symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="."+path.name+".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def save_manifest(state_dir: Path, manifest: dict):
    atomic_write(state_dir/"manifest.json", (json.dumps(manifest, indent=2)+"\n").encode())


def load_manifest(state_dir: Path) -> dict:
    path = state_dir/"manifest.json"
    require(path.is_file(), f"No recovery manifest at {path}; run prepare first")
    manifest = json.loads(path.read_text())
    require(manifest.get("schema") == SCHEMA, "Unsupported recovery manifest schema")
    require(manifest.get("state_dir") == str(state_dir.resolve()), "Recovery directory moved; refusing ambiguous paths")
    require(manifest.get("copy") == str(state_dir.resolve()/"SteamVR"), "Unexpected copy path in manifest")
    allowed = {str(path) for _, path in integration_paths()}
    for record in manifest.get("files", []):
        require(record.get("path") in allowed, "Unexpected integration path in recovery manifest")
        require(within(Path(record["backup"]).resolve(), state_dir.resolve()), "Backup path escaped the owned state directory")
    for name in manifest.get("artifacts", {}):
        require(not Path(name).is_absolute() and within((state_dir/name).resolve(), state_dir.resolve()), "Artifact path escaped the owned state directory")
    return manifest


@contextlib.contextmanager
def state_lock(state_dir: Path):
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(not state_dir.is_symlink(), "State directory must not be a symlink")
    info = state_dir.stat()
    require(info.st_uid == os.getuid() and not info.st_mode & 0o022, "State directory must be owned by you and not writable by other users")
    require(not (state_dir/"session.lock").is_symlink(), "Session lock must not be a symlink")
    with (state_dir/"session.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FixError("Another command supervises this session; close SteamVR normally first") from error
        yield


def running_vr_processes(copy_root: Path | None = None) -> list[dict]:
    result = []
    root = Path(copy_root).resolve() if copy_root else None
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            comm = (proc/"comm").read_text().strip()
            inaccessible = False
            try:
                executable = Path(os.readlink(proc/"exe").removesuffix(" (deleted)"))
                args = [arg.decode(errors="replace") for arg in (proc/"cmdline").read_bytes().split(b"\0") if arg]
            except PermissionError:
                # A capable compositor may be non-dumpable even for its owner.
                # A known VR process with unreadable details must not be treated
                # as absent just because ptrace access is restricted.
                executable, args, inaccessible = Path("unreadable"), [], True
            if root:
                matches = within(executable, root) or any(arg.startswith(str(root)+"/bin/") for arg in args)
                if not matches:
                    # Also follow runtime helpers/descendants whose executable is
                    # outside the copy; inspect only this exact environment marker.
                    marker = b"VR_OVERRIDE="+os.fsencode(root)
                    try:
                        matches = marker in (proc/"environ").read_bytes().split(b"\0")
                    except PermissionError:
                        inaccessible = True
                matches = matches or (inaccessible and comm in VR_NAMES)
            else:
                matches = executable.name in VR_NAMES or comm in VR_NAMES or any(Path(arg).name in {"vrstartup.sh", "vrstartup-helper.sh", "vrmonitor.sh"} for arg in args[:3])
            if matches and int(proc.name) != os.getpid():
                result.append({"pid": int(proc.name), "executable": str(executable), "name": comm})
        except (OSError, ProcessLookupError):
            continue
    return result


def require_stopped():
    active = running_vr_processes()
    require(not active, "SteamVR is running. Finish any update and close SteamVR through its GUI before continuing. Active PIDs: "+", ".join(str(p["pid"]) for p in active))


def validate_state_path(path: Path):
    require(not any(char.isspace() or char == ":" for char in str(path)), "The state directory cannot contain whitespace or ':' because LD_PRELOAD treats them as library separators; choose a different --state-dir")


def monitor_ready(process: dict, copy: Path, shim: Path) -> bool:
    if process["executable"] != str(copy/"bin/linux64/vrmonitor.real"):
        return False
    try:
        mappings = (Path("/proc")/str(process["pid"])/"maps").read_text()
        paths = {fields[5] for line in mappings.splitlines() if len(fields := line.split(maxsplit=5)) == 6}
        return {str(copy/"bin/linux64/libnrfserlib.so"), str(shim)} <= paths
    except OSError:
        return False


def default_steam() -> Path:
    for path in [Path.home()/".local/share/Steam", Path.home()/".steam/steam"]:
        if (path/"steamapps").is_dir():
            return path.resolve()
    raise FixError("Native Steam was not found; install/run native Steam first")


def installation_paths(args) -> tuple[Path, Path, Path]:
    steam = default_steam()
    install = Path(args.steamvr).expanduser().resolve() if args.steamvr else steam/"steamapps/common/SteamVR"
    sniper = Path(args.sniper).expanduser().resolve() if args.sniper else steam/"steamapps/common/SteamLinuxRuntime_sniper"
    return install, sniper, steam


def verify_sniper(sniper: Path):
    require((sniper/"run-in-sniper").is_file(), "Native Steam Linux Runtime sniper is required")
    versions = (sniper/"VERSIONS.txt").read_text()
    require(any(line.split()[:2] == ["sniper", SNIPER_VERSION] for line in versions.splitlines()), f"Only the tested sniper runtime {SNIPER_VERSION} is currently supported")


def verify_capability(install: Path):
    launcher = install/"bin/linux64/vrcompositor-launcher"
    output = command(["getcap", launcher], capture_output=True, text=True).stdout
    require("cap_sys_nice=" in output, "The stock compositor launcher lacks cap_sys_nice. Complete ordinary SteamVR setup first; this tool never grants capabilities or runs as root.")


def verify_dependencies():
    require(platform.system() == "Linux" and platform.machine() == "x86_64", "Only native x86-64 Linux is supported")
    for name in ("gcc", "ld", "objcopy", "cp", "getcap", "bash"):
        require(shutil.which(name), f"Required existing tool is unavailable: {name}")


def apparent_size(directory: Path) -> int:
    total = 0
    for parent, dirs, files in os.walk(directory, followlinks=False):
        for name in files:
            file = Path(parent)/name
            if not file.is_symlink():
                total += file.stat().st_size
    return total


def faithful_copy(install: Path, destination: Path, state_dir: Path):
    fd, probe_name = tempfile.mkstemp(prefix=".reflink-probe-", dir=state_dir)
    os.close(fd)
    probe = Path(probe_name)
    cloned = subprocess.run(["cp", "--reflink=always", str(install/"bin/linux64/libnrfserlib.so"), str(probe)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    probe.unlink(missing_ok=True)
    required = 256*1024*1024 if cloned else apparent_size(install)+256*1024*1024
    require(shutil.disk_usage(state_dir).free >= required, f"Insufficient free space: need at least {required//(1024*1024)} MiB for {'reflink metadata and changes' if cloned else 'a full copy (reflink unavailable)'}")
    require(not os.path.lexists(destination), "Copy destination already exists; use a fresh state directory")
    command(["cp", "-a", "--no-preserve=xattr", "--reflink=auto", str(install), str(destination)])


def prepare(args, state_dir: Path):
    require_stopped()
    validate_state_path(state_dir)
    verify_dependencies()
    install, sniper, steam = installation_paths(args)
    verify_install(install)
    verify_sniper(sniper)
    verify_capability(install)
    require(not within(state_dir, install) and not within(install, state_dir), "State directory and installed SteamVR must not contain one another")
    require(not (state_dir/"manifest.json").exists(), "This state directory already contains a session; use it with launch/restore or choose a fresh --state-dir")
    copy = state_dir/"SteamVR"
    manifest = {"schema": SCHEMA, "state_dir": str(state_dir), "install": str(install), "sniper": str(sniper), "steam": str(steam), "copy": str(copy), "status": "preparing", "files": [], "created": time.time()}
    save_manifest(state_dir, manifest)
    artifacts = build_artifacts(state_dir/"build")
    patched = patch_library((install/"bin/linux64/libnrfserlib.so").read_bytes(), artifacts["adapter"].read_bytes())
    faithful_copy(install, copy, state_dir)
    verify_install(copy)
    for relative in ("bin/linux64/libnrfserlib.so", "bin/linux64/vrmonitor", "bin/linux64/vrcompositor-launcher", "bin/vrsetup.sh"):
        file = copy/relative
        require(not file.is_symlink() and within(file.resolve(), copy.resolve()), f"Copy modification target escapes its directory: {relative}")
    atomic_write(copy/"bin/linux64/libnrfserlib.so", patched, 0o755)
    launcher = copy/"bin/linux64/vrcompositor-launcher"
    launcher.unlink()
    launcher.symlink_to(install/"bin/linux64/vrcompositor-launcher")
    setup = copy/"bin/vrsetup.sh"
    text = setup.read_text()
    old_cap = 'getcap "${STEAMVR_TOOLSDIR}/bin/linux64/vrcompositor-launcher"'
    old_assign = 'local VRCOMPOSITOR_LAUNCHER="${STEAMVR_TOOLSDIR}/bin/linux64/vrcompositor-launcher"'
    require(text.count(old_cap) == 1 and text.count(old_assign) == 1, "Unexpected vrsetup.sh; refusing unreviewed setup edits")
    text = text.replace(old_cap, 'getcap "$(readlink -f "${STEAMVR_TOOLSDIR}/bin/linux64/vrcompositor-launcher")"')
    text = text.replace(old_assign, 'local VRCOMPOSITOR_LAUNCHER="$(readlink -f "${STEAMVR_TOOLSDIR}/bin/linux64/vrcompositor-launcher")"')
    atomic_write(setup, text.encode(), 0o755)
    monitor = copy/"bin/linux64/vrmonitor"
    real = monitor.with_name("vrmonitor.real")
    monitor.rename(real)
    shim = artifacts["shim"].resolve()
    wrapper = "#!/bin/bash\nset -eu\nexport LD_PRELOAD="+shlex.quote(str(shim))+"\nexec -a \"$0\" "+shlex.quote(str(real))+" \"$@\"\n"
    atomic_write(monitor, wrapper.encode(), 0o755)
    manifest["artifacts"] = {str(path.relative_to(state_dir)): digest(path.read_bytes()) for path in [artifacts["adapter"], shim, monitor, real, setup, copy/"bin/linux64/libnrfserlib.so", copy/"bin/linux64/libpc_ble_driver_shared_sd_api_v6.so", copy/FIRMWARE_RELATIVE]}
    manifest["status"] = "prepared"
    save_manifest(state_dir, manifest)
    verify_install(install)
    print(f"Prepared exact-build private copy: {copy}\nInstalled SteamVR is unchanged. No firmware action has run.")


def integration_paths() -> list[tuple[str, Path]]:
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home()/".config")))
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home()/".local/share")))
    return [("openvr", config/"openvr/openvrpaths.vrpath"),
            ("desktop", data/"applications/valve-vrmonitor.desktop"),
            ("desktop", data/"applications/valve-steamvr.desktop")]


def plan_integration(manifest: dict, state_dir: Path) -> list[dict]:
    records = []
    backup_dir = state_dir/("backup-"+str(time.time_ns()))
    backup_dir.mkdir(mode=0o700)
    for kind, path in integration_paths():
        before = snapshot(path)
        require(before["exists"], f"Run stock SteamVR once before using this tool; integration file is absent: {path}")
        data = snapshot_bytes(before)
        if kind == "openvr":
            original = json.loads(data)
            require(isinstance(original, dict) and original.get("runtime") == [manifest["install"]], "OpenVR must be registered to the selected stock SteamVR using its canonical path. Run stock SteamVR once or resolve an existing path alias/configuration before using this experiment.")
            temporary = dict(original)
            temporary["runtime"] = [manifest["copy"]]
            expected_bytes = (json.dumps(temporary, indent=2)+"\n").encode()
        else:
            require(manifest["install"].encode() in data, f"Desktop handler does not refer to selected stock SteamVR: {path}")
            expected_bytes = data.replace(manifest["install"].encode(), manifest["copy"].encode())
        backup = backup_dir/path.name
        atomic_write(backup, data, before["mode"])
        expected = {"exists": True, "sha256": digest(expected_bytes), "content_b64": base64.b64encode(expected_bytes).decode(), "mode": before["mode"]}
        records.append({"kind": kind, "path": str(path), "before": before, "expected": expected, "backup": str(backup), "copy_runtime": manifest["copy"]})
    return records


def restore_file(record: dict) -> bool:
    """Compare-and-restore one owned change; never overwrite unrelated changes."""
    path = Path(record["path"])
    current = snapshot(path)
    before, expected = record["before"], record["expected"]
    if same_file(current, before):
        return True
    if same_file(current, expected):
        if before["exists"]:
            atomic_write(path, snapshot_bytes(before), before["mode"])
        else:
            path.unlink()
        return True
    if record.get("kind") == "openvr" and current["exists"] and before["exists"]:
        try:
            now = json.loads(snapshot_bytes(current))
            old = json.loads(snapshot_bytes(before))
            if not isinstance(now, dict) or not isinstance(old, dict):
                return False
            if now.get("runtime") == old.get("runtime"):
                return True
            if now.get("runtime") == [record["copy_runtime"]]:
                now["runtime"] = old["runtime"]
                atomic_write(path, (json.dumps(now, indent=2)+"\n").encode(), current["mode"])
                return True
        except (ValueError, KeyError, TypeError):
            pass
    return False


def restore_state(state_dir: Path) -> bool:
    require_stopped()
    manifest = load_manifest(state_dir)
    conflicts = []
    for record in manifest.get("files", []):
        try:
            if not restore_file(record):
                conflicts.append(record["path"])
        except (OSError, FixError) as error:
            conflicts.append(f"{record['path']}: {error}")
    manifest["status"] = "recovery_needed" if conflicts else "restored"
    manifest["restore_conflicts"] = conflicts
    save_manifest(state_dir, manifest)
    if conflicts:
        print("Unrelated integration changes were preserved. Review these files and their manifest backups:\n"+"\n".join(conflicts), file=sys.stderr)
        return False
    print("Original OpenVR runtime and desktop handlers restored. Private artifacts retained for inspection; installed SteamVR was not patched.")
    return True


def verify_prepared(manifest: dict, state_dir: Path):
    verify_dependencies()
    require(manifest["status"] in {"prepared", "restored"}, "Session is incomplete or needs recovery; run restore first")
    verify_install(Path(manifest["install"]))
    verify_sniper(Path(manifest["sniper"]))
    verify_capability(Path(manifest["install"]))
    for name, expected in manifest["artifacts"].items():
        file = state_dir/name
        require(not file.is_symlink() and digest(file.read_bytes()) == expected, f"Prepared artifact changed: {name}")
    require((Path(manifest["copy"])/"bin/linux64/vrcompositor-launcher").resolve() == Path(manifest["install"])/"bin/linux64/vrcompositor-launcher", "Compositor launcher no longer refers to the original capable file")


def launch(args, state_dir: Path):
    require(args.single_station, "launch requires --single-station: power only the one Base Station 2.0 being updated and turn the controllers off. The separate multi-station scan crash is not fixed.")
    require_stopped()
    validate_state_path(state_dir)
    manifest = load_manifest(state_dir)
    verify_prepared(manifest, state_dir)
    manifest["files"] = plan_integration(manifest, state_dir)
    manifest["status"] = "launching"
    manifest["ready_verified"] = False
    manifest["supervisor_pid"] = os.getpid()
    save_manifest(state_dir, manifest)  # Recovery information precedes every external write.
    for record in manifest["files"]:
        require(same_file(snapshot(Path(record["path"])), record["before"]), "Integration changed during launch preparation; run restore to review")
        atomic_write(Path(record["path"]), snapshot_bytes(record["expected"]), record["expected"]["mode"])
    copy = Path(manifest["copy"])
    env = dict(os.environ)
    for name in ("LD_PRELOAD", "LD_LIBRARY_PATH", "STEAMVR_VRENV", "STEAMVR_TOOLSDIR", "VALVE_SKIP_RUNTIME_SAFETY", "PRESSURE_VESSEL_RUNTIME", "_STEAMVR_LOGGING_READY"):
        env.pop(name, None)
    env.update(VR_OVERRIDE=str(copy), STEAM_BASE_FOLDER=manifest["steam"])
    old_handlers = {}
    def do_not_interrupt(signum, frame):
        print("\nSteamVR was not stopped. Finish any update and close SteamVR through its GUI; restoration will follow. Keep this supervisor running.", flush=True)
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        old_handlers[signum] = signal.signal(signum, do_not_interrupt)
    started = time.monotonic()
    quiet_since = None
    seen_runtime = False
    ready = False
    readiness_warning = False
    child = None
    try:
        with (state_dir/"launch.log").open("ab", buffering=0) as log:
            child = subprocess.Popen([str(Path(manifest["sniper"])/"run-in-sniper"), "--filesystem="+str(state_dir), "--", str(copy/"bin/vrstartup.sh")], env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            manifest["status"] = "running"
            save_manifest(state_dir, manifest)
            print("Experimental SteamVR is starting. Wait for the verified-readiness message before attempting an update. Close SteamVR normally afterward; this supervisor waits for every copied VR process before restoring integration.\nDo not interrupt power or stop processes during an update.", flush=True)
            while True:
                active = running_vr_processes(copy)
                seen_runtime = seen_runtime or any(Path(p["executable"]).name in {"vrmonitor.real", "vrserver"} for p in active)
                now = time.monotonic()
                if not ready and any(monitor_ready(process, copy, state_dir/"build/libnrf_handoff_timing.so") for process in active):
                    ready = True
                    manifest["ready_verified"] = True
                    save_manifest(state_dir, manifest)
                    print("Ready: the private vrmonitor maps both the corrected ZIP library and the timing shim. Firmware updates remain manual in SteamVR's GUI.", flush=True)
                if not ready and not readiness_warning and now-started > 60:
                    readiness_warning = True
                    print("Readiness was not verified. Do not start an update. Close SteamVR normally and inspect launch.log; no process will be forcibly stopped.", file=sys.stderr, flush=True)
                if active or child.poll() is None:
                    quiet_since = None
                elif seen_runtime or now-started > 60:
                    if quiet_since is None:
                        quiet_since = now
                    elif now-quiet_since >= 15:
                        break
                time.sleep(1)
        require(not running_vr_processes(copy), "Copied VR process still running; restore postponed")
        restored = restore_state(state_dir)
        if not ready:
            print("The private monitor's two repair libraries were never verified. Launch failed; inspect launch.log. No firmware action was initiated by this tool.", file=sys.stderr)
        return restored and ready
    except BaseException:
        manifest = load_manifest(state_dir)
        manifest["status"] = "recovery_needed"
        save_manifest(state_dir, manifest)
        print("Recovery manifest preserved. No VR process was terminated. Close SteamVR normally, then run restore.", file=sys.stderr)
        raise
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def doctor(args, state_dir: Path):
    verify_dependencies()
    install, sniper, steam = installation_paths(args)
    hashes = verify_install(install)
    verify_sniper(sniper)
    verify_capability(install)
    print(json.dumps({"supported_exact_build": True, "install": str(install), "sniper": str(sniper), "sniper_version": SNIPER_VERSION, "hashes": hashes, "state_dir": str(state_dir), "running_vr_processes": running_vr_processes(), "experimental_scope": "Base Station 2.0 only; one station powered; controllers off; no automatic firmware commands"}, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    default_state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home()/".local/state")))/"steamvr-linux-wireless-fix"
    for name in ("doctor", "prepare", "launch", "restore"):
        sub = commands.add_parser(name)
        sub.add_argument("--steamvr", help="native installed SteamVR directory (doctor/prepare)")
        sub.add_argument("--sniper", help="native SteamLinuxRuntime_sniper directory (doctor/prepare)")
        sub.add_argument("--state-dir", default=str(default_state), help="private session and recovery directory")
        if name == "launch":
            sub.add_argument("--single-station", action="store_true", help="acknowledge only the target Base Station 2.0 is powered and controllers are off")
    args = parser.parse_args(argv)
    try:
        require(os.geteuid() != 0, "Do not run this experimental tool or SteamVR as root")
        state_dir = Path(args.state_dir).expanduser().absolute()
        require(not state_dir.is_symlink(), "State directory cannot be a symlink")
        state_dir = state_dir.resolve()
        validate_state_path(state_dir)
        if args.action == "doctor":
            doctor(args, state_dir)
            return 0
        if args.action == "prepare":
            install, _, _ = installation_paths(args)
            require(not within(state_dir, install) and not within(install, state_dir), "State directory and installed SteamVR must not contain one another")
        with state_lock(state_dir):
            if args.action == "prepare":
                prepare(args, state_dir)
            elif args.action == "launch":
                return 0 if launch(args, state_dir) else 1
            else:
                return 0 if restore_state(state_dir) else 1
        return 0
    except (FixError, OSError, ValueError, KeyError) as error:
        print("Error: "+str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
