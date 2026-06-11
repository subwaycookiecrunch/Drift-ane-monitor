#include <stdio.h>
#include <stddef.h>
#include <sys/resource.h>

int main() {
    printf("size: %zu\n", sizeof(struct rusage_info_v6));
    printf("offset of ri_neural_footprint: %zu\n", offsetof(struct rusage_info_v6, ri_neural_footprint));
    printf("offset of ri_energy_nj: %zu\n", offsetof(struct rusage_info_v6, ri_energy_nj));
    printf("offset of ri_flags: %zu\n", offsetof(struct rusage_info_v6, ri_flags));
    return 0;
}
