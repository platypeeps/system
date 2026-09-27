// Test double for open(2), loaded into msgsnap with DYLD_INSERT_LIBRARIES.
//
// msgsnap's source path is compiled in and the binary takes no flag that
// could change it, by design (src/msgsnap.swift, the header comment). So the
// only way to make its open(2) misbehave on demand, without giving the FDA
// holder a test hook, is to stand between it and the kernel. dyld's
// interposing does that: every open() the process makes lands here first,
// including the one libswiftDarwin makes on the binary's behalf.
//
// Only opens of the compiled-in source path are touched. Everything else
// goes straight through, so the binary's own I/O is unaffected.
//
// Environment, read once at load:
//   MSGSNAP_TEST_EINTR     how many leading opens of the source fail with
//                          EINTR. -1 means every one. Default 0.
//   MSGSNAP_TEST_ERRNO     what the first open after those does: 0 opens
//                          MSGSNAP_TEST_REDIRECT instead of the source (so a
//                          test needs no Full Disk Access), any other value
//                          fails with that errno. Default 0.
//   MSGSNAP_TEST_REDIRECT  the file to open in place of the source when
//                          MSGSNAP_TEST_ERRNO is 0.
//   MSGSNAP_TEST_COUNT     a file that receives the number of source opens
//                          seen so far, rewritten on every call.
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define DYLD_INTERPOSE(_replacement, _replacee) \
    __attribute__((used)) static struct { const void *replacement; const void *replacee; } \
    _interpose_##_replacee __attribute__((section("__DATA,__interpose"))) = \
    { (const void *)(unsigned long)&_replacement, (const void *)(unsigned long)&_replacee };

static const char SOURCE_SUFFIX[] = "/Library/Messages/chat.db";

static long eintr_budget = 0;
static int final_errno = 0;
static const char *redirect = NULL;
static const char *count_file = NULL;
static long seen = 0;

__attribute__((constructor)) static void configure(void) {
    const char *v;
    if ((v = getenv("MSGSNAP_TEST_EINTR")) != NULL) eintr_budget = strtol(v, NULL, 10);
    if ((v = getenv("MSGSNAP_TEST_ERRNO")) != NULL) final_errno = (int)strtol(v, NULL, 10);
    redirect = getenv("MSGSNAP_TEST_REDIRECT");
    count_file = getenv("MSGSNAP_TEST_COUNT");
}

static int is_source(const char *path) {
    size_t n = strlen(path), m = sizeof(SOURCE_SUFFIX) - 1;
    return n >= m && strcmp(path + n - m, SOURCE_SUFFIX) == 0;
}

// The interposing image's own calls are not interposed, so this reaches the
// real open(2). It is also only ever asked to open a path that is not the
// source, which keeps it out of the branch below either way.
static void record(void) {
    if (count_file == NULL) return;
    char buf[32];
    int len = snprintf(buf, sizeof buf, "%ld\n", seen);
    int fd = open(count_file, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return;
    (void)write(fd, buf, (size_t)len);
    close(fd);
}

static int test_open(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list ap;
        va_start(ap, flags);
        mode = (mode_t)va_arg(ap, int);
        va_end(ap);
    }
    if (!is_source(path)) return open(path, flags, mode);

    seen += 1;
    record();
    if (eintr_budget < 0 || seen <= eintr_budget) {
        errno = EINTR;
        return -1;
    }
    if (final_errno != 0) {
        errno = final_errno;
        return -1;
    }
    if (redirect == NULL) {
        errno = ENOENT;
        return -1;
    }
    return open(redirect, flags, mode);
}
DYLD_INTERPOSE(test_open, open)
