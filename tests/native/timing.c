#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
extern void fake_result(uint32_t);
extern void *fake_adapter(void);
extern uint16_t fake_connection(void),fake_attribute(void);
extern uint8_t fake_reason(void);
extern unsigned fake_confirm_calls(void),fake_disconnect_calls(void);
extern uint32_t sd_ble_gattc_hv_confirm(void*,uint16_t,uint16_t);
extern uint32_t sd_ble_gap_disconnect(void*,uint16_t,uint8_t);
static double now(void) { struct timespec t; assert(!clock_gettime(CLOCK_MONOTONIC,&t)); return t.tv_sec+t.tv_nsec/1e9; }
static void confirm(void *adapter,uint16_t connection,uint16_t attribute,uint32_t code) {
 unsigned count=fake_confirm_calls(); fake_result(code); double start=now();
 uint32_t result=sd_ble_gattc_hv_confirm(adapter,connection,attribute);
 assert(errno==EADDRNOTAVAIL); assert(result==code); assert(now()-start<0.2);
 assert(fake_confirm_calls()==count+1); assert(fake_adapter()==adapter);
 assert(fake_connection()==connection); assert(fake_attribute()==attribute);
}
static void disconnect(const char *label,void *adapter,uint16_t connection,uint8_t reason,uint32_t code,int delayed) {
 unsigned count=fake_disconnect_calls(); fake_result(code); double start=now();
 uint32_t result=sd_ble_gap_disconnect(adapter,connection,reason);
 assert(errno==ENETUNREACH); double elapsed=now()-start;
 assert(result==code); assert(fake_disconnect_calls()==count+1);
 assert(fake_adapter()==adapter); assert(fake_connection()==connection); assert(fake_reason()==reason);
 if(delayed)assert(elapsed>=0.49 && elapsed<2.0); else assert(elapsed<0.2);
 printf("PASS %s delay=%.6f code=0x%08x\n",label,elapsed,result);
}
int main(void) {
 int token_a,token_b; void *a=&token_a,*b=&token_b;
 if(getenv("TEST_EXPIRY")) {
  confirm(a,0x1234,28,0);
  struct timespec interval={2,50000000L}; while(nanosleep(&interval,&interval) && errno==EINTR){}
  disconnect("expired confirmation",a,0x1234,0x13,0x81,0);
  confirm(a,0x1234,28,0);
  disconnect("new confirmation after expiry",a,0x1234,0x13,0x82,1);
  puts("PASS: every backend called exactly once; expired marks do not affect later reused connections.");
  return 0;
 }
 disconnect("no confirmation",a,0xffff,0xff,0xabcdef01,0);
 confirm(a,0x1234,27,0);
 disconnect("wrong attribute",a,0x1234,0x13,0x91,0);
 confirm(a,0x1234,28,0x42);
 disconnect("failed confirmation",a,0x1234,0x13,0x92,0);
 confirm(a,0x1234,28,0);
 disconnect("other adapter",b,0x1234,0x13,0x93,0);
 disconnect("other connection",a,0x5678,0x13,0x94,0);
 disconnect("matching confirmation",a,0x1234,0x13,0xfeedbeef,1);
 disconnect("consumed once",a,0x1234,0x13,0x96,0);
 confirm(a,0x1234,28,0);
 disconnect("cannot rearm",a,0x1234,0x13,0x97,0);
 confirm(NULL,0xabcd,0xffff,0xdeadbeef);
 disconnect("null pointer forwarding",NULL,0xabcd,0x28,0x98,0);
 puts("PASS: every backend called exactly once with original arguments, return code and errno; only one matching disconnect delayed.");
 return 0;
}
