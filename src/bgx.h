#ifndef BGX_H
#define BGX_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/types.h>
#include <time.h>

#define BGX_SCHEMA 2
#define BGX_MAX_META (8u * 1024u * 1024u)
#define BGX_LOG_NAME 64

typedef struct { char text[512]; } Error;
int fail(Error *err, const char *fmt, ...);
int syserr(Error *err, const char *what);
void append_error(Error *err, const char *fmt, ...);
char *join_path(const char *a, const char *b, Error *err);
int write_all(int fd, const void *data, size_t length, Error *err);
int read_all(int fd, size_t limit, char **out, size_t *length, Error *err);
int valid_name(const char *name, Error *err);
int format_time(time_t t, char out[32]);
int parse_time(const char *s, time_t *out);
void print_arg(FILE *out, const char *arg);
int64_t monotonic_ms(void);

typedef struct {
    pid_t pid;
    pid_t pgid;
    char birth[80];
    char boot[80];
} Identity;

typedef struct {
    char *name;
    char **argv;
    size_t argc;
    char *cwd;
    time_t started;
    bool exited; /* Confirmed completion; retained identity is historical. */
    bool has_identity;
    Identity identity;
    char log[BGX_LOG_NAME]; /* log basename inside the instance directory */
} Record;
void record_free(Record *record);

/* Latest launch attempt that has not become the confirmed execution.
   PREPARED: evidence is durable and the release gate was never written.
   UNCERTAIN: the candidate may have been released or its cleanup failed.
   COMMITTING: exec was confirmed; adoption of this attempt may be in progress.
   FAILED: adoption did not complete and no candidate processes remain (the
   candidate may have execed before it was stopped). */
typedef enum { ATTEMPT_PREPARED, ATTEMPT_UNCERTAIN, ATTEMPT_COMMITTING, ATTEMPT_FAILED } AttemptState;
const char *attempt_state_name(AttemptState state);

typedef struct {
    char *name;
    char **argv;
    size_t argc;
    char *cwd;
    time_t started;
    bool has_identity;
    Identity identity;
    bool has_log;
    char log[BGX_LOG_NAME];
    AttemptState state;
    char error[512];
} Attempt;
void attempt_free(Attempt *attempt);

/* Bounded artifact written before a publication may replace meta.json. While it
   exists, the adoption is unresolved: previous, when present, is the confirmed
   execution used by reads. Its removal is the adoption success point. */
typedef struct {
    bool has_previous;
    Record previous;
} Recovery;
void recovery_free(Recovery *recovery);

#ifdef BGX_TESTING
void bgx_test_point(const char *point);
int bgx_test_fault(const char *point);
void bgx_test_child(pid_t pid);
#else
#define bgx_test_point(point) ((void)0)
#define bgx_test_fault(point) 0
#define bgx_test_child(pid) ((void)0)
#endif

typedef struct {
    char *path;
    int root;
    int instances;
    int locks;
} Store;
int store_open(Store *s, bool create, Error *err); /* 1: absent */
int store_open_locks(Store *s, Error *err); /* existing store; creates the lock directory */
void store_close(Store *s);
int store_lock(Store *s, const char *name, Error *err); /* owned fd or -1 */
int store_save(Store *s, const Record *record, Error *err);
int store_save_attempt(Store *s, const Attempt *attempt, Error *err);
int store_load_task(Store *s, const char *name, Record *meta, bool *has_meta, Attempt *attempt,
                    bool *has_attempt, Recovery *recovery, bool *has_recovery,
                    Error *err); /* stable three-record snapshot; 1: no task data */
int store_save_recovery(Store *s, const char *name, const Recovery *recovery, Error *err);
int store_remove_recovery(Store *s, const char *name, Error *err); /* adoption success point */
int store_finalize_failed_attempt(Store *s, const char *name, Attempt *attempt, Error *err);
int store_remove_attempt(Store *s, const char *name, const Attempt *attempt, Error *err);
int store_discard_attempt(Store *s, const char *name, Error *err);
int store_prepare_attempt(Store *s, const char *name, char log[BGX_LOG_NAME], Error *err); /* owned log fd or -1 */
/* Publishes attempt as the confirmed record. *published is set when the
   meta.json rename took effect, even if a later directory sync failed; that is
   publication, not final adoption success. */
int store_commit_attempt(Store *s, const char *name, const Attempt *attempt, bool *published, Error *err);
/* Best-effort retirement after the adoption success point: attempt.json and
   the previous execution's log. */
int store_cleanup_committed(Store *s, const char *name, const Attempt *attempt, const char *retired_log);
int store_cleanup(Store *s, const char *name, Error *err); /* best-effort crash residue removal */
int store_remove(Store *s, const char *name, Error *err);
int store_log(Store *s, const char *name, Error *err); /* owned fd or -1 */
char *store_log_path(Store *s, const char *name, const char *log, Error *err); /* owned absolute path */
int store_names(Store *s, char ***names, size_t *count, Error *err);

typedef enum { RUNNING, EXITED, STALE, UNKNOWN } Status;
const char *status_name(Status status);
typedef enum { LAUNCH_OK, LAUNCH_FAILED, LAUNCH_UNCERTAIN } LaunchResult;
/* Guards candidate release: must persist the identity durably before the gate
   is written; a nonzero return refuses to release the candidate. */
typedef int (*ReleaseGuard)(void *context, const Identity *identity, Error *err);
LaunchResult process_start(char *const argv[], int logfd, int cwdfd, ReleaseGuard guard, void *guard_context,
                           Identity *out, Error *err);
int process_abort_launch(const Identity *id, Error *err);
Status process_inspect(const Identity *id, Error *err);
int process_stop(const Identity *id, Error *err);

/* Native process observation; a zombie is not a live member of a scope. */
typedef struct { pid_t pgid; uid_t uid; bool zombie; bool execed; char birth[80]; } ProcInfo;
int platform_process(pid_t pid, ProcInfo *out, Error *err); /* 1: absent */
int platform_boot(char out[80], Error *err);
int platform_group_live(pid_t pgid, bool *live, Error *err);
/* False when platform_process reports an exited but unreaped child as absent;
   Darwin hides zombies from proc_pidinfo, Linux leaves them in /proc. */
extern const bool platform_process_sees_zombies;

#endif
