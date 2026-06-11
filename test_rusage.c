#include <stdio.h>
#include <sys/resource.h>
#include <libproc.h>
int main() {
    struct rusage_info_v6 v6;
    printf("v6 size %lu\n", sizeof(v6));
    printf("ri_neural_engine_time offset: %lu\n", offsetof(struct rusage_info_v6, ri_neural_engine_time));
    return 0;
}
