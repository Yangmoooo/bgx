/* Compiled only into the fault-test executable, never the shipped binary. */
#include "bgx.h"

#include <fcntl.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int bgx_test_fault(const char *point) {
    const char *fault = getenv("BGX_TEST_FAULT");
    return fault && !strcmp(fault, point);
}

void bgx_test_child(pid_t pid) {
    const char *file = getenv("BGX_TEST_CHILD_NOTIFY");
    if (!file) return;
    int fd = open(file, O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0600);
    if (fd < 0) _exit(126);
    char data[64];
    int length = snprintf(data, sizeof(data), "%ld\n", (long)pid);
    Error err = {0};
    if (write_all(fd, data, (size_t)length, &err)) _exit(126);
    close(fd);
}

void bgx_test_point(const char *point) {
    const char *selected = getenv("BGX_TEST_POINT");
    if (!selected || strcmp(selected, point)) return;
    const char *file = getenv("BGX_TEST_NOTIFY");
    if (!file) _exit(126);
    int fd = open(file, O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0600);
    if (fd < 0) _exit(126);
    char data[64];
    int length = snprintf(data, sizeof(data), "%ld\n", (long)getpid());
    Error err = {0};
    if (write_all(fd, data, (size_t)length, &err)) _exit(126);
    close(fd);
    raise(SIGSTOP);
}
