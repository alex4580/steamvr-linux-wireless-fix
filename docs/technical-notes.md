# Findings and scope of the experimental repair

The analysis below concerns native Linux x86_64 SteamVR 2.18.2 beta, app build 25664061. Stock stable 2.17.10 also crashed on this system, but the adapter offsets and identities below are specific to the beta build. Findings come from saved core memory, disassembly, offline harnesses, logs, and one controlled hardware test; they have not been confirmed against Valve's source.

## Exact identities

| Artifact | SHA-256 |
| --- | --- |
| `libnrfserlib.so` | `840fcca06c14ecdec68567ac41fc347f2dfa0ae26d425283f45006599ff7d49a` |
| `vrmonitor` | `6aa3f55de3d80e5299ddbf26afce6dc48ca34edab6896969d70175a3a1144e3e` |
| `lighthouse_tx_vader_radio_full_dfu.v2_9_2004771.zip` | `1e0885a02844a36f38ec356915d48345c17d68c7f686dfcf86d71350155a80d0` |

The tested host used CachyOS, an Index HMD, and two Base Station 2.0 units. One replacement station needed MCU firmware 3002 → 3008. Native Bluetooth sleep/wake already worked before the repair. The Steam client build was 1788652215; Steam client beta opt-in was not verified. SteamVR ran through `sniper_platform_3.0.20260805.254768`, not Proton.

## 1. ZIP entry layout mismatch: reproduced offline

The firmware reader in `libnrfserlib.so` at module offset `0x5ebc0` supplies a record using a 260-byte filename field and reads its uncompressed size at record offset `0x130`. Its `GetZipItem` implementation uses a 4096-byte filename field, a total record size of `0x1030`, and the size field at `0x1028`.

The saved crash contains the correct firmware length, 297956 bytes, at `record+0x1028`, while the reader tries to allocate a pointer-like 139716371572944 bytes from `record+0x130`. The mismatch also writes beyond the caller's stack object. It is not ordinary memory exhaustion, and limiting the allocation would leave the invalid writes in place. A platform-dependent filename constant is a plausible source-level cause, not a confirmed source finding.

The assembly adapter allocates the larger record, calls the existing implementation, and copies its fields into the smaller caller layout. Only the two relevant call sites are redirected in a private library copy. It preserves return values and leaves the decoder and archive payloads unchanged.

A hardware-isolated harness reproduced `std::bad_alloc` with the original full reader. The repaired copy read all three archive entries byte-for-byte identically to Python's ZIP decoder, read them again using the same reader object, and preserved the missing-file error path. See [the full reader results](../evidence/offline-full-reader-results.json) and [detailed analysis](../evidence/technical-appendix.txt).

## 2. Scan-result empty-string ownership: diagnosed, not repaired

A separate crash occurs while `vrmonitor` destroys a scan-result vector. Its string field points to `libnrfserlib.so`'s static empty COW-string storage. The destructor recognizes only `vrmonitor`'s own empty-string singleton, then decrements and attempts to free the other module's static storage. Both modules contain private C++ runtime code.

This finding is independent of the ZIP mismatch. The successful hardware test avoided the observed trigger by powering only the replacement station and leaving controllers off; the community repair does not alter this destructor. A successful single-station test does not establish that all single-station scans avoid it. See [the core evidence](../evidence/empty-string-evidence.txt).

Valve should investigate consistent C++ runtime linkage and object ownership across the interface. Suppressing arbitrary `free` calls or changing allocation environment variables would not establish correct ownership.

## 3. Bluetooth DFU handoff: evidence supports a timing defect

With the ZIP repair alone, SteamVR parsed the archive, authenticated, received buttonless-DFU response `20 01 01`, and then timed out reaching the bootloader. The log showed an indication confirmation requested at 00:49:10.175811 and a local disconnect at 00:49:10.176429, less than 1 ms apart.

Nordic's API documents a successful `sd_ble_gattc_hv_confirm` call as queueing the confirmation for transmission. Its [reference updater](https://github.com/NordicSemiconductor/pc-nrfutil/blob/master/nordicsemi/dfu/dfu_transport_ble.py) waits for the peer to disconnect before reconnecting to the bootloader. The discovered characteristic was `0x0003`, so adding one to the application Bluetooth address matches the unbonded buttonless-DFU protocol; changing that address was not required. See the [Nordic API declaration](https://github.com/NordicSemiconductor/pc-ble-driver/blob/master/include/sd_api_v6/ble_gattc.h).

The experimental process-scoped shim forwards the confirmation call unchanged. After success for the observed attribute handle 28, it delays one matching disconnect by 500 ms, restricted to the same adapter/connection and a short expiry window. Original backend arguments, return values, and errors are preserved. It does not modify authentication or payloads. Offline fake-backend tests cover matching, expiry, one-shot behavior, error propagation, and symbol lookup with locally loaded libraries.

The delayed run connected to the expected bootloader and completed the update. This supports a handoff timing defect, but it does **not** reveal the exact over-air packet sequence or prove that 500 ms works universally. SteamVR logs its disconnect intention before the intercepted call. The eventual event was `BLE_HCI_CONNECTION_TIMEOUT`, not a clean peer-initiated disconnect. An upstream fix should coordinate confirmation and disconnection explicitly instead of relying on an arbitrary sleep.

## Controlled hardware result

On 2026-10-09, the isolated-copy experiment transferred the unchanged 144-byte init packet and 297956-byte firmware image. All 75 logged CRC responses match Valve's archive. Final CRC-32 values were `088e2011` for the init packet and `8d750ec4` for the firmware. SteamVR entered `FirmwareWizard_State_BaseStation_WirelessSuccess` at 01:03:48 CEST.

Afterward, unmodified installed SteamVR reported firmware 3008 and no update available for both connected stations. The user confirmed both were detected, the replacement's warning was gone, and both slept on normal exit. Installed binary hashes remained unchanged. See [CRC validation](../evidence/live-crc-validation.json) and [stock-runtime readback](../evidence/readback-anonymized.json).

This validates one update with the underlying repair components. It does not constitute another firmware-update test of the packaged command-line wrapper, broad distribution coverage, a repair of the separate empty-string bug, or a guarantee of hardware recovery.

## Upstream reporting

Valve's [Linux tracker README](https://github.com/ValveSoftware/SteamVR-for-Linux) directs bug reports to that tracker. Existing open issue [#653](https://github.com/ValveSoftware/SteamVR-for-Linux/issues/653) includes a matching scan-vector destruction stack; [#709](https://github.com/ValveSoftware/SteamVR-for-Linux/issues/709) and [#589](https://github.com/ValveSoftware/SteamVR-for-Linux/issues/589) describe related update/recovery failures. These similarities do not prove that every historical report has the same root cause.

The planned submission is one comment on #653 linking this new evidence and those related reports. The [current bug template](https://github.com/ValveSoftware/SteamVR-for-Linux/blob/master/.github/ISSUE_TEMPLATE/bug_report.md) requests Steam/SteamVR beta testing, versions, runtime diagnostics, logs, and minidumps. Our evidence states the actual tested builds; it does not claim unverified Steam client beta status. Public attachments contain selected anonymized evidence, not full core dumps.
