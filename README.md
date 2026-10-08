# SteamVR Linux wireless update fix — experimental

A source-only, build-specific community repair for a Valve Index Base Station 2.0 wireless firmware update failure in native Linux SteamVR **2.18.2 beta, build 25664061, x86_64**. It prepares a separate SteamVR copy with a ZIP metadata layout adapter and a narrowly scoped Bluetooth handoff delay. SteamVR's own update wizard still performs the update using Valve's installed, unchanged firmware.

**Evidence so far: one successful replacement-station update on one CachyOS / Index system.** That controlled experiment used the same two repair components, before they were packaged into this command-line wrapper. The packaged wrapper subsequently passed a [launch and restoration smoke test](evidence/packaged-launcher-smoke.json), with both repair components verified as loaded and normal runtime registration restored. That test performed no additional firmware update. This is not an official Valve fix or a general repair for Bluetooth, tracking, or all SteamVR versions.

## What it addresses

Two updater problems were isolated: an incompatible ZIP entry structure causes invalid memory writes and `std::bad_alloc`; after correcting that, a very short indication-confirmation-to-disconnect interval prevents the tested station from entering reachable DFU mode. The experiment completed MCU firmware **3002 → 3008**, with every logged transfer CRC matching Valve's archive. Unmodified SteamVR subsequently read back firmware 3008 with no update pending, and both stations still slept normally.

A **separate invalid-free bug in scan-result destruction remains unfixed**. The successful test kept only the station being updated powered and kept the controllers off. Single-station operation avoided that bug in this test; it is not a universal guarantee.

See [technical notes](docs/technical-notes.md), [anonymized findings](evidence/report.txt), [CRC validation](evidence/live-crc-validation.json), and [stock-runtime firmware readback](evidence/readback-anonymized.json). The [experimental release](https://github.com/alex4580/steamvr-linux-wireless-fix/releases/tag/v0.1.0) also contains the fuller sanitized report bundle.

## Requirements and limits

- Native Linux Steam and the exact supported SteamVR binaries. Flatpak, other SteamVR builds, other architectures, and other headset/base-station combinations are untested. The tool rejects unknown binary hashes.
- The installed Steam Linux Runtime 3.0 (`sniper`) version `3.0.20260805.254768`, Python 3.10 or newer, a C compiler (`gcc`), GNU binutils (`ld`, `objcopy`), GNU `cp`, and `getcap`. The tool downloads or installs no packages. Ordinary SteamVR setup must already be complete, including the stock compositor launcher's `cap_sys_nice` permission.
- Enough free space for a private SteamVR copy if the filesystem cannot use copy-on-write cloning.
- A station that still powers on and is available to SteamVR's wireless updater. This does not repair a blinking-red hardware fault or provide a separate firmware recovery protocol.

The code builds locally; no Valve executable, firmware image, or prebuilt replacement library is distributed. It never automates firmware confirmation clicks, changes firmware bytes, bypasses authentication, or claims a successful update when SteamVR reports an error. Updating firmware still writes to the station once **you** start the update in SteamVR. Do not interrupt its power during that operation.

The 500 ms delay worked in this one experiment. It is not a proven universal timing requirement, and the code is restricted to the observed call sequence. Upstream should implement the Bluetooth confirmation/disconnection lifecycle properly.

## Updates and compatibility

This release is **not version-agnostic**. It uses the firmware archive already installed by Steam, but accepts only the tested archive hash, exact SteamVR binaries, and specified Sniper runtime. It neither bundles firmware nor automatically downloads the latest firmware. If Steam installs a different archive or binary, the tool rejects it; it does not automatically adapt patch offsets to new versions.

Supporting another SteamVR build, runtime, or firmware archive requires a reviewed compatibility update to this repository and new validation. Updating the repository alone does not make an untested combination supported. A Valve fix could make this temporary repair unnecessary; use the supported native updater when that fix is available and verified.

## Use

Download and extract the [source release](https://github.com/alex4580/steamvr-linux-wireless-fix/releases/tag/v0.1.0), open a terminal in its directory, and review the source before running it. Run these commands as your normal desktop user, without `sudo`.

1. Close SteamVR normally. Check compatibility:

   ```sh
   python3 steamvr_wireless_fix.py doctor
   ```

2. Build and prepare the private session:

   ```sh
   python3 steamvr_wireless_fix.py prepare
   ```

3. Before launching, leave **only the station being updated powered** and turn both controllers off. Keep the Index connected normally. Keep the terminal open while SteamVR runs, and acknowledge that isolation explicitly:

   ```sh
   python3 steamvr_wireless_fix.py launch --single-station
   ```

4. Wait for the terminal to say `Ready` (it checks that both repair libraries are actually loaded), then use SteamVR's own update/recovery wizard. The tool does not choose a station or start a firmware write for you. If SteamVR reports failure, retain the logs and investigate that specific failure; repeated attempts are not an established remedy.

5. After the update finishes, close SteamVR normally. The supervisor waits for the copied processes to exit, then restores the original OpenVR runtime registration and SteamVR desktop handlers. If that restoration was interrupted, run the recovery command after SteamVR has closed:

   ```sh
   python3 steamvr_wireless_fix.py restore
   ```

6. Power the other station back on and start SteamVR normally through Steam. Verify both stations are detected, the update warning is gone, and native sleep/wake still works.

`doctor` and `prepare` accept `--steamvr /path/to/SteamVR` and `--sniper /path/to/SteamLinuxRuntime_sniper` when the default locations are unsuitable. Every subcommand accepts `--state-dir /private/state/path`; `launch` and `restore` use the installation paths saved there. Keep the same state directory throughout a session. Private copies and backups are retained for inspection. Use `python3 steamvr_wireless_fix.py --help` and the subcommand's `--help` for current options.

For offline verification of the repair components, see [the hardware-free test instructions](tests/README.md).

## Troubleshooting and reporting

If `doctor` rejects the build, do not bypass the hash check or apply these binary offsets to another version. A matching version label alone is insufficient. If a prepared session becomes incompatible after a Steam update, return to the installed runtime and prepare again only when that build is supported.

If an update still crashes after scanning, the unrepaired empty-string ownership defect may be involved. A connection timeout is a different failure from a parser crash; include the stage and error rather than assuming both have the same cause. The Linux “Disable Power Management” admin/helper error is also a separate unsupported helper path in the tested build; running Steam as root is not the remedy established here.

For upstream context, see Valve issues [#653](https://github.com/ValveSoftware/SteamVR-for-Linux/issues/653), [#709](https://github.com/ValveSoftware/SteamVR-for-Linux/issues/709), and [#589](https://github.com/ValveSoftware/SteamVR-for-Linux/issues/589). Review logs before sharing: they can contain home-directory paths, device serials, Bluetooth addresses, and account information. Full core dumps are not part of this project's public evidence.

Original repair code is available under the [MIT license](LICENSE). Valve's separately installed software and firmware retain their own terms.
