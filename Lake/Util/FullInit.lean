/-
Copyright (c) 2026 Adam Topaz. All rights reserved.
Released under Apache 2.0 license as described in the file LICENSE.
Authors: Adam Topaz
-/
module

prelude
public import Init.System.IO

namespace Lake

/--
Blake (2026-10-31, `lazy-init`): what `fullInitialize` runs. The `lake` executable
sets it (`LakeMain`); elsewhere it does nothing.
-/
public builtin_initialize fullInitializeRef : IO.Ref (BaseIO Unit) ← IO.mkRef (pure ())

/--
Blake (2026-10-31, `lazy-init`): finishes initializing Lean. On Linux the `lake`
executable starts with only the parts of Lean that Lake's own imports need, which is
enough to build a workspace configured in TOML (see `c/blake_init.c`). This must run
before anything that may need the rest: commands other than `build`, and loading a
configuration written in Lean. It does nothing after the first call.
-/
public def fullInitialize : BaseIO Unit := do
  (← fullInitializeRef.get)
