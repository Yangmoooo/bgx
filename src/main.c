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

typedef enum {
    CMD_START, CMD_STOP, CMD_LIST, CMD_INFO, CMD_LOG,
    CMD_REMOVE, CMD_PRUNE, CMD_HELP, CMD_VERSION, CMD_COUNT
} Command;

static const struct {
    const char *name;
    const char *syntax;
    const char *description;
    const char *details;
    const char *aliases[3];
} commands[] = {
    [CMD_START] = {"start", "start [NAME] -- PROGRAM [ARGS...]", "Start a background command and print its name.",
     "Omit NAME to generate a program name plus a four-character random suffix.\n"
     "An explicit name can be reused only after its previous run has exited; reuse discards\n"
     "the previous record and log, even if the new command fails to start.\n"
     "The command inherits the current directory and environment.\n"
     "Output is logged; terminal input is disabled.\n", {"s"}},
    [CMD_STOP] = {"stop", "stop NAME", "Stop a background command and its process group.",
     "Send TERM, wait up to 5 seconds, then KILL if needed. Keep the record and log.\n", {NULL}},
    [CMD_LIST] = {"list", "list", "List background commands and their status.",
     "Show name, status, PID, uptime, and command.\n", {"ls"}},
    [CMD_INFO] = {"info", "info NAME", "Show a task's record and current status.",
     "Show recorded process IDs, local start time, uptime, directory, command, and log path.\n"
     "Recorded IDs are historical metadata, not proof that a process is still running.\n"
     "Startup failures include their diagnostics; pending launches have unknown status.\n", {NULL}},
    [CMD_REMOVE] = {"remove", "remove NAME [--force]", "Remove a command's record and log.",
     "The command must have exited. --force deletes data without stopping any processes.\n", {"rm"}},
    [CMD_PRUNE] = {"prune", "prune [--dry-run]", "Remove records and logs for tasks that have exited.",
     "Only tasks assessed as exited qualify; running tasks are skipped silently and no\n"
     "processes are signalled. Stale or unknown identity, unreadable records, and failed\n"
     "deletions are reported by name and make the command exit nonzero after other\n"
     "eligible tasks are removed.\n"
     "--dry-run lists what would be removed without changing files or waiting on locks.\n", {NULL}},
    [CMD_LOG] = {"log", "log NAME [--tail N] [-f|--follow]", "Print a command's log, optionally following new output.",
     "Print the full log by default, or the last N lines with --tail N.\n"
     "N must be a nonnegative integer; 0 prints nothing.\n"
     "-f/--follow keeps printing appended output until interrupted or the output closes.\n"
     "A follow session stays with the log opened for this execution, even after the name is reused.\n", {NULL}},
    [CMD_HELP] = {"help", "help [COMMAND]", "Show general or command-specific help.", "", {"-h", "--help"}},
    [CMD_VERSION] = {"version", "version", "Show the bgx version.", "", {"-V", "--version"}},
};

static void print_aliases(FILE *out, size_t index) {
    if (!commands[index].aliases[0]) return;
    fputs(" (aliases: ", out);
    for (size_t j = 0; commands[index].aliases[j]; ++j)
        fprintf(out, "%s%s", j ? ", " : "", commands[index].aliases[j]);
    fputc(')', out);
}

static void usage(FILE *out) {
    size_t width = 0;
    for (size_t i = 0; i < sizeof(commands) / sizeof(commands[0]); ++i) {
        size_t length = strlen(commands[i].syntax);
        if (length > width) width = length;
    }
    fputs("bgx - Run and manage background commands\n\n"
          "Usage:\n", out);
    for (size_t i = 0; i < sizeof(commands) / sizeof(commands[0]); ++i) {
        if (i == CMD_LIST || i == CMD_REMOVE || i == CMD_HELP) fputc('\n', out);
        fprintf(out, "  bgx %-*s  %s", (int)width, commands[i].syntax, commands[i].description);
        print_aliases(out, i);
        fputc('\n', out);
    }
}

static Command resolve_command(const char *cmd) {
    for (size_t i = 0; i < sizeof(commands) / sizeof(commands[0]); ++i) {
        if (!strcmp(cmd, commands[i].name)) return (Command)i;
        for (size_t j = 0; commands[i].aliases[j]; ++j)
            if (!strcmp(cmd, commands[i].aliases[j])) return (Command)i;
    }
    return CMD_COUNT;
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

typedef struct {
    Command command;
    bool help, force, follow, dry_run, has_tail;
    Command help_command;
    const char *name;
    uintmax_t tail_lines;
    int program_argc;
    char **program_argv;
} Arguments;

/* Parse only: borrow argv strings and never open state or execute a command.
   CMD_COUNT denotes general help (or an unrecognized command on error). */
static int parse_arguments(int argc, char **argv, Arguments *out, Error *err) {
    *out = (Arguments){.command = CMD_HELP, .help_command = CMD_COUNT};
    if (argc == 1) { out->help = true; return 0; }
    Command command = out->command = resolve_command(argv[1]);
    if (command == CMD_COUNT) return fail(err, "unknown command: %s", argv[1]);
    if (command == CMD_HELP) {
        if (argc > 3) return fail(err, "unexpected argument: %s", argv[3]);
        if (argc == 3) {
            out->help_command = resolve_command(argv[2]);
            if (out->help_command == CMD_COUNT) return fail(err, "unknown command: %s", argv[2]);
        }
        out->help = true;
        return 0;
    }
    /* Help takes precedence over other bgx arguments, but never crosses --. */
    for (int i = 2; i < argc && strcmp(argv[i], "--"); ++i) {
        if (is_help(argv[i])) {
            out->help = true;
            out->help_command = command;
            return 0;
        }
    }
    if (command == CMD_START) {
        int separator = 2;
        if (argc > 2 && strcmp(argv[2], "--")) {
            if (argv[2][0] == '-') return fail(err, "unknown option: %s", argv[2]);
            out->name = argv[2];
            if (valid_name(out->name, err)) return -1;
            separator++;
        }
        if (argc <= separator || strcmp(argv[separator], "--"))
            return fail(err, "expected '--' before program");
        if (argc <= separator + 1 || !*argv[separator + 1]) return fail(err, "missing program after '--'");
        out->program_argc = argc - separator - 1;
        out->program_argv = argv + separator + 1;
        return 0;
    }
    bool needs_name = command == CMD_STOP || command == CMD_INFO ||
                      command == CMD_REMOVE || command == CMD_LOG;
    for (int i = 2; i < argc; ++i) {
        const char *arg = argv[i];
        bool *flag = NULL;
        if (command == CMD_REMOVE && !strcmp(arg, "--force")) flag = &out->force;
        else if (command == CMD_PRUNE && !strcmp(arg, "--dry-run")) flag = &out->dry_run;
        else if (command == CMD_LOG && (!strcmp(arg, "-f") || !strcmp(arg, "--follow"))) flag = &out->follow;
        if (flag) {
            if (*flag) return fail(err, "duplicate option: %s", arg);
            *flag = true;
        } else if (command == CMD_LOG && !strcmp(arg, "--tail")) {
            if (out->has_tail) return fail(err, "duplicate option: --tail");
            if (++i == argc) return fail(err, "--tail requires a value");
            const char *number = argv[i];
            if (!*number || strspn(number, "0123456789") != strlen(number))
                return fail(err, "--tail requires a nonnegative integer");
            errno = 0;
            out->tail_lines = strtoumax(number, NULL, 10);
            if (errno == ERANGE) return fail(err, "--tail count is too large");
            out->has_tail = true;
        } else if (arg[0] == '-') return fail(err, "unknown option: %s", arg);
        else if (!needs_name || out->name) return fail(err, "unexpected argument: %s", arg);
        else out->name = arg;
    }
    if (needs_name) {
        if (!out->name) return fail(err, "missing task name");
        return valid_name(out->name, err);
    }
    return 0;
}

static Status task_status(const Record *record, Error *err) {
    if (record->launch == PENDING) {
        fail(err, "launch outcome has not been confirmed");
        return UNKNOWN;
    }
    if (record->exited) return EXITED;
    return process_inspect(&record->identity, err);
}

static int unsafe_status(const char *name, Status state, Error *err) {
    return fail(err, "%s has %s identity; inspect it or use bgx remove %s --force to delete data without stopping processes",
                name, status_name(state), name);
}

typedef struct {
    Store *store;
    Record *record;
} ReleaseContext;

/* The sole pending record must identify the child durably before it may exec. */
static int persist_release(void *context, const Identity *identity, Error *err) {
    ReleaseContext *release = context;
    release->record->has_identity = true;
    release->record->identity = *identity;
    if (bgx_test_fault("save_release")) return fail(err, "injected release identity write failure");
    return store_save(release->store, release->record, NULL, err);
}

/* There is no previous definition to restore. A failed launch retains only its
   own diagnostics. Unconfirmed execution or cleanup remains pending/unknown. */
static void save_launch_error(Store *s, Record *record, bool cleared, Error *err) {
    record->launch = cleared ? FAILED : PENDING;
    record->exited = cleared;
    snprintf(record->error, sizeof(record->error), "%s", err->text);
    Error stored = {0};
    int rc = bgx_test_fault("save_diagnostics")
                 ? fail(&stored, "injected diagnostics write failure")
                 : store_save(s, record, NULL, &stored);
    if (rc) append_error(err, "; could not save diagnostics: %.120s", stored.text);
}

static int launch_task(Store *s, const char *name, char **argv, int argc,
                       const char *cwd, Error *err) {
    Record record = {.launch = PENDING, .started = time(NULL)};
    record.name = strdup(name);
    record.cwd = strdup(cwd);
    record.argv = calloc((size_t)argc + 1, sizeof(*record.argv));
    if (!record.name || !record.cwd || !record.argv) {
        record_free(&record);
        return fail(err, "out of memory");
    }
    if (record.started < 0) { record_free(&record); return fail(err, "cannot read current time"); }
    for (int i = 0; i < argc; ++i) {
        record.argv[record.argc] = strdup(argv[i]);
        if (!record.argv[record.argc]) { record_free(&record); return fail(err, "out of memory"); }
        record.argc++;
    }
    int rc = -1;
    int logfd = store_create_log(s, name, err);
    if (logfd < 0) goto done;
    if (store_save(s, &record, NULL, err)) {
        close(logfd);
        save_launch_error(s, &record, true, err);
        goto done;
    }
    bgx_test_point("launch_prepared");
    ReleaseContext release = {s, &record};
    Identity identity = {0};
    LaunchResult launch = process_start(record.argv, logfd, persist_release, &release, &identity, err);
    close(logfd);
    if (identity.pid > 0) {
        record.has_identity = true;
        record.identity = identity;
    }
    if (launch != LAUNCH_OK) {
        save_launch_error(s, &record, launch == LAUNCH_FAILED, err);
        goto done;
    }
    bgx_test_point("before_commit");
    record.launch = STARTED;
    bool published = false;
    rc = bgx_test_fault("save_identity")
             ? fail(err, "injected metadata write failure")
             : store_save(s, &record, &published, err);
    if (rc && published) {
        /* Exec and publication are confirmed. Do not roll back a record readers
           may already have seen or kill its process on a directory sync error. */
        append_error(err, "; command started and is recorded; inspect it with bgx info %s", name);
    } else if (rc) {
        Error cleanup = {0};
        bool cleared = process_abort_launch(&identity, &cleanup) == 0;
        if (!cleared) append_error(err, "; cleanup failed: %.140s", cleanup.text);
        save_launch_error(s, &record, cleared, err);
    }
done:
    record_free(&record);
    return rc;
}

static int start_locked(Store *s, const char *name, Record *record, int argc, char **argv, Error *err) {
    if (record->name) {
        Status state = task_status(record, err);
        if (state == RUNNING) return fail(err, "%s is running; stop it before reusing its name", name);
        if (state != EXITED) return unsafe_status(name, state, err);
    }
    char *cwd = getcwd(NULL, 0);
    if (!cwd) return syserr(err, "get working directory");
    /* Validate arguments and the old run before retiring its directory. Readers
       with a pinned directory/log cannot accidentally bind to the new run. */
    int rc = record->name ? store_remove(s, name, err) : 0;
    if (!rc) rc = launch_task(s, name, argv, argc, cwd, err);
    free(cwd);
    return rc;
}

static int stop_locked(Store *s, const char *name, Record *record, Error *err) {
    Status state = task_status(record, err);
    if (state == RUNNING) {
        if (process_stop(&record->identity, err)) return -1;
    } else if (state != EXITED) return unsafe_status(name, state, err);
    if (record->exited) return 0;
    record->exited = true;
    return store_save(s, record, NULL, err);
}

static int remove_locked(Store *s, const char *name, Record *record, Error *err) {
    Status state = task_status(record, err);
    if (state == RUNNING) return fail(err, "%s is running; stop it before removing", name);
    if (state != EXITED) return unsafe_status(name, state, err);
    return store_remove(s, name, err);
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

static int follow_log(int fd, const uintmax_t *tail, Error *err) {
    struct stat st;
    if (fstat(fd, &st)) return syserr(err, "inspect log");
    off_t start = 0;
    if (tail && seek_tail(fd, st.st_size, *tail, &start, err)) return -1;
    if (lseek(fd, start, SEEK_SET) < 0) return syserr(err, "seek log");
    char buffer[32768];
    for (;;) {
        ssize_t n = read(fd, buffer, sizeof(buffer));
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) return syserr(err, "read log");
        if (n > 0) {
            /* A closed downstream pipe completes the view without a diagnostic. */
            if (write_all(STDOUT_FILENO, buffer, (size_t)n, err)) return errno == EPIPE ? 0 : -1;
            continue;
        }
        struct timespec delay = {.tv_nsec = 200000000};
        while (nanosleep(&delay, &delay) && errno == EINTR) {}
        struct stat now;
        if (fstat(fd, &now)) return syserr(err, "inspect log");
        off_t offset = lseek(fd, 0, SEEK_CUR);
        if (offset < 0) return syserr(err, "seek log");
        /* Observed shrinkage means this same file was truncated; restart it. */
        if (offset > now.st_size && lseek(fd, 0, SEEK_SET) < 0) return syserr(err, "seek log");
    }
}

static int log_instance(const char *name, const uintmax_t *tail, bool follow, Error *err) {
    Store s;
    int opened = store_open(&s, false, err);
    if (opened < 0) return -1;
    if (opened == 1) return fail(err, "instance not found: %s", name);
    int fd = store_log(&s, name, err);
    store_close(&s);
    if (fd < 0) return -1;
    int rc = follow ? follow_log(fd, tail, err) : print_log(fd, tail, err);
    close(fd);
    return rc;
}

static void info_value(const char *label, const char *value) {
    printf("%-14s ", label);
    if (value) print_arg(stdout, value);
    else fputc('-', stdout);
    fputc('\n', stdout);
}

static void info_argv(const char *label, const char *const *argv, size_t argc) {
    printf("%-14s ", label);
    for (size_t i = 0; i < argc; ++i) {
        if (i) fputc(' ', stdout);
        print_arg(stdout, argv[i]);
    }
    fputc('\n', stdout);
}

static int print_info(const Record *record, const char *log_path, Error *err) {
    Error observation = {0};
    Status state = task_status(record, &observation);
    char pid[32] = "-", pgid[32] = "-", uptime[32] = "-", started[64];
    struct tm local;
    if (!localtime_r(&record->started, &local) ||
        !strftime(started, sizeof(started), "%Y-%m-%d %H:%M:%S %z", &local))
        return fail(err, "cannot format local start time");
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
    printf("%-14s %s\n", "Launch:", launch_state_name(record->launch));
    if ((state == STALE || state == UNKNOWN) && observation.text[0])
        printf("%-14s %s\n", "Reason:", observation.text);
    printf("%-14s %s\n", "Recorded PID:", pid);
    printf("%-14s %s\n", "Recorded PGID:", pgid);
    printf("%-14s %s\n", "Started:", started);
    printf("%-14s %s\n", "Uptime:", uptime);
    info_value("Directory:", record->cwd);
    info_argv("Command:", (const char *const *)record->argv, record->argc);
    info_value("Log:", log_path);
    if (*record->error) info_value("Error:", record->error);
    return fflush(stdout) ? syserr(err, "write output") : 0;
}

static int info_instance(const char *name, Error *err) {
    Store s;
    int opened = store_open(&s, false, err);
    if (opened < 0) return -1;
    if (opened == 1) return fail(err, "instance not found: %s", name);
    Record record;
    int loaded = store_load(&s, name, &record, err);
    char *path = loaded == 0 ? store_log_path(&s, name, err) : NULL;
    int rc = -1;
    if (loaded == 1) fail(err, "instance not found: %s", name);
    else if (loaded < 0) {
        Error cause = *err;
        fail(err, "%s: %.360s", name, cause.text);
    } else if (path) rc = print_info(&record, path, err);
    record_free(&record);
    free(path);
    store_close(&s);
    return rc;
}

static int mutate_instance(Command command, const char *name, bool force,
                           int program_argc, char **program_argv, Error *err) {
    Store s;
    if (store_open(&s, true, err)) return -1;
    bool automatic = command == CMD_START && !name;
    char generated[30];
    int rc = -1, lock = -1;
    for (unsigned tries = 0; tries < 100; ++tries) {
        if (automatic) {
            if (generate_name(program_argv[0], generated, err)) goto done;
            if (tries == 0 && bgx_test_fault("name_collision"))
                memcpy(generated + strlen(generated) - 4, "0000", 4);
            name = generated;
        }
        bgx_test_point("before_name_lock");
        lock = store_lock(&s, name, err);
        if (lock < 0) goto done;
        if (!automatic) break;
        int occupied = store_exists(&s, name, err);
        if (occupied < 0) goto done;
        if (!occupied) break;
        close(lock);
        lock = -1;
    }
    if (lock < 0) { fail(err, "could not allocate an unused name"); goto done; }
    if (command == CMD_REMOVE && force) {
        fputs("bgx: --force deletes the record and log only; no processes will be stopped; old processes may still be running.\n", stderr);
        rc = store_remove(&s, name, err);
    } else {
        Record record;
        int loaded = store_load(&s, name, &record, err);
        if (loaded < 0) rc = -1;
        else if (command == CMD_START) rc = start_locked(&s, name, &record, program_argc, program_argv, err);
        else if (loaded == 1) fail(err, "instance not found: %s", name);
        else if (command == CMD_STOP) rc = stop_locked(&s, name, &record, err);
        else if (command == CMD_REMOVE) rc = remove_locked(&s, name, &record, err);
        record_free(&record);
    }
    if (!rc && command == CMD_START) {
        printf("%s\n", name);
        if (fflush(stdout)) {
            syserr(err, "write name");
            append_error(err, "; command started and is recorded");
            rc = -1;
        }
    }
done:
    if (rc && command == CMD_START && name) {
        Error cause = *err;
        fail(err, "%s: %.370s", name, cause.text);
    }
    if (lock >= 0) close(lock);
    store_close(&s);
    return rc;
}

/* Per-task prune problems are named and make the batch exit nonzero without
   stopping later candidates. */
static void prune_name_error(const char *name, const char *message) {
    fprintf(stderr, "bgx: %s: %s\n", name, message);
}

/* Delete one candidate under its name lock. The record is reloaded and reassessed
   while locked so a task that started while waiting is never removed. */
static void prune_candidate(Store *s, const char *name, size_t *removed, bool *failed) {
    Error detail = {0};
    int lock = store_lock(s, name, &detail);
    if (lock < 0) { prune_name_error(name, detail.text); *failed = true; return; }
    Record task;
    int loaded = store_load(s, name, &task, &detail);
    if (loaded == 1) {
        /* Concurrent preparation/removal: the record is already gone. */
    } else if (loaded < 0) {
        prune_name_error(name, detail.text);
        *failed = true;
    } else {
        Status state = task_status(&task, &detail);
        if (state == RUNNING) {
            /* The name was reused for a running task while waiting. */
        } else if (state != EXITED) {
            prune_name_error(name, detail.text[0] ? detail.text : status_name(state));
            *failed = true;
        } else if (store_remove(s, name, &detail)) {
            prune_name_error(name, detail.text);
            *failed = true;
        } else {
            printf("Removed %s\n", name);
            (*removed)++;
        }
        record_free(&task);
    }
    close(lock);
}

static int prune_instances(bool dry_run, Error *err) {
    Store s;
    int opened = store_open(&s, false, err);
    if (opened < 0) return -1;
    char **names = NULL;
    size_t count = 0;
    /* Enumerate fully before deleting: a partial name list must not authorize work. */
    int rc = opened == 1 ? 0 : store_names(&s, &names, &count, err);
    size_t removed = 0;
    bool failed = false, locks = false;
    for (size_t i = 0; !rc && i < count; ++i) {
        Record task;
        Error detail = {0};
        int loaded = store_load(&s, names[i], &task, &detail);
        if (loaded == 1) continue; /* Concurrent preparation/removal. */
        if (loaded < 0) {
            prune_name_error(names[i], detail.text);
            failed = true;
            continue;
        }
        Status state = task_status(&task, &detail);
        record_free(&task);
        if (state == RUNNING) continue; /* Running tasks are silent, normal skips. */
        if (state != EXITED) {
            prune_name_error(names[i], detail.text[0] ? detail.text : status_name(state));
            failed = true;
            continue;
        }
        if (dry_run) {
            printf("Would remove %s\n", names[i]);
            removed++;
            continue;
        }
        if (!locks) {
            /* An existing store without a lock directory still coordinates names. */
            if (store_open_locks(&s, err)) { rc = -1; break; }
            locks = true;
        }
        bgx_test_point("prune_candidate");
        prune_candidate(&s, names[i], &removed, &failed);
    }
    for (size_t i = 0; i < count; ++i) free(names[i]);
    free(names);
    store_close(&s);
    if (rc) return -1;
    printf(dry_run ? "Would remove %zu task(s).\n" : "Removed %zu task(s).\n", removed);
    if (fflush(stdout)) return syserr(err, "write output");
    if (failed) return fail(err, "some tasks could not be pruned");
    return 0;
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
        Record task;
        Error load_error = {0};
        int loaded = store_load(&s, names[i], &task, &load_error);
        if (loaded == 1) continue; /* Concurrent preparation/removal. */
        if (loaded < 0) {
            printf("%-24s %-9s %-8s %-9s %s\n", names[i], status_name(UNKNOWN), "-", "-", "-");
            fprintf(stderr, "bgx: %s: %s\n", names[i], load_error.text);
            incomplete = true;
            continue;
        }
        Error inspect_error = {0};
        Status state = task_status(&task, &inspect_error);
        const Record *confirmed = &task;
        const char *const *argv = (const char *const *)task.argv;
        size_t argc = task.argc;
        char pid[32] = "-", uptime[32] = "-";
        if (state == RUNNING && confirmed && confirmed->has_identity) {
            snprintf(pid, sizeof(pid), "%ld", (long)confirmed->identity.pid);
            time_t now = time(NULL);
            long long seconds = now > confirmed->started ? (long long)(now - confirmed->started) : 0;
            snprintf(uptime, sizeof(uptime), "%02lld:%02lld:%02lld",
                     seconds / 3600, (seconds % 3600) / 60, seconds % 60);
        }
        printf("%-24s %-9s %-8s %-9s ", names[i], status_name(state), pid, uptime);
        for (size_t j = 0; j < argc; ++j) {
            if (j) fputc(' ', stdout);
            print_arg(stdout, argv[j]);
        }
        fputc('\n', stdout);
        record_free(&task);
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
    Error err = {0};
    Arguments args;
    if (parse_arguments(argc, argv, &args, &err)) {
        fprintf(stderr, "bgx: %s\n\n", err.text);
        if (args.command == CMD_COUNT) usage(stderr);
        else fprintf(stderr, "Usage:\n  bgx %s\n", commands[args.command].syntax);
        return 2;
    }
    if (args.help) {
        if (args.help_command == CMD_COUNT) usage(stdout);
        else command_help(stdout, commands[args.help_command].name);
        return 0;
    }
    int rc;
    switch (args.command) {
    case CMD_VERSION: printf("bgx %s\n", BGX_VERSION); return 0;
    case CMD_LIST: rc = list_instances(&err); break;
    case CMD_PRUNE: rc = prune_instances(args.dry_run, &err); break;
    case CMD_INFO: rc = info_instance(args.name, &err); break;
    case CMD_LOG:
        rc = log_instance(args.name, args.has_tail ? &args.tail_lines : NULL, args.follow, &err);
        break;
    case CMD_START: case CMD_STOP: case CMD_REMOVE:
        rc = mutate_instance(args.command, args.name, args.force,
                             args.program_argc, args.program_argv, &err);
        break;
    default: return 2; /* Help and invalid commands are handled before execution. */
    }
    if (rc) fprintf(stderr, "bgx: %s\n", err.text[0] ? err.text : "operation failed");
    return rc ? 1 : 0;
}
