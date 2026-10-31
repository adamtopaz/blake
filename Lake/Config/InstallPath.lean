/-
Copyright (c) 2021 Mac Malone. All rights reserved.
Released under Apache 2.0 license as described in the file LICENSE.
Authors: Mac Malone
-/
module

prelude
public import Lean.Compiler.FFI
public import Lake.Config.Dynlib
public import Lake.Config.Defaults
public import Lake.Util.NativeLib
import Init.Data.UInt.Lemmas
import Init.Data.String.Modify
import Init.Data.String.Legacy
import Init.System.Platform

open System Lean.Compiler.FFI

namespace Lake

/-- Convert the string value of an environment variable to a boolean. -/
public def envToBool? (o : String) : Option Bool :=
  if ["y", "yes", "t", "true", "on", "1"].contains o.toLower then some true
  else if ["n", "no", "f", "false", "off", "0"].contains o.toLower then some false
  else none

/-! ## Data Structures -/

/-- Information about the local Elan setup. -/
public structure ElanInstall where
  home : FilePath
  elan : FilePath
  binDir := home / "bin"
  toolchainsDir := home / "toolchains"
  deriving Inhabited, Repr

/-- Convert an Elan toolchain name to an Elan toolchain directory name. -/
@[inline] public partial def toolchain2Dir (toolchain : String) : FilePath :=
  go "" 0
where
  go (acc : String) (pos : String.Pos.Raw) : FilePath :=
    if h : pos.atEnd toolchain then
      FilePath.mk acc
    else
      let c := pos.get' toolchain h
      let pos' := pos.next' toolchain h
      if c = '/' then
        go (acc ++ "--") pos'
      else if c = ':'  then
        go (acc ++ "---") pos'
      else
        go (acc.push c) pos'

@[inline] public def ElanInstall.toolchainDir (toolchain : String) (elan : ElanInstall) : FilePath :=
  elan.toolchainsDir / toolchain2Dir toolchain

/-- Standard path of `lean` in a Lean installation. -/
public def leanExe (sysroot : FilePath) :=
  sysroot / "bin" / "lean" |>.addExtension FilePath.exeExtension

/-- Standard path of `leanir` in a Lean installation. -/
public def leanirExe (sysroot : FilePath) :=
  sysroot / "bin" / "leanir" |>.addExtension FilePath.exeExtension

/-- Standard path of `leanc` in a Lean installation. -/
public def leancExe (sysroot : FilePath) :=
  sysroot / "bin" / "leanc" |>.addExtension FilePath.exeExtension

/-- Standard path of `leantar` in a Lean installation. -/
public def leantarExe (sysroot : FilePath) :=
  sysroot / "bin" / "leantar" |>.addExtension FilePath.exeExtension

/-- Standard path of `llvm-ar` in a Lean installation. -/
public def leanArExe (sysroot : FilePath) :=
  sysroot / "bin" / "llvm-ar" |>.addExtension FilePath.exeExtension

/-- Standard path of `clang` in a Lean installation. -/
public def leanCcExe (sysroot : FilePath) :=
  sysroot / "bin" / "clang" |>.addExtension FilePath.exeExtension

/-- Standard path of shared libraries in a Lean installation. -/
public def leanSharedLibDir (sysroot : FilePath) :=
  if Platform.isWindows then
    sysroot / "bin"
  else
    sysroot / "lib" / "lean"

/-- The entire set of core shared libraries in a Lean installation. -/
public def leanSharedDynlibs (sysroot : FilePath) : Array Dynlib :=
  -- libLake_shared links against the split libs on all platforms,
  -- so they must be included in the bundle even when they are empty stubs.
  if System.Platform.isWindows then libs winLib else libs unixLib
where
  @[inline] winLib name deps :=
    -- On Windows, libraries are in `bin` and link to one another
    {name, path := sysroot / "bin" / s!"lib{name}.dll", deps}
  @[inline] unixLib name _ :=
     -- On Unix, libraries are in `lean/lib` and are independent
    {name, path := sysroot / "lib" / "lean" / s!"lib{name}.{sharedLibExt}"}
  @[inline] libs f :=
    let init := f "Init_shared" #[]
    let lean1 := f "leanshared_1" #[init]
    let lean2 := f  "leanshared_2" #[lean1, init]
    let lean := f "leanshared" #[lean2, lean1, init]
    #[lean, lean2, lean1, init]

theorem size_leanSharedDynlibs_pos : 0 < (leanSharedDynlibs sysroot).size := by
  unfold leanSharedDynlibs; split <;> simp [leanSharedDynlibs.libs]

/-- The primary core shared library (i.e., `libleanshared`) in a Lean installation. -/
public def leanSharedDynlib (sysroot : FilePath) : Dynlib :=
  (leanSharedDynlibs sysroot)[(0 : USize)]'size_leanSharedDynlibs_pos

/-- `libleanshared` file name. -/
public def leanSharedLib : FilePath :=
  FilePath.addExtension "libleanshared" sharedLibExt

/-- `Init` shared library file name. -/
@[deprecated "leanSharedDynlibs" (since := "2026-06-29")]
public def initSharedLib : FilePath :=
  FilePath.addExtension "libInit_shared" sharedLibExt

/-- Path information about the local Lean installation. -/
public structure LeanInstall where
  sysroot : FilePath
  githash : String := ""
  srcDir := sysroot / "src" / "lean"
  leanLibDir := sysroot / "lib" / "lean"
  includeDir := sysroot / "include"
  systemLibDir := sysroot / "lib"
  binDir := sysroot / "bin"
  lean := leanExe sysroot
  leanir := leanirExe sysroot
  leanc := leancExe sysroot
  leantar := leantarExe sysroot
  sharedDynlibs := leanSharedDynlibs sysroot
  sharedDynlib := leanSharedDynlib sysroot
  ar : FilePath := "ar"
  cc : FilePath := "cc"
  customCc : Bool := true
  cFlags := getCFlags'.push "-Wno-unused-command-line-argument"
  linkStaticFlags := getLinkerFlags' (linkStatic := true)
  linkSharedFlags := getLinkerFlags' (linkStatic := false)
  ccFlags := cFlags
  ccLinkStaticFlags := linkStaticFlags
  ccLinkSharedFlags := linkSharedFlags
  deriving Inhabited, Repr

@[inline] public def LeanInstall.sharedLib (self : LeanInstall) : FilePath :=
  self.sharedDynlib.path

@[deprecated "sharedDynlibs" (since := "2026-06-29")]
public nonrec def LeanInstall.initSharedLib (self : LeanInstall) : FilePath :=
  leanSharedLibDir self.sysroot / initSharedLib

/--
A `SearchPath` including the Lean installation's shared library directories
(i.e., the system library and Lean library directories).
-/
public def LeanInstall.sharedLibPath (self : LeanInstall) : SearchPath :=
  if Platform.isWindows then
    [self.binDir]
  else
    [self.leanLibDir, self.systemLibDir]

/-- The `LEAN_CC` of the Lean installation. -/
public def LeanInstall.leanCc? (self : LeanInstall) : Option String :=
  if self.customCc then some self.cc.toString else none

/-- The link-time flags for the C compiler of the Lean installation. -/
public def LeanInstall.ccLinkFlags (shared : Bool) (self : LeanInstall) : Array String :=
  if shared then self.ccLinkSharedFlags else self.ccLinkStaticFlags

/-- Lake executable file name. -/
public def lakeExe : FilePath :=
  FilePath.addExtension "lake" FilePath.exeExtension

/-- Path information about the local Lake installation. -/
public structure LakeInstall where
  home : FilePath
  srcDir := home
  binDir := home / defaultBuildDir / defaultBinDir
  libDir := home / defaultBuildDir / defaultLeanLibDir
  sharedDynlib : Dynlib := {
    name := "Lake"
    path := libDir / nameToSharedLib "Lake"
  }
  lake := binDir / lakeExe
  deriving Inhabited, Repr

@[inline] public def LakeInstall.sharedLib (self : LakeInstall) : FilePath :=
  self.sharedDynlib.path

/-- Construct a Lake installation co-located with the specified Lean installation. -/
public def LakeInstall.ofLean (lean : LeanInstall) : LakeInstall where
  home := lean.sysroot
  srcDir := lean.srcDir / "lake"
  binDir := lean.binDir
  libDir := lean.leanLibDir
  lake := lean.binDir / lakeExe
  sharedDynlib := {
    name := "Lake_shared"
    deps := lean.sharedDynlibs
    path :=
      if Platform.isWindows then
        lean.binDir / "libLake_shared.dll"
      else
        lean.leanLibDir / s!"libLake_shared.{sharedLibExt}"
    }

/-! ## Detection Functions -/

/--
Attempt to detect an Elan installation by checking the `ELAN` and `ELAN_HOME`
environment variables. If `ELAN` is set but empty, Elan is considered disabled.
-/
public def findElanInstall? : BaseIO (Option ElanInstall) := do
  if let some home ← IO.getEnv "ELAN_HOME" then
    let elan := (← IO.getEnv "ELAN").getD "elan"
    if elan.trimAscii.isEmpty then
      return none
    else
      return some {elan, home}
  return none

/--
Attempt to find the sysroot of the given `lean` command (if it exists)
by calling `lean --print-prefix` and returning the path it prints.
Defaults to trying the `lean` in `PATH`.
-/
public def findLeanSysroot? (lean := "lean") : BaseIO (Option FilePath) := do
  let act : IO _ := do
    let out ← IO.Process.output {
      cmd := lean,
      args := #["--print-prefix"]
    }
    if out.exitCode == 0 then
      pure <| some <| FilePath.mk <| out.stdout.trimAscii.copy
    else
      pure <| none
  act.catchExceptions fun _ => pure none

/-
Modified for Blake (2026-10-30): `initOleanHasLeanGithash` (used by `LeanInstall.get`
and `findElanLeanSysroot?`).

An `.olean` header is `olean`, a format version byte (2), a flags byte, the
Lean version string padded to 33 bytes, and the 40-byte githash of the Lean
that wrote it. `lean` refuses to import an `.olean` whose header githash differs
from its own ("incompatible header"), so in a working installation the githash
in `Init.olean` is the one `lean --githash` prints.
-/

/--
Whether the `Init.olean` of the Lean installation at `sysroot` records this
Lake's own `Lean.githash` (whose header layout is known), i.e., whether that
installation's `lean --githash` would print `Lean.githash`.
-/
public def initOleanHasLeanGithash (sysroot : FilePath) : BaseIO Bool := do
  let githash := Lean.githash.toUTF8.data
  if githash.size != 40 then
    return false
  EIO.catchExceptions (h := fun _ => pure false) do
    let h ← IO.FS.Handle.mk (sysroot / "lib" / "lean" / "Init.olean") .read
    let hdr ← h.read 80
    return hdr.size == 80 &&
      hdr.data.extract 0 6 == #[0x6f, 0x6c, 0x65, 0x61, 0x6e, 2] && -- `olean`, version 2
      hdr.data.extract 40 80 == githash

/--
Construct the `LeanInstall` object for the given Lean sysroot.

Does the following:
1. Find `lean`'s githash.
2. Finds the  `ar` and `cc` to use with Lean.
3. Computes the sub-paths of the Lean install.

For (1), If `lake` is not-collocated with `lean`, invoke `lean --githash`;
otherwise, use Lake's `Lean.githash`. If the invocation fails, `githash` is
set to the empty string.

For (2), if `LEAN_AR` or `LEAN_CC` are defined, it uses those paths.
Otherwise, if Lean is packaged with an `llvm-ar` and/or `clang`, use them.
If not, use the `ar` and/or `cc` from the `AR` / `CC` environment variables
or the system's `PATH`. This last step is needed because internal builds of
Lean do not bundle these tools (unlike user-facing releases).

We also track whether `LEAN_CC` was set to determine whether it should
be set in the future for `lake env`. This is because if `LEAN_CC` was not set,
it needs to remain not set for `leanc` to work.
Even setting it to the bundled compiler will break `leanc` -- see
[leanprover/lean4#1281](https://github.com/leanprover/lean4/issues/1281).

For (3), it assumes that the Lean installation is organized the normal way.
That is, with its binaries located in `<lean-sysroot>/bin`, its
Lean libraries in `<lean-sysroot>/lib/lean`, and its system libraries in
`<lean-sysroot>/lib`.
-/
public def LeanInstall.get (sysroot : FilePath) (collocated : Bool := false) : BaseIO LeanInstall := do
  let githash ← do
    if collocated then
      pure Lean.githash
    -- Modified for Blake (2026-10-30): a Lake that is not installed next to `lean`
    -- spent ~30 ms per invocation running `lean --githash`. When the toolchain's
    -- `Init.olean` header records this Lake's own `Lean.githash`, that is what
    -- `lean --githash` prints (see `initOleanHasLeanGithash`), so skip the process.
    else if (← initOleanHasLeanGithash sysroot) then
      pure Lean.githash
    else
      -- Remark: This is expensive (at least on Windows), so try to avoid it.
      getGithash
  let ar ← findAr
  setCc {sysroot, githash, ar}
where
  getGithash := do
    EIO.catchExceptions (h := fun _ => pure "") do
      let out ← IO.Process.output {
        cmd := leanExe sysroot |>.toString,
        args := #["--githash"]
      }
      return out.stdout.trimAscii.copy
  findAr := do
    if let some ar ← IO.getEnv "LEAN_AR" then
      return FilePath.mk ar
    else
      let ar := leanArExe sysroot
      if (← ar.pathExists) then
        return ar
      else if let some ar ← IO.getEnv "AR" then
        return ar
      else
        return "ar"
  setCc i := do
    if let some cc ← IO.getEnv "LEAN_CC" then
      return withCustomCc i cc
    else
      let cc := leanCcExe sysroot
      if (← cc.pathExists) then
        return withInternalCc i cc
      else if let some cc ← IO.getEnv "CC" then
        return withCustomCc i cc
      else
        return withCustomCc i "cc"
  @[inline] withCustomCc (i : LeanInstall) cc :=
    {i with cc}
  withInternalCc (i : LeanInstall) cc :=
    let ccLinkFlags := getInternalLinkerFlags sysroot
    {i with
      cc, customCc := false
      ccFlags := i.cFlags ++ getInternalCFlags sysroot
      ccLinkStaticFlags := ccLinkFlags ++ i.linkStaticFlags
      ccLinkSharedFlags := ccLinkFlags ++ i.linkSharedFlags
    }

/--
Attempt to detect the installation of the given `lean` command
by calling `findLeanSysroot?`. See `LeanInstall.get` for how it assumes the
Lean install is organized.
-/
public def findLeanCmdInstall? (lean := "lean") : BaseIO (Option LeanInstall) :=
  OptionT.run do LeanInstall.get (← findLeanSysroot? lean)

/--
Check if the running Lake's executable is co-located with Lean, and, if so,
try to return their joint home by assuming they are both located at `<home>/bin`.
-/
public def findLakeLeanJointHome? : BaseIO (Option FilePath) := do
  if let .ok appPath ← IO.appPath.toBaseIO then
    if let some appDir := appPath.parent then
      let leanExe := appDir / "lean" |>.addExtension FilePath.exeExtension
      if (← leanExe.pathExists) then
        return appDir.parent
  return none

/--
Get the root of Lake's installation by assuming the executable
is located at `<lake-home>/.lake/build/bin/lake`.
-/
public def lakeBuildHome? (lake : FilePath) : Option FilePath := do
  (← (← (← lake.parent).parent).parent).parent

/--
Heuristically validate that `getLakeBuildHome?` is a proper Lake installation
by check for `Lake.olean` in the installation's `lib` directory.
-/
public def getLakeInstall? (lake : FilePath) : BaseIO (Option LakeInstall) := do
  let some home := lakeBuildHome? lake | return none
  let lake : LakeInstall := {home, lake}
  if (← lake.libDir / "Lake.olean" |>.pathExists) then
    return some lake
  return none

/-
Modified for Blake (2026-10-30): `findElanLeanSysroot?` and its helpers. A Lake
that is not installed next to `lean` ran `lean --print-prefix` on every
invocation (~35 ms through elan's proxy, most of it `lean` initializing). In the
common, unambiguous case the answer follows from elan's own rules (elan v4.2.1,
`Cfg::find_override_toolchain_or_default`), so Lake reads it off the file system.
-/

/-- Whether `c` may appear in the `owner/repo` origin of an elan toolchain name. -/
private def isElanOriginChar (c : Char) : Bool :=
  c.isAlphanum || c == '-' || c == '_'

/-- Whether `c` may appear in the release of an elan toolchain name. -/
private def isElanReleaseChar (c : Char) : Bool :=
  c.isAlphanum || c == '-' || c == '.'

/--
If `name` names a pinned release, `owner/repo:v<digit>...` (e.g.,
`leanprover/lean4:v4.35.0`), its elan toolchain directory name and its release.
Channels (`stable`, `nightly`, ...), bare names, paths, and anything else are `none`.
-/
private def parsePinnedElanToolchain? (name : String) : Option (String × String) :=
  match name.splitOn ":" with
  | [origin, release] =>
    match origin.splitOn "/", release.toList with
    | [owner, repo], 'v' :: d :: _ =>
      if !owner.isEmpty && owner.all isElanOriginChar &&
          !repo.isEmpty && repo.all isElanOriginChar &&
          d.isDigit && release.all isElanReleaseChar then
        some ((toolchain2Dir name).toString, release)
      else
        none
    | _, _ => none
  | _ => none

/--
Whether elan's `settings.toml` (given as text) defines no directory overrides.
Errs on the side of `false`: any line mentioning `overrides` other than an
empty `[overrides]` table counts as an override.
-/
private def elanSettingsHaveNoOverrides (settings : String) : Bool := Id.run do
  let mut inOverrides := false
  for line in settings.splitOn "\n" do
    let line := line.trimAscii.copy
    if line.isEmpty || line.startsWith "#" then
      continue
    if line == "[overrides]" then
      inOverrides := true
    else if inOverrides || (line.splitOn "overrides").length > 1 then
      return false
    else if line.startsWith "[" then
      inOverrides := false
  return true

/-- The first `PATH` entry named `name`, searched the way `execvp` does. -/
private def findInPath? (name : String) : BaseIO (Option FilePath) := do
  let some path ← IO.getEnv "PATH" | return none
  for dir in path.splitOn ":" do
    let file := FilePath.mk (if dir.isEmpty then "." else dir) / name
    if (← file.pathExists) then
      return some file
  return none

/--
Whether `lean` (a `PATH` entry) is elan's proxy: the same file as the `elan`
next to it, which elan's installer creates as a hard link (or a symbolic link,
as package managers do).
-/
private def isElanProxy (lean : FilePath) : BaseIO Bool := do
  let some dir := lean.parent | return false
  EIO.catchExceptions (h := fun _ => pure false) do
    let lean ← IO.FS.realPath lean
    let elan ← IO.FS.realPath (dir / "elan")
    if lean == elan then
      return true
    let l ← lean.metadata
    let e ← elan.metadata
    return l.type == .file && e.type == .file && l.numLinks > 1 &&
      l.byteSize == e.byteSize && l.modified == e.modified

/--
The sysroot that `lean --print-prefix` would print, found without running it,
when `lean` in `PATH` is elan's proxy and elan's choice of toolchain is
unambiguous; otherwise `none`, and the caller asks `lean`.

Elan picks, for the canonical working directory: `ELAN_TOOLCHAIN` if set; else,
walking up from the directory, the first directory override in its
`settings.toml`, `lean-toolchain` file, or `leanpkg.toml` file; else its default
toolchain. A `lean-toolchain` name is first tried as a path to a toolchain
directory, then as a linked toolchain, then as a release, which is installed if
missing. This function only answers when there is no `ELAN_TOOLCHAIN`, no
override at all, no `leanpkg.toml` on the way, the nearest `lean-toolchain`
pins a release (see `parsePinnedElanToolchain?`) that is installed and that no
path or linked toolchain shadows, and that release's `Init.olean` records this
Lake's `Lean.githash`. Like `lean --print-prefix`, it reports the directory
above the real location of the toolchain's `bin/lean`.
-/
public def findElanLeanSysroot? : BaseIO (Option FilePath) := do
  if Platform.isWindows then
    return none
  if let some tc ← IO.getEnv "ELAN_TOOLCHAIN" then
    unless tc.isEmpty do
      return none
  let some lean ← findInPath? "lean" | return none
  unless (← isElanProxy lean) do
    return none
  EIO.catchExceptions (h := fun _ => pure none) do
    let cwd ← IO.currentDir
    let elanHome ← do
      if let some home ← IO.getEnv "ELAN_HOME" then
        if home.isEmpty then
          return none
        pure <| if FilePath.isAbsolute home then FilePath.mk home else cwd / home
      else if let some home ← IO.getEnv "HOME" then
        if home.isEmpty then
          return none
        pure <| FilePath.mk home / ".elan"
      else
        return none
    unless elanSettingsHaveNoOverrides (← IO.FS.readFile (elanHome / "settings.toml")) do
      return none
    let toolchainsDir := elanHome / "toolchains"
    let toolchainsDirReal ← IO.FS.realPath toolchainsDir
    let mut dir ← IO.FS.realPath cwd
    for _ in [0:1024] do
      let toolchainFile := dir / "lean-toolchain"
      if (← toolchainFile.pathExists) then
        let name := ((← IO.FS.readFile toolchainFile).splitOn "\n").headD "" |>.trimAscii.copy
        let some (tcDir, release) := parsePinnedElanToolchain? name | return none
        if (← (dir / name).pathExists) then
          return none -- possibly a path toolchain
        if (← toolchainsDir.readDir).any (·.fileName == release) then
          return none -- possibly a linked toolchain
        let leanExe := toolchainsDir / tcDir / "bin" / "lean"
        unless (← leanExe.metadata).type == .file do
          return none
        let some sysroot := (← IO.FS.realPath leanExe).parent >>= (·.parent) | return none
        unless (← initOleanHasLeanGithash sysroot) do
          return none
        return some sysroot
      if (← (dir / "leanpkg.toml").pathExists) then
        return none
      let some parent := dir.parent | return none
      if parent == dir || parent == toolchainsDir || parent == toolchainsDirReal then
        return none
      dir := parent
    return none

/--
Attempt to detect Lean's installation by using the `LEAN` and `LEAN_SYSROOT`
environment variables.

If `LEAN_SYSROOT` is set, use it. Otherwise, check `LEAN` for the `lean`
executable. If `LEAN` is set but empty, Lean will be considered disabled.
Otherwise, Lean's location will be determined by trying `findLeanSysroot?`
using value of `LEAN` or, if unset, the `lean` in `PATH`.

See `LeanInstall.get` for how it assumes the Lean install is organized.
-/
public def findLeanInstall? : BaseIO (Option LeanInstall) := do
  if let some sysroot ← IO.getEnv "LEAN_SYSROOT" then
    return some <| ← LeanInstall.get sysroot
  let lean ← do
    if let some lean ← IO.getEnv "LEAN" then
      if lean.trimAscii.isEmpty then
        return none
      else
        pure lean
    else
      pure "lean"
  -- Modified for Blake (2026-10-30): try `findElanLeanSysroot?` before running `lean`.
  let sysroot? ← do
    if lean == "lean" then
      if let some sysroot ← findElanLeanSysroot? then
        pure (some sysroot)
      else
        findLeanSysroot? lean
    else
      findLeanSysroot? lean
  if let some sysroot := sysroot? then
    return some <| ← LeanInstall.get sysroot
  return none

/--
Attempt to detect Lake's installation by first checking the `lakeBuildHome?`
of the running executable, then trying the `LAKE_HOME` environment variable.

It assumes that the Lake installation is organized the same way it is built.
That is, with its binary located at `<lake-home>/.lake/build/bin/lake` and its
static library and `.olean` files in `<lake-home>/.lake/build/lib`, and
its source files located directly in `<lake-home>`.
-/
public def findLakeInstall? : BaseIO (Option LakeInstall) := do
  if let Except.ok lake ← IO.appPath.toBaseIO then
    if let some lake ← getLakeInstall? lake then
      return some lake
  if let some home ← IO.getEnv "LAKE_HOME" then
    return some {home}
  return none

/--
Attempt to automatically detect an Elan, Lake, and Lean installation.

First, it calls `findElanInstall?` to detect a Elan installation.
Then it attempts to detect if Lake and Lean are part of a single installation
where the `lake` executable is co-located with the `lean` executable (i.e., they
are in the same directory). If Lean and Lake are not co-located, Lake will
attempt  to find the their installations separately by calling
`findLeanInstall?` and `findLakeInstall?`. Setting `LAKE_OVERRIDE_LEAN` to true
will force Lake to use `findLeanInstall?` even if co-located.

When co-located, Lake will assume that Lean and Lake's binaries are located in
`<sysroot>/bin`, their Lean libraries  in `<sysroot>/lib/lean`, Lean's source files
in `<sysroot>/src/lean`, and Lake's source files in `<sysroot>/src/lean/lake`,
following the pattern of a regular Lean toolchain.
-/
public def findInstall? : BaseIO (Option ElanInstall × Option LeanInstall × Option LakeInstall) := do
  let elan? ← findElanInstall?
  if let some sysroot ← findLakeLeanJointHome? then
    if (← IO.getEnv "LAKE_OVERRIDE_LEAN").bind envToBool? |>.getD false then
      let lake := LakeInstall.ofLean {sysroot}
      return (elan?, ← findLeanInstall?, some lake)
    else
      let lean ← LeanInstall.get sysroot (collocated := true)
      let lake := LakeInstall.ofLean lean
      return (elan?, some lean, some lake)
  else
    return (elan?, ← findLeanInstall?, ← findLakeInstall?)
