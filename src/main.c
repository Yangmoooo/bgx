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
    CMD_START, CMD_RESTART, CMD_REPLACE, CMD_STOP, CMD_LIST, CMD_INFO,
    CMD_REMOVE, CMD_PRUNE, CMD_LOG, CMD_HELP, CMD_VERSION, CMD_COUNT
} Command;

static const struct {
    const char *name;
    const char *syntax;
    const char *description;
    const char *details;
    const char *aliases[3];
} commands[] = {
    [CMD_START] = {"start", "start NAME -- PROGRAM [ARGS...]", "Start a new named background command.",
     "Rejects names that already exist, even exited ones; use restart or replace for those.\n"
     "The command inherits the current directory and environment.\n"
     "Output is logged; terminal input is disabled.\n", {"s"}},
    [CMD_RESTART] = {"restart", "restart NAME", "Launch another execution of an existing task.",
     "Uses the task's recorded command and directory with the current invocation's environment.\n"
     "A running execution is stopped and confirmed ended first. The task's recorded definition\n"
     "is unchanged; exit 0 means the new execution started.\n", {NULL}},
    [CMD_REPLACE] = {"replace", "replace NAME -- PROGRAM [ARGS...]", "Adopt a new command for an existing task and launch it.",
     "Uses the supplied command and the current directory and environment.\n"
     "A running execution is stopped and confirmed ended first. The new definition is adopted\n"
     "only when the launch succeeds.\n", {NULL}},
    [CMD_STOP] = {"stop", "stop NAME", "Stop a background command and its process group.",
     "Send TERM, wait up to 5 seconds, then KILL if needed. Keep the record and log.\n", {NULL}},
    [CMD_LIST] = {"list", "list", "List background commands and their status.",
     "Show name, status, PID, uptime, and command.\n", {"ls", "ps"}},
    [CMD_INFO] = {"info", "info NAME", "Show a task's record and current status.",
     "Show recorded process IDs, local start time, uptime, directory, command, and log path.\n"
     "Recorded IDs are historical metadata, not proof that a process is still running.\n"
     "A pending or failed launch attempt is shown separately from the confirmed execution.\n", {NULL}},
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
     "A follow session stays with the log opened for this execution, even after the name is reused.\n", {"logs"}},
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
    fputs("bgx - named background commands\n\n"
          "Usage:\n", out);
    for (size_t i = 0; i < sizeof(commands) / sizeof(commands[0]); ++i) {
        fprintf(out, "  bgx %-32s %s", commands[i].syntax, commands[i].description);
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
    if (command == CMD_START || command == CMD_REPLACE) {
        if (argc < 3 || !strcmp(argv[2], "--")) return fail(err, "missing task name");
        if (argv[2][0] == '-') return fail(err, "unknown option: %s", argv[2]);
        out->name = argv[2];
        if (valid_name(out->name, err)) return -1;
        if (argc < 4 || strcmp(argv[3], "--")) return fail(err, "expected '--' after task name");
        if (argc < 5 || !*argv[4]) return fail(err, "missing program after '--'");
        out->program_argc = argc - 4;
        out->program_argv = argv + 4;
        return 0;
    }
    bool needs_name = command == CMD_RESTART || command == CMD_STOP || command == CMD_INFO ||
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

/* A task's durable state: the confirmed execution (meta.json), at most one
   latest launch attempt (attempt.json), and an optional recovery artifact
   (recovery.json) that marks an adoption whose outcome is not final. */
typedef struct {
    Record meta;
    bool has_meta;
    Attempt attempt;
    bool has_attempt;
    Recovery recovery;
    bool has_recovery;
} Task;

static void task_free(Task *task) {
    record_free(&task->meta);
    attempt_free(&task->attempt);
    recovery_free(&task->recovery);
}

static int task_load(Store *s, const char *name, Task *task, Error *err) {
    memset(task, 0, sizeof(*task));
    int loaded = store_load_task(s, name, &task->meta, &task->has_meta, &task->attempt, &task->has_attempt,
                                 &task->recovery, &task->has_recovery, err);
    if (loaded < 0) { task_free(task); return -1; }
    return loaded;
}

/* The confirmed execution for observation. While a recovery artifact exists,
   its previous record is the confirmed execution even if meta.json already
   names the candidate; a first launch's marker has no previous record. */
static const Record *task_confirmed(const Task *task) {
    if (task->has_recovery) return task->recovery.has_previous ? &task->recovery.previous : NULL;
    return task->has_meta ? &task->meta : NULL;
}

/* A crash after finalizing a commit but before removing attempt.json leaves a
   committing attempt naming that execution. Unresolved adoption is never
   residue: the recovery artifact must be gone for that interpretation. */
static bool attempt_committed_residue(const Task *task) {
    if (task->has_recovery || !task->has_meta || !task->has_attempt) return false;
    const Attempt *attempt = &task->attempt;
    const Record *meta = &task->meta;
    return attempt->state == ATTEMPT_COMMITTING &&
           attempt->has_log && !strcmp(attempt->log, meta->log) &&
           attempt->has_identity && meta->has_identity &&
           attempt->identity.pid == meta->identity.pid &&
           !strcmp(attempt->identity.birth, meta->identity.birth) &&
           !strcmp(attempt->identity.boot, meta->identity.boot);
}

static const Attempt *task_attempt(const Task *task) {
    return task->has_attempt && !attempt_committed_residue(task) ? &task->attempt : NULL;
}

/* Mutating-command recovery. Only provably harmless states are resolved: a
   pre-release attempt, post-success residue, or a recovery artifact whose
   candidate is already definitively failed. An unresolved adoption is left in
   place for the caller to refuse. */
static int task_recover(Store *s, const char *name, Task *task, Error *err) {
    if (task->has_recovery) {
        if (!task->has_attempt || task->attempt.state != ATTEMPT_FAILED) return 0;
        if (task->recovery.has_previous) {
            /* The candidate is definitively failed: make A the confirmed record
               again, then retire the marker. Repeating either step is safe. */
            if (store_save(s, &task->recovery.previous, err)) return -1;
            if (store_remove_recovery(s, name, err)) return -1;
            record_free(&task->meta);
            task->meta = task->recovery.previous;
            memset(&task->recovery.previous, 0, sizeof(task->recovery.previous));
            task->recovery.has_previous = false;
            task->has_meta = true;
            task->has_recovery = false;
            return 0;
        }
        /* A first launch failed definitively: its artifacts are removed. */
        if (store_remove(s, name, err)) return -1;
        task_free(task);
        return 0;
    }
    if (attempt_committed_residue(task)) {
        if (store_discard_attempt(s, name, err)) return -1;
        attempt_free(&task->attempt);
        task->has_attempt = false;
        return 0;
    }
    if (!task->has_attempt || task->attempt.state != ATTEMPT_PREPARED) return 0;
    if (!task->has_meta) {
        attempt_free(&task->attempt);
        task->has_attempt = false;
        return store_remove(s, name, err);
    }
    if (task->attempt.has_log && !strcmp(task->attempt.log, task->meta.log)) {
        /* Damaged state that names the confirmed log: drop the record only. */
        if (store_discard_attempt(s, name, err)) return -1;
        attempt_free(&task->attempt);
        task->has_attempt = false;
        return 0;
    }
    if (store_remove_attempt(s, name, &task->attempt, err)) return -1;
    attempt_free(&task->attempt);
    task->has_attempt = false;
    return 0;
}

static Status observed(const Record *record, Error *err) {
    if (record->exited || !record->has_identity) return EXITED;
    return process_inspect(&record->identity, err);
}

/* Effective task status. A recovery artifact means an adoption is unresolved:
   its previous record is the confirmed execution, and the pending candidate
   dominates until it is definitively failed. */
static Status task_status(const Task *task, Error *err) {
    const Attempt *attempt = task_attempt(task);
    if (task->has_recovery) {
        if (!task->has_attempt || (attempt && attempt->state != ATTEMPT_FAILED)) {
            fail(err, "commit outcome has not been confirmed");
            return UNKNOWN;
        }
        if (!task->recovery.has_previous) {
            fail(err, "launch attempt has no confirmed execution");
            return UNKNOWN;
        }
        return observed(&task->recovery.previous, err);
    }
    if (attempt) {
        if (attempt->state == ATTEMPT_COMMITTING) {
            fail(err, "commit outcome has not been confirmed");
            return UNKNOWN;
        }
        if (attempt->state == ATTEMPT_UNCERTAIN) {
            fail(err, "launch outcome has not been confirmed");
            return UNKNOWN;
        }
        if (!task->has_meta) {
            fail(err, attempt->state == ATTEMPT_PREPARED
                          ? "launch was interrupted before the process started"
                          : "launch attempt has no confirmed execution");
            return UNKNOWN;
        }
    }
    if (!task->has_meta) {
        fail(err, "no confirmed execution");
        return UNKNOWN;
    }
    return observed(&task->meta, err);
}

static int unsafe_status(const char *name, Status state, Error *err) {
    return fail(err, "%s has %s identity; inspect it or use bgx remove %s --force to delete data without stopping processes",
                name, status_name(state), name);
}

static int cleanup_launch(Store *s, const char *name, Error *err) {
    Error cleanup = {0};
    if (store_remove(s, name, &cleanup)) {
        size_t used = strlen(err->text);
        snprintf(err->text + used, sizeof(err->text) - used, "; cleanup failed: %.180s", cleanup.text);
    }
    return -1;
}

typedef struct {
    Store *store;
    const Attempt *attempt;
} ReleaseContext;

/* Runs inside process_start before the candidate may exec. */
static int persist_release(void *context, const Identity *identity, Error *err) {
    ReleaseContext *release = context;
    Attempt released = *release->attempt;
    released.has_identity = true;
    released.identity = *identity;
    released.state = ATTEMPT_UNCERTAIN;
    return store_save_attempt(release->store, &released, err);
}

/* Abort a released candidate after adoption failed. Returns true when cleanup
   is confirmed; otherwise appends the cleanup failure so the caller can record
   visible uncertainty. */
static bool abort_candidate(const Identity *identity, Error *err) {
    Error stop_error = {0};
    if (!process_abort_launch(identity, &stop_error)) return true;
    append_error(err, "; cleanup PID %ld failed: %.140s", (long)identity->pid, stop_error.text);
    return false;
}

static void mark_attempt_uncertain(Store *s, Attempt *attempt, const char *cause, Error *err) {
    attempt->state = ATTEMPT_UNCERTAIN;
    snprintf(attempt->error, sizeof(attempt->error), "%.450s", cause);
    Error stored = {0};
    int saved = bgx_test_fault("uncertain_failure")
                    ? fail(&stored, "injected uncertain attempt write failure")
                    : store_save_attempt(s, attempt, &stored);
    if (saved) append_error(err, "; could not persist uncertain attempt: %.120s", stored.text);
}

/* Persist a definitively failed attempt (no candidate processes remain) and
   report its retained output when there is any. */
static void finalize_failed_attempt(Store *s, const char *name, Attempt *attempt, const char *cause, Error *err) {
    attempt->state = ATTEMPT_FAILED;
    snprintf(attempt->error, sizeof(attempt->error), "%.450s", cause);
    Error stored = {0};
    if (store_finalize_failed_attempt(s, name, attempt, &stored))
        append_error(err, "; could not persist attempt diagnostics: %.120s", stored.text);
    if (attempt->has_log) {
        Error ignored = {0};
        char *path = store_log_path(s, name, attempt->log, &ignored);
        if (path) {
            append_error(err, "; attempt log: %.180s", path);
            free(path);
        }
    }
}

/* Adoption failed before the publishing rename: meta.json still holds A, so a
   recovery marker is stale and no durable A copy is needed. */
static int fail_before_publication(Store *s, const char *name, const Record *confirmed, Attempt *attempt,
                                   const char *cause, Error *err) {
    if (abort_candidate(&attempt->identity, err)) {
        if (confirmed) finalize_failed_attempt(s, name, attempt, cause, err);
        else cleanup_launch(s, name, err);
    } else {
        mark_attempt_uncertain(s, attempt, cause, err);
    }
    Error ignored = {0};
    store_remove_recovery(s, name, &ignored);
    return -1;
}

/* Adoption failed after the publishing rename may have taken effect: meta.json
   can already name B, while recovery.json holds A. Keep A durable and visible
   until the outcome is resolved; restore it only after confirmed cleanup. */
static int fail_after_publication(Store *s, const char *name, const Record *confirmed, Attempt *attempt,
                                  const char *cause, Error *err) {
    if (abort_candidate(&attempt->identity, err)) {
        if (confirmed) {
            /* Persist B failed first: a crash then leaves a state a later
               command can finish resolving without a live candidate. */
            finalize_failed_attempt(s, name, attempt, cause, err);
            bgx_test_point("adoption_failed");
            Error restore_error = {0};
            int restored = bgx_test_fault("restore_failure")
                               ? fail(&restore_error, "injected confirmed-record restore failure")
                               : store_save(s, confirmed, &restore_error);
            if (restored) {
                append_error(err, "; could not restore confirmed record: %.120s", restore_error.text);
                return -1; /* recovery.json keeps A visible */
            }
            Error ignored = {0};
            store_remove_recovery(s, name, &ignored);
        } else {
            cleanup_launch(s, name, err);
        }
    } else {
        /* Candidate may live: leave recovery.json in place so A stays the
           durable confirmed record and the unresolved state stays unknown. */
        mark_attempt_uncertain(s, attempt, cause, err);
    }
    return -1;
}

/* Prepare, launch, and commit one execution. confirmed is the task's current
   confirmed record, or NULL when the name had no execution yet. On success the
   attempt becomes the confirmed record; on failure the confirmed record, if
   any, is preserved and the attempt is retained only as diagnostics. */
static int launch_task(Store *s, const char *name, const Record *confirmed, char **argv, int argc,
                       const char *cwd, int cwdfd, Error *err) {
    Attempt attempt = {0};
    attempt.name = strdup(name);
    attempt.cwd = strdup(cwd);
    attempt.started = time(NULL);
    attempt.argv = calloc((size_t)argc + 1, sizeof(*attempt.argv));
    attempt.state = ATTEMPT_PREPARED;
    if (!attempt.name || !attempt.cwd || !attempt.argv) { attempt_free(&attempt); return fail(err, "out of memory"); }
    if (attempt.started < 0) { attempt_free(&attempt); return fail(err, "cannot read current time"); }
    for (int i = 0; i < argc; ++i) {
        attempt.argv[attempt.argc] = strdup(argv[i]);
        if (!attempt.argv[attempt.argc]) { attempt_free(&attempt); return fail(err, "out of memory"); }
        attempt.argc++;
    }
    int rc = -1;
    int logfd = store_prepare_attempt(s, name, attempt.log, err);
    if (logfd < 0) goto done;
    attempt.has_log = true;
    if (store_save_attempt(s, &attempt, err)) { close(logfd); goto done; }
    /* The attempt supersedes any earlier failed one; its now-unreferenced
       diagnostics are residue. */
    {
        Error ignored = {0};
        store_cleanup(s, name, &ignored);
    }
    bgx_test_point("attempt_prepared");
    ReleaseContext release = {s, &attempt};
    Identity identity = {0};
    LaunchResult launch = process_start(attempt.argv, logfd, cwdfd, persist_release, &release, &identity, err);
    close(logfd);
    if (identity.pid > 0) {
        attempt.has_identity = true;
        attempt.identity = identity;
    }
    if (launch == LAUNCH_OK) {
        bgx_test_point("before_commit");
        /* Adoption becomes possible here: record that a commit may replace the
           confirmed record before the publishing rename, so an interrupted
           commit is never read as an untouched task. */
        attempt.state = ATTEMPT_COMMITTING;
        Error marker = {0};
        int marked = bgx_test_fault("commit_marker_failure")
                         ? fail(&marker, "injected commit evidence write failure")
                         : store_save_attempt(s, &attempt, &marker);
        if (marked) {
            /* The marker failure is the cause; do not leak a stale message from
               an earlier read into the invocation diagnostics. */
            *err = marker;
            rc = fail_before_publication(s, name, confirmed, &attempt, marker.text, err);
            goto done;
        }
        /* Bounded recovery artifact: while it exists the adoption is unresolved
           and its previous record is the confirmed execution for reads. */
        Recovery recovery = {0};
        if (confirmed) {
            recovery.has_previous = true;
            recovery.previous = *confirmed; /* shallow: temporary view only */
        }
        int saved_recovery = bgx_test_fault("recovery_failure")
                                 ? fail(err, "injected recovery evidence write failure")
                                 : store_save_recovery(s, name, &recovery, err);
        if (saved_recovery) {
            Error cause = *err;
            rc = fail_before_publication(s, name, confirmed, &attempt, cause.text, err);
            goto done;
        }
        bgx_test_point("before_publication");
        bool published = false;
        int committed = bgx_test_fault("save_identity")
                            ? fail(err, "injected metadata write failure")
                            : store_commit_attempt(s, name, &attempt, &published, err);
        if (committed) {
            Error cause = *err;
            rc = published ? fail_after_publication(s, name, confirmed, &attempt, cause.text, err)
                           : fail_before_publication(s, name, confirmed, &attempt, cause.text, err);
            goto done;
        }
        /* Publication and its directory syncs succeeded. Removing the recovery
           marker is the adoption success point: only after it does meta.json
           alone describe the confirmed execution. */
        int finalized = bgx_test_fault("remove_recovery_failure")
                            ? fail(err, "injected recovery removal failure")
                            : store_remove_recovery(s, name, err);
        if (finalized) {
            Error cause = *err;
            rc = fail_after_publication(s, name, confirmed, &attempt, cause.text, err);
            goto done;
        }
        bgx_test_point("commit_finalized");
        store_cleanup_committed(s, name, &attempt, confirmed ? confirmed->log : NULL);
        rc = 0;
        goto done;
    }
    if (launch == LAUNCH_UNCERTAIN) {
        Error cause = *err;
        attempt.state = ATTEMPT_UNCERTAIN;
        snprintf(attempt.error, sizeof(attempt.error), "%.450s", cause.text);
        Error stored = {0};
        if (store_save_attempt(s, &attempt, &stored))
            append_error(err, "; could not persist launch evidence: %.120s", stored.text);
        goto done;
    }
    /* Definite failure: the candidate never ran and its cleanup is confirmed. */
    if (!confirmed) {
        rc = cleanup_launch(s, name, err);
        goto done;
    }
    finalize_failed_attempt(s, name, &attempt, err->text, err);
    goto done;
done:
    attempt_free(&attempt);
    return rc;
}

static int start_locked(Store *s, const char *name, Task *task, int argc, char **argv, Error *err) {
    if (task_recover(s, name, task, err)) return -1;
    if (task->has_recovery) return unsafe_status(name, UNKNOWN, err);
    if (task->has_meta)
        return fail(err, "%s already exists; use bgx restart %s or bgx replace %s -- PROGRAM to run it again",
                    name, name, name);
    if (task->has_attempt) return unsafe_status(name, UNKNOWN, err);
    char *cwd = getcwd(NULL, 0);
    if (!cwd) return syserr(err, "get working directory");
    int rc = launch_task(s, name, NULL, argv, argc, cwd, -1, err);
    free(cwd);
    return rc;
}

/* restart and replace share preparation, stop, launch, and commit. */
static int relaunch_locked(Store *s, const char *name, Task *task, bool replace,
                           int program_argc, char **program_argv, Error *err) {
    if (task_recover(s, name, task, err)) return -1;
    if (task->has_recovery) return unsafe_status(name, UNKNOWN, err);
    if (task->has_attempt && task->attempt.state != ATTEMPT_FAILED) return unsafe_status(name, UNKNOWN, err);
    /* Recovery may have removed a first launch that never had a confirmed
       execution; restart/replace must not become an implicit start. */
    if (!task->has_meta)
        return fail(err, "%s has no confirmed execution; use bgx start %s -- PROGRAM", name, name);
    char *cwd = NULL;
    int cwdfd = -1;
    char **argv = program_argv;
    int argc = program_argc;
    if (replace) {
        cwd = getcwd(NULL, 0);
        if (!cwd) return syserr(err, "get working directory");
    } else {
        argv = task->meta.argv;
        argc = (int)task->meta.argc;
        /* Reliable prerequisite: the recorded directory must still be usable
           before a running execution is stopped. */
        cwdfd = open(task->meta.cwd, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
        if (cwdfd < 0) {
            Error detail = {0};
            syserr(&detail, "open recorded directory");
            return fail(err, "cannot start in recorded directory %s: %.380s", task->meta.cwd, detail.text);
        }
        cwd = strdup(task->meta.cwd);
        if (!cwd) { close(cwdfd); return fail(err, "out of memory"); }
    }
    Status state = observed(&task->meta, err);
    if (state == RUNNING) {
        if (process_stop(&task->meta.identity, err)) goto fail;
        task->meta.exited = true;
        if (store_save(s, &task->meta, err)) goto fail;
    } else if (state == EXITED) {
        /* Confirmed completion becomes durable before any candidate may fail,
           preserving identity and the rest of the historical record. */
        if (!task->meta.exited) {
            task->meta.exited = true;
            if (store_save(s, &task->meta, err)) goto fail;
        }
    } else {
        unsafe_status(name, state, err);
        goto fail;
    }
    int rc = launch_task(s, name, &task->meta, argv, argc, cwd, cwdfd, err);
    close(cwdfd);
    free(cwd);
    return rc;
fail:
    if (cwdfd >= 0) close(cwdfd);
    free(cwd);
    return -1;
}

static int stop_locked(Store *s, const char *name, Task *task, Error *err) {
    if (task_recover(s, name, task, err)) return -1;
    if (task->has_recovery) return unsafe_status(name, UNKNOWN, err);
    if (task->has_attempt && task->attempt.state != ATTEMPT_FAILED) return unsafe_status(name, UNKNOWN, err);
    if (!task->has_meta) return fail(err, "instance not found: %s", name);
    Status state = observed(&task->meta, err);
    if (state == RUNNING) {
        if (process_stop(&task->meta.identity, err)) return -1;
    } else if (state != EXITED) return unsafe_status(name, state, err);
    if (task->meta.exited) return 0;
    task->meta.exited = true;
    return store_save(s, &task->meta, err);
}

static int remove_locked(Store *s, const char *name, Task *task, Error *err) {
    if (task_recover(s, name, task, err)) return -1;
    if (!task->has_meta && !task->has_attempt && !task->has_recovery)
        return fail(err, "instance not found: %s", name);
    Status state = task_status(task, err);
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

static int print_info(const char *name, const Task *task, const char *log_path, const char *attempt_log_path, Error *err) {
    Error observation = {0};
    Status state = task_status(task, &observation);
    const Record *confirmed = task_confirmed(task);
    char pid[32] = "-", pgid[32] = "-", uptime[32] = "-", started[64] = "-";
    if (confirmed) {
        struct tm local;
        if (!localtime_r(&confirmed->started, &local) ||
            !strftime(started, sizeof(started), "%Y-%m-%d %H:%M:%S %z", &local))
            return fail(err, "cannot format local start time");
        if (confirmed->has_identity) {
            snprintf(pid, sizeof(pid), "%ld", (long)confirmed->identity.pid);
            snprintf(pgid, sizeof(pgid), "%ld", (long)confirmed->identity.pgid);
        }
        if (state == RUNNING) {
            time_t now = time(NULL);
            long long seconds = now > confirmed->started ? (long long)(now - confirmed->started) : 0;
            snprintf(uptime, sizeof(uptime), "%02lld:%02lld:%02lld",
                     seconds / 3600, (seconds % 3600) / 60, seconds % 60);
        }
    }
    printf("%-14s %s\n", "Name:", confirmed ? confirmed->name : name);
    printf("%-14s %s\n", "Status:", status_name(state));
    if ((state == STALE || state == UNKNOWN) && observation.text[0])
        printf("%-14s %s\n", "Reason:", observation.text);
    printf("%-14s %s\n", "Recorded PID:", pid);
    printf("%-14s %s\n", "Recorded PGID:", pgid);
    printf("%-14s %s\n", "Started:", started);
    printf("%-14s %s\n", "Uptime:", uptime);
    info_value("Directory:", confirmed ? confirmed->cwd : NULL);
    info_argv("Command:", confirmed ? (const char *const *)confirmed->argv : NULL,
              confirmed ? confirmed->argc : 0);
    info_value("Log:", log_path);
    const Attempt *display = task_attempt(task);
    if (display) {
        const Attempt *attempt = display;
        if (attempt->state == ATTEMPT_FAILED) {
            info_argv("Failed command:", (const char *const *)attempt->argv, attempt->argc);
            info_value("Failed directory:", attempt->cwd);
            if (attempt->has_identity) {
                char apid[32], apgid[32];
                snprintf(apid, sizeof(apid), "%ld", (long)attempt->identity.pid);
                snprintf(apgid, sizeof(apgid), "%ld", (long)attempt->identity.pgid);
                printf("%-14s %s\n", "Failed PID:", apid);
                printf("%-14s %s\n", "Failed PGID:", apgid);
            }
        } else {
            info_argv("Pending command:", (const char *const *)attempt->argv, attempt->argc);
            info_value("Pending directory:", attempt->cwd);
            char apid[32] = "-", apgid[32] = "-";
            if (attempt->has_identity) {
                snprintf(apid, sizeof(apid), "%ld", (long)attempt->identity.pid);
                snprintf(apgid, sizeof(apgid), "%ld", (long)attempt->identity.pgid);
            }
            printf("%-14s %s\n", "Pending PID:", apid);
            printf("%-14s %s\n", "Pending PGID:", apgid);
        }
        if (attempt->error[0]) info_value("Attempt error:", attempt->error);
        if (attempt_log_path) info_value("Attempt log:", attempt_log_path);
    }
    return fflush(stdout) ? syserr(err, "write output") : 0;
}

static int info_instance(const char *name, Error *err) {
    Store s;
    int opened = store_open(&s, false, err);
    if (opened < 0) return -1;
    if (opened == 1) return fail(err, "instance not found: %s", name);
    Task task;
    int loaded = task_load(&s, name, &task, err);
    char *log_path = NULL, *attempt_log_path = NULL;
    bool path_error = false;
    if (!loaded) {
        const Attempt *attempt = task_attempt(&task);
        const Record *confirmed = task_confirmed(&task);
        /* While adoption is unresolved the confirmed log is the previous
           execution's log, not meta.json's candidate log. */
        const char *current = confirmed ? confirmed->log
                           : (attempt && attempt->has_log ? attempt->log : NULL);
        if (current && !(log_path = store_log_path(&s, name, current, err))) path_error = true;
        if (!path_error && confirmed && attempt && attempt->has_log &&
            !(attempt_log_path = store_log_path(&s, name, attempt->log, err)))
            path_error = true;
    }
    store_close(&s);
    int rc = -1;
    if (loaded == 1) fail(err, "instance not found: %s", name);
    else if (loaded < 0) {
        Error cause = *err;
        fail(err, "%s: %.360s", name, cause.text);
    } else if (!path_error) rc = print_info(name, &task, log_path, attempt_log_path, err);
    task_free(&task);
    free(log_path);
    free(attempt_log_path);
    return rc;
}

static int mutate_instance(const char *command, const char *name, bool force,
                           int program_argc, char **program_argv, Error *err) {
    Store s;
    if (store_open(&s, true, err)) return -1;
    bgx_test_point("before_name_lock");
    int lock = store_lock(&s, name, err);
    if (lock < 0) { store_close(&s); return -1; }
    int rc = -1;
    if (!strcmp(command, "remove") && force) {
        fputs("bgx: --force deletes the record and log only; no processes will be stopped; old processes may still be running.\n", stderr);
        rc = store_remove(&s, name, err);
    } else {
        Task task;
        int loaded = task_load(&s, name, &task, err);
        if (loaded < 0) rc = -1;
        else if (!strcmp(command, "start")) rc = start_locked(&s, name, &task, program_argc, program_argv, err);
        else if (loaded == 1) fail(err, "instance not found: %s", name);
        else if (!strcmp(command, "stop")) rc = stop_locked(&s, name, &task, err);
        else if (!strcmp(command, "restart")) rc = relaunch_locked(&s, name, &task, false, 0, NULL, err);
        else if (!strcmp(command, "replace")) rc = relaunch_locked(&s, name, &task, true, program_argc, program_argv, err);
        else if (!strcmp(command, "remove")) rc = remove_locked(&s, name, &task, err);
        task_free(&task);
    }
    close(lock);
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
    Task task;
    int loaded = task_load(s, name, &task, &detail);
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
        task_free(&task);
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
        Task task;
        Error detail = {0};
        int loaded = task_load(&s, names[i], &task, &detail);
        if (loaded == 1) continue; /* Concurrent preparation/removal. */
        if (loaded < 0) {
            prune_name_error(names[i], detail.text);
            failed = true;
            continue;
        }
        Status state = task_status(&task, &detail);
        task_free(&task);
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
        Task task;
        Error load_error = {0};
        int loaded = task_load(&s, names[i], &task, &load_error);
        if (loaded == 1) continue; /* Concurrent preparation/removal. */
        if (loaded < 0) {
            printf("%-24s %-9s %-8s %-9s %s\n", names[i], status_name(UNKNOWN), "-", "-", "-");
            fprintf(stderr, "bgx: %s: %s\n", names[i], load_error.text);
            incomplete = true;
            continue;
        }
        Error inspect_error = {0};
        Status state = task_status(&task, &inspect_error);
        const Attempt *attempt = task_attempt(&task);
        const Record *confirmed = task_confirmed(&task);
        const char *const *argv = NULL;
        size_t argc = 0;
        if (attempt && attempt->state != ATTEMPT_FAILED) {
            /* A possible new process must not be labelled with the old command. */
            argv = (const char *const *)attempt->argv;
            argc = attempt->argc;
        } else if (confirmed) {
            argv = (const char *const *)confirmed->argv;
            argc = confirmed->argc;
        } else if (task.has_attempt) {
            argv = (const char *const *)task.attempt.argv;
            argc = task.attempt.argc;
        }
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
        task_free(&task);
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
    case CMD_START: case CMD_RESTART: case CMD_REPLACE: case CMD_STOP: case CMD_REMOVE:
        rc = mutate_instance(commands[args.command].name, args.name, args.force,
                             args.program_argc, args.program_argv, &err);
        break;
    default: return 2; /* Help and invalid commands are handled before execution. */
    }
    if (rc) fprintf(stderr, "bgx: %s\n", err.text[0] ? err.text : "operation failed");
    return rc ? 1 : 0;
}
