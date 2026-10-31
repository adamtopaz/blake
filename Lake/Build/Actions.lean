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
  (dead : IO.Ref Bool)
: IO Unit := do
  try
    loop
  finally
    dead.set true
    for (_, p) in ← pending.modifyGet (·, {}) do
      p.resolve none
where
  loop : IO Unit := do
    let line ← child.stdout.getLine
    if line.isEmpty then return
    if let [id, code] := line.trimAscii.copy.splitOn " " then
      if let (some id, some code) := (id.toNat?, code.toNat?) then
        if let some p ← pending.modifyGet fun m => (m.get? id, m.erase id) then
          p.resolve (some code)
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
    let reader ← IO.asTask (prio := .dedicated) (readZygoteReplies child pending dead)
    let z := {child, writeLock := ← Std.BaseMutex.new, pending, nextId := ← IO.mkRef 0, dead, reader}
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

/-- Stop the build's `blake-zygote` (at the end of a build; it may not outlive Lake). -/
public def stopZygotes : BaseIO Unit := do
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
  IO.FS.writeFile setupFile (toJson setup).pretty
  args := args ++ #["--setup", setupFile.toString]
  args := args.push "--json"
  withLogErrorPos do
  let spawnArgs : IO.Process.SpawnArgs := {
    args
    cmd := lean.toString
    env := #[
      ("LEAN_PATH", leanPath.toString)
    ]
  }
  -- Blake: through `blake-zygote` when possible (see `zygoteFor?`).
  let out ← id do
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
