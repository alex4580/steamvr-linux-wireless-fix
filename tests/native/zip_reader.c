#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>

struct record { char *empty; char *filename; uint32_t size; uint32_t padding; void *bytes; };
struct object { unsigned char untouched[0xa8]; struct record *begin,*end,*capacity; };
_Static_assert(sizeof(struct record)==0x20,"record ABI");
_Static_assert(sizeof(struct object)==0xc0,"object footprint");
static void fail(const char *s) { fprintf(stderr,"FAIL: %s\n",s); exit(1); }
static void *guarded(size_t n) {
 size_t pages=(n+16+4095)/4096, total=(pages+2)*4096;
 char *p=mmap(0,total,PROT_NONE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
 if(p==MAP_FAILED || mprotect(p+4096,pages*4096,PROT_READ|PROT_WRITE))fail("mmap/mprotect");
 char *out=p+4096+pages*4096-n; memset(out-16,0xA5,16); return out;
}
static void canary(void *p) { for(int i=-16;i<0;i++)if(((unsigned char*)p)[i]!=0xA5)fail("front canary"); }
int main(int argc,char **argv) {
 struct rlimit core={0,0},as={512*1024*1024,512*1024*1024};
 if(setrlimit(RLIMIT_CORE,&core) || setrlimit(RLIMIT_AS,&as))fail("rlimits");
 setbuf(stdout,NULL);
 if(argc!=4)fail("usage: test_full_reader LIBRARY ARCHIVE patched|original");
 int patched=!strcmp(argv[3],"patched");
 if(!patched && strcmp(argv[3],"original"))fail("mode");
 void *lib=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL); if(!lib)fail(dlerror());
 void *sym=dlsym(lib,"NRFSerLib_GetBaseStationWirelessInterface"); Dl_info info;
 if(!sym || !dladdr(sym,&info))fail("dladdr");
 uintptr_t base=(uintptr_t)info.dli_fbase;
 if((uintptr_t)sym-base!=0x62860)fail("unexpected library build");
 uintptr_t sites[]={0x5ec7d,0x5ed18};
 for(int i=0;i<2;i++) {
  unsigned char *site=(void*)(base+sites[i]); int32_t disp; memcpy(&disp,site+1,4);
  uintptr_t target=base+(patched?0xed000:0x77990);
  if(*site!=0xe8 || (uintptr_t)(site+5+disp)!=target)fail("call target");
 }
 unsigned char (*read_zip)(struct object*,const char**)=(void*)(base+0x5ebc0);
 struct object *obj=guarded(sizeof(*obj)); memset(obj,0,sizeof(*obj));
 const char *path=argv[2];
 printf("MODE\t%s\n",argv[3]);
 for(int round=0;round<2;round++) {
  unsigned char result=read_zip(obj,&path);
  printf("RESULT\t%d\t%u\n",round,result);
  if(!result)fail("actual firmware ZIP reader returned false");
  canary(obj);
  for(size_t i=0;i<sizeof(obj->untouched);i++)if(obj->untouched[i])fail("unexpected object field write");
  ptrdiff_t count=obj->end-obj->begin;
  if(count!=3 || obj->capacity<obj->end)fail("unexpected vector size");
  for(ptrdiff_t i=0;i<count;i++) {
   struct record *record=obj->begin+i;
   if(record->empty[0] || !record->filename || !record->bytes || !record->size || record->size>1048576)fail("invalid vector record");
   char output[64]; snprintf(output,sizeof(output),"full-%d-entry-%td.bin",round,i);
   FILE *f=fopen(output,"wb"); if(!f)fail("output open");
   if(fwrite(record->bytes,1,record->size,f)!=record->size || fclose(f))fail("output write");
   printf("ENTRY\t%d\t%td\t%s\t%u\n",round,i,record->filename,record->size);
  }
 }
 path="/nonexistent-test-archive.zip";
 if(read_zip(obj,&path))fail("missing archive succeeded");
 if(obj->end!=obj->begin)fail("vector not cleared before missing-archive error");
 canary(obj);
 puts("PASS: actual firmware ZIP reader, repeated vector reuse, and missing archive error.");
 /* Private object teardown is intentionally not invoked; process exit reclaims it. */
 return 0;
}
