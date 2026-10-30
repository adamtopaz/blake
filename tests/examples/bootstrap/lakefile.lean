-- Modified for Blake (2026-10-29): Lake's sources live at the repository root
-- rather than under `src/lake`.
import Lake
open System Lake DSL

package lake where
  srcDir := ".." / ".." / ".."

lean_lib Lake

@[default_target]
lean_exe lake where
  root := `LakeMain
  supportInterpreter := true
