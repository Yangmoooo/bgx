#include "bgx.h"
#include "cJSON.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pwd.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

/* Lowercase filename plus an uppercase-position bitmap: Web -> web.1.
   No two names differ only by filename case, even on case-insensitive volumes.
   The longest key is 161 bytes, leaving room for the lock suffix. */
#define NAME_KEY_SIZE (128 + 1 + 32 + 1)
static const char hex_digits[] = "0123456789abcdef";

const char *attempt_state_name(AttemptState state) {
    switch (state) {
    case ATTEMPT_PREPARED: return "prepared";
    case ATTEMPT_UNCERTAIN: return "uncertain";
    case ATTEMPT_COMMITTING: return "committing";
    default: return "failed";
    }
}

void attempt_free(Attempt *attempt) {
    free(attempt->name);
    free(attempt->cwd);
    for (size_t i = 0; i < attempt->argc; ++i) free(attempt->argv[i]);
    free(attempt->argv);
    memset(attempt, 0, sizeof(*attempt));
}

void recovery_free(Recovery *recovery) {
    if (recovery->has_previous) record_free(&recovery->previous);
    memset(recovery, 0, sizeof(*recovery));
}

static int name_key(const char *name, char key[NAME_KEY_SIZE], Error *err) {
    if (valid_name(name, err)) return -1;
    size_t length = strlen(name);
    unsigned char uppercase[32] = {0};
    bool has_uppercase = false;
    for (size_t i = 0; i < length; ++i) {
        char c = name[i];
        if (c >= 'A' && c <= 'Z') {
            c += 'a' - 'A';
            uppercase[i / 4] |= 1u << (i % 4);
            has_uppercase = true;
        }
        key[i] = c;
    }
    size_t used = length;
    if (has_uppercase) {
        key[used++] = '.';
        for (size_t i = 0; i < (length + 3) / 4; ++i)
            key[used++] = hex_digits[uppercase[i]];
    }
    key[used] = 0;
    return 0;
}

static int key_name(const char *key, char name[129], Error *err) {
    size_t length = strcspn(key, ".");
    if (!length || length > 128) return fail(err, "invalid instance directory name");
    memcpy(name, key, length);
    name[length] = 0;
    if (key[length]) {
        if (strlen(key + length + 1) != (length + 3) / 4)
            return fail(err, "invalid instance name encoding");
        for (size_t i = 0; i < length; ++i) {
            const char *digit = strchr(hex_digits, key[length + 1 + i / 4]);
            if (!digit) return fail(err, "invalid instance name encoding");
            if ((unsigned)(digit - hex_digits) & (1u << (i % 4))) {
                if (name[i] < 'a' || name[i] > 'z') return fail(err, "invalid instance name encoding");
                name[i] -= 'a' - 'A';
            }
        }
    }
    char canonical[NAME_KEY_SIZE];
    if (name_key(name, canonical, err)) return -1;
    if (strcmp(canonical, key)) return fail(err, "noncanonical instance directory name");
    return 0;
}

static int mkdir_parents(const char *path, Error *err) {
    char *copy = strdup(path);
    if (!copy) return fail(err, "out of memory");
    for (char *p = copy + 1; ; ++p) {
        if (*p && *p != '/') continue;
        char saved = *p;
        *p = 0;
        if (mkdir(copy, 0700) && errno != EEXIST) {
            free(copy);
            return syserr(err, "create state directory");
        }
        *p = saved;
        if (!saved) break;
    }
    free(copy);
    return 0;
}

static int open_dir(int parent, const char *name, bool create, Error *err) {
    if (create && mkdirat(parent, name, 0700) && errno != EEXIST)
        return syserr(err, "create directory");
    int fd = openat(parent, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) return syserr(err, "open directory");
    struct stat st;
    if (fstat(fd, &st) || st.st_uid != geteuid()) {
        close(fd);
        return fail(err, "managed directory is not owned by current user");
    }
    if (create && fchmod(fd, 0700)) {
        close(fd);
        return syserr(err, "secure directory");
    }
    return fd;
}

static int open_instance(Store *s, const char *name, bool create, Error *err) {
    char key[NAME_KEY_SIZE];
    if (name_key(name, key, err)) return -1;
    return open_dir(s->instances, key, create, err);
}

int store_open(Store *s, bool create, Error *err) {
    *s = (Store){.root = -1, .instances = -1, .locks = -1};
    const char *base = getenv("BGX_STATE_HOME");
    if (base && *base) s->path = strdup(base);
    else {
        base = getenv("XDG_STATE_HOME");
        if (base && *base) s->path = join_path(base, "bgx", err);
        else {
            base = getenv("HOME");
            if (!base || !*base) {
                struct passwd *pw = getpwuid(getuid());
                base = pw ? pw->pw_dir : NULL;
            }
            if (!base || !*base) return fail(err, "cannot determine home directory");
            s->path = join_path(base, ".local/state/bgx", err);
        }
    }
    if (!s->path) return fail(err, "out of memory");
    if (s->path[0] != '/') {
        char *cwd = getcwd(NULL, 0);
        if (!cwd) { store_close(s); return syserr(err, "get working directory"); }
        char *absolute = join_path(cwd, s->path, err);
        free(cwd);
        free(s->path);
        s->path = absolute;
        if (!absolute) { store_close(s); return -1; }
    }
    if (create && mkdir_parents(s->path, err)) { store_close(s); return -1; }
    s->root = open(s->path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (s->root < 0) {
        int absent = errno == ENOENT;
        if (!absent) syserr(err, "open state directory");
        store_close(s);
        return absent && !create ? 1 : -1;
    }
    struct stat st;
    if (fstat(s->root, &st) || st.st_uid != geteuid()) {
        store_close(s);
        return fail(err, "state directory is not owned by current user");
    }
    if (create && fchmod(s->root, 0700)) {
        store_close(s);
        return syserr(err, "secure state directory");
    }
    s->instances = open_dir(s->root, "instances", create, err);
    if (s->instances < 0) {
        int absent = errno == ENOENT;
        store_close(s);
        return absent && !create ? 1 : -1;
    }
    if (create) {
        s->locks = open_dir(s->root, "locks", true, err);
        if (s->locks < 0) { store_close(s); return -1; }
    }
    return 0;
}

/* Mutating commands that opened an existing store without create=true need the
   lock directory before they can coordinate names. */
int store_open_locks(Store *s, Error *err) {
    if (s->locks >= 0) return 0;
    if (s->root < 0) return fail(err, "state directory is not open");
    s->locks = open_dir(s->root, "locks", true, err);
    return s->locks < 0 ? -1 : 0;
}

void store_close(Store *s) {
    if (s->root >= 0) close(s->root);
    if (s->instances >= 0) close(s->instances);
    if (s->locks >= 0) close(s->locks);
    free(s->path);
    *s = (Store){.root = -1, .instances = -1, .locks = -1};
}

int store_lock(Store *s, const char *name, Error *err) {
    char key[NAME_KEY_SIZE], file[NAME_KEY_SIZE + 5];
    if (name_key(name, key, err)) return -1;
    snprintf(file, sizeof(file), "%s.lock", key);
    int fd = openat(s->locks, file, O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd < 0) return syserr(err, "open name lock");
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_uid != geteuid() || st.st_nlink != 1) {
        close(fd);
        return fail(err, "invalid lock file");
    }
    if (fchmod(fd, 0600)) { close(fd); return syserr(err, "secure lock"); }
    int rc;
    do { rc = flock(fd, LOCK_EX); } while (rc < 0 && errno == EINTR);
    if (rc < 0) { close(fd); return syserr(err, "lock name"); }
    return fd;
}

static int regular_file(int dir, const char *name, int flags, Error *err) {
    int fd = openat(dir, name, flags | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC, 0600);
    if (fd < 0) return syserr(err, name);
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_uid != geteuid() || st.st_nlink != 1) {
        close(fd);
        return fail(err, "%s must be a private regular file", name);
    }
    return fd;
}

static bool only_keys(const cJSON *obj, const char *const *keys) {
    if (!cJSON_IsObject(obj)) return false;
    for (const cJSON *item = obj->child; item; item = item->next) {
        bool known = false;
        for (size_t i = 0; keys[i]; ++i)
            if (item->string && !strcmp(keys[i], item->string)) known = true;
        if (!known) return false;
        for (const cJSON *prior = obj->child; prior != item; prior = prior->next)
            if (!strcmp(prior->string, item->string)) return false;
    }
    return true;
}

static const char *string_field(const cJSON *obj, const char *key) {
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(obj, key);
    return cJSON_IsString(v) ? v->valuestring : NULL;
}

static int pid_field(const cJSON *obj, const char *key, pid_t *out) {
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(obj, key);
    if (!cJSON_IsNumber(v) || !(v->valuedouble >= 2 && v->valuedouble <= INT_MAX)) return -1;
    int n = (int)v->valuedouble;
    if ((double)n != v->valuedouble) return -1;
    *out = (pid_t)n;
    return 0;
}

/* A record may only reference logs this store created: a basename with an
   unambiguous numeric serial. This also rejects path traversal. */
static bool valid_log_name(const char *log) {
    if (!log || strncmp(log, "log-", 4)) return false;
    const char *digits = log + 4;
    size_t length = strlen(digits);
    if (!length || length > 18) return false;
    for (size_t i = 0; i < length; ++i)
        if (digits[i] < '0' || digits[i] > '9') return false;
    return true;
}

static bool decode_identity(const cJSON *id, bool *has, Identity *out) {
    const char *const identity_keys[] = {"kind", "pid", "process_group", "start_marker", "boot_id", NULL};
    *has = false;
    if (!id || cJSON_IsNull(id)) return true;
    const char *kind = string_field(id, "kind");
    const char *birth = string_field(id, "start_marker");
    const char *boot = string_field(id, "boot_id");
    if (!only_keys(id, identity_keys) || !kind || strcmp(kind, "unix") ||
        !birth || !*birth || strlen(birth) >= sizeof(out->birth) ||
        !boot || !*boot || strlen(boot) >= sizeof(out->boot) ||
        pid_field(id, "pid", &out->pid) || pid_field(id, "process_group", &out->pgid) ||
        out->pid != out->pgid) return false;
    strcpy(out->birth, birth);
    strcpy(out->boot, boot);
    *has = true;
    return true;
}

/* 0: ok, 1: invalid, 2: out of memory */
static int decode_argv(const cJSON *args, char ***out_argv, size_t *out_argc) {
    if (!cJSON_IsArray(args)) return 1;
    int count = cJSON_GetArraySize(args);
    if (count <= 0) return 1;
    char **argv = calloc((size_t)count + 1, sizeof(*argv));
    if (!argv) return 2;
    size_t argc = 0;
    const cJSON *item = args->child;
    for (int i = 0; i < count; ++i, item = item->next) {
        if (!cJSON_IsString(item) || (!i && !*item->valuestring)) {
            for (size_t j = 0; j < argc; ++j) free(argv[j]);
            free(argv);
            return 1;
        }
        argv[argc] = strdup(item->valuestring);
        if (!argv[argc]) {
            for (size_t j = 0; j < argc; ++j) free(argv[j]);
            free(argv);
            return 2;
        }
        argc++;
    }
    *out_argv = argv;
    *out_argc = argc;
    return 0;
}

/* Reject escaped NUL rather than silently accepting a truncated field/key. */
static int reject_nul(const char *data, size_t length, Error *err) {
    if (memchr(data, 0, length)) return fail(err, "NUL byte in metadata");
    for (size_t i = 0; i < length; ++i) {
        if (data[i] != '\\' || i + 1 == length) continue;
        if (i + 5 < length && !memcmp(data + i + 1, "u0000", 5))
            return fail(err, "NUL escape in metadata");
        ++i;
    }
    return 0;
}

/* Decodes one record object (also used for a recovery artifact's nested
   previous record). 0: ok, -1: invalid or out of memory. */
static int decode_record_fields(const cJSON *json, const char *name, Record *r, Error *err) {
    const char *const keys[] = {"schema_version", "name", "argv", "cwd", "started_at", "exited",
                                "process_identity", "log", NULL};
    int rc = -1;
    const cJSON *schema = cJSON_GetObjectItemCaseSensitive(json, "schema_version");
    const char *stored_name = string_field(json, "name");
    const char *cwd = string_field(json, "cwd");
    const char *started = string_field(json, "started_at");
    const char *log = string_field(json, "log");
    const cJSON *args = cJSON_GetObjectItemCaseSensitive(json, "argv");
    const cJSON *exited = cJSON_GetObjectItemCaseSensitive(json, "exited");
    if (!only_keys(json, keys) || !cJSON_IsNumber(schema) || schema->valuedouble != BGX_SCHEMA ||
        !stored_name || strcmp(name, stored_name) || !cwd || cwd[0] != '/' ||
        !started || parse_time(started, &r->started) || !valid_log_name(log) ||
        (exited && !cJSON_IsBool(exited))) goto invalid;
    r->exited = cJSON_IsTrue(exited);
    if (!decode_identity(cJSON_GetObjectItemCaseSensitive(json, "process_identity"), &r->has_identity, &r->identity))
        goto invalid;
    switch (decode_argv(args, &r->argv, &r->argc)) {
    case 0: break;
    case 1: goto invalid;
    default: goto oom;
    }
    r->name = strdup(name);
    r->cwd = strdup(cwd);
    if (!r->name || !r->cwd) goto oom;
    strcpy(r->log, log);
    rc = 0;
    goto done;
invalid:
    fail(err, "invalid, unsupported or incomplete record for %s", name);
    goto done;
oom:
    fail(err, "out of memory");
done:
    if (rc) record_free(r);
    return rc;
}

static int decode_record(const char *data, size_t length, const char *name, Record *r, Error *err) {
    if (reject_nul(data, length, err)) return -1;
    const char *end = NULL;
    cJSON *json = cJSON_ParseWithLengthOpts(data, length + 1, &end, 1);
    if (!json) return fail(err, "invalid JSON metadata");
    int rc = decode_record_fields(json, name, r, err);
    cJSON_Delete(json);
    return rc;
}

static int decode_recovery(const char *data, size_t length, const char *name, Recovery *out, Error *err) {
    memset(out, 0, sizeof(*out));
    if (reject_nul(data, length, err)) return -1;
    const char *end = NULL;
    cJSON *json = cJSON_ParseWithLengthOpts(data, length + 1, &end, 1);
    if (!json) return fail(err, "invalid JSON metadata");
    const char *const keys[] = {"schema_version", "name", "previous", NULL};
    const cJSON *schema = cJSON_GetObjectItemCaseSensitive(json, "schema_version");
    const char *stored_name = string_field(json, "name");
    const cJSON *previous = cJSON_GetObjectItemCaseSensitive(json, "previous");
    int rc = -1;
    if (!only_keys(json, keys) || !cJSON_IsNumber(schema) || schema->valuedouble != BGX_SCHEMA ||
        !stored_name || strcmp(name, stored_name)) goto invalid;
    if (previous && !cJSON_IsNull(previous)) {
        if (decode_record_fields(previous, name, &out->previous, err)) goto invalid;
        out->has_previous = true;
    }
    rc = 0;
    goto done;
invalid:
    fail(err, "invalid, unsupported or incomplete recovery record for %s", name);
    goto done;
done:
    cJSON_Delete(json);
    if (rc) recovery_free(out);
    return rc;
}

static int decode_attempt(const char *data, size_t length, const char *name, Attempt *a, Error *err) {
    if (reject_nul(data, length, err)) return -1;
    const char *end = NULL;
    cJSON *json = cJSON_ParseWithLengthOpts(data, length + 1, &end, 1);
    if (!json) return fail(err, "invalid JSON metadata");
    const char *const keys[] = {"schema_version", "name", "state", "argv", "cwd", "started_at",
                                "process_identity", "log", "error", NULL};
    int rc = -1;
    const cJSON *schema = cJSON_GetObjectItemCaseSensitive(json, "schema_version");
    const char *stored_name = string_field(json, "name");
    const char *state = string_field(json, "state");
    const char *cwd = string_field(json, "cwd");
    const char *started = string_field(json, "started_at");
    const char *log = string_field(json, "log");
    const char *error = string_field(json, "error");
    const cJSON *args = cJSON_GetObjectItemCaseSensitive(json, "argv");
    AttemptState parsed = ATTEMPT_FAILED;
    bool known_state = false;
    for (int value = 0; value <= (int)ATTEMPT_FAILED; ++value) {
        if (state && !strcmp(state, attempt_state_name((AttemptState)value))) {
            parsed = (AttemptState)value;
            known_state = true;
        }
    }
    if (!only_keys(json, keys) || !cJSON_IsNumber(schema) || schema->valuedouble != BGX_SCHEMA ||
        !stored_name || strcmp(name, stored_name) || !cwd || cwd[0] != '/' ||
        !started || parse_time(started, &a->started) || !known_state ||
        (log && !valid_log_name(log)) || (error && strlen(error) >= sizeof(a->error))) goto invalid;
    a->state = parsed;
    if (log) {
        a->has_log = true;
        strcpy(a->log, log);
    }
    if (error) snprintf(a->error, sizeof(a->error), "%s", error);
    if (!decode_identity(cJSON_GetObjectItemCaseSensitive(json, "process_identity"), &a->has_identity, &a->identity))
        goto invalid;
    switch (decode_argv(args, &a->argv, &a->argc)) {
    case 0: break;
    case 1: goto invalid;
    default: goto oom;
    }
    a->name = strdup(name);
    a->cwd = strdup(cwd);
    if (!a->name || !a->cwd) goto oom;
    rc = 0;
    goto done;
invalid:
    fail(err, "invalid, unsupported or incomplete launch attempt for %s", name);
    goto done;
oom:
    fail(err, "out of memory");
done:
    cJSON_Delete(json);
    if (rc) attempt_free(a);
    return rc;
}

static cJSON *encode_argv(const char *const *argv, size_t argc) {
    cJSON *args = cJSON_CreateArray();
    if (!args) return NULL;
    for (size_t i = 0; i < argc; ++i) {
        cJSON *item = cJSON_CreateString(argv[i]);
        if (!item) { cJSON_Delete(args); return NULL; }
        if (!cJSON_AddItemToArray(args, item)) { cJSON_Delete(item); cJSON_Delete(args); return NULL; }
    }
    return args;
}

static bool add_identity(cJSON *obj, bool has_identity, const Identity *identity) {
    if (!has_identity) return true;
    cJSON *id = cJSON_AddObjectToObject(obj, "process_identity");
    return id && cJSON_AddStringToObject(id, "kind", "unix") &&
           cJSON_AddNumberToObject(id, "pid", identity->pid) &&
           cJSON_AddNumberToObject(id, "process_group", identity->pgid) &&
           cJSON_AddStringToObject(id, "start_marker", identity->birth) &&
           cJSON_AddStringToObject(id, "boot_id", identity->boot);
}

static cJSON *encode_record(const Record *r) {
    char started[32];
    if (format_time(r->started, started)) return NULL;
    cJSON *obj = cJSON_CreateObject();
    if (!obj) return NULL;
    if (!cJSON_AddNumberToObject(obj, "schema_version", BGX_SCHEMA) ||
        !cJSON_AddStringToObject(obj, "name", r->name) ||
        !cJSON_AddStringToObject(obj, "cwd", r->cwd) ||
        !cJSON_AddStringToObject(obj, "started_at", started) ||
        !cJSON_AddBoolToObject(obj, "exited", r->exited) ||
        !cJSON_AddStringToObject(obj, "log", r->log)) goto error;
    cJSON *args = encode_argv((const char *const *)r->argv, r->argc);
    if (!args || !cJSON_AddItemToObject(obj, "argv", args)) { cJSON_Delete(args); goto error; }
    if (!add_identity(obj, r->has_identity, &r->identity)) goto error;
    return obj;
error:
    cJSON_Delete(obj);
    return NULL;
}

static cJSON *encode_recovery(const char *name, const Recovery *recovery) {
    cJSON *obj = cJSON_CreateObject();
    if (!obj) return NULL;
    if (!cJSON_AddNumberToObject(obj, "schema_version", BGX_SCHEMA) ||
        !cJSON_AddStringToObject(obj, "name", name)) goto error;
    if (recovery->has_previous) {
        cJSON *previous = encode_record(&recovery->previous);
        if (!previous || !cJSON_AddItemToObject(obj, "previous", previous)) {
            cJSON_Delete(previous);
            goto error;
        }
    }
    return obj;
error:
    cJSON_Delete(obj);
    return NULL;
}

static cJSON *encode_attempt(const Attempt *a) {
    char started[32];
    if (format_time(a->started, started)) return NULL;
    cJSON *obj = cJSON_CreateObject();
    if (!obj) return NULL;
    if (!cJSON_AddNumberToObject(obj, "schema_version", BGX_SCHEMA) ||
        !cJSON_AddStringToObject(obj, "name", a->name) ||
        !cJSON_AddStringToObject(obj, "state", attempt_state_name(a->state)) ||
        !cJSON_AddStringToObject(obj, "cwd", a->cwd) ||
        !cJSON_AddStringToObject(obj, "started_at", started)) goto error;
    cJSON *args = encode_argv((const char *const *)a->argv, a->argc);
    if (!args || !cJSON_AddItemToObject(obj, "argv", args)) { cJSON_Delete(args); goto error; }
    if (a->has_log && !cJSON_AddStringToObject(obj, "log", a->log)) goto error;
    if (a->error[0] && !cJSON_AddStringToObject(obj, "error", a->error)) goto error;
    if (!add_identity(obj, a->has_identity, &a->identity)) goto error;
    return obj;
error:
    cJSON_Delete(obj);
    return NULL;
}

static int load_record(int dir, const char *name, Record *out, Error *err) {
    memset(out, 0, sizeof(*out));
    int fd = regular_file(dir, "meta.json", O_RDONLY, err);
    if (fd < 0) return errno == ENOENT ? 1 : -1;
    char *data = NULL;
    size_t length = 0;
    int rc = read_all(fd, BGX_MAX_META, &data, &length, err);
    close(fd);
    if (!rc) rc = decode_record(data, length, name, out, err);
    free(data);
    return rc;
}

static int load_attempt(int dir, const char *name, Attempt *out, Error *err) {
    memset(out, 0, sizeof(*out));
    int fd = regular_file(dir, "attempt.json", O_RDONLY, err);
    if (fd < 0) return errno == ENOENT ? 1 : -1;
    char *data = NULL;
    size_t length = 0;
    int rc = read_all(fd, BGX_MAX_META, &data, &length, err);
    close(fd);
    if (!rc) rc = decode_attempt(data, length, name, out, err);
    free(data);
    return rc;
}

static int load_recovery(int dir, const char *name, Recovery *out, Error *err) {
    memset(out, 0, sizeof(*out));
    int fd = regular_file(dir, "recovery.json", O_RDONLY, err);
    if (fd < 0) return errno == ENOENT ? 1 : -1;
    char *data = NULL;
    size_t length = 0;
    int rc = read_all(fd, BGX_MAX_META, &data, &length, err);
    close(fd);
    if (!rc) rc = decode_recovery(data, length, name, out, err);
    free(data);
    return rc;
}

/* Write one record atomically: temp file, fsync, rename, fsync directories.
   A crash can only leave an unreferenced temp file behind. Publication is the
   successful rename: *published is set there and never cleared, so a caller can
   tell an adoption that happened from one that failed before the rename. The
   commit fault injections fire only after the rename. */
static int write_record_file(Store *s, const char *name, const char *target, const char *prefix,
                             cJSON *obj, const char *renamed_point, bool inject_commit_faults,
                             bool *published, Error *err) {
    if (published) *published = false;
    int dir = open_instance(s, name, true, err);
    if (dir < 0) return -1;
    char *json = cJSON_Print(obj);
    if (!json) { close(dir); return fail(err, "encode record: out of memory"); }
    size_t length = strlen(json);
    if (length > BGX_MAX_META) {
        free(json);
        close(dir);
        return fail(err, "record exceeds size limit");
    }
    char temp[80];
    int fd = -1;
    for (unsigned i = 0; i < 100; ++i) {
        snprintf(temp, sizeof(temp), "%s-%ld-%u.tmp", prefix, (long)getpid(), i);
        fd = openat(dir, temp, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
        if (fd >= 0 || errno != EEXIST) break;
    }
    if (fd < 0) { free(json); close(dir); return syserr(err, "create temporary record"); }
    int rc = fchmod(fd, 0600) ? syserr(err, "secure record") : 0;
    if (!rc) rc = write_all(fd, json, length, err);
    if (!rc) rc = write_all(fd, "\n", 1, err);
    if (!rc && fsync(fd)) rc = syserr(err, "sync record");
    if (close(fd) && !rc) rc = syserr(err, "close record");
    if (!rc) {
        if (renameat(dir, temp, dir, target)) rc = syserr(err, "replace record");
        else if (published) *published = true;
    }
    if (!rc && renamed_point) bgx_test_point(renamed_point);
    if (!rc && inject_commit_faults && bgx_test_fault("commit_sync_dir"))
        rc = fail(err, "injected instance directory sync failure");
    if (!rc && fsync(dir)) rc = syserr(err, "sync instance directory");
    if (!rc && inject_commit_faults && bgx_test_fault("commit_sync_registry"))
        rc = fail(err, "injected registry sync failure");
    if (!rc && fsync(s->instances)) rc = syserr(err, "sync registry directory");
    unlinkat(dir, temp, 0);
    close(dir);
    free(json);
    return rc;
}

int store_save(Store *s, const Record *record, Error *err) {
    cJSON *obj = encode_record(record);
    if (!obj) return fail(err, "encode record: out of memory");
    int rc = write_record_file(s, record->name, "meta.json", ".meta", obj, NULL, false, NULL, err);
    cJSON_Delete(obj);
    return rc;
}

int store_save_attempt(Store *s, const Attempt *attempt, Error *err) {
    cJSON *obj = encode_attempt(attempt);
    if (!obj) return fail(err, "encode launch attempt: out of memory");
    int rc = write_record_file(s, attempt->name, "attempt.json", ".attempt", obj, NULL, false, NULL, err);
    cJSON_Delete(obj);
    return rc;
}

int store_save_recovery(Store *s, const char *name, const Recovery *recovery, Error *err) {
    cJSON *obj = encode_recovery(name, recovery);
    if (!obj) return fail(err, "encode recovery record: out of memory");
    int rc = write_record_file(s, name, "recovery.json", ".recovery", obj, NULL, false, NULL, err);
    cJSON_Delete(obj);
    return rc;
}

/* One coherent view of a task's two records without taking the name lock.
   Each file is sampled twice through the same pinned directory; a change
   between the samples restarts the read, and a persistently moving pair is
   reported instead of returning a combination that never existed. */
typedef struct {
    char *data;
    size_t length;
    bool present;
} Snapshot;

static void snapshot_free(Snapshot *snapshot) {
    free(snapshot->data);
    memset(snapshot, 0, sizeof(*snapshot));
}

static int snapshot_read(int dir, const char *file, Snapshot *out, Error *err) {
    memset(out, 0, sizeof(*out));
    int fd = regular_file(dir, file, O_RDONLY, err);
    if (fd < 0) return errno == ENOENT ? 0 : -1;
    int rc = read_all(fd, BGX_MAX_META, &out->data, &out->length, err);
    close(fd);
    if (rc) return -1;
    out->present = true;
    return 0;
}

static bool snapshot_same(const Snapshot *a, const Snapshot *b) {
    if (a->present != b->present) return false;
    if (!a->present) return true;
    return a->length == b->length && !memcmp(a->data, b->data, a->length);
}

static int load_task_snapshot(int dir, const char *name, Record *meta, bool *has_meta,
                              Attempt *attempt, bool *has_attempt,
                              Recovery *recovery, bool *has_recovery, Error *err) {
    memset(meta, 0, sizeof(*meta));
    memset(attempt, 0, sizeof(*attempt));
    memset(recovery, 0, sizeof(*recovery));
    *has_meta = false;
    *has_attempt = false;
    *has_recovery = false;
    for (int pass = 0; pass < 8; ++pass) {
        Snapshot first_meta = {0}, first_attempt = {0}, first_recovery = {0};
        Snapshot second_meta = {0}, second_attempt = {0}, second_recovery = {0};
        int rc = snapshot_read(dir, "meta.json", &first_meta, err);
        if (!rc) rc = snapshot_read(dir, "attempt.json", &first_attempt, err);
        if (!rc) rc = snapshot_read(dir, "recovery.json", &first_recovery, err);
        if (!rc && pass == 0) bgx_test_point("task_snapshot");
        if (!rc) rc = snapshot_read(dir, "meta.json", &second_meta, err);
        if (!rc) rc = snapshot_read(dir, "attempt.json", &second_attempt, err);
        if (!rc) rc = snapshot_read(dir, "recovery.json", &second_recovery, err);
        if (!rc && snapshot_same(&first_meta, &second_meta) &&
            snapshot_same(&first_attempt, &second_attempt) &&
            snapshot_same(&first_recovery, &second_recovery)) {
            if (second_meta.present) rc = decode_record(second_meta.data, second_meta.length, name, meta, err);
            if (!rc && second_attempt.present)
                rc = decode_attempt(second_attempt.data, second_attempt.length, name, attempt, err);
            if (!rc && second_recovery.present)
                rc = decode_recovery(second_recovery.data, second_recovery.length, name, recovery, err);
            *has_meta = !rc && second_meta.present;
            *has_attempt = !rc && second_attempt.present;
            *has_recovery = !rc && second_recovery.present;
            snapshot_free(&first_meta); snapshot_free(&first_attempt); snapshot_free(&first_recovery);
            snapshot_free(&second_meta); snapshot_free(&second_attempt); snapshot_free(&second_recovery);
            return rc;
        }
        snapshot_free(&first_meta); snapshot_free(&first_attempt); snapshot_free(&first_recovery);
        snapshot_free(&second_meta); snapshot_free(&second_attempt); snapshot_free(&second_recovery);
        if (rc) return -1;
    }
    return fail(err, "task records changed while reading %s", name);
}

int store_load_task(Store *s, const char *name, Record *meta, bool *has_meta, Attempt *attempt,
                    bool *has_attempt, Recovery *recovery, bool *has_recovery, Error *err) {
    memset(meta, 0, sizeof(*meta));
    memset(attempt, 0, sizeof(*attempt));
    memset(recovery, 0, sizeof(*recovery));
    *has_meta = false;
    *has_attempt = false;
    *has_recovery = false;
    int dir = open_instance(s, name, false, err);
    if (dir < 0) return errno == ENOENT ? 1 : -1;
    int rc = load_task_snapshot(dir, name, meta, has_meta, attempt, has_attempt,
                                recovery, has_recovery, err);
    close(dir);
    if (!rc && !*has_meta && !*has_attempt && !*has_recovery) return 1;
    return rc;
}

static int next_log_serial(Store *s, const char *name, char log[BGX_LOG_NAME], Error *err) {
    int dir = open_instance(s, name, true, err);
    if (dir < 0) return -1;
    unsigned long long highest = 0;
    DIR *entries = fdopendir(dup(dir));
    if (entries) {
        struct dirent *ent;
        while ((ent = readdir(entries))) {
            unsigned long long value;
            if (!valid_log_name(ent->d_name)) continue;
            if (sscanf(ent->d_name + 4, "%llu", &value) == 1 && value > highest) highest = value;
        }
        closedir(entries);
    }
    if (highest >= 999999999999999999ULL) { close(dir); return fail(err, "log serials exhausted"); }
    for (unsigned long long value = highest + 1, tries = 0; tries < 1000; ++value, ++tries) {
        snprintf(log, BGX_LOG_NAME, "log-%llu", value);
        int fd = openat(dir, log, O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC | O_NOFOLLOW, 0600);
        if (fd >= 0) {
            if (fchmod(fd, 0600)) { close(fd); close(dir); return syserr(err, "secure log"); }
            close(dir);
            return fd;
        }
        if (errno != EEXIST) { close(dir); return syserr(err, "create log"); }
    }
    close(dir);
    return fail(err, "cannot allocate a log file");
}

int store_prepare_attempt(Store *s, const char *name, char log[BGX_LOG_NAME], Error *err) {
    return next_log_serial(s, name, log, err);
}

/* Failed attempts keep their log only when it holds output; the log field then
   names an existing file and is omitted otherwise. The updated record is saved
   before the empty file is unlinked so no durable record ever names a missing
   log and a later execution cannot reuse a still-referenced serial. */
int store_finalize_failed_attempt(Store *s, const char *name, Attempt *attempt, Error *err) {
    bool discard = false;
    char discarded[BGX_LOG_NAME] = {0};
    if (attempt->has_log) {
        int dir = open_instance(s, name, false, err);
        if (dir < 0) return -1;
        struct stat st;
        int inspected = fstatat(dir, attempt->log, &st, AT_SYMLINK_NOFOLLOW);
        if (inspected && errno == ENOENT) {
            attempt->has_log = false;
        } else if (!inspected && S_ISREG(st.st_mode) && st.st_size == 0) {
            discard = true;
            snprintf(discarded, sizeof(discarded), "%s", attempt->log);
            attempt->has_log = false;
        }
        close(dir);
    }
    if (store_save_attempt(s, attempt, err)) return -1;
    if (discard) {
        Error ignored = {0};
        int dir = open_instance(s, name, false, &ignored);
        if (dir >= 0) {
            unlinkat(dir, discarded, 0);
            fsync(dir);
            close(dir);
        }
    }
    return 0;
}

int store_remove_attempt(Store *s, const char *name, const Attempt *attempt, Error *err) {
    int dir = open_instance(s, name, false, err);
    if (dir < 0) return errno == ENOENT ? 0 : -1;
    int rc = 0;
    if (unlinkat(dir, "attempt.json", 0) && errno != ENOENT) rc = syserr(err, "remove launch attempt");
    if (!rc && attempt->has_log && unlinkat(dir, attempt->log, 0) && errno != ENOENT)
        rc = syserr(err, "remove attempt log");
    if (!rc && fsync(dir)) rc = syserr(err, "sync instance directory");
    close(dir);
    return rc;
}

/* Used for an attempt record left behind by an interrupted commit: the log it
   names is the confirmed execution's log, so only the record is removed. */
int store_discard_attempt(Store *s, const char *name, Error *err) {
    int dir = open_instance(s, name, false, err);
    if (dir < 0) return errno == ENOENT ? 0 : -1;
    int rc = 0;
    if (unlinkat(dir, "attempt.json", 0) && errno != ENOENT) rc = syserr(err, "remove launch attempt");
    if (!rc && fsync(dir)) rc = syserr(err, "sync instance directory");
    close(dir);
    return rc;
}

int store_remove_recovery(Store *s, const char *name, Error *err) {
    int dir = open_instance(s, name, false, err);
    if (dir < 0) return errno == ENOENT ? 0 : -1;
    int rc = 0;
    if (unlinkat(dir, "recovery.json", 0) && errno != ENOENT) rc = syserr(err, "remove recovery record");
    /* The unlink itself is the adoption success point; syncing is best effort. */
    if (!rc) fsync(dir);
    close(dir);
    return rc;
}

int store_commit_attempt(Store *s, const char *name, const Attempt *attempt, bool *published, Error *err) {
    if (published) *published = false;
    if (!attempt->has_identity || !attempt->has_log || attempt->argc == 0 ||
        !attempt->name || !attempt->cwd)
        return fail(err, "invalid launch attempt");
    Record record = {0};
    record.name = strdup(attempt->name);
    record.cwd = strdup(attempt->cwd);
    record.argv = calloc(attempt->argc + 1, sizeof(*record.argv));
    record.started = attempt->started;
    record.exited = false;
    record.has_identity = attempt->has_identity;
    record.identity = attempt->identity;
    strcpy(record.log, attempt->log);
    bool ok = record.name && record.cwd && record.argv;
    for (size_t i = 0; ok && i < attempt->argc; ++i) {
        record.argv[i] = strdup(attempt->argv[i]);
        ok = record.argv[i] != NULL;
        record.argc = i + 1;
    }
    if (!ok) { record_free(&record); return fail(err, "out of memory"); }
    cJSON *obj = encode_record(&record);
    record_free(&record);
    if (!obj) return fail(err, "encode record: out of memory");
    int rc = write_record_file(s, name, "meta.json", ".meta", obj, "meta_renamed", true, published, err);
    cJSON_Delete(obj);
    return rc;
}

/* Best-effort retirement after the adoption success point: the attempt record
   and the previous execution's log. A crash here leaves recognizable residue. */
int store_cleanup_committed(Store *s, const char *name, const Attempt *attempt, const char *retired_log) {
    Error ignored = {0};
    int dir = open_instance(s, name, false, &ignored);
    if (dir < 0) return 0;
    unlinkat(dir, "attempt.json", 0);
    if (retired_log && *retired_log && strcmp(retired_log, attempt->log))
        unlinkat(dir, retired_log, 0);
    fsync(dir);
    close(dir);
    return 0;
}

static bool residue_name(const char *entry) {
    if (valid_log_name(entry)) return true;
    static const char *const prefixes[] = {".meta-", ".attempt-", ".recovery-", NULL};
    for (size_t i = 0; prefixes[i]; ++i) {
        size_t length = strlen(prefixes[i]);
        if (!strncmp(entry, prefixes[i], length) && strlen(entry) > length + 4 &&
            !strcmp(entry + strlen(entry) - 4, ".tmp"))
            return true;
    }
    return false;
}

/* Remove files no retained record references: stale execution logs, superseded
   attempt logs, and interrupted temp writes. Unreadable records make cleanup
   skip the directory rather than risk deleting referenced data. */
int store_cleanup(Store *s, const char *name, Error *err) {
    int dir = open_instance(s, name, false, err);
    if (dir < 0) return errno == ENOENT ? 0 : -1;
    Record record = {0};
    Attempt attempt = {0};
    Recovery recovery = {0};
    Error detail = {0};
    int meta = load_record(dir, name, &record, &detail);
    int candidate = meta < 0 ? -1 : load_attempt(dir, name, &attempt, &detail);
    int pending = candidate < 0 ? -1 : load_recovery(dir, name, &recovery, &detail);
    if (meta < 0 || candidate < 0 || pending < 0) {
        record_free(&record);
        attempt_free(&attempt);
        recovery_free(&recovery);
        close(dir);
        return 0;
    }
    DIR *entries = fdopendir(dup(dir));
    if (!entries) {
        record_free(&record);
        attempt_free(&attempt);
        recovery_free(&recovery);
        close(dir);
        return syserr(err, "read instance directory");
    }
    for (;;) {
        errno = 0;
        struct dirent *ent = readdir(entries);
        if (!ent) break;
        if (!strcmp(ent->d_name, ".") || !strcmp(ent->d_name, "..")) continue;
        if (!strcmp(ent->d_name, "meta.json") || !strcmp(ent->d_name, "attempt.json") ||
            !strcmp(ent->d_name, "recovery.json")) continue;
        if (meta == 0 && !strcmp(ent->d_name, record.log)) continue;
        if (candidate == 0 && attempt.has_log && !strcmp(ent->d_name, attempt.log)) continue;
        if (pending == 0 && recovery.has_previous && !strcmp(ent->d_name, recovery.previous.log))
            continue;
        if (!residue_name(ent->d_name)) continue;
        struct stat st;
        if (fstatat(dir, ent->d_name, &st, AT_SYMLINK_NOFOLLOW)) continue;
        if (!S_ISREG(st.st_mode) || st.st_uid != geteuid()) continue;
        unlinkat(dir, ent->d_name, 0);
    }
    closedir(entries);
    fsync(dir);
    record_free(&record);
    attempt_free(&attempt);
    recovery_free(&recovery);
    close(dir);
    return 0;
}

/* dirfd traversal never follows links, even for force cleanup of damaged data. */
static int remove_entry(int parent, const char *name, unsigned depth, Error *err) {
    struct stat st;
    if (fstatat(parent, name, &st, AT_SYMLINK_NOFOLLOW)) {
        if (errno == ENOENT) return 0;
        return syserr(err, "inspect cleanup entry");
    }
    if (!S_ISDIR(st.st_mode)) {
        if (unlinkat(parent, name, 0)) return syserr(err, "remove file");
        return 0;
    }
    if (depth > 64) return fail(err, "cleanup directory nesting too deep");
    int fd = openat(parent, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) return syserr(err, "open cleanup directory");
    DIR *dir = fdopendir(fd);
    if (!dir) { close(fd); return syserr(err, "read cleanup directory"); }
    int rc = 0;
    for (;;) {
        errno = 0;
        struct dirent *ent = readdir(dir);
        if (!ent) { if (errno) rc = syserr(err, "list cleanup directory"); break; }
        if (!strcmp(ent->d_name, ".") || !strcmp(ent->d_name, "..")) continue;
        if (remove_entry(fd, ent->d_name, depth + 1, err)) { rc = -1; break; }
    }
    closedir(dir);
    if (!rc && unlinkat(parent, name, AT_REMOVEDIR)) rc = syserr(err, "remove instance directory");
    return rc;
}

int store_remove(Store *s, const char *name, Error *err) {
    char key[NAME_KEY_SIZE];
    if (name_key(name, key, err)) return -1;
    if (remove_entry(s->instances, key, 0, err)) return -1;
    if (fsync(s->instances)) return syserr(err, "sync registry directory");
    return 0;
}

int store_log(Store *s, const char *name, Error *err) {
    int dir = open_instance(s, name, false, err);
    if (dir < 0) return errno == ENOENT ? fail(err, "instance not found: %s", name) : -1;
    /* One stable snapshot, then bind the log by the serial that snapshot
       names; a recovery artifact's previous record is the confirmed execution
       while an adoption is unresolved. Retired serials are never reused. */
    Record record = {0};
    Attempt attempt = {0};
    Recovery recovery = {0};
    bool has_meta = false, has_attempt = false, has_recovery = false;
    int loaded = load_task_snapshot(dir, name, &record, &has_meta, &attempt, &has_attempt,
                                    &recovery, &has_recovery, err);
    int fd = -1;
    if (!loaded) {
        bgx_test_point("log_record_loaded");
        const char *log = NULL;
        if (has_recovery && recovery.has_previous) log = recovery.previous.log;
        else if (has_meta) log = record.log;
        else if (has_attempt && attempt.has_log) log = attempt.log;
        if (log) fd = regular_file(dir, log, O_RDONLY, err);
        else if (has_attempt || has_recovery) fail(err, "no log retained for %s", name);
        else fail(err, "instance not found: %s", name);
    }
    record_free(&record);
    attempt_free(&attempt);
    recovery_free(&recovery);
    close(dir);
    return fd;
}

char *store_log_path(Store *s, const char *name, const char *log, Error *err) {
    char key[NAME_KEY_SIZE], relative[NAME_KEY_SIZE + BGX_LOG_NAME + sizeof("instances//")];
    if (name_key(name, key, err)) return NULL;
    if (!valid_log_name(log)) { fail(err, "invalid log name"); return NULL; }
    snprintf(relative, sizeof(relative), "instances/%s/%s", key, log);
    return join_path(s->path, relative, err);
}

static int compare_names(const void *a, const void *b) {
    return strcmp(*(char *const *)a, *(char *const *)b);
}

int store_names(Store *s, char ***names, size_t *count, Error *err) {
    *names = NULL; *count = 0;
    int fd = dup(s->instances);
    if (fd < 0) return syserr(err, "duplicate registry directory");
    DIR *dir = fdopendir(fd);
    if (!dir) { close(fd); return syserr(err, "read registry"); }
    int rc = 0;
    for (;;) {
        errno = 0;
        struct dirent *ent = readdir(dir);
        if (!ent) { if (errno) rc = syserr(err, "list registry"); break; }
        if (ent->d_name[0] == '.') continue;
        struct stat st;
        if (fstatat(fd, ent->d_name, &st, AT_SYMLINK_NOFOLLOW)) {
            if (errno == ENOENT) continue;
            rc = syserr(err, "inspect registry entry"); break;
        }
        if (!S_ISDIR(st.st_mode)) continue;
        char decoded[129];
        if (key_name(ent->d_name, decoded, err)) { rc = -1; break; }
        char *name = strdup(decoded);
        char **grown = realloc(*names, (*count + 1) * sizeof(**names));
        if (!name || !grown) {
            free(name);
            if (grown) *names = grown;
            rc = fail(err, "out of memory"); break;
        }
        *names = grown;
        (*names)[(*count)++] = name;
    }
    closedir(dir);
    if (!rc && *count) qsort(*names, *count, sizeof(**names), compare_names);
    return rc;
}
