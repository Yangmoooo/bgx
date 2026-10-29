#include "bgx.h"

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int fail(Error *err, const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(err->text, sizeof(err->text), fmt, ap);
    va_end(ap);
    errno = EINVAL;
    return -1;
}

int syserr(Error *err, const char *what) {
    int saved = errno;
    int rc = fail(err, "%s: %s", what, strerror(saved));
    errno = saved;
    return rc;
}

void append_error(Error *err, const char *fmt, ...) {
    size_t used = strlen(err->text);
    if (used >= sizeof(err->text) - 1) return;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(err->text + used, sizeof(err->text) - used, fmt, ap);
    va_end(ap);
}

char *join_path(const char *a, const char *b, Error *err) {
    size_t na = strlen(a), nb = strlen(b);
    if (na > SIZE_MAX - nb - 2) { fail(err, "path too long"); return NULL; }
    char *out = malloc(na + nb + 2);
    if (!out) { fail(err, "out of memory"); return NULL; }
    snprintf(out, na + nb + 2, "%s/%s", a, b);
    return out;
}

int write_all(int fd, const void *data, size_t length, Error *err) {
    const char *p = data;
    while (length) {
        ssize_t n = write(fd, p, length);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return syserr(err, "write");
        p += n;
        length -= (size_t)n;
    }
    return 0;
}

int read_all(int fd, size_t limit, char **out, size_t *length, Error *err) {
    size_t cap = 4096, used = 0;
    if (cap > limit + 1) cap = limit + 1;
    char *data = malloc(cap + 1);
    if (!data) return fail(err, "out of memory");
    for (;;) {
        if (used == cap) {
            if (cap > limit) { free(data); return fail(err, "file exceeds size limit"); }
            size_t next = cap > (limit + 1) / 2 ? limit + 1 : cap * 2;
            char *grown = realloc(data, next + 1);
            if (!grown) { free(data); return fail(err, "out of memory"); }
            data = grown;
            cap = next;
        }
        ssize_t n = read(fd, data + used, cap - used);
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) { free(data); return syserr(err, "read"); }
        if (n == 0) break;
        used += (size_t)n;
        if (used > limit) { free(data); return fail(err, "file exceeds size limit"); }
    }
    data[used] = 0;
    *out = data;
    *length = used;
    return 0;
}

int valid_name(const char *name, Error *err) {
    size_t len = strlen(name);
    if (!len || len > 128) return fail(err, "name must contain 1-128 characters");
    for (size_t i = 0; i < len; ++i) {
        unsigned char c = (unsigned char)name[i];
        bool alnum = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9');
        if (!alnum && (i == 0 || (c != '_' && c != '-')))
            return fail(err, "name must use ASCII letters, digits, '_' or '-', beginning with a letter or digit");
    }
    return 0;
}

int generate_name(const char *program, char out[30], Error *err) {
    const char *base = strrchr(program, '/');
    base = base ? base + 1 : program;
    size_t used = 0;
    for (const unsigned char *p = (const unsigned char *)base; *p && used < 24; ++p) {
        unsigned char c = *p;
        if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
        bool alnum = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9');
        if (!used && !alnum) continue;
        out[used++] = alnum || c == '_' || c == '-' ? (char)c : '-';
    }
    if (!used) { memcpy(out, "task", 4); used = 4; }
    out[used++] = '-';
    int fd = open("/dev/urandom", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return syserr(err, "open random source");
    const char alphabet[] = "abcdefghijklmnopqrstuvwxyz0123456789";
    for (unsigned i = 0; i < 4;) {
        unsigned char byte;
        ssize_t n = read(fd, &byte, 1);
        if (n < 0 && errno == EINTR) continue;
        if (n != 1) {
            if (n < 0) syserr(err, "read random source");
            else fail(err, "random source ended unexpectedly");
            close(fd);
            return -1;
        }
        if (byte >= 252) continue; /* Rejection sampling avoids modulo bias. */
        out[used++] = alphabet[byte % 36];
        ++i;
    }
    close(fd);
    out[used] = 0;
    return 0;
}

int format_time(time_t t, char out[32]) {
    struct tm tm;
    return gmtime_r(&t, &tm) && strftime(out, 32, "%Y-%m-%dT%H:%M:%SZ", &tm) ? 0 : -1;
}

int parse_time(const char *s, time_t *out) {
    struct tm tm = {0};
    char *end = strptime(s, "%Y-%m-%dT%H:%M:%SZ", &tm);
    if (!end || *end) return -1;
    time_t t = timegm(&tm);
    char roundtrip[32];
    if (t < 0 || format_time(t, roundtrip) || strcmp(roundtrip, s)) return -1;
    *out = t;
    return 0;
}

void print_arg(FILE *out, const char *arg) {
    bool quote = !*arg;
    for (const unsigned char *p = (const unsigned char *)arg; *p; ++p)
        if (*p <= 32 || *p == 127 || *p == '"' || *p == '\\' || *p == '\'') quote = true;
    if (!quote) { fputs(arg, out); return; }
    fputc('"', out);
    for (const unsigned char *p = (const unsigned char *)arg; *p; ++p) {
        if (*p == '"' || *p == '\\') { fputc('\\', out); fputc(*p, out); }
        else if (*p < 32 || *p == 127) fprintf(out, "\\u%04x", (unsigned)*p);
        else fputc(*p, out);
    }
    fputc('"', out);
}

int64_t monotonic_ms(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts)) return -1;
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

void record_free(Record *r) {
    free(r->name);
    free(r->cwd);
    for (size_t i = 0; i < r->argc; ++i) free(r->argv[i]);
    free(r->argv);
    memset(r, 0, sizeof(*r));
}
