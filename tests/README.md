# Hardware-free validation

These tests contain original test source code only. They do not contain Valve binaries, firmware, logs, core dumps, device identifiers, or credentials.

Run the fast safety tests from the project root:

```sh
python3 -m unittest discover -s tests -v
```

Run the native timing tests with a fake driver:

```sh
python3 tests/run_local_validation.py
```

To additionally test the actual firmware ZIP reader, provide your own supported SteamVR installation:

```sh
python3 tests/run_local_validation.py --steamvr "$HOME/.local/share/Steam/steamapps/common/SteamVR"
```

The native tests require `gcc`, `ld`, `objcopy`, `nm`, and `bwrap`. They refuse to run the real archive reader unless the local library and firmware archive match the supported hashes. There is no fallback that runs without isolation.

The runner uses temporary directories, separate network/PID namespaces, a fresh minimal `/dev` without physical devices, no `/sys`, and read-only access to installed Valve libraries. It never starts SteamVR or calls a Bluetooth API. All compiled test binaries, copied libraries, and extracted firmware bytes are deleted when the runner exits normally or raises an exception.

The original reader is expected to fail with `SIGABRT` (often `std::bad_alloc`) or `SIGSEGV` inside the sandbox; the exact symptom depends on what the invalid stack access encounters. The repaired copy must parse the same archive twice, handle a missing archive, and produce bytes identical to Python's ZIP reader. Native object buffers have guard pages and canaries; core dumps are disabled and address space is bounded.

Timing tests use only the fake backend. They check both ordinary linking and `RTLD_LOCAL` lookup, exact argument/result/error forwarding, unsuccessful and mismatched confirmations, a single matching 500 ms delay, and expired confirmation state. The fake driver exists only inside the temporary test directory; never put it on a real SteamVR library path.

These tests establish the scoped code behavior. They do not certify firmware transfer on other hardware or repair the separate cross-module empty-string crash.
