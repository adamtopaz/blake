/*
Blake (2026-10-30): a fork server for `lean`.

The process initializes Lean once, exactly as `lean` does (`lean_initialize` of the
toolchain's `libleanshared`), and then serves compile requests from Blake on stdin.
For each request it forks a child that redirects its output, sets `LEAN_PATH`, and
runs the steps of `lean_main` (Lean v4.35.0, `src/util/shell.cpp`) that follow
`lean::initializer`, ending in `lean_shell_main`, the same driver code that the `lean`
executable runs. The child therefore writes the same artifacts as `lean`, without
paying for `exec`, dynamic linking, and Lean's initialization on every module.

Protocol (binary, on stdin): a request is a decimal field count followed by that many
fields, each terminated by a NUL byte: the stdout file, the stderr file, the value of
`LEAN_PATH`, and then `argv` (starting with `argv[0]`). A count of 0, or end of input,
ends the server. The reply (on stdout) is the child's exit code as a decimal line
(128 + the signal number if it was killed by a signal).
*/
#include <lean/lean.h>
#include <fcntl.h>
#include <getopt.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern void lean_initialize(void);
extern lean_object * lean_init_search_path(lean_object *);
extern lean_object * lean_enable_initializer_execution(lean_object *);
extern lean_object * lean_shell_options_mk(lean_object *);
extern lean_object * lean_shell_options_process(lean_object *, uint32_t, lean_object *, lean_object *);
extern uint32_t lean_shell_options_get_num_threads(lean_object *);
extern lean_object * lean_shell_main(lean_object *, lean_object *, lean_object *);

/* The option table and string of `lean_main` (Lean v4.35.0, multi-threaded build). */
static struct option long_opts[] = {
  {"version", no_argument, 0, 'v'}, {"help", no_argument, 0, 'h'}, {"githash", no_argument, 0, 'g'},
  {"short-version", no_argument, 0, 'V'}, {"run", no_argument, 0, 'r'}, {"o", optional_argument, 0, 'o'},
  {"i", optional_argument, 0, 'i'}, {"stdin", no_argument, 0, 'I'}, {"root", required_argument, 0, 'R'},
  {"memory", required_argument, 0, 'M'}, {"trust", required_argument, 0, 't'}, {"profile", no_argument, 0, 'P'},
  {"stats", no_argument, 0, 'a'}, {"quiet", no_argument, 0, 'q'}, {"deps", no_argument, 0, 'd'},
  {"src-deps", no_argument, 0, 'O'}, {"deps-json", no_argument, 0, 'N'}, {"timeout", optional_argument, 0, 'T'},
  {"c", optional_argument, 0, 'c'}, {"bc", optional_argument, 0, 'b'}, {"features", no_argument, 0, 'f'},
  {"exitOnPanic", no_argument, 0, 'e'}, {"threads", required_argument, 0, 'j'}, {"tstack", required_argument, 0, 's'},
  {"server", no_argument, 0, 'S'}, {"worker", no_argument, 0, 'W'}, {"plugin", required_argument, 0, 'p'},
  {"load-dynlib", required_argument, 0, 'l'}, {"setup", required_argument, 0, 'u'}, {"error", required_argument, 0, 'E'},
  {"json", no_argument, 0, 'J'}, {"print-prefix", no_argument, 0, 'x'}, {"print-libdir", no_argument, 0, 'L'},
  {"incr-save", required_argument, 0, 'Y'}, {"incr-load", required_argument, 0, 'Z'},
  {"incr-header-save", required_argument, 0, 'H'}, {0, 0, 0, 0}};
static const char * opt_str = "PdD:o:i:b:c:C:qgvVht:012j:012rR:M:012T:012ap:eE:Y:Z:H:s:012";

struct run_args { lean_object * args; lean_object * opts; int rc; };

static void * run_main(void * p) {
  struct run_args * a = p;
  lean_object * r = lean_shell_main(a->args, a->opts, lean_io_mk_world());
  a->rc = lean_io_result_is_ok(r) ? (int)lean_unbox_uint32(lean_io_result_get_value(r)) : 1;
  return NULL;
}

/* What `lean_main` does after `lean::initializer`. */
static int child_main(int argc, char ** argv) {
  lean_dec(lean_init_search_path(lean_io_mk_world()));
  lean_dec(lean_enable_initializer_execution(lean_io_mk_world()));
  lean_object * opts = lean_shell_options_mk(lean_box(0));
  optind = 1;
  for (;;) {
    int c = getopt_long(argc, argv, opt_str, long_opts, NULL);
    if (c == -1) break;
    lean_object * arg = lean_box(0); /* none */
    if (optarg) { arg = lean_alloc_ctor(1, 1, 0); lean_ctor_set(arg, 0, lean_mk_string(optarg)); }
    lean_object * r = lean_shell_options_process(opts, (uint32_t)c, arg, lean_io_mk_world());
    if (!lean_io_result_is_ok(r)) return (int)lean_unbox(lean_io_result_get_error(r));
    opts = lean_io_result_get_value(r); lean_inc(opts); lean_dec_ref(r);
  }
  lean_io_mark_end_initialization();
  lean_inc(opts);
  lean_init_task_manager_using(lean_shell_options_get_num_threads(opts));
  lean_object * args = lean_box(0);
  for (int i = argc - 1; i >= optind; i--) {
    lean_object * cell = lean_alloc_ctor(1, 2, 0);
    lean_ctor_set(cell, 0, lean_mk_string(argv[i])); lean_ctor_set(cell, 1, args); args = cell;
  }
  struct run_args a = {args, opts, 1};
  pthread_attr_t attr; pthread_attr_init(&attr); pthread_attr_setstacksize(&attr, 64u << 20);
  pthread_t t;
  if (pthread_create(&t, &attr, run_main, &a) != 0) return 1;
  pthread_join(t, NULL);
  return a.rc;
}

/* Reads one NUL-terminated field from stdin into a malloc'd buffer; NULL at end of input. */
static char * read_field(void) {
  size_t cap = 256, n = 0; char * buf = malloc(cap);
  for (;;) {
    int ch = getchar();
    if (ch == EOF) { free(buf); return NULL; }
    if (n + 1 >= cap) { cap *= 2; buf = realloc(buf, cap); }
    buf[n++] = (char)ch;
    if (ch == 0) return buf;
  }
}

int main(void) {
  lean_initialize();
  lean_set_panic_messages(true);
  for (;;) {
    char * count_s = read_field();
    if (!count_s) return 0;
    int count = atoi(count_s); free(count_s);
    if (count < 4) return 0;
    char ** fields = calloc((size_t)count + 1, sizeof(char *));
    for (int i = 0; i < count; i++) {
      fields[i] = read_field();
      if (!fields[i]) return 0;
    }
    fflush(NULL);
    pid_t pid = fork();
    if (pid == 0) {
      int devnull = open("/dev/null", O_RDONLY);
      int out = open(fields[0], O_WRONLY | O_CREAT | O_TRUNC, 0644);
      int err = open(fields[1], O_WRONLY | O_CREAT | O_TRUNC, 0644);
      if (devnull < 0 || out < 0 || err < 0) _exit(127);
      dup2(devnull, 0); dup2(out, 1); dup2(err, 2);
      close(devnull); close(out); close(err);
      setenv("LEAN_PATH", fields[2], 1);
      int rc = child_main(count - 3, fields + 3);
      fflush(NULL);
      _exit(rc);
    }
    int code = 127;
    if (pid > 0) {
      int st;
      while (waitpid(pid, &st, 0) < 0) {}
      code = WIFEXITED(st) ? WEXITSTATUS(st) : (WIFSIGNALED(st) ? 128 + WTERMSIG(st) : 127);
    }
    for (int i = 0; i < count; i++) free(fields[i]);
    free(fields);
    printf("%d\n", code);
    fflush(stdout);
  }
}
