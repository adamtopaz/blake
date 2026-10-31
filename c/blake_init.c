/*
Blake (2026-10-31, `lazy-init`): defers most of Lean's initialization until it is needed.

Every module of Lake that imports a `Lean` module calls `lean_initialize()` from its
runtime initializer, and `lean_initialize()` initializes all of Lean (`initialize_Lean`,
the C++ kernel and library). `Lake.CLI.Main` also imports `LeanExport.Basic`, which
imports all of `Lean`. Together they are most of Blake's start-up (~30 ms), although
`lake build` on a TOML workspace needs neither: Lake's own imports initialize what it
uses (`Lake.Load.Toml`, `Lake.Build.*`).

On Linux, the `lake` executable is linked with `--wrap` for `lean_initialize`,
`lean_io_initializing`, and the runtime initializers of `LeanExport.Basic` and
`Lake.CLI.Check` (the only modules that bring in `LeanExport`):
- At start-up, `lean_initialize()` initializes only the runtime and the C++ utilities
  (what it does before `initialize_Init`), and the two module initializers do nothing.
- `blake_full_initialize` (Lake's `fullInitialize`) does the rest exactly as
  `lean_initialize()` would, plus the two deferred initializers. Lake calls it before
  any command other than `build` and before loading a configuration written in Lean.
  Initializers may only register attributes and extensions while `IO.initializing` is
  true, so it reports true while they run.
Elsewhere `lean_initialize()` is not wrapped and `blake_full_initialize` does nothing.
*/
#include <lean/lean.h>

#if defined(__linux__)

void lean_initialize_runtime_module(void);
lean_object * initialize_Init(uint8_t builtin);
lean_object * initialize_Std(uint8_t builtin);
lean_object * initialize_Lean(uint8_t builtin);
lean_object * __real_runtime_initialize_LeanExport_Basic(uint8_t builtin);
lean_object * __real_runtime_initialize_Lake_CLI_Check(uint8_t builtin);
uint8_t __real_lean_io_initializing(void);

/* The steps of `lean_initialize()` (Lean v4.35.0, `src/initialize/init.cpp`) after the
   runtime: `lean::initialize_util_module` minus `initialize_runtime_module`, then the
   kernel and library modules. */
void _ZN4lean15save_stack_infoEb(_Bool main);
void _ZN4lean16initialize_asciiEv(void);
void _ZN4lean15initialize_nameEv(void);
void _ZN4lean25initialize_name_generatorEv(void);
void _ZN4lean18initialize_optionsEv(void);
void _ZN4lean24initialize_kernel_moduleEv(void);
void _ZN4lean21init_default_print_fnEv(void);
void _ZN4lean30initialize_library_core_moduleEv(void);
void _ZN4lean25initialize_library_moduleEv(void);
void _ZN4lean31initialize_constructions_moduleEv(void);

static int g_started = 0, g_full = 0, g_in_full = 0;

void __wrap_lean_initialize(void) {
  if (g_started) return;
  g_started = 1;
  _ZN4lean15save_stack_infoEb(1);
  lean_initialize_runtime_module();
  _ZN4lean16initialize_asciiEv();
  _ZN4lean15initialize_nameEv();
  _ZN4lean25initialize_name_generatorEv();
  _ZN4lean18initialize_optionsEv();
}

lean_object * __wrap_runtime_initialize_LeanExport_Basic(uint8_t builtin) {
  return g_in_full ? __real_runtime_initialize_LeanExport_Basic(builtin) : lean_io_result_mk_ok(lean_box(0));
}

lean_object * __wrap_runtime_initialize_Lake_CLI_Check(uint8_t builtin) {
  return g_in_full ? __real_runtime_initialize_Lake_CLI_Check(builtin) : lean_io_result_mk_ok(lean_box(0));
}

/* `BaseIO` externs take no world token and return their value unboxed (Lean v4.35.0). */
uint8_t __wrap_lean_io_initializing(void) {
  return g_in_full ? 1 : __real_lean_io_initializing();
}

static lean_object * run(lean_object * r) {
  if (lean_io_result_is_error(r)) return r;
  lean_dec_ref(r);
  return NULL;
}

LEAN_EXPORT lean_object * blake_full_initialize(void) {
  if (g_full) return lean_box(0);
  g_full = 1;
  g_in_full = 1;
  lean_object * err = NULL;
  if (!err) err = run(initialize_Init(1));
  if (!err) err = run(initialize_Std(1));
  if (!err) err = run(initialize_Lean(1));
  if (!err) {
    _ZN4lean24initialize_kernel_moduleEv();
    _ZN4lean21init_default_print_fnEv();
    _ZN4lean30initialize_library_core_moduleEv();
    _ZN4lean25initialize_library_moduleEv();
    _ZN4lean31initialize_constructions_moduleEv();
  }
  if (!err) err = run(__real_runtime_initialize_LeanExport_Basic(1));
  if (!err) err = run(__real_runtime_initialize_Lake_CLI_Check(1));
  g_in_full = 0;
  if (err) {
    lean_io_result_show_error(err);
    lean_dec_ref(err);
    lean_internal_panic("Blake: failed to initialize Lean");
  }
  return lean_box(0);
}

#else

LEAN_EXPORT lean_object * blake_full_initialize(void) {
  return lean_box(0);
}

#endif
