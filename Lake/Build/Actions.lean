/-
Copyright (c) 2017 Microsoft Corporation. All rights reserved.
Released under Apache 2.0 license as described in the file LICENSE.
Authors: Gabriel Ebner, Sebastian Ullrich, Mac Malone, Siddharth Bhat
-/
module

prelude
public import Lake.Util.Log
import Lake.Util.Proc
import Lake.Util.FilePath
import Lake.Util.IO
import Lake.Util.Url
import Lake.Config.InstallPath
import Std.Data.HashMap
import Std.Data.HashSet
import Std.Sync.Mutex
import Init.Data.String.Legacy
import Init.Data.String.Search
import Init.Data.String.TakeDrop
import Init.System.Platform
import Lean.CoreM
import Lean.Compiler.Options

/-! # Common Build Actions
Low level actions to build common Lean artifacts via the Lean toolchain.
-/

open System
open Lean hiding SearchPath

namespace Lake

/-
Modified for Blake (2026-10-30): `lean` runs through `blake-zygote` when possible.
`blake-zygote` (`c/blake_zygote.c`, built next to Blake's binary) initializes Lean once
and compiles each module in a forked child that runs `lean`'s own driver
(`lean_shell_main`), so a module no longer pays for `exec`, dynamic linking, and
Lean's initialization (~25-30 ms). It is used only when the toolchain is the very
Lean that Blake was built with (its `Init.olean` records Blake's `Lean.githash`);
otherwise, or if anything goes wrong, Lake runs `lean` as before.
-/

private abbrev ZygoteChild :=
  IO.Process.Child {stdin := .piped, stdout := .piped, stderr := .inherit}

/-
Modified for Blake (2026-10-30, `one-zygote`): one zygote per build serves all compiles,
which it runs concurrently in separate children; requests carry an id and replies come
back as `<id> <exit code>` lines, read by one reader task.
-/

private structure Zygote where
  child : ZygoteChild
  /-- Serializes writes of requests. -/
  writeLock : Std.BaseMutex
  /-- Compiles waiting for their reply, by request id. -/
  pending : IO.Ref (Std.HashMap Nat (IO.Promise (Option Nat)))
  nextId : IO.Ref Nat
  /-- Set by the reader once the zygote's output has ended. -/
  dead : IO.Ref Bool
  /--
  Signaled (under `replyLock`) whenever a reply has been delivered. Compiles wait on this
  OS-level condition variable rather than with `IO.wait`: a worker blocked in `IO.wait`
  makes Lean's task manager start another worker, which would let more compiles run at
  once than Lake's job limit allows.
  -/
  replyLock : Std.BaseMutex
  replied : Std.Condvar
  reader : Task (Except IO.Error Unit)

/-- The build's zygote, if started. -/
private builtin_initialize zygoteRef : IO.Ref (Option Zygote) ← IO.mkRef none

/-- Serializes starting and stopping the zygote. -/
private builtin_initialize zygoteLock : Std.BaseMutex ← Std.BaseMutex.new

/-- The `blake-zygote` to use for the `lean` at `lean`, if any. -/
private def zygoteFor? (lean : FilePath) : BaseIO (Option FilePath) := do
  let .ok appPath ← IO.appPath.toBaseIO | return none
  let some appDir := appPath.parent | return none
  let zygote := appDir / "blake-zygote"
  -- An empty file is the build's placeholder for a zygote that could not be built.
  let .ok md ← zygote.metadata.toBaseIO | return none
  unless md.byteSize > 0 do return none
  let some sysroot := lean.parent >>= (·.parent) | return none
  unless (← initOleanHasLeanGithash sysroot) do return none
  return some zygote

/-- Resolves the pending compiles from the zygote's replies; fails them all at the end. -/
private partial def readZygoteReplies
  (child : ZygoteChild) (pending : IO.Ref (Std.HashMap Nat (IO.Promise (Option Nat))))
  (dead : IO.Ref Bool) (replyLock : Std.BaseMutex) (replied : Std.Condvar)
: IO Unit := do
  try
    loop
  finally
    dead.set true
    for (_, p) in ← pending.modifyGet (·, {}) do
      p.resolve none
    notify
where
  notify : BaseIO Unit := do
    replyLock.lock
    replied.notifyAll
    replyLock.unlock
  loop : IO Unit := do
    let line ← child.stdout.getLine
    if line.isEmpty then return
    if let [id, code] := line.trimAscii.copy.splitOn " " then
      if let (some id, some code) := (id.toNat?, code.toNat?) then
        if let some p ← pending.modifyGet fun m => (m.get? id, m.erase id) then
          p.resolve (some code)
          notify
    loop

/-- The build's zygote, started (once) if needed. -/
private def getZygote (zygote : FilePath) : IO Zygote := do
  zygoteLock.lock
  try
    if let some z ← zygoteRef.get then
      return z
    let child ← IO.Process.spawn {cmd := zygote.toString, stdin := .piped, stdout := .piped, stderr := .inherit}
    let pending ← IO.mkRef {}
    let dead ← IO.mkRef false
    let replyLock ← Std.BaseMutex.new
    let replied ← Std.Condvar.new
    let reader ← IO.asTask (prio := .dedicated) (readZygoteReplies child pending dead replyLock replied)
    let writeLock ← Std.BaseMutex.new
    let nextId ← IO.mkRef 0
    let z : Zygote := {child, writeLock, pending, nextId, dead, replyLock, replied, reader}
    zygoteRef.set (some z)
    return z
  finally
    zygoteLock.unlock

/--
Compile with `blake-zygote`: `args` as for `lean`, with `LEAN_PATH` set to `leanPath`.
Returns `none` if the zygote failed (Lake then runs `lean` itself).
-/
private def runLeanZygote
  (zygote : FilePath) (args : Array String) (leanPath : String)
: IO (Option IO.Process.Output) := do
  let z ← getZygote zygote
  let id ← z.nextId.modifyGet fun n => (n, n + 1)
  let promise ← IO.Promise.new
  z.pending.modify (·.insert id promise)
  if (← z.dead.get) then
    if let some p ← z.pending.modifyGet fun m => (m.get? id, m.erase id) then
      p.resolve none
  let (_, outFile) ← IO.FS.createTempFile
  let (_, errFile) ← IO.FS.createTempFile
  try
    let fields := #[toString id, outFile.toString, errFile.toString, leanPath, "lean"] ++ args
    let request := fields.foldl (init := s!"{fields.size}\x00") (· ++ · ++ "\x00")
    z.writeLock.lock
    try
      z.child.stdin.putStr request
      z.child.stdin.flush
    finally
      z.writeLock.unlock
    -- Wait on the condition variable, not with `IO.wait` (see `Zygote.replied`).
    z.replyLock.lock
    while !(← promise.isResolved) do
      z.replied.wait z.replyLock
    z.replyLock.unlock
    let some code := (← IO.wait promise.result?).join
      | return none
    return some {exitCode := code.toUInt32, stdout := ← IO.FS.readFile outFile, stderr := ← IO.FS.readFile errFile}
  finally
    try IO.FS.removeFile outFile catch _ => pure ()
    try IO.FS.removeFile errFile catch _ => pure ()

/--
Start the build's `blake-zygote` for the `lean` at `lean` ahead of time, so that its
initialization overlaps with whatever the build does before its first compile.
-/
public def prestartZygote (lean : FilePath) : BaseIO Unit := do
  let some zygote ← zygoteFor? lean | return
  discard (getZygote zygote).toBaseIO

/-
Modified for Blake (2026-10-31, `header-stub`): a module's header snapshot (see
`Module.headerSnapshot`) is saved by a separate `lean` run on a stub file that holds
only the module's header. It runs alongside the module's compile and does not hold up
the module's job; the build waits for it at its end (`stopZygotes`).
-/

/-- A header snapshot to save alongside a compile (see `Module.headerSnapshot`). -/
public structure HeaderSave where
  /-- The module's header: the text of its source up to its first command. -/
  header : String
  /-- The snapshot file; `lean` also writes `<snapshot>.deps`. -/
  snapshot : FilePath
  /-- Runs once the snapshot has been saved. -/
  onSaved : IO Unit

/-- The header saves of this build that may still be running. -/
private builtin_initialize headerSavesRef : IO.Ref (Array (Task (Except IO.Error Unit))) ←
  IO.mkRef #[]

/-- Runs `lean` without logging, through the zygote when possible; returns its exit code. -/
private def runLeanQuiet (lean : FilePath) (args : Array String) (leanPath : SearchPath) : IO UInt32 := do
  if let some zygote ← zygoteFor? lean then
    if let some sysroot := lean.parent >>= (·.parent) then
      if let some out ← runLeanZygote zygote args (leanPath ++ [sysroot / "lib" / "lean"]).toString then
        return out.exitCode
  let out ← IO.Process.output {cmd := lean.toString, args, env := #[("LEAN_PATH", leanPath.toString)]}
  return out.exitCode

/--
Starts saving a header snapshot in the background: `lean` with the module's arguments
and setup file runs on a stub holding the module's header (so its header, positions
included, is the module's, which is what `--incr-load` checks) and saves the snapshot.
-/
private def startHeaderSave
  (save : HeaderSave) (lean : FilePath) (leanArgs : Array String) (setupFile : FilePath)
  (leanPath : SearchPath)
: IO Unit := do
  let stub := save.snapshot.addExtension "lean"
  IO.FS.writeFile stub save.header
  let args := leanArgs ++ #[stub.toString, "--setup", setupFile.toString,
    s!"--incr-header-save={save.snapshot}", "--json"]
  let task ← IO.asTask (prio := .dedicated) do
    let ok ← try pure ((← runLeanQuiet lean args leanPath) == 0) catch _ => pure false
    removeFileIfExists stub
    if ok && (← save.snapshot.pathExists) then
      save.onSaved
    else
      removeFileIfExists save.snapshot
      removeFileIfExists (save.snapshot.addExtension "deps")
  headerSavesRef.modify (·.push task)

/-
Modified for Blake (2026-10-31, `spec-compile`): a module whose source was edited is
compiled speculatively as soon as the library's modules are known
(`LeanLib.recBuildLean`), before Lake has checked its imports, with the setup file of
its previous compile and its outputs written aside (`<file>.spec`). When Lake then
compiles it (`compileLeanModule`), it adopts that compile's outputs and messages only
if it would run the very same command with the very same setup file, and neither the
source nor any imported artifact listed in the setup has changed since the
speculation started (their modification times); otherwise it compiles as usual.
-/

private structure Speculation where
  /-- The arguments of the compile it stands for (the real output and setup paths). -/
  args : Array String
  /-- The setup file contents it ran with. -/
  setup : String
  leanPath : String
  srcFile : FilePath
  srcMTime : IO.FS.SystemTime
  /-- The imported artifacts named by the setup, with their modification times. -/
  inputs : Array (FilePath × IO.FS.SystemTime)
  /-- Its outputs: (where it wrote them, where they belong). -/
  outputs : Array (FilePath × FilePath)
  /-- Files to delete once it is no longer needed. -/
  scratch : Array FilePath
  task : Task (Except IO.Error (Option IO.Process.Output))

/-- Speculative compiles not yet claimed, by source file. -/
private builtin_initialize speculationsRef : IO.Ref (Std.HashMap String Speculation) ← IO.mkRef {}

/-- Speculative compiles claimed or discarded, to clean up at the end of the build. -/
private builtin_initialize spentSpeculationsRef : IO.Ref (Array Speculation) ← IO.mkRef #[]

private partial def jsonStrings (j : Lean.Json) (acc : Array String) : Array String :=
  match j with
  | .str s => acc.push s
  | .arr a => a.foldl (fun acc j => jsonStrings j acc) acc
  | .obj o => o.foldl (fun acc _ j => jsonStrings j acc) acc
  | _ => acc

/--
Starts a speculative compile of a non-`module` file with outputs `olean`, `ilean` and
`cFile`, from its previous setup file, unless one of its imports is in `edited` (module
names) or the setup cannot be used.
-/
public def speculateCompile
  (lean : FilePath) (leanPath : SearchPath) (leanFile setupFile olean ilean cFile : FilePath)
  (leanArgs : Array String) (incrLoad? : Option FilePath) (edited : Std.HashSet String)
: BaseIO Unit := do
  let some zygote ← zygoteFor? lean | return
  let some sysroot := lean.parent >>= (·.parent) | return
  let .ok setup ← (IO.FS.readFile setupFile).toBaseIO | return
  let .ok json := Lean.Json.parse setup | return
  let .ok false := json.getObjValAs? Bool "isModule" | return
  let .ok (.obj imports) := json.getObjVal? "importArts" | return
  if imports.any fun name _ => edited.contains name then return
  let .ok srcMd ← leanFile.metadata.toBaseIO | return
  let mut inputs := #[]
  for f in jsonStrings (.obj imports) #[] do
    let .ok md ← (FilePath.mk f).metadata.toBaseIO | return
    inputs := inputs.push (FilePath.mk f, md.modified)
  let tmp (f : FilePath) := f.addExtension "spec"
  let specSetup := tmp setupFile
  let .ok _ ← (IO.FS.writeFile specSetup setup).toBaseIO | return
  let incrArgs := (incrLoad?.map (s!"--incr-load={·}")).toArray
  let args := leanArgs ++ #[leanFile.toString, "-o", olean.toString, "-i", ilean.toString,
    "-c", cFile.toString, "--setup", setupFile.toString, "--json"] ++ incrArgs
  let specArgs := leanArgs ++ #[leanFile.toString, "-o", (tmp olean).toString,
    "-i", (tmp ilean).toString, "-c", (tmp cFile).toString, "--setup", specSetup.toString,
    "--json"] ++ incrArgs
  let task ← IO.asTask (prio := .dedicated) <|
    runLeanZygote zygote specArgs (leanPath ++ [sysroot / "lib" / "lean"]).toString
  speculationsRef.modify (·.insert leanFile.toString {
    args, setup, leanPath := leanPath.toString, srcFile := leanFile, srcMTime := srcMd.modified,
    inputs, task
    outputs := #[(tmp olean, olean), (tmp ilean, ilean), (tmp cFile, cFile)]
    scratch := #[tmp olean, tmp ilean, tmp cFile, specSetup]})

/-- Whether a speculative compile stands for a compile with these inputs. -/
private def Speculation.matches
  (s : Speculation) (args : Array String) (setup leanPath : String)
: BaseIO Bool := do
  if s.args != args || s.setup != setup || s.leanPath != leanPath then
    return false
  let .ok md ← s.srcFile.metadata.toBaseIO | return false
  unless md.modified == s.srcMTime do return false
  for (f, t) in s.inputs do
    let .ok md ← f.metadata.toBaseIO | return false
    unless md.modified == t do return false
  return true

/--
The output of a speculative compile of `leanFile` that stands for this compile, with
its outputs moved into place, if there is one.
-/
private def claimSpeculation
  (leanFile : FilePath) (args : Array String) (setup leanPath : String)
: BaseIO (Option IO.Process.Output) := do
  let some s ← speculationsRef.modifyGet fun m => (m.get? leanFile.toString, m.erase leanFile.toString)
    | return none
  spentSpeculationsRef.modify (·.push s)
  unless (← s.matches args setup leanPath) do return none
  let .ok (some out) ← IO.wait s.task | return none
  for (tmp, real) in s.outputs do
    if (← tmp.pathExists) then
      let .ok _ ← (IO.FS.rename tmp real).toBaseIO | return none
  return some out

/--
Stop the build's `blake-zygote` (at the end of a build; it may not outlive Lake),
after waiting for the header saves still running.
-/
public def stopZygotes : BaseIO Unit := do
  for t in ← headerSavesRef.modifyGet (·, #[]) do
    discard <| IO.wait t
  -- Blake (`spec-compile`): wait for speculative compiles and remove their leftovers.
  let specs := (← speculationsRef.modifyGet fun m => (m.toArray.map (·.2), {})) ++
    (← spentSpeculationsRef.modifyGet (·, #[]))
  for sp in specs do
    discard <| IO.wait sp.task
    for f in sp.scratch do
      discard <| (removeFileIfExists f).toBaseIO
  zygoteLock.lock
  let z? ← zygoteRef.modifyGet (·, none)
  zygoteLock.unlock
  let some z := z? | return
  let stop : IO Unit := do
    z.writeLock.lock
    try
      z.child.stdin.putStr "0\x00"
      z.child.stdin.flush
    finally
      z.writeLock.unlock
    discard <| IO.wait z.reader
    discard z.child.wait
  stop.catchExceptions fun _ => z.child.kill.catchExceptions fun _ => pure ()

public def compileLeanModule
  (leanFile relLeanFile : FilePath)
  (setup : ModuleSetup) (setupFile : FilePath)
  (arts : ModuleArtifacts)
  (leanArgs : Array String := #[])
  (leanPath : SearchPath := [])
  (lean : FilePath := "lean")
  (leanir : FilePath := "leanir")
  (incrLoad? : Option FilePath := none)
  (headerSave? : Option HeaderSave := none)
: LogIO Unit := do
  let mut args := leanArgs.push leanFile.toString
  if let some oleanFile := arts.olean? then
    createParentDirs oleanFile
    args := args ++ #["-o", oleanFile.toString]
  if let some ileanFile := arts.ilean? then
    createParentDirs ileanFile
    args := args ++ #["-i", ileanFile.toString]
  let opts := setup.options.toOptions
  let postponeCompile := setup.isModule && Compiler.compiler.postponeCompile.get opts
  if !postponeCompile then
    if let some cFile := arts.c? then
      createParentDirs cFile
      args := args ++ #["-c", cFile.toString]
  if let some bcFile := arts.bc? then
    createParentDirs bcFile
    args := args ++ #["-b", bcFile.toString]
  createParentDirs setupFile
  let setupJson := (toJson setup).pretty
  IO.FS.writeFile setupFile setupJson
  args := args ++ #["--setup", setupFile.toString]
  args := args.push "--json"
  withLogErrorPos do
  -- Blake: through `blake-zygote` when possible (see `zygoteFor?`).
  let run (args : Array String) : LogIO IO.Process.Output := do
    let spawnArgs : IO.Process.SpawnArgs := {
      args
      cmd := lean.toString
      env := #[
        ("LEAN_PATH", leanPath.toString)
      ]
    }
    if let some zygote ← zygoteFor? lean then
      if let some sysroot := lean.parent >>= (·.parent) then
        logVerbose (mkCmdLog spawnArgs)
        -- The zygote's built-in search path is relative to its own location, so the
        -- toolchain's library directory (which `lean` would add last) is added here.
        let zygoteLeanPath := (leanPath ++ [sysroot / "lib" / "lean"]).toString
        if let .ok (some out) ← (runLeanZygote zygote args zygoteLeanPath).toBaseIO then
          return out
        return ← rawProc spawnArgs (quiet := true)
    rawProc spawnArgs
  -- Modified for Blake (2026-10-31): Lean's header snapshots, for modules being edited
  -- (see `Module.headerSnapshot`).
  if let some save := headerSave? then
    startHeaderSave save lean leanArgs setupFile leanPath
  let incrArgs := (incrLoad?.map (s!"--incr-load={·}")).toArray
  -- Blake (`spec-compile`): a speculative compile of this very command, if any.
  let spec? ← if headerSave?.isSome then pure none else
    claimSpeculation leanFile (args ++ incrArgs) setupJson leanPath.toString
  let mut out ← match spec? with
    | some out => do
      logVerbose <| mkCmdLog {args := args ++ incrArgs, cmd := lean.toString, env := #[("LEAN_PATH", leanPath.toString)]}
      pure out
    | none => run (args ++ incrArgs)
  if let some snap := incrLoad? then
    -- `lean` reports errors in the source as JSON on stdout and exits with code 1.
    -- Anything else means that the snapshot could not be used: compile without it.
    if out.exitCode ≠ 0 && (out.exitCode ≠ 1 || !out.stderr.isEmpty) then
      removeFileIfExists (snap.addExtension "key")
      removeFileIfExists snap
      removeFileIfExists (snap.addExtension "deps")
      out ← run args
  let outLogPos ← getLogPos
  unless out.stdout.isEmpty do
    let txt ← out.stdout.split '\n' |>.foldM (init := "") fun (txt : String) ln => do
      let ln := ln.copy
      if let .ok (msg : SerialMessage) := Json.parse ln >>= fromJson? then
        unless txt.isEmpty do
          logInfo s!"stdout:\n{txt}"
        let msg := {msg with fileName := mkRelPathString relLeanFile}
        logSerialMessage msg
        return txt
      else if txt.isEmpty && ln.isEmpty then
        return txt
      else
        return txt ++ ln ++ "\n"
    unless txt.isEmpty do
      logInfo s!"stdout:\n{txt}"
  unless out.stderr.isEmpty do
    logInfo s!"stderr:\n{out.stderr.trimAscii}"
  -- Elide the generic "Lean exited with code 1" when Lean already
  -- reported errors (the usual compiler-failure case). Keep it for other
  -- nonzero codes, for code 1 without diagnostics, and for the anomalous
  -- case where Lean logs errors but exits successfully.
  -- See https://github.com/leanprover/lean4/issues/10825
  let hasErrors := (← getLog).takeFrom outLogPos |>.any (·.level matches .error)
  if out.exitCode = 1 && hasErrors then
    failure
  else if out.exitCode ≠ 0 || hasErrors then
    error s!"Lean exited with code {out.exitCode}"
  if postponeCompile then
    if let (some irFile, some cFile) := (arts.ir?, arts.c?) then
      createParentDirs irFile
      createParentDirs cFile
      try
        proc {
          cmd := leanir.toString
          args := #[setupFile.toString, irFile.toString, cFile.toString]
          env := #[
            ("LEAN_PATH", leanPath.toString)
          ]
        }
      catch e =>
        if let some oleanFile := arts.olean? then
          removeFileIfExists oleanFile
        throw e

public def compileO
  (oFile srcFile : FilePath)
  (moreArgs : Array String := #[]) (compiler : FilePath := "cc")
: LogIO Unit := do
  createParentDirs oFile
  proc {
    cmd := compiler.toString
    args := #["-c", "-o", oFile.toString, srcFile.toString] ++ moreArgs
  }

public def mkArgs (basePath : FilePath) (args : Array String) : LogIO (Array String) := do
  -- Use response file to avoid potentially exceeding CLI length limits.
  -- On Windows this is always needed; on macOS/Linux this is needed for large
  -- projects like Mathlib where the number of object files exceeds ARG_MAX.
  let rspFile := basePath.addExtension "rsp"
  let h ← IO.FS.Handle.mk rspFile .write
  args.forM fun arg =>
    -- Escape special characters
    let arg := arg.foldl (init := "") fun s c =>
      if c == '\\' || c == '"' then
        s.push '\\' |>.push c
      else
        s.push c
    h.putStr s!"\"{arg}\"\n"
  return #[s!"@{rspFile}"]

public def compileStaticLib
  (libFile : FilePath) (oFiles : Array FilePath)
  (ar : FilePath := "ar") (thin := false)
: LogIO Unit := do
  createParentDirs libFile
  -- `ar rcs` does not remove old files from the archive, so it must be deleted first
  removeFileIfExists libFile
  let args := #["rcs"]
  let args := if thin then args.push "--thin" else args
  let args := args.push libFile.toString ++ (← mkArgs libFile <| oFiles.map toString)
  proc {cmd := ar.toString, args}

public def compileSharedLib
  (libFile : FilePath) (linkArgs : Array String)
  (linker : FilePath := "cc") (macosxDeploymentTarget? : Option String := none)
: LogIO Unit := do
  createParentDirs libFile
  proc {
    cmd := linker.toString
    args := #["-shared", "-o", libFile.toString] ++ (← mkArgs libFile linkArgs)
    -- See `BuildConfig.macosxDeploymentTarget?` for details
    env := macosxDeploymentTarget?.elim #[] fun ver => #[("MACOSX_DEPLOYMENT_TARGET", some ver)]
  }

public def compileExe
  (binFile : FilePath) (linkArgs : Array String)
  (linker : FilePath := "cc") (macosxDeploymentTarget? : Option String := none)
: LogIO Unit := do
  createParentDirs binFile
  proc {
    cmd := linker.toString
    args := #["-o", binFile.toString] ++ (← mkArgs binFile linkArgs)
    -- See `BuildConfig.macosxDeploymentTarget?` for details
    env :=  macosxDeploymentTarget?.elim #[] fun ver => #[("MACOSX_DEPLOYMENT_TARGET", some ver)]
  }

/-- Download a file using `curl`, clobbering any existing file. -/
public def download
  (url : String) (file : FilePath) (headers : Array String := #[])
: LogIO PUnit := do
  if (← file.pathExists) then
    IO.FS.removeFile file
  else
    createParentDirs file
  let args := #["-s", "-S", "-f", "-o", file.toString, "-L", url]
  let args := headers.foldl (init := args) (· ++ #["-H", ·])
  proc (quiet := true) {cmd := ← Internal.getCurl, args}

/-- Unpack an archive `file` using `tar` into the directory `dir`. -/
public def untar (file : FilePath) (dir : FilePath) (gzip := true) : LogIO PUnit := do
  IO.FS.createDirAll dir
  let mut opts := "-xvv"
  if gzip then
    opts := opts.push 'z'
  proc (quiet := true) {
    cmd := "tar",
    args := #[opts, "-f", file.toString, "-C", dir.toString]
  }

/-- Pack a directory `dir` using `tar` into the archive `file`. -/
public def tar
  (dir : FilePath) (file : FilePath)
  (gzip := true) (excludePaths : Array FilePath := #[])
: LogIO PUnit := do
  createParentDirs file
  let mut args := #["-cvv"]
  if gzip then
    args := args.push "-z"
  for path in excludePaths do
    args := args.push s!"--exclude={path}"
  proc (quiet := true) {
    cmd := "tar"
    args := args ++ #["-f", file.toString, "-C", dir.toString, "."]
    -- don't pack `._` files on MacOS
    env := if Platform.isOSX then #[("COPYFILE_DISABLE", "true")] else #[]
  }
