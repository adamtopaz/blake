/-
Copyright (c) 2022 Mac Malone. All rights reserved.
Released under Apache 2.0 license as described in the file LICENSE.
Authors: Mac Malone
-/
module

prelude
public import Lake.Util.Store
public import Lake.Build.Job.Basic

/-!
# The Lake Build Store

The Lake build store is the map of Lake build keys to build task and/or
build results that is slowly filled during a recursive build (e.g., via
topological-based build of an initial key's dependencies).
-/

namespace Lake
open Lean (Name NameMap)

/-- A monad equipped with a Lake build store. -/
public abbrev MonadBuildStore (m) := MonadDStore BuildKey (Job <| BuildData ·) m

/-- An entry of a bucket of a `BuildStore`. -/
public structure BuildStore.Entry where
  hash : UInt64
  key : BuildKey
  job : Job (BuildData key)

/--
The type of the Lake build store.

Modified for Blake (2026-10-31, `bucket-store`): this used to be just `map`. The store
is shared by the threads of a build, so it lives in a reference that job tasks capture,
which marks it, and every map stored into it, as multi-threaded. Multi-threaded objects
are never exclusive, so every insertion into the persistent map copied its whole path
(~14 nodes for the ~10 jobs per module of a 1000-module library), marked the copies
multi-threaded, and freed the old path with atomic reference counting, all on the
build's critical path (the fetch thread) and while other threads waited for the
reference. A store made by `BuildStore.new` (as every Lake build's is, see
`RecBuildT.run'`) instead keeps its entries in `buckets`, a fixed array of references to
lists, indexed by the key's hash: an insertion conses one cell onto one bucket.
A store made without buckets (`{}`) behaves as before.
-/
public structure BuildStore where
  map : Std.DTreeMap BuildKey (Job <| BuildData ·) BuildKey.quickCmp := {}
  buckets : Array (IO.Ref (List BuildStore.Entry)) := #[]

@[inline] public def BuildStore.empty : BuildStore := {}

public instance : EmptyCollection BuildStore := ⟨BuildStore.empty⟩

/-- A store with hash buckets (see `BuildStore`). -/
public def BuildStore.new (numBuckets := 4096) : ST IO.RealWorld BuildStore := do
  return {buckets := ← (Array.replicate numBuckets ()).mapM fun _ => ST.mkRef []}

namespace BuildStore

/-- Finds the job of `key` (whose hash is `h`) in a bucket. -/
public def findEntry? (key : BuildKey) (h : UInt64) : List Entry → Option (Job (BuildData key))
  | [] => none
  | e :: es =>
    if e.hash == h then
      if heq : e.key = key then some (heq ▸ e.job) else findEntry? key h es
    else findEntry? key h es

end BuildStore

public instance [MonadLiftT (ST IO.RealWorld) m] [Monad m] :
  MonadDStore BuildKey (Job <| BuildData ·) (StateRefT' IO.RealWorld BuildStore m)
where
  fetch? key := do
    let s ← get
    if h : 0 < s.buckets.size then
      let hash := hash key
      let bucket := s.buckets[hash.toNat % s.buckets.size]'(Nat.mod_lt _ h)
      return BuildStore.findEntry? key hash (← (bucket.get : ST IO.RealWorld _))
    else
      return s.map.get? key
  store key job := do
    let s ← get
    if h : 0 < s.buckets.size then
      let hash := hash key
      let bucket := s.buckets[hash.toNat % s.buckets.size]'(Nat.mod_lt _ h)
      (bucket.modify ({hash, key, job} :: ·) : ST IO.RealWorld _)
    else
      modify fun s => {s with map := s.map.insert key job}

namespace BuildStore

set_option linter.deprecated false in
def getModuleFacetJob? (facet : Name) [FamilyOut FacetOut facet α]
    (k : BuildKey) (v : Job (BuildData k)) : Option (Name × Job α) :=
  match k with
  | .moduleFacet m f
  | .packageModuleFacet p m f =>
    if h : f = facet then
      have of_data := by unfold BuildData; simp [h]
      some (m, cast of_data v)
    else none
  | _ => none

set_option linter.deprecated false in
/-- Derive an array of built module facets from the store. -/
@[deprecated "Deprecated without replacement." (since := "2025-11-13")]
public def collectModuleFacetArray
  (self : BuildStore) (facet : Name) [FamilyOut FacetOut facet α]
: Array (Job α) := Id.run do
  let mut res : Array (Job α) := #[]
  for ⟨k, v⟩ in self.map do
    if let some (_, job) := getModuleFacetJob? facet k v then
      res := res.push job
  return res

set_option linter.deprecated false in
/-- Derive a map of module names to built facets from the store. -/
@[deprecated "Deprecated without replacement." (since := "2025-11-13")]
public def collectModuleFacetMap
  (self : BuildStore) (facet : Name) [FamilyOut FacetOut facet α]
: NameMap (Job α) := Id.run do
  let mut res := Lean.mkNameMap (Job α)
  for ⟨k, v⟩ in self.map do
    if let some (m, job) := getModuleFacetJob? facet k v then
      res := res.insert m job
  return res

def getPackageFacetJob? (facet : Name) [FamilyOut FacetOut facet α]
    (k : BuildKey) (v : Job (BuildData k)) : Option (Job α) :=
  match k with
  | .packageFacet p f =>
    if h : f = facet then
      have of_data := by unfold BuildData; simp [h]
      some (cast of_data v)
    else none
  | _ => none

/-- Derive an array of built package facets from the store. -/
@[deprecated "Deprecated without replacement." (since := "2025-11-13")]
public def collectPackageFacetArray
  (self : BuildStore) (facet : Name) [FamilyOut FacetOut facet α]
: Array (Job α) := Id.run do
  let mut res : Array (Job α) := #[]
  for ⟨k, v⟩ in self.map do
    if let some job := getPackageFacetJob? facet k v then
      res := res.push job
  return res

def getTargetFacetJob? (facet : Name) [FamilyOut FacetOut facet α]
    (k : BuildKey) (v : Job (BuildData k)) : Option (Job α) :=
  match k with
  | .targetFacet _ _ f =>
    if h : f = facet then
      have of_data := by unfold BuildData; simp [h]
      some (cast of_data v)
    else none
  | _ => none

/-- Derive an array of built target facets from the store. -/
@[deprecated "Deprecated without replacement." (since := "2025-11-13")]
public def collectTargetFacetArray
  (self : BuildStore) (facet : Name) [FamilyOut FacetOut facet α]
: Array (Job α) := Id.run do
  let mut res : Array (Job α) := #[]
  for ⟨k, v⟩ in self.map do
    if let some job := getTargetFacetJob? facet k v then
      res := res.push job
  return res

set_option linter.deprecated false in
/-- Derive an array of built external shared libraries from the store. -/
@[deprecated "Deprecated without replacement." (since := "2025-11-13")]
public def collectSharedExternLibs
  (self : BuildStore) [FamilyOut FacetOut `externLib.shared α]
: Array (Job α) := self.collectTargetFacetArray `externLib.shared
