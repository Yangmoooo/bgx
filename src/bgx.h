#ifndef BGX_H
#define BGX_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/types.h>
#include <time.h>

#define BGX_SCHEMA 1
#define BGX_MAX_META (8u * 1024u * 1024u)

typedef struct { char text[512]; } Error;
int fail(Error *err, const char *fmt, ...);
int syserr(Error *err, const char *what);
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
    bool starting;
    bool exited; /* Confirmed completion; retained identity is historical. */
    bool has_identity;
    Identity identity;
} Record;
void record_free(Record *record);

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
int store_load(Store *s, const char *name, Record *out, Error *err); /* 1: absent */
int store_save(Store *s, const Record *record, Error *err);
int store_prepare(Store *s, const char *name, Error *err); /* owned log fd or -1 */
int store_remove(Store *s, const char *name, Error *err);
int store_log(Store *s, const char *name, Error *err); /* validates record; owned fd or -1 */
char *store_log_path(Store *s, const char *name, Error *err); /* owned absolute path */
int store_names(Store *s, char ***names, size_t *count, Error *err);

typedef enum { RUNNING, EXITED, STALE, UNKNOWN } Status;
const char *status_name(Status status);
typedef enum { LAUNCH_OK, LAUNCH_FAILED, LAUNCH_UNCERTAIN } LaunchResult;
LaunchResult process_start(char *const argv[], int logfd, Identity *out, Error *err);
int process_abort_launch(const Identity *id, Error *err);
Status process_inspect(const Identity *id, Error *err);
int process_stop(const Identity *id, Error *err);

/* Native process observation; a zombie is not a live member of a scope. */
typedef struct { pid_t pgid; uid_t uid; bool zombie; bool execed; char birth[80]; } ProcInfo;
int platform_process(pid_t pid, ProcInfo *out, Error *err); /* 1: absent */
int platform_boot(char out[80], Error *err);
int platform_group_live(pid_t pgid, bool *live, Error *err);

#endif
