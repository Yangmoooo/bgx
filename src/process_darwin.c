#include "bgx.h"

#include <errno.h>
#include <inttypes.h>
#include <libproc.h>
#include <stdlib.h>
#include <string.h>
#include <sys/proc.h>
#include <sys/proc_info.h>
#include <sys/sysctl.h>
#include <sys/time.h>

int platform_boot(char out[80], Error *err) {
    struct timeval boot;
    size_t length = sizeof(boot);
    if (sysctlbyname("kern.boottime", &boot, &length, NULL, 0))
        return syserr(err, "read boot time");
    snprintf(out, 80, "%lld:%06d", (long long)boot.tv_sec, (int)boot.tv_usec);
    return 0;
}

int platform_process(pid_t pid, ProcInfo *out, Error *err) {
    memset(out, 0, sizeof(*out));
    struct proc_bsdinfo info;
    errno = 0;
    int n = proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, sizeof(info));
    if (n != (int)sizeof(info)) {
        if (n == 0 && errno == ESRCH) return 1;
        return fail(err, "inspect PID %ld: %s", (long)pid, errno ? strerror(errno) : "incomplete process information");
    }
    out->pgid = (pid_t)info.pbi_pgid;
    out->uid = info.pbi_uid;
    out->zombie = info.pbi_status == SZOMB;
    out->execed = (info.pbi_flags & PROC_FLAG_EXEC) != 0;
    snprintf(out->birth, sizeof(out->birth), "%" PRIu64 ":%06" PRIu64,
             info.pbi_start_tvsec, info.pbi_start_tvusec);
    return 0;
}

int platform_group_live(pid_t pgid, bool *live, Error *err) {
    *live = false;
    int capacity = 4096;
    for (int attempt = 0; attempt < 6; ++attempt) {
        pid_t *pids = malloc((size_t)capacity);
        if (!pids) return fail(err, "out of memory");
        errno = 0;
        int bytes = proc_listpids(PROC_PGRP_ONLY, (uint32_t)pgid, pids, capacity);
        if (bgx_test_fault("listpids_error")) { bytes = 0; errno = EPERM; }
        /* libproc returns zero both for an empty result and for syscall errors. */
        if (bytes < 0 || (!bytes && errno)) { free(pids); return syserr(err, "list process group"); }
        if (bytes >= capacity) { free(pids); capacity *= 2; continue; }
        int rc = 0;
        for (size_t i = 0; i < (size_t)bytes / sizeof(*pids); ++i) {
            if (pids[i] <= 0) continue;
            ProcInfo info;
            int found = platform_process(pids[i], &info, err);
            if (found < 0) { rc = -1; break; }
            if (!found && info.pgid == pgid && !info.zombie) { *live = true; break; }
        }
        free(pids);
        return rc;
    }
    return fail(err, "process group changed too quickly to inspect");
}
