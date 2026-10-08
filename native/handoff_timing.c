/* Experimental, scoped timing test. No authentication, payload or status edits.
 * Official pc-ble-driver API v6 signatures are recorded in the technical notes.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <time.h>
#include <unistd.h>

/* adapter_t is never dereferenced; only the exact opaque pointer is forwarded. */
typedef struct opaque_adapter adapter_t;
typedef uint32_t (*confirm_fn)(adapter_t *, uint16_t, uint16_t);
typedef uint32_t (*disconnect_fn)(adapter_t *, uint16_t, uint8_t);
#define EXPORTED __attribute__((visibility("default")))
EXPORTED uint32_t sd_ble_gattc_hv_confirm(adapter_t *, uint16_t, uint16_t);
EXPORTED uint32_t sd_ble_gap_disconnect(adapter_t *, uint16_t, uint8_t);

static pthread_once_t resolve_once = PTHREAD_ONCE_INIT;
static pthread_mutex_t state_lock = PTHREAD_MUTEX_INITIALIZER;
static confirm_fn real_confirm;
static disconnect_fn real_disconnect;
static adapter_t *armed_adapter;
static uint16_t armed_connection;
static struct timespec armed_at;
static bool armed, consumed;

static void resolve_functions(void) {
    real_confirm = (confirm_fn)dlsym(RTLD_NEXT, "sd_ble_gattc_hv_confirm");
    real_disconnect = (disconnect_fn)dlsym(RTLD_NEXT, "sd_ble_gap_disconnect");
    /* SteamVR may load the already-present driver in a local dlopen scope.
     * NOLOAD prevents loading/initializing a new backend as a side effect. */
    if (!real_confirm || !real_disconnect) {
        void *existing = dlopen("libpc_ble_driver_shared_sd_api_v6.so",
                               RTLD_NOW | RTLD_NOLOAD | RTLD_LOCAL);
        if (existing) {
            if (!real_confirm)
                real_confirm = (confirm_fn)dlsym(existing, "sd_ble_gattc_hv_confirm");
            if (!real_disconnect)
                real_disconnect = (disconnect_fn)dlsym(existing, "sd_ble_gap_disconnect");
            /* Keep the handle for the lifetime of its saved function pointers. */
        }
    }
    if (!real_confirm || !real_disconnect ||
        real_confirm == sd_ble_gattc_hv_confirm || real_disconnect == sd_ble_gap_disconnect) {
        static const char message[] = "[nrf-handoff-timing] original symbols unavailable; refusing to fabricate BLE results\n";
        (void)write(STDERR_FILENO, message, sizeof(message)-1);
        _exit(127);
    }
}

EXPORTED uint32_t sd_ble_gattc_hv_confirm(adapter_t *adapter, uint16_t connection, uint16_t attribute) {
    int entry_errno = errno;
    pthread_once(&resolve_once, resolve_functions);
    errno = entry_errno;
    uint32_t result = real_confirm(adapter, connection, attribute);
    int backend_errno = errno;
    if (result == 0 && attribute == 28) {
        struct timespec completed;
        bool have_time = clock_gettime(CLOCK_MONOTONIC, &completed) == 0;
        pthread_mutex_lock(&state_lock);
        if (!consumed && have_time) {
            armed_adapter = adapter;
            armed_connection = connection;
            armed_at = completed;
            armed = true;
            fprintf(stderr, "[nrf-handoff-timing] armed after successful HVC attribute=28 connection=%u\n", connection);
        }
        pthread_mutex_unlock(&state_lock);
    }
    errno = backend_errno;
    return result;
}

EXPORTED uint32_t sd_ble_gap_disconnect(adapter_t *adapter, uint16_t connection, uint8_t reason) {
    int entry_errno = errno;
    pthread_once(&resolve_once, resolve_functions);
    bool delay = false;
    struct timespec now;
    bool have_time = clock_gettime(CLOCK_MONOTONIC, &now) == 0;
    pthread_mutex_lock(&state_lock);
    if (armed && !consumed && adapter == armed_adapter && connection == armed_connection) {
        armed = false;
        int64_t age = have_time ? (int64_t)(now.tv_sec-armed_at.tv_sec)*1000000000LL
                                  + now.tv_nsec-armed_at.tv_nsec : -1;
        if (age >= 0 && age <= 2000000000LL) {
            consumed = true;
            delay = true;
        }
    }
    pthread_mutex_unlock(&state_lock);
    if (delay) {
        struct timespec deadline;
        if (clock_gettime(CLOCK_MONOTONIC, &deadline) == 0) {
            deadline.tv_nsec += 500000000L;
            if (deadline.tv_nsec >= 1000000000L) {
                deadline.tv_sec++;
                deadline.tv_nsec -= 1000000000L;
            }
            fprintf(stderr, "[nrf-handoff-timing] delaying matching disconnect 500ms once; connection=%u reason=%u\n", connection, reason);
            int status;
            do { status = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &deadline, NULL); }
            while (status == EINTR);
            if (status)
                fprintf(stderr, "[nrf-handoff-timing] clock_nanosleep error=%d; forwarding original call\n", status);
        } else {
            fprintf(stderr, "[nrf-handoff-timing] monotonic clock unavailable; forwarding original call\n");
        }
    }
    errno = entry_errno;
    return real_disconnect(adapter, connection, reason);
}
