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

const char *launch_state_name(LaunchState state) {
    switch (state) {
    case PENDING: return "pending";
    case STARTED: return "started";
    case FAILED: return "failed";
    }
    return "invalid";
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

/* Decode the only durable record for this run. */
static int decode_record_fields(const cJSON *json, const char *name, Record *r, Error *err) {
    const char *const keys[] = {"schema_version", "argv", "cwd", "started_at", "exited",
                                "process_identity", "launch", "error", NULL};
    int rc = -1;
    const cJSON *schema = cJSON_GetObjectItemCaseSensitive(json, "schema_version");
    const char *cwd = string_field(json, "cwd");
    const char *started = string_field(json, "started_at");
    const char *launch = string_field(json, "launch");
    const char *error = string_field(json, "error");
    const cJSON *args = cJSON_GetObjectItemCaseSensitive(json, "argv");
    const cJSON *exited = cJSON_GetObjectItemCaseSensitive(json, "exited");
    if (!only_keys(json, keys) || !cJSON_IsNumber(schema) || schema->valuedouble != BGX_SCHEMA ||
        !cwd || cwd[0] != '/' ||
        !started || parse_time(started, &r->started) || !launch ||
        !cJSON_IsBool(exited) || !error || strlen(error) >= sizeof(r->error)) goto invalid;
    if (!strcmp(launch, "pending")) r->launch = PENDING;
    else if (!strcmp(launch, "started")) r->launch = STARTED;
    else if (!strcmp(launch, "failed")) r->launch = FAILED;
    else goto invalid;
    r->exited = cJSON_IsTrue(exited);
    if (!decode_identity(cJSON_GetObjectItemCaseSensitive(json, "process_identity"), &r->has_identity, &r->identity))
        goto invalid;
    if ((r->launch == STARTED && !r->has_identity) ||
        (r->launch == PENDING && r->exited) ||
        (r->launch == FAILED && (!r->exited || !*error))) goto invalid;
    switch (decode_argv(args, &r->argv, &r->argc)) {
    case 0: break;
    case 1: goto invalid;
    default: goto oom;
    }
    r->name = strdup(name);
    r->cwd = strdup(cwd);
    if (!r->name || !r->cwd) goto oom;
    strcpy(r->error, error);
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
        !cJSON_AddStringToObject(obj, "cwd", r->cwd) ||
        !cJSON_AddStringToObject(obj, "started_at", started) ||
        !cJSON_AddBoolToObject(obj, "exited", r->exited) ||
        !cJSON_AddStringToObject(obj, "launch", launch_state_name(r->launch)) ||
        !cJSON_AddStringToObject(obj, "error", r->error)) goto error;
    cJSON *args = encode_argv((const char *const *)r->argv, r->argc);
    if (!args || !cJSON_AddItemToObject(obj, "argv", args)) { cJSON_Delete(args); goto error; }
    if (!add_identity(obj, r->has_identity, &r->identity)) goto error;
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

/* The atomic rename publishes one complete record. Once STARTED is published,
   callers must not roll it back or kill the child on a later sync error. */
static int write_record_file(Store *s, const char *name, cJSON *obj, bool started,
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
        snprintf(temp, sizeof(temp), ".meta-%ld-%u.tmp", (long)getpid(), i);
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
        if (renameat(dir, temp, dir, "meta.json")) rc = syserr(err, "replace record");
        else if (published) *published = true;
    }
    if (!rc && started) bgx_test_point("meta_renamed");
    if (!rc && started && bgx_test_fault("commit_sync_dir"))
        rc = fail(err, "injected instance directory sync failure");
    if (!rc && fsync(dir)) rc = syserr(err, "sync instance directory");
    if (!rc && started && bgx_test_fault("commit_sync_registry"))
        rc = fail(err, "injected registry sync failure");
    if (!rc && fsync(s->instances)) rc = syserr(err, "sync registry directory");
    unlinkat(dir, temp, 0);
    close(dir);
    free(json);
    return rc;
}

int store_save(Store *s, const Record *record, bool *published, Error *err) {
    if (published) *published = false;
    cJSON *obj = encode_record(record);
    if (!obj) return fail(err, "encode record: out of memory");
    int rc = write_record_file(s, record->name, obj, record->launch == STARTED && !record->exited,
                               published, err);
    cJSON_Delete(obj);
    return rc;
}

int store_load(Store *s, const char *name, Record *record, Error *err) {
    memset(record, 0, sizeof(*record));
    int dir = open_instance(s, name, false, err);
    if (dir < 0) return errno == ENOENT ? 1 : -1;
    int rc = load_record(dir, name, record, err);
    close(dir);
    /* An incomplete directory is occupied, not permission to overwrite evidence. */
    if (rc == 1) return fail(err, "incomplete record for %s; inspect or remove it with --force", name);
    return rc;
}

int store_exists(Store *s, const char *name, Error *err) {
    char key[NAME_KEY_SIZE];
    if (name_key(name, key, err)) return -1;
    struct stat st;
    if (!fstatat(s->instances, key, &st, AT_SYMLINK_NOFOLLOW)) return 1;
    return errno == ENOENT ? 0 : syserr(err, "inspect name");
}

int store_create_log(Store *s, const char *name, Error *err) {
    int dir = open_instance(s, name, true, err);
    if (dir < 0) return -1;
    int fd = regular_file(dir, BGX_LOG_FILE, O_CREAT | O_EXCL | O_WRONLY, err);
    if (fd >= 0 && fchmod(fd, 0600)) {
        syserr(err, "secure log");
        close(fd);
        fd = -1;
    }
    close(dir);
    return fd;
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

int store_rename(Store *s, const char *name, const char *new_name, Error *err) {
    char source[NAME_KEY_SIZE], target[NAME_KEY_SIZE];
    if (name_key(name, source, err) || name_key(new_name, target, err)) return -1;
    int exists = store_exists(s, new_name, err);
    if (exists < 0) return -1;
    if (exists) return fail(err, "name already exists: %s", new_name);
    bgx_test_point("before_rename");
    if (renameat(s->instances, source, s->instances, target)) return syserr(err, "rename instance");
    bgx_test_point("after_rename");
    if (bgx_test_fault("rename_sync") || fsync(s->instances))
        return fail(err, "renamed %s to %s, but syncing the state directory failed; use the new name",
                    name, new_name);
    return 0;
}

int store_log(Store *s, const char *name, Error *err) {
    int dir = open_instance(s, name, false, err);
    if (dir < 0) return errno == ENOENT ? fail(err, "instance not found: %s", name) : -1;
    /* Pin the instance directory: name reuse removes and recreates it, so a
       reader can never jump from an old record to a new run's log. */
    Record record = {0};
    int loaded = load_record(dir, name, &record, err);
    int fd = -1;
    if (!loaded) {
        bgx_test_point("log_record_loaded");
        fd = regular_file(dir, BGX_LOG_FILE, O_RDONLY, err);
    } else if (loaded == 1) fail(err, "incomplete record for %s", name);
    record_free(&record);
    close(dir);
    return fd;
}

char *store_log_path(Store *s, const char *name, Error *err) {
    char key[NAME_KEY_SIZE], relative[NAME_KEY_SIZE + sizeof("instances/" BGX_LOG_FILE) + 1];
    if (name_key(name, key, err)) return NULL;
    snprintf(relative, sizeof(relative), "instances/%s/" BGX_LOG_FILE, key);
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
