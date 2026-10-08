#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
int main(int argc,char **argv) {
 if(argc!=2)return 2;
 void *handle=dlopen(argv[1],RTLD_NOW|RTLD_LOCAL);
 if(!handle){fprintf(stderr,"%s\n",dlerror());return 3;}
 int (*test)(void)=(int(*)(void))dlsym(handle,"run_test");
 if(!test)return 4;
 return test();
}
