/* Compiled only into the fault-test executable, never the shipped binary. */
#include "bgx.h"

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* Fault names may be comma-separated so one invocation can combine injections
   that occur at different points (for example a post-rename sync failure and an
   abort that cannot confirm cleanup). */
int bgx_test_fault(const char *point) {
    const char *fault = getenv("BGX_TEST_FAULT");
    if (!fault) return 0;
    size_t length = strlen(point);
    for (const char *item = fault; *item; ) {
        size_t item_length = strcspn(item, ",");
        if (item_length == length && !strncmp(item, point, item_length)) return 1;
        item += item_length;
        if (*item) item++;
    }
    return 0;
}

static void notify_pid(const char *file, pid_t pid) {
    int fd = open(file, O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0600);
    if (fd < 0) _exit(126);
    char data[64];
    int length = snprintf(data, sizeof(data), "%ld\n", (long)pid);
    Error err = {0};
    if (write_all(fd, data, (size_t)length, &err)) _exit(126);
    close(fd);
}

void bgx_test_child(pid_t pid) {
    const char *file = getenv("BGX_TEST_CHILD_NOTIFY");
    if (file) notify_pid(file, pid);
}

void bgx_test_point(const char *point) {
    const char *selected = getenv("BGX_TEST_POINT");
    if (!selected || strcmp(selected, point)) return;
    const char *file = getenv("BGX_TEST_NOTIFY");
    if (!file) _exit(126);
    notify_pid(file, getpid());
    if (bgx_test_fault("delay_before_stop")) {
        struct timespec delay = {.tv_nsec = 100000000};
        while (nanosleep(&delay, &delay) && errno == EINTR) {}
    }
    raise(SIGSTOP);
}
