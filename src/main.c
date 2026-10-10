#include "bgx.h"

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#ifndef BGX_VERSION
#define BGX_VERSION "dev"
#endif

static const struct {
    const char *name;
    const char *syntax;
    const char *description;
    const char *details;
    const char *aliases[3];
} commands[] = {
    {"start", "start NAME -- PROGRAM [ARGS...]", "Start a named background command.",
     "Names are case-sensitive and accept ASCII letters, digits, '_' and '-'.\n"
     "The command inherits the current directory and environment.\n"
     "Output is logged; terminal input is disabled. Reusing an exited name deletes its old log.\n", {"s"}},
    {"stop", "stop NAME", "Stop a background command and its process group.",
     "Send TERM, wait up to 5 seconds, then KILL if needed. Keep the record and log.\n", {NULL}},
    {"list", "list", "List background commands and their status.",
     "Show name, status, PID, uptime, and command.\n", {"ls", "ps"}},
    {"info", "info NAME", "Show a task's record and current status.",
     "Show recorded process IDs, local start time, uptime, directory, command, and log path.\n"
     "Recorded IDs are historical metadata, not proof that a process is still running.\n", {NULL}},
    {"remove", "remove NAME [--force]", "Remove a command's record and log.",
     "The command must have exited. --force deletes data without stopping any processes.\n", {"rm"}},
    {"log", "log NAME [--tail N]", "Print a command's log.",
     "Print the full log by default, or the last N lines with --tail N.\n"
     "N must be a nonnegative integer; 0 prints nothing. Output does not follow new data.\n", {"logs"}},
    {"help", "help [COMMAND]", "Show general or command-specific help.", "", {"-h", "--help"}},
    {"version", "version", "Show the bgx version.", "", {"-V", "--version"}},
};

static void print_aliases(FILE *out, size_t index) {
    if (!commands[index].aliases[0]) return;
    fputs(" (aliases: ", out);
    for (size_t j = 0; commands[index].aliases[j]; ++j)
        fprintf(out, "%s%s", j ? ", " : "", commands[index].aliases[j]);
    fputc(')', out);
}

static void usage(FILE *out) {
    fputs("bgx - named background commands\n\n"
          "Usage:\n", out);
    for (size_t i = 0; i < sizeof(commands) / sizeof(commands[0]); ++i) {
        fprintf(out, "  bgx %-32s %s", commands[i].syntax, commands[i].description);
        print_aliases(out, i);
        fputc('\n', out);
    }
}

static const char *resolve_command(const char *cmd) {
    for (size_t i = 0; i < sizeof(commands) / sizeof(commands[0]); ++i) {
        if (!strcmp(cmd, commands[i].name)) return commands[i].name;
        for (size_t j = 0; commands[i].aliases[j]; ++j)
            if (!strcmp(cmd, commands[i].aliases[j])) return commands[i].name;
    }
    return NULL;
}

static void command_help(FILE *out, const char *cmd) {
    for (size_t i = 0; i < sizeof(commands) / sizeof(commands[0]); ++i) {
        if (strcmp(cmd, commands[i].name)) continue;
        fprintf(out, "%s\n\nUsage:\n  bgx %s", commands[i].description, commands[i].syntax);
        print_aliases(out, i);
        fputc('\n', out);
        if (*commands[i].details) fprintf(out, "\n%s", commands[i].details);
        return;
    }
}

static bool is_help(const char *arg) { return !strcmp(arg, "--help") || !strcmp(arg, "-h"); }

static Status observed(const Record *record, Error *err) {
    if (record->starting) {
        fail(err, "launch outcome has not been confirmed");
        return UNKNOWN;
    }
    if (record->exited || !record->has_identity) return EXITED;
    return process_inspect(&record->identity, err);
}

static int unsafe_status(const char *name, Status state, Error *err) {
    return fail(err, "%s has %s identity; inspect it or use bgx remove %s --force to delete data without stopping processes",
                name, status_name(state), name);
}

static int init_record(Record *r, const char *name, int argc, char **argv, Error *err) {
    memset(r, 0, sizeof(*r));
    r->name = strdup(name);
    r->cwd = getcwd(NULL, 0);
    r->argv = calloc((size_t)argc + 1, sizeof(*r->argv));
    r->starting = true;
    r->started = time(NULL);
    if (!r->name || !r->argv) { record_free(r); return fail(err, "out of memory"); }
    if (!r->cwd) { record_free(r); return syserr(err, "get working directory"); }
    if (r->started < 0) { record_free(r); return fail(err, "cannot read current time"); }
    for (int i = 0; i < argc; ++i) {
        r->argv[r->argc] = strdup(argv[i]);
        if (!r->argv[r->argc]) { record_free(r); return fail(err, "out of memory"); }
        r->argc++;
    }
    return 0;
}

static int cleanup_launch(Store *s, const char *name, Error *err) {
    Error cleanup = {0};
    if (store_remove(s, name, &cleanup)) {
        size_t used = strlen(err->text);
        snprintf(err->text + used, sizeof(err->text) - used, "; cleanup failed: %.180s", cleanup.text);
    }
    return -1;
}

static int start_locked(Store *s, const char *name, int argc, char **argv, Error *err) {
    Record old;
    int loaded = store_load(s, name, &old, err);
    if (loaded < 0) return -1;
    if (!loaded) {
        Status state = observed(&old, err);
        record_free(&old);
        if (state == RUNNING) return fail(err, "%s is running; use bgx stop %s first", name, name);
        if (state != EXITED) return unsafe_status(name, state, err);
    }
    Record record;
    if (init_record(&record, name, argc, argv, err)) return -1;
    if (store_remove(s, name, err)) { record_free(&record); return -1; }
    int logfd = store_prepare(s, name, err);
    if (logfd < 0) {
        record_free(&record);
        return cleanup_launch(s, name, err);
    }
    if (store_save(s, &record, err)) {
        close(logfd); record_free(&record);
        return cleanup_launch(s, name, err);
    }
    LaunchResult launch = process_start(record.argv, logfd, &record.identity, err);
    close(logfd);
    if (launch != LAUNCH_OK) {
        record_free(&record);
        if (launch == LAUNCH_UNCERTAIN) return -1; /* Keep the durable starting marker. */
        return cleanup_launch(s, name, err);
    }
    record.starting = false;
    record.has_identity = true;
    bgx_test_point("before_commit");
    int rc = bgx_test_fault("save_identity") ? fail(err, "injected metadata write failure") : store_save(s, &record, err);
    if (rc) {
        Error stop_error = {0};
        if (process_abort_launch(&record.identity, &stop_error)) {
            size_t used = strlen(err->text);
            snprintf(err->text + used, sizeof(err->text) - used,
                     "; cleanup PID %ld failed: %.140s", (long)record.identity.pid, stop_error.text);
        } else cleanup_launch(s, name, err);
    }
    record_free(&record);
    return rc;
}

static int stop_locked(Store *s, const char *name, Record *r, Error *err) {
    Status state = observed(r, err);
    if (state == RUNNING) {
        if (process_stop(&r->identity, err)) return -1;
    } else if (state != EXITED) return unsafe_status(name, state, err);
    if (r->exited) return 0;
    r->exited = true;
    return store_save(s, r, err);
}

static int seek_tail(int fd, off_t end, uintmax_t lines, off_t *start, Error *err) {
    *start = end;
    if (!lines) return 0;
    char buffer[32768];
    off_t position = end;
    while (position > 0) {
        size_t length = position < (off_t)sizeof(buffer) ? (size_t)position : sizeof(buffer);
        position -= (off_t)length;
        size_t used = 0;
        while (used < length) {
            ssize_t n = pread(fd, buffer + used, length - used, position + (off_t)used);
            if (n < 0 && errno == EINTR) continue;
            if (n < 0) return syserr(err, "read log");
            if (!n) return fail(err, "log was truncated while reading");
            used += (size_t)n;
        }
        for (size_t i = length; i > 0; --i) {
            off_t offset = position + (off_t)i - 1;
            /* A final newline terminates the last line; it does not add an empty line. */
            if (buffer[i - 1] == '\n' && offset != end - 1 && --lines == 0) {
                *start = offset + 1;
                return 0;
            }
        }
    }
    *start = 0;
    return 0;
}

static int print_log(int fd, const uintmax_t *tail, Error *err) {
    struct stat st;
    if (fstat(fd, &st)) return syserr(err, "inspect log");
    off_t start = 0;
    if (tail && seek_tail(fd, st.st_size, *tail, &start, err)) return -1;
    if (lseek(fd, start, SEEK_SET) < 0) return syserr(err, "seek log");
    off_t remaining = st.st_size - start;
    char buffer[32768];
    while (remaining > 0) {
        size_t length = remaining < (off_t)sizeof(buffer) ? (size_t)remaining : sizeof(buffer);
        ssize_t n = read(fd, buffer, length);
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) return syserr(err, "read log");
        if (!n) break;
        if (write_all(STDOUT_FILENO, buffer, (size_t)n, err)) return -1;
        remaining -= n;
    }
    return 0;
}

static int log_instance(const char *name, const uintmax_t *tail, Error *err) {
    Store s;
    int opened = store_open(&s, false, err);
    if (opened < 0) return -1;
    if (opened == 1) return fail(err, "instance not found: %s", name);
    int fd = store_log(&s, name, err);
    store_close(&s);
    if (fd < 0) return -1;
    int rc = print_log(fd, tail, err);
    close(fd);
    return rc;
}

static void info_value(const char *label, const char *value) {
    printf("%-14s ", label);
    print_arg(stdout, value);
    fputc('\n', stdout);
}

static int print_info(const Record *record, const char *log_path, Error *err) {
    struct tm local;
    char started[64];
    if (!localtime_r(&record->started, &local) ||
        !strftime(started, sizeof(started), "%Y-%m-%d %H:%M:%S %z", &local))
        return fail(err, "cannot format local start time");
    Error observation = {0};
    Status state = observed(record, &observation);
    char pid[32] = "-", pgid[32] = "-", uptime[32] = "-";
    if (record->has_identity) {
        snprintf(pid, sizeof(pid), "%ld", (long)record->identity.pid);
        snprintf(pgid, sizeof(pgid), "%ld", (long)record->identity.pgid);
    }
    if (state == RUNNING) {
        time_t now = time(NULL);
        long long seconds = now > record->started ? (long long)(now - record->started) : 0;
        snprintf(uptime, sizeof(uptime), "%02lld:%02lld:%02lld",
                 seconds / 3600, (seconds % 3600) / 60, seconds % 60);
    }
    printf("%-14s %s\n", "Name:", record->name);
    printf("%-14s %s\n", "Status:", status_name(state));
    if ((state == STALE || state == UNKNOWN) && observation.text[0])
        printf("%-14s %s\n", "Reason:", observation.text);
    printf("%-14s %s\n", "Recorded PID:", pid);
    printf("%-14s %s\n", "Recorded PGID:", pgid);
    printf("%-14s %s\n", "Started:", started);
    printf("%-14s %s\n", "Uptime:", uptime);
    info_value("Directory:", record->cwd);
    printf("%-14s ", "Command:");
    for (size_t i = 0; i < record->argc; ++i) {
        if (i) fputc(' ', stdout);
        print_arg(stdout, record->argv[i]);
    }
    fputc('\n', stdout);
    info_value("Log:", log_path);
    return fflush(stdout) ? syserr(err, "write output") : 0;
}

static int info_instance(const char *name, Error *err) {
    Store s;
    int opened = store_open(&s, false, err);
    if (opened < 0) return -1;
    if (opened == 1) return fail(err, "instance not found: %s", name);
    Record record;
    int loaded = store_load(&s, name, &record, err);
    char *log_path = loaded ? NULL : store_log_path(&s, name, err);
    store_close(&s);
    int rc = -1;
    if (loaded == 1) fail(err, "instance not found: %s", name);
    else if (loaded < 0) {
        Error cause = *err;
        fail(err, "%s: %.360s", name, cause.text);
    } else if (log_path) rc = print_info(&record, log_path, err);
    free(log_path);
    record_free(&record);
    return rc;
}

static int mutate_instance(const char *command, const char *name, bool force,
                           int argc, char **argv, Error *err) {
    Store s;
    if (store_open(&s, true, err)) return -1;
    int lock = store_lock(&s, name, err);
    if (lock < 0) { store_close(&s); return -1; }
    int rc = -1;
    Record record = {0};
    if (!strcmp(command, "start")) rc = start_locked(&s, name, argc, argv, err);
    else if (!strcmp(command, "remove") && force) {
        fputs("bgx: --force deletes the record and log only; no processes will be stopped; old processes may still be running.\n", stderr);
        rc = store_remove(&s, name, err);
    } else {
        int loaded = store_load(&s, name, &record, err);
        if (loaded == 1) fail(err, "instance not found: %s", name);
        if (!loaded) {
            if (!strcmp(command, "stop")) rc = stop_locked(&s, name, &record, err);
            else if (!strcmp(command, "remove")) {
                Status state = observed(&record, err);
                if (state == EXITED) rc = store_remove(&s, name, err);
                else if (state == RUNNING) fail(err, "%s is running; stop it before removing", name);
                else unsafe_status(name, state, err);
            }
        }
    }
    record_free(&record);
    close(lock);
    store_close(&s);
    return rc;
}

static int list_instances(Error *err) {
    Store s;
    int opened = store_open(&s, false, err);
    if (opened < 0) return -1;
    printf("%-24s %-9s %-8s %-9s %s\n", "NAME", "STATUS", "PID", "UPTIME", "COMMAND");
    if (opened == 1) return fflush(stdout) ? syserr(err, "write output") : 0;
    char **names = NULL;
    size_t count = 0;
    int rc = store_names(&s, &names, &count, err);
    bool incomplete = false;
    for (size_t i = 0; !rc && i < count; ++i) {
        Record record;
        Error load_error = {0};
        int loaded = store_load(&s, names[i], &record, &load_error);
        if (loaded == 1) continue; /* Concurrent preparation/removal. */
        if (loaded < 0) {
            printf("%-24s %-9s %-8s %-9s %s\n", names[i], status_name(UNKNOWN), "-", "-", "-");
            fprintf(stderr, "bgx: %s: %s\n", names[i], load_error.text);
            incomplete = true;
            continue;
        }
        Error inspect_error = {0};
        Status state = observed(&record, &inspect_error);
        char pid[32] = "-", uptime[32] = "-";
        if (state == RUNNING) {
            snprintf(pid, sizeof(pid), "%ld", (long)record.identity.pid);
            time_t now = time(NULL);
            long long seconds = now > record.started ? (long long)(now - record.started) : 0;
            snprintf(uptime, sizeof(uptime), "%02lld:%02lld:%02lld",
                     seconds / 3600, (seconds % 3600) / 60, seconds % 60);
        }
        printf("%-24s %-9s %-8s %-9s ", record.name, status_name(state), pid, uptime);
        for (size_t j = 0; j < record.argc; ++j) {
            if (j) fputc(' ', stdout);
            print_arg(stdout, record.argv[j]);
        }
        fputc('\n', stdout);
        record_free(&record);
    }
    for (size_t i = 0; i < count; ++i) free(names[i]);
    free(names);
    store_close(&s);
    if (fflush(stdout) && !rc) rc = syserr(err, "write output");
    if (!rc && incomplete) rc = fail(err, "some instance records could not be read");
    return rc;
}

int main(int argc, char **argv) {
    /* Keep handshake descriptors above stderr even when invoked with closed stdio. */
    for (int fd = 0; fd < 3; ++fd) {
        if (fcntl(fd, F_GETFD) >= 0 || errno != EBADF) continue;
        int input = open("/dev/null", O_RDWR);
        if (input < 0) return 1;
        if (input != fd) { if (dup2(input, fd) < 0) { close(input); return 1; } close(input); }
    }
    signal(SIGPIPE, SIG_IGN);
    signal(SIGCHLD, SIG_DFL);
    if (argc == 1) { usage(stdout); return 0; }
    Error err = {0};
    const char *command = resolve_command(argv[1]);
    if (!command) goto syntax;
    if (argc == 2 && !strcmp(command, "version")) {
        printf("bgx %s\n", BGX_VERSION); return 0;
    }
    if (!strcmp(command, "help") && (argc == 2 || (argc == 3 && resolve_command(argv[2])))) {
        if (argc == 2) usage(stdout);
        else command_help(stdout, resolve_command(argv[2]));
        return 0;
    }
    if (argc == 3 && is_help(argv[2])) { command_help(stdout, command); return 0; }
    int rc;
    if (argc == 2 && !strcmp(command, "list")) rc = list_instances(&err);
    else {
        const char *name = argc > 2 ? argv[2] : "";
        bool force = false;
        uintmax_t tail_lines = 0;
        const uintmax_t *tail = NULL;
        int program_argc = 0;
        char **program_argv = NULL;
        if (!strcmp(command, "start")) {
            if (argc < 5 || strcmp(argv[3], "--") || !*argv[4]) goto syntax;
            program_argc = argc - 4; program_argv = argv + 4;
        } else if (!strcmp(command, "remove")) {
            if (argc == 4 && !strcmp(argv[2], "--force")) { force = true; name = argv[3]; }
            else if (argc == 4 && !strcmp(argv[3], "--force")) force = true;
            else if (argc != 3) goto syntax;
        } else if (!strcmp(command, "log")) {
            if (argc == 5) {
                const char *number;
                if (!strcmp(argv[3], "--tail")) number = argv[4];
                else if (!strcmp(argv[2], "--tail")) { number = argv[3]; name = argv[4]; }
                else goto syntax;
                if (!*number || strspn(number, "0123456789") != strlen(number)) {
                    fail(&err, "--tail requires a nonnegative integer"); goto syntax;
                }
                errno = 0;
                tail_lines = strtoumax(number, NULL, 10);
                if (errno == ERANGE) { fail(&err, "--tail count is too large"); goto syntax; }
                tail = &tail_lines;
            } else if (argc != 3) goto syntax;
        } else if ((!strcmp(command, "stop") || !strcmp(command, "info")) && argc == 3) {
            /* Name is an argument, even if it is also a subcommand word. */
        } else goto syntax;
        if (valid_name(name, &err)) goto syntax;
        if (!strcmp(command, "log")) rc = log_instance(name, tail, &err);
        else if (!strcmp(command, "info")) rc = info_instance(name, &err);
        else rc = mutate_instance(command, name, force, program_argc, program_argv, &err);
    }
    if (rc) fprintf(stderr, "bgx: %s\n", err.text[0] ? err.text : "operation failed");
    return rc ? 1 : 0;
syntax:
    fprintf(stderr, "bgx: %s\n\n", err.text[0] ? err.text : "invalid arguments");
    usage(stderr);
    return 2;
}
