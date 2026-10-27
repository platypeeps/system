/* job-launch: the program launchd starts for a cron job (sd:6456).
 *
 * macOS charges a job's file access to the process launchd started. When
 * that is Apple's /bin/bash, its Full Disk Access covers only Apple tools:
 * a Homebrew or uv python under it is charged to itself, and Homebrew
 * python's grant drops on each upgrade. A non-Apple parent does pass its
 * grant on, so this binary, granted once, stays the charged process while
 * it is not rebuilt.
 *
 * It must fork and wait, never exec: after an exec the process is the new
 * program, and TCC charges that one.
 *
 * usage: job-launch PROGRAM [ARG...]; exits with the child's status, or
 * 128+N when a signal N ended it. INT, TERM and HUP go to the child.
 */
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

static pid_t child;

static void pass_on(int sig) {
    if (child > 0) kill(child, sig);
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: job-launch PROGRAM [ARG...]\n");
        return 1;
    }
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = pass_on;
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);

    child = fork();
    if (child < 0) {
        perror("job-launch: fork");
        return 1;
    }
    if (child == 0) {
        execv(argv[1], argv + 1);
        fprintf(stderr, "job-launch: cannot run %s: %s\n", argv[1], strerror(errno));
        _exit(127);
    }
    int status;
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR) {
            perror("job-launch: waitpid");
            return 1;
        }
    }
    return WIFSIGNALED(status) ? 128 + WTERMSIG(status) : WEXITSTATUS(status);
}
