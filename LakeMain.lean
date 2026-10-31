/-
Copyright (c) 2017 Microsoft Corporation. All rights reserved.
Released under Apache 2.0 license as described in the file LICENSE.
Authors: Gabriel Ebner, Sebastian Ullrich, Mac Malone
-/
module

prelude
public import Init.System.IO
import Lake.DSL -- shake: keep (registers builtins)
import Lake.CLI.Main
import Lake.Util.FullInit

/-- Blake (2026-10-31, `lazy-init`): see `c/blake_init.c`, linked into this executable. -/
@[extern "blake_full_initialize"]
opaque blakeFullInitialize : BaseIO Unit

/-- Modified for Blake (2026-10-31): sets `Lake.fullInitializeRef`. -/
public def main (args : List String) : IO UInt32 := do
  Lake.fullInitializeRef.set blakeFullInitialize
  Lake.cli args
