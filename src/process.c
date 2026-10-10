#include "bgx.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

const char *status_name(Status status) {
    switch (status) {
    case RUNNING: return "running";
    case EXITED: return "exited";
    case STALE: return "stale";
    default: return "unknown";
    }
}

typedef struct { int stage; int error; } LaunchMessage;
enum { READY, SESSION, REDIRECT, EXECUTING, EXECUTE };

#ifndef BGX_LAUNCH_TIMEOUT_MS
#define BGX_LAUNCH_TIMEOUT_MS 5000
#endif
#ifndef BGX_CLEANUP_TIMEOUT_MS
#define BGX_CLEANUP_TIMEOUT_MS 2000
#endif

static volatile sig_atomic_t interrupted;

static void launch_signal(int signal_number) {
    interrupted = signal_number;
}

/* Preserve the caller's handlers; only the bounded launch transaction handles
   these signals. The child restores defaults before executing the target. */
typedef struct { struct sigaction old[3]; size_t count; } LaunchSignals;
static const int launch_signals[] = {SIGINT, SIGTERM, SIGHUP};

static int handle_launch_signals(LaunchSignals *saved, Error *err) {
    struct sigaction action = {0};
    action.sa_handler = launch_signal;
    sigemptyset(&action.sa_mask);
    interrupted = 0;
    for (size_t i = 0; i < 3; ++i) {
        if (sigaction(launch_signals[i], &action, &saved->old[i]))
            return syserr(err, "install launch signal handler");
        saved->count++;
    }
    return 0;
}

static void restore_launch_signals(const LaunchSignals *saved) {
    for (size_t i = 0; i < saved->count; ++i)
        sigaction(launch_signals[i], &saved->old[i], NULL);
}

static void free_candidates(char **paths) {
    if (!paths) return;
    for (size_t i = 0; paths[i]; ++i) free(paths[i]);
    free(paths);
}

/* Resolve search candidates before fork. Use execve, not execvp: execvp can
   silently reinterpret an executable text file as a shell script on ENOEXEC. */
static char **exec_candidates(const char *program, Error *err) {
    if (strchr(program, '/')) {
        char **paths = calloc(2, sizeof(*paths));
        if (paths) paths[0] = strdup(program);
        if (!paths || !paths[0]) { free_candidates(paths); fail(err, "out of memory"); return NULL; }
        return paths;
    }
    const char *env = getenv("PATH");
    char *search = NULL;
    if (env) search = strdup(env);
    else {
        size_t size = confstr(_CS_PATH, NULL, 0);
        if (!size) { fail(err, "cannot determine default executable path"); return NULL; }
        search = malloc(size);
        if (search) confstr(_CS_PATH, search, size);
    }
    if (!search) { fail(err, "out of memory"); return NULL; }
    size_t count = 1;
    for (const char *p = search; *p; ++p) if (*p == ':') count++;
    char **paths = calloc(count + 1, sizeof(*paths));
    if (!paths) { free(search); fail(err, "out of memory"); return NULL; }
    char *part = search;
    for (size_t i = 0; i < count; ++i) {
        char *end = strchr(part, ':');
        if (end) *end = 0;
        paths[i] = *part ? join_path(part, program, err) : strdup(program);
        if (!paths[i]) { free(search); free_candidates(paths); fail(err, "out of memory"); return NULL; }
        if (end) part = end + 1;
    }
    free(search);
    return paths;
}

static void child_report(int fd, int stage, int error) {
    LaunchMessage message = {stage, error};
    const char *p = (const char *)&message;
    size_t remaining = sizeof(message);
    while (remaining) {
        ssize_t n = write(fd, p, remaining);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) break;
        p += n; remaining -= (size_t)n;
    }
}

static int read_message(int fd, LaunchMessage *message, int64_t deadline, Error *err) {
    memset(message, 0, sizeof(*message));
    char *p = (char *)message;
    size_t used = 0;
    while (used < sizeof(*message)) {
        if (interrupted) return fail(err, "launch interrupted by signal %d", (int)interrupted);
        int64_t now = monotonic_ms();
        if (now < 0) return syserr(err, "read launch clock");
        if (now >= deadline) return fail(err, "launch handshake timed out");
        struct pollfd pollfd = {.fd = fd, .events = POLLIN};
        int ready = poll(&pollfd, 1, (int)(deadline - now));
        if (ready < 0 && errno == EINTR) continue;
        if (ready < 0) return syserr(err, "poll launch handshake");
        if (!ready) return fail(err, "launch handshake timed out");
        ssize_t n = read(fd, p + used, sizeof(*message) - used);
        if (n < 0 && errno == EINTR) continue;
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) continue;
        if (n < 0) return syserr(err, "read launch handshake");
        if (!n) return used ? fail(err, "incomplete launch handshake") : 1;
        used += (size_t)n;
    }
    return 0;
}

static int max_open_fd(Error *err) {
    DIR *dir = opendir("/dev/fd");
    if (!dir) return syserr(err, "inspect inherited descriptors");
    int highest = dirfd(dir);
    struct dirent *ent;
    while ((ent = readdir(dir))) {
        char *end;
        long fd = strtol(ent->d_name, &end, 10);
        if (*ent->d_name && !*end && fd >= 0 && fd <= INT_MAX && fd > highest) highest = (int)fd;
    }
    closedir(dir);
    return highest;
}

static int child_exit_info(pid_t pid, siginfo_t *info, Error *err) {
    memset(info, 0, sizeof(*info));
    int rc;
    do { rc = waitid(P_PID, (id_t)pid, info, WEXITED | WNOHANG | WNOWAIT); }
    while (rc < 0 && errno == EINTR);
    return rc < 0 ? syserr(err, "inspect child exit") : 0;
}

static bool cleanup_child(pid_t pid, bool released, Error *err) {
    /* This is our unreaped child: its PID cannot have been reused. */
    siginfo_t info;
    if (child_exit_info(pid, &info, err)) return false;
    bool group = getpgid(pid) == pid;
    if (group && kill(-pid, SIGKILL) && errno != ESRCH) { syserr(err, "kill launch scope"); return false; }
    if (kill(pid, SIGKILL) && errno != ESRCH) { syserr(err, "kill launch child"); return false; }
    int64_t now = monotonic_ms();
    if (now < 0) { syserr(err, "read cleanup clock"); return false; }
    int64_t deadline = now + BGX_CLEANUP_TIMEOUT_MS;
    bool reaped = false;
    for (;;) {
        if (!reaped) {
            if (child_exit_info(pid, &info, err)) return false;
            if (info.si_pid == pid && !bgx_test_fault("cleanup_timeout")) {
                pid_t result;
                do { result = waitpid(pid, NULL, WNOHANG); } while (result < 0 && errno == EINTR);
                if (result != pid) { fail(err, "could not reap launch child"); return false; }
                reaped = true;
            }
        }
        bool live = false;
        if (released && group && platform_group_live(pid, &live, err)) return false;
        if (reaped && !live) return true;
        now = monotonic_ms();
        if (now < 0 || now >= deadline) { fail(err, "launch cleanup timed out"); return false; }
        struct timespec delay = {.tv_nsec = 10000000};
        nanosleep(&delay, NULL);
    }
}

int process_abort_launch(const Identity *id, Error *err) {
    // Only used by this launch's parent before it exits. No signals are sent
    // after reaping, so PID reuse cannot turn cleanup into an unrelated kill.
    return cleanup_child(id->pid, true, err) ? 0 : -1;
}

LaunchResult process_start(char *const argv[], int logfd, Identity *out, Error *err) {
    int status_pipe[2] = {-1, -1}, gate[2] = {-1, -1};
    pid_t pid = -1;
    bool released = false, definite_failure = false;
    LaunchResult outcome = LAUNCH_FAILED;
    LaunchSignals saved_signals = {0};
    char **candidates = exec_candidates(argv[0], err);
    if (!candidates) return LAUNCH_FAILED;
    int input = open("/dev/null", O_RDONLY | O_CLOEXEC);
    if (input < 0) { syserr(err, "open detached input"); goto failed; }
    if (handle_launch_signals(&saved_signals, err)) goto failed;
    if (pipe(status_pipe) || pipe(gate)) {
        syserr(err, "create launch pipes");
        goto failed;
    }
    for (int i = 0; i < 2; ++i) {
        if (fcntl(status_pipe[i], F_SETFD, FD_CLOEXEC) || fcntl(gate[i], F_SETFD, FD_CLOEXEC)) {
            syserr(err, "secure launch descriptors"); goto failed;
        }
    }
    if (fcntl(status_pipe[0], F_SETFL, O_NONBLOCK)) { syserr(err, "configure launch pipe"); goto failed; }
    int highest = max_open_fd(err);
    if (highest < 0) goto failed;
    char boot[80];
    if (platform_boot(boot, err)) goto failed;
    int64_t now = monotonic_ms();
    if (now < 0) { syserr(err, "read launch clock"); goto failed; }
    int64_t deadline = now + BGX_LAUNCH_TIMEOUT_MS;
    pid = fork();
    if (pid < 0) { syserr(err, "fork"); goto failed; }
    if (pid == 0) {
        close(status_pipe[0]); close(gate[1]);
        bgx_test_point("before_ready");
        if (setsid() < 0) { child_report(status_pipe[1], SESSION, errno); _exit(127); }
        if (dup2(input, STDIN_FILENO) < 0 || dup2(logfd, STDOUT_FILENO) < 0 || dup2(logfd, STDERR_FILENO) < 0) {
            child_report(status_pipe[1], REDIRECT, errno); _exit(127);
        }
        for (int fd = 3; fd <= highest; ++fd)
            if (fd != status_pipe[1] && fd != gate[0]) close(fd);
        struct sigaction action;
        memset(&action, 0, sizeof(action)); action.sa_handler = SIG_DFL; sigemptyset(&action.sa_mask);
        const int signals[] = {SIGPIPE, SIGHUP, SIGINT, SIGQUIT, SIGTERM, SIGCHLD};
        for (size_t i = 0; i < sizeof(signals) / sizeof(signals[0]); ++i) sigaction(signals[i], &action, NULL);
        sigset_t empty; sigemptyset(&empty); sigprocmask(SIG_SETMASK, &empty, NULL);
        child_report(status_pipe[1], READY, 0);
        char go;
        ssize_t n;
        do { n = read(gate[0], &go, 1); } while (n < 0 && errno == EINTR);
        close(gate[0]);
        if (n != 1) _exit(127);
        bgx_test_point("before_exec");
        child_report(status_pipe[1], EXECUTING, 0);
        bgx_test_point("after_exec_notice");
        if (bgx_test_fault("partial_exec_error")) {
            (void)write(status_pipe[1], "x", 1);
            _exit(127);
        }
        if (bgx_test_fault("invalid_exec_message")) {
            child_report(status_pipe[1], READY, 0);
            _exit(127);
        }
        bool denied = false;
        for (size_t i = 0; candidates[i]; ++i) {
            execve(candidates[i], argv, environ);
            int error = errno;
            if (error == EACCES) { denied = true; continue; }
            if (error == ENOENT || error == ENOTDIR) continue;
            child_report(status_pipe[1], EXECUTE, error);
            _exit(127);
        }
        child_report(status_pipe[1], EXECUTE, denied ? EACCES : ENOENT);
        _exit(127);
    }
    bgx_test_child(pid);
    close(input); input = -1;
    close(status_pipe[1]); status_pipe[1] = -1;
    close(gate[0]); gate[0] = -1;
    LaunchMessage message;
    int result = read_message(status_pipe[0], &message, deadline, err);
    if (result || message.stage != READY) {
        if (result == 1) fail(err, "child exited before launch handshake");
        else if (!result) fail(err, "child setup failed: %s", strerror(message.error));
        goto failed;
    }
    /* The child waits here: capture birth information before an instant exec/exit. */
    ProcInfo info;
    int found = platform_process(pid, &info, err);
    if (found != 0) {
        if (found == 1) fail(err, "child disappeared while reading identity");
        goto failed;
    }
    if (info.execed) { fail(err, "cannot establish pre-exec identity"); goto failed; }
    *out = (Identity){.pid = pid, .pgid = pid};
    strcpy(out->birth, info.birth); strcpy(out->boot, boot);
    bgx_test_point("before_gate");
    if (interrupted) { fail(err, "launch interrupted by signal %d", (int)interrupted); goto failed; }
    if (write_all(gate[1], "x", 1, err)) goto failed;
    released = true;
    close(gate[1]); gate[1] = -1;
    result = read_message(status_pipe[0], &message, deadline, err);
    if (result || message.stage != EXECUTING || message.error) {
        if (result == 1) fail(err, "child disappeared before exec; launch outcome is uncertain");
        else if (!result) fail(err, "unexpected launch message before exec");
        goto failed;
    }
    result = read_message(status_pipe[0], &message, deadline, err);
    close(status_pipe[0]); status_pipe[0] = -1;
    if (result == 1) {
        bgx_test_point("after_eof");
        /* EOF alone is ambiguous. Check the kernel's exec flag while our child
           remains unreaped, so an exit cannot cause PID reuse. Fast normal
           exits remain valid launches (including exit 127). */
        siginfo_t info;
        if (interrupted || child_exit_info(pid, &info, err)) {
            if (interrupted) fail(err, "launch interrupted by signal %d", (int)interrupted);
            goto failed;
        }
        ProcInfo completed;
        int inspected = platform_process(pid, &completed, err);
        if (inspected || !completed.execed || strcmp(completed.birth, out->birth)) {
            if (inspected >= 0) fail(err, "exec completion could not be confirmed; launch outcome is uncertain");
            goto failed;
        }
        if (info.si_pid == pid && info.si_code != CLD_EXITED) {
            fail(err, "child terminated by signal %d during launch; outcome is uncertain", info.si_status);
            goto failed;
        }
        // Leave the child unreaped until the caller commits its identity. If
        // committing fails, it can still use the PID without a reuse race.
        free_candidates(candidates);
        restore_launch_signals(&saved_signals);
        return LAUNCH_OK;
    }
    if (!result && message.stage == EXECUTE && message.error > 0) {
        definite_failure = true;
        fail(err, "execute %s: %s", argv[0], strerror(message.error));
    } else if (!result) fail(err, "unexpected exec result");
failed:
    if (input >= 0) close(input);
    for (int i = 0; i < 2; ++i) {
        if (status_pipe[i] >= 0) close(status_pipe[i]);
        if (gate[i] >= 0) close(gate[i]);
    }
    if (pid > 0) {
        Error cleanup = {0};
        bool cleared = cleanup_child(pid, released, &cleanup);
        if (!cleared || (released && !definite_failure)) {
            outcome = LAUNCH_UNCERTAIN;
            size_t used = strlen(err->text);
            snprintf(err->text + used, sizeof(err->text) - used,
                     "; starting record retained (child PID %ld)%s%.120s",
                     (long)pid, cleared ? "" : "; cleanup: ", cleared ? "" : cleanup.text);
        }
    }
    free_candidates(candidates);
    restore_launch_signals(&saved_signals);
    return outcome;
}

Status process_inspect(const Identity *id, Error *err) {
    if (id->pid <= 1 || id->pgid != id->pid || !*id->birth || !*id->boot) {
        fail(err, "invalid process identity"); return UNKNOWN;
    }
    char boot[80];
    if (platform_boot(boot, err)) return UNKNOWN;
    /* A different boot cannot contain this run. Do not inspect or signal its
       old PID/PGID: those numbers may now belong to unrelated processes. */
    if (strcmp(boot, id->boot)) return EXITED;
    ProcInfo info;
    int found = platform_process(id->pid, &info, err);
    if (found < 0) return UNKNOWN;
    if (!found) {
        if (strcmp(info.birth, id->birth)) {
            fail(err, "process start marker does not match recorded identity"); return STALE;
        }
        if (info.pgid != id->pgid) {
            fail(err, "process group does not match recorded identity"); return STALE;
        }
        if (info.uid != geteuid()) {
            fail(err, "effective user differs (expected %lu, found %lu)",
                 (unsigned long)geteuid(), (unsigned long)info.uid);
            return STALE;
        }
    }
    if (kill(-id->pgid, 0) < 0) {
        if (errno == ESRCH) return EXITED;
        syserr(err, "inspect process group"); return UNKNOWN;
    }
    bool live;
    if (platform_group_live(id->pgid, &live, err)) return UNKNOWN;
    return live ? RUNNING : EXITED;
}

static int wait_exit(const Identity *id, int milliseconds, Error *err) {
    int64_t start = monotonic_ms();
    if (start < 0) return syserr(err, "read monotonic clock");
    for (;;) {
        Error observation = {0};
        Status state = process_inspect(id, &observation);
        if (state == EXITED) return 0;
        if (state == STALE) return fail(err, "process identity became stale while stopping");
        int64_t now = monotonic_ms();
        if (now < 0) return syserr(err, "read monotonic clock");
        if (now - start >= milliseconds) {
            if (state == UNKNOWN) return fail(err, "cannot confirm scope exit: %s", observation.text);
            return 1;
        }
        // A process can disappear partway through native inspection. Only
        // retry observation here; any subsequent signal revalidates identity.
        struct timespec delay = {.tv_sec = 0, .tv_nsec = 50000000};
        while (nanosleep(&delay, &delay) && errno == EINTR) {}
    }
}

int process_stop(const Identity *id, Error *err) {
    const int signals[] = {SIGTERM, SIGKILL};
    const int waits[] = {5000, 2000};
    for (int step = 0; step < 2; ++step) {
        Status state = process_inspect(id, err);
        if (state == EXITED) return 0;
        if (state != RUNNING) return fail(err, "refuse to signal %s identity", status_name(state));
        if (kill(-id->pgid, signals[step]) && errno != ESRCH) return syserr(err, "signal process group");
        int rc = wait_exit(id, waits[step], err);
        if (rc <= 0) return rc;
    }
    return fail(err, "process scope did not exit after SIGKILL");
}
