/-
Blake build configuration.

Adapted from `tests/lake/lakefile.toml` and `src/lakefile.toml.in` in the
Lean 4 repository. Blake is built as a `bootstrap` package, the same way Lake
is built inside the Lean tree. A bootstrap build does not namespace native
symbols by package name, so `libLake` exports `initialize_Lake`, which Lean
looks up when `lake` loads its own shared library as a plugin (e.g. to build
packages with `precompileModules`). Together with `nativeLibDir`, this matches
the installation layout a source-built `lake` expects of itself (see
`LakeInstall` in `Lake/Config/InstallPath.lean`).
-/
import Lake
open System Lake DSL

package blake where
  bootstrap := true
  nativeLibDir := "lib" / "lean"
  libPrefixOnWindows := true
  -- A bootstrap package compiles C against `<buildDir>/include` rather than the
  -- toolchain's include directory, so populate it first (see `leanInclude`).
  extraDepTargets := #[`leanInclude]

/--
Copies Lean's C headers (`lean/lean.h` and friends) from the detected Lean
toolchain into the package's bootstrap include directory, which Lake uses as
the include path for all C compilation in a `bootstrap` package
(see `leanIncludeDirs` in `Lake/Build/Run.lean`).
-/
target leanInclude pkg : FilePath := do
  let srcDir := (← getLeanIncludeDir) / "lean"
  let dstDir := pkg.bootstrapIncludeDir / "lean"
  IO.FS.createDirAll dstDir
  for entry in (← srcDir.readDir) do
    if (← entry.path.isDir) then continue
    let dst := dstDir / entry.fileName
    let contents ← IO.FS.readBinFile entry.path
    let upToDate ← do
      if (← dst.pathExists) then pure ((← IO.FS.readBinFile dst) == contents) else pure false
    unless upToDate do
      IO.FS.writeBinFile dst contents
  return .pure pkg.bootstrapIncludeDir

lean_lib LeanExport where
  libName := "LeanExport"
  globs := #[.andSubmodules `LeanExport]
  defaultFacets := #[LeanLib.sharedFacet]

@[default_target]
lean_lib Lake where
  libName := "Lake"
  defaultFacets := #[LeanLib.sharedFacet]
  -- `Lake.Check` links the export parser, which is vendored in-tree as `LeanExport`
  needs := #[LeanExport]

/-
Blake (2026-10-30): `blake-zygote`, a fork server that runs `lean`'s own driver
(`lean_shell_main`) in pre-initialized children (see `c/blake_zygote.c` and
`Lake/Build/Zygote.lean`). It links the toolchain's `libleanshared` so that it is
initialized exactly like `lean`.
-/
target blakeZygote pkg : FilePath := do
  let srcJob ← inputTextFile <| pkg.dir / "c" / "blake_zygote.c"
  -- The toolchain's bundled clang has only the headers that Lean's generated C needs
  -- (`-nostdinc`); the zygote needs POSIX headers, so it is built with `LEAN_CC` if
  -- set, else the system `cc`. If that fails, Blake still builds and runs `lean`.
  let lean ← getLeanInstall
  let cc : FilePath := if lean.customCc then lean.cc else "cc"
  let inc ← getLeanIncludeDir
  let libDir ← getLeanLibDir
  srcJob.mapM fun src => do
    let exe := pkg.binDir / "blake-zygote"
    addPlatformTrace
    addLeanTrace
    addPureTrace cc.toString "cc"
    let args := #["-o", exe.toString, src.toString, "-O2", "-I", inc.toString,
      "-L", libDir.toString, "-lleanshared", s!"-Wl,-rpath,{libDir}",
      s!"-Wl,-rpath-link,{libDir}", "-lpthread"]
    buildFileUnlessUpToDate' exe do
      createParentDirs exe
      let out ← rawProc {cmd := cc.toString, args}
      unless out.exitCode == 0 do
        logWarning s!"blake-zygote could not be built (Blake will run `lean` directly):\n{out.stderr}"
        IO.FS.writeFile exe "" -- placeholder; `zygoteFor?` ignores a non-executable zygote
    return exe

/-- Blake (2026-10-31, `lazy-init`): deferred initialization of Lean (`c/blake_init.c`). -/
target blakeInit pkg : FilePath := do
  let srcJob ← inputTextFile <| pkg.dir / "c" / "blake_init.c"
  let oFile := pkg.buildDir / "c" / "blake_init.o"
  buildO oFile srcJob #["-I", (← getLeanIncludeDir).toString] #["-O2", "-fPIC"] (← getLeanCc)

@[default_target]
lean_exe lake where
  root := `LakeMain
  needs := #[Lake, blakeZygote]
  supportInterpreter := true
  moreLinkObjs := #[blakeInit]
  -- See `c/blake_init.c`; `--wrap` is a GNU linker option, used on Linux only.
  moreLinkArgs :=
    if System.Platform.isWindows || System.Platform.isOSX then #[] else
      #["lean_initialize", "lean_io_initializing", "runtime_initialize_LeanExport_Basic",
        "runtime_initialize_Lake_CLI_Check"].map (s!"-Wl,--wrap={·}")
