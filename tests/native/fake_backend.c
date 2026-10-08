#include <errno.h>
#include <stdint.h>
#include <time.h>
static uint32_t wanted_result;
static void *seen_adapter;
static uint16_t seen_connection, seen_attribute;
static uint8_t seen_reason;
static unsigned confirm_calls, disconnect_calls;
void fake_result(uint32_t result) { wanted_result=result; }
void *fake_adapter(void) { return seen_adapter; }
uint16_t fake_connection(void) { return seen_connection; }
uint16_t fake_attribute(void) { return seen_attribute; }
uint8_t fake_reason(void) { return seen_reason; }
unsigned fake_confirm_calls(void) { return confirm_calls; }
unsigned fake_disconnect_calls(void) { return disconnect_calls; }
uint32_t sd_ble_gattc_hv_confirm(void *adapter,uint16_t connection,uint16_t attribute) {
 seen_adapter=adapter; seen_connection=connection; seen_attribute=attribute;
 confirm_calls++; errno=EADDRNOTAVAIL; return wanted_result;
}
uint32_t sd_ble_gap_disconnect(void *adapter,uint16_t connection,uint8_t reason) {
 seen_adapter=adapter; seen_connection=connection; seen_reason=reason;
 disconnect_calls++; errno=ENETUNREACH; return wanted_result;
}
