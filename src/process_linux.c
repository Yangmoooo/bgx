#include "bgx.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

int platform_boot(char out[80], Error *err) {
    int fd = open("/proc/sys/kernel/random/boot_id", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return syserr(err, "read boot ID");
    char *data = NULL;
    size_t length = 0;
    int rc = read_all(fd, 79, &data, &length, err);
    close(fd);
    if (rc) return rc;
    while (length && (data[length-1] == '\n' || data[length-1] == '\r')) data[--length] = 0;
    if (!length) rc = fail(err, "empty boot ID");
    else memcpy(out, data, length + 1);
    free(data);
    return rc;
}

int platform_process(pid_t pid, ProcInfo *out, Error *err) {
    memset(out, 0, sizeof(*out));
    char path[80];
    snprintf(path, sizeof(path), "/proc/%ld/stat", (long)pid);
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        if (errno == ENOENT || errno == ESRCH) return 1;
        return syserr(err, "read process stat");
    }
    struct stat st;
    if (fstat(fd, &st)) { close(fd); return syserr(err, "stat process identity"); }
    char *data = NULL;
    size_t length = 0;
    int rc = read_all(fd, 1024 * 1024, &data, &length, err);
    close(fd);
    if (rc) return rc;
    char *end = strrchr(data, ')');
    if (!end || end[1] != ' ') { free(data); return fail(err, "malformed process stat"); }
    char *save = NULL, *field = strtok_r(end + 2, " ", &save);
    bool have_group = false, have_birth = false, have_flags = false;
    memset(out, 0, sizeof(*out));
    out->uid = st.st_uid;
    for (int index = 0; field; ++index, field = strtok_r(NULL, " ", &save)) {
        if (index == 0) out->zombie = field[0] == 'Z' || field[0] == 'X';
        if (index == 2) {
            char *tail;
            errno = 0;
            long n = strtol(field, &tail, 10);
            if (errno || *tail || n < 0 || n > INT_MAX) break;
            out->pgid = (pid_t)n;
            have_group = true;
        }
        if (index == 6) {
            /* /proc/PID/stat field 9 exposes task flags. PF_FORKNOEXEC (0x40)
               is set at fork and cleared by exec (Linux include/linux/sched.h). */
            char *tail;
            errno = 0;
            unsigned long flags = strtoul(field, &tail, 10);
            if (errno || *tail) break;
            out->execed = (flags & 0x40UL) == 0;
            have_flags = true;
        }
        if (index == 19) {
            size_t n = strspn(field, "0123456789");
            if (!n || field[n] || n >= sizeof(out->birth)) break;
            memcpy(out->birth, field, n + 1);
            have_birth = true;
            break;
        }
    }
    free(data);
    return have_group && have_birth && have_flags ? 0 : fail(err, "incomplete process stat");
}

int platform_group_live(pid_t pgid, bool *live, Error *err) {
    *live = false;
    DIR *dir = opendir("/proc");
    if (!dir) return syserr(err, "open process table");
    int rc = 0;
    for (;;) {
        errno = 0;
        struct dirent *ent = readdir(dir);
        if (!ent) { if (errno) rc = syserr(err, "list processes"); break; }
        if (ent->d_name[0] < '0' || ent->d_name[0] > '9') continue;
        char *end;
        long pid = strtol(ent->d_name, &end, 10);
        if (*end || pid <= 1 || pid > INT_MAX) continue;
        if (getpgid((pid_t)pid) != pgid) continue;
        ProcInfo info;
        int found = platform_process((pid_t)pid, &info, err);
        if (found < 0) { rc = -1; break; }
        if (!found && info.pgid == pgid && !info.zombie) { *live = true; break; }
    }
    closedir(dir);
    return rc;
}
