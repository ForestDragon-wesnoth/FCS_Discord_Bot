# Audit history

The narratives of the hands-on audit passes (1-36), moved out of CLAUDE.md
verbatim when it became a subsystem reference. The lasting rules these passes
produced live in CLAUDE.md (each subsystem's invariants, the audit toolkit in
§5); this file keeps the detail: what was swept, what was verified clean, the
measurements, and the harness mistakes made along the way. Scenario numbers are
as of the pass. Read it when auditing an area, to see what was already covered.
Pass 1 predates this file and left no entry.

## Pass 2
- **Audit-pass-2 bug fixes (scenarios 468-473).** A second interaction-bug
  sweep (zones / fog-LOS / movement subsystems). Fixed:
  - **`move_group_dirs` now validates the whole footprint + blocking** (was
    anchor-only). Phase 1 checks every swept-footprint cell for bounds + the
    `block_walk` condition each step, and the FINAL footprint for occupancy
    (footprint-aware `cell_occupant`, fellow group members treated as
    transparent via `_occupancy_ignore(extra=...)`) — so a multi-tile member
    can't march off-grid / onto another body's non-anchor cells, and a group
    can't walk through an impassable tile/zone/corpse. Mirrors the
    single-entity `Entity.move_dirs` contract. Scenarios 468-469.
  - **LOS-only vision casts from the whole body** (`_entity_has_los` /
    `_team_has_los`). They used the anchor cell only, disagreeing with
    `_member_sees` (which checks every footprint cell) — so a large viewer's
    `can_see_losonly` / `team_sees_cell_losonly` was wrong. Now ANY footprint
    cell with a clear line counts. Scenario 470.
  - **`_restamp_parts_for` carries a part's auras + sub-parts.** It snapped a
    part's position but never re-stamped that part's anchored aura or its own
    sub-parts (a part-of-a-part) — same class as the nested-mount bug fixed in
    #80. Now walks the whole part subtree (BFS via `entity_part_subtree`,
    parents first) and re-stamps each moved part's auras. Scenario 471.
  - **`hidden_rider_grants_vision` rule (default False).** A hidden rider
    (passenger in a region-less slot) was excluded from being SEEN but still
    contributed to its team's vision/fog — an asymmetry. Now gated by the rule
    via the shared `_vision_member_ok` (used by `_team_sees` / `_team_has_los`
    / `_record_vision`); default off = symmetric (a passenger grants no
    sight). An explicit per-entity `can_see(<rider>,...)` is unaffected.
    Scenario 472.
  - **`resize_grid` shifts `channel_views`.** Resize repositions all
    coordinate-bearing content by the anchor offset but had missed the
    per-channel viewport CAMERA (added after resize was written), so a
    center/edge-anchored resize left the camera framing the wrong region. Now
    offset like everything else (resolve_viewport re-clamps on read).
    Scenario 473.

## Pass 3
- **Audit-pass-3 fix: attached parts share the parent's TURN CLOCK (scenarios
  474-477).** Three per-unit "clocks" iterated only `turn_order` members (round)
  or the active entity (turn). Attached parts carry no initiative (excluded from
  turn_order), so a glued/region/located part's statuses, turn/round passives,
  and turn-scheduled effects NEVER fired — silently contradicting the doc
  ("parts tick normally; a part's tick can `damage_part(self,n)` to route to
  main"). Fix shape (user-approved): a part rides its parent's clock. The shared
  `Match._attached_tick_parts(base_targets)` BFS-walks each base target's part
  subtree and returns the parts that LACK independent initiative, STOPPING
  descent at an independent part (it's its own target and ticks on its own turn,
  carrying its own sub-parts) — deduped so a deep part isn't double-counted and
  an independent part reached via both its own turn-order slot and its parent
  isn't double-ticked. Wired into all three clocks:
  - **Statuses:** `fire_status_tick` appends the helper's parts to its targets.
    Each part's own definition `tick_when` still gates whether it fires.
  - **Turn/round passives:** `fire_hook` gained an `own_only_targets` param —
    those ids fire ONLY their entity-owned passives, NOT match-wide globals or
    team passives (which already fired once per acting unit, so they must not
    re-run per part). The six `on_turn_*`/`on_round_*` calls in `next_turn` /
    `_advance_index` pass `own_only_targets=self._attached_tick_parts(...)`.
  - **Turn-scheduled effects:** the two `fire_scheduled_turn(cur/new_cur)` sites
    also call it for each attached part.
  So a DoT/regen/bleed on a limb both lives on the limb and (via
  `damage_part(self,n)`) can bleed into the main body. (Also re-audited and
  found correct: `Match.to_dict`/`from_dict` round-trips every persistent field
  — `pending_requests` is intentionally runtime-only — and push/pull/swap are
  footprint-aware for multi-tile bodies.)

- **Audit-pass-3 fix: action rollback preserves ALL runtime-only state
  (scenario 478).** `action._rollback_match` restores a failed action's
  transaction by rebuilding the Match from the pre-state snapshot and copying
  its fields back, then re-applying a curated list of runtime-only (underscore)
  fields the snapshot doesn't carry. That list had gone STALE — it missed the
  event-bus fields (`_event_stack`/`_event_depth`/`_event_warned`/
  `_event_warnings`) and others (`_summon_count`, `_death_processing`,
  `_death_check_suppressed_ids`, `_alive_eval_depth`, `_vision_memo`,
  `_turn_order_dirty`, `_request_seq`, `pending_requests`). The headline crash:
  an action that `emit()`s an event whose handler runs a FAILING sub-action —
  the sub-action's rollback wiped the LIVE `_event_stack` (holding the outer
  emit's frame), so the handler's next `event_get` and the emit's own cleanup
  hit "pop from empty list", crashing the whole outer action. Fix: preserve the
  COMPLETE set of runtime fields (rollback only restores SERIALIZED state;
  transient in-flight state — the emit stack, summon budget, etc. — must
  survive). The list must stay in sync with Match's underscore fields; a
  `hasattr` guard makes a future-missing name a no-op rather than a crash.
  COMPANION fix in `run_action`: because `_summon_count` is now PRESERVED across
  a rollback, the choice-REPLAY loop (which rolls back + re-runs the body per
  interactive `choose`) resets it to the action-start value each attempt —
  otherwise a summon-before-`choose` would accumulate the per-command summon
  budget across replays and falsely hit `summon_event_limit`. Snapshotted
  alongside the existing per-attempt RNG/cursor/buffer resets.

- **Audit-pass-3 fix: a turn_end tick that empties the turn order no longer
  crashes next_turn (scenario 479).** PRE-EXISTING (parts-independent): if a
  `turn_end`/round hook or status tick removed the LAST entity in `turn_order`
  (e.g. a lethal DoT on the only combatant — now also reachable via a part tick
  routing `damage_part` to its vital parent), `next_turn` then computed
  `(active_index + 1) % len(turn_order)` against an empty order →
  ZeroDivisionError (surfacing as a 💥). Guarded every point a hook/tick can
  empty the order: `_advance_index` bails if `turn_order` is empty; `next_turn`
  returns `(None, log)` after the opening round_start, after `turn_end` hooks,
  and after `_advance_index`'s round-wrap ticks; `_skip_to_eligible` stops on an
  emptied order and clamps a stale `active_index`. A two-combatant table where
  one self-kills still advances cleanly to the survivor.

- **Audit-pass-3 enhancement: `!mod show` flags unrecognized modifier ops
  (scenario 480).** The fold (`_apply_modifier_op`) treats an op outside the
  recognized set (`add`/`inc%`/`more%`/`set`/`min`/`max`, now the module
  constant `MODIFIER_OPS`) as a lenient ADD — convenient, but it silently
  swallows a typo like `inc` for `inc%` (a flat +N instead of a %). Behavior is
  UNCHANGED (still lenient-add, so no existing match breaks); `!mod show` now
  marks any such line with ⚠️ and appends an advisory naming the bad op(s) +
  the valid set, via `Match.unknown_modifier_ops(mods)`. Read-only diagnostic
  surface only — the fold itself doesn't warn (no clean channel mid-formula).

## Pass 4
- **Audit-pass-4 fixes: multi-tile interaction sweep (scenarios 491-492).** A
  fourth interaction-bug sweep, this time hunting anchor-only assumptions in
  OLDER features against multi-tile entities (three read-only survey agents
  across zones/auras/tiles, vision/LOS/targeting, and AoE/spawn/corpse/mount;
  every flagged candidate verified in code before fixing). Two real bugs found
  + fixed; the rest of the surface re-confirmed footprint-correct.
  - **`move_group_dirs` fired tile/zone movement hooks at the ANCHOR cell
    only.** Group movement (`!ent move group:<name> ...`) validated the whole
    swept footprint (audit-pass-2) but then fired `on_enter`/`on_exit`/`on_stop`
    via the anchor-only `fire_tile_hook`/`fire_zone_*_hooks` instead of the
    footprint-aware `fire_footprint_tile_*`/`fire_footprint_zone_*` that
    `Entity.move_dirs` uses — contradicting its own docstring ("per intermediate
    tile … same as single-entity move_dirs"). So a multi-tile group member
    crossing a hazard band / zone edge under-fired hooks (a 2×2 walking over a
    damage strip burned once, not per covered cell). Now mirrors `move_dirs`
    exactly (per-step `old_cells`/`new_cells` via `entity_cells`); byte-identical
    for a 1×1 member, correct for a footprint. Group move ALSO now fires the
    per-step `on_entity_step` hook (after each cell's `on_enter`) that
    single-entity `move_dirs` fires — a pre-existing, footprint-independent
    parity gap (per-cell reactions + snake-trail follow now work under group
    move). Scenario 493.
  - **`entities_in_area(x, y, n)` measured distance to the ANCHOR cell.** The
    coord-rooted twin of `entities_within` used `_distance(x, y, e.x, e.y, mode)`
    while `entities_within` had been refactored to the footprint-aware
    nearest-cell gap — so a large body partly inside an AoE radius was wrongly
    excluded (a 4×4 at (10,10) missed a blast at (14,14) r2 because the anchor
    was 4 away though a corner cell was 1 away). Now routes through
    `Match.cell_entity_distance(x, y, e, mode)` (the point-vs-footprint gap added
    with the `within:` find predicate), so the entity- and coord-rooted area
    queries agree.
  - **Re-verified footprint-correct (no change needed), so future sweeps can
    skip them:** all vision/fog/LOS casts (`_member_sees`, `_entity_has_los`,
    `_team_sees_entity`, `_record_vision`, `entity_visible_to` — union/any-cell),
    targeting geometry (`side_hit`/`hit_location`/`directional_get` box-face
    hitbox, `entity_center`/`aoe_origin`), the spatial/LOS enumerators
    (`entities_within`/`nearest_entity` via `entity_gap_distance`,
    `entities_in_cone`/`_rect`/`_line_ignorelos`/`_on_los`/`_line_until` via
    `_alive_at`/`_occupants` any-cell), `damage_spread` spatial filtering,
    `chain_targets`, `summon_near`/`_find_free_cell_near` (whole footprint
    validated, defaults applied first), corpses (`corpse_cells`/`revive`),
    mounts (`_find_dismount_cell`/`_restamp_riders_for`), `resize_grid` (cut if
    ANY cell off-grid), and single-entity `tp`/`move_dirs`/push/pull/swap.

## Pass 5
- **Audit-pass-5 fixes: general correctness sweep (scenarios 494-495).** A
  broader bug hunt (NOT multi-tile-scoped — four read-only survey agents across
  serialization/undo, status/modifier/event, death/corpse/transform/mount, and
  action/formula/dispatch; every candidate verified in code before fixing). Two
  real bugs fixed:
  - **`Entity.to_dict` shallow-copied `vars` → corrupted undo + action
    rollback for nested vars.** `to_dict` did `"vars": dict(self.vars)` (shallow)
    while the sibling `status` was `deepcopy`'d. Entity vars hold nested dicts
    (`inventory`, `modifiers`, …) that `_set_path` mutates IN PLACE, so a command
    snapshot SHARED the live nested objects; a later dotted-path write then
    corrupted the snapshot, and `!history undo` / transactional action rollback
    restored the wrong (mutated) value (a nested var could even vanish entirely).
    Fixed to `copy.deepcopy(self.vars)`. This is the snapshot path behind BOTH
    undo and `action._rollback_match` (both go through `Match.to_dict` →
    `Entity.to_dict`), so it fixes both at once. Footprint-INDEPENDENT — a
    long-standing latent bug any nested-var undo would hit.
  - **`_restamp_parts_for` didn't carry a moved part's RIDERS.** When a parent
    moved, the part-restamp synced each glued part's anchor + its anchored auras
    (`_restamp_anchors_for`) but never `_restamp_riders_for(part)`, so a body
    part that is ALSO a vehicle left its riders behind (same class as the
    nested-mount bug #80, but for a part-vehicle). Added the symmetric
    `_restamp_riders_for(e.id)` call (recursion stays bounded — part subtree is
    acyclic, mount cycles are can_mount-guarded).
  - **Re-verified correct (no change):** Match/zone serialization round-trips all
    persistent fields; `action._rollback_match`'s runtime-field list is complete
    (audit-pass-3); modifier fold min/max ops; event-stack preservation across
    nested-action rollback; status counter auto-removal; formula sandbox
    `_who_arg` HOOK_CONTEXT handling + `normalize_body_source` at every body
    boundary; dispatch gate (no batch/foreach/macro/action bypass).
  - **OPEN QUESTION raised with the user → RESOLVED (status resistance +
    `add_level`).** The old gate `(name not in e.status or new_level is not
    None)` let an implicit +1 (`!status apply x poison` with no level) on an
    already-present `add_level` status BYPASS resistance, while an explicit
    level was resisted — an asymmetry. The user's call: keep `apply_status`
    consistently resistance-aware AND add a SEPARATE force primitive that
    ignores resistance (resistance stays LEVEL-only — no duration channel).
    Shipped (scenarios 496-497):
    - **Resistance is now mode-aware** via `Match._resistance_applies(e, name,
      sdef, level_given)`: a flat level-reduction resistance applies only when
      a level is actually added/set — a FIRST application, an `add_level`
      increment (implicit +1 OR explicit), or a `replace` with an explicit
      level. `refresh`/`extend`/`none` set no level, so resistance no longer
      touches them (fixes BOTH the implicit-+1 bypass AND a previously-possible
      bug where an explicit level on a `refresh` ran the resistance gate and
      could no-op the duration refresh). So an implicit `add_level` +1 with
      resist≥1 is now fully resisted (consistent); `status_apply_block_reason`
      shares the same helper so command feedback matches.
    - **`force` path** — `apply_status(..., force=True)` skips the immunity +
      resistance gating entirely (the level/increment lands regardless);
      cross-status `blocked_by` and the part immune/redirect rules are STILL
      honored (force is specifically the "ignore resistance" axis, not a
      bypass-everything hammer). Surfaced as the `status_force(eid, name[,
      level, duration])` formula primitive (twin of `status_apply`) and the
      `!status force <eid> <name> [level] [duration]` command (host-gated like
      apply; reply reads "Force-applied").

## Pass 6
- **Audit-pass-6 fixes: cross-subsystem correctness sweep (scenarios 498-506).**
  A sixth bug hunt (four read-only survey agents across transform/disguise,
  dispatch/foreach/macro/watcher/undo, dice/modifier/shield numerics, and
  mounts/vehicles; every candidate verified in code before fixing). Nine real
  bugs fixed, three of which needed a user design call:
  - **Snapshot shallow-copy, tiles + zones (HIGH).** `Match.to_dict` stored each
    tile's data dict BY REFERENCE and `_zone_to_dict` stored a zone's
    `data`/`hooks` by reference (only `cells` was rebuilt). Since `tile_set_path`/
    `zone_set_path` (and the `tile_set`/`zone_set` primitives) mutate IN PLACE,
    an in-place `!tile set`/`!zone set` corrupted the prior command snapshot —
    defeating undo change-detection (pre==post → no snapshot) AND action
    rollback. Same class as the audit-pass-5 `Entity.to_dict` vars fix, missed
    for tiles/zones. Fixed with `copy.deepcopy` in both serializers (498-500).
  - **Segment `__follows` not remapped on id re-mint (MED-HIGH).** Both
    `_apply_statblock_parts` (transform/revert of a captured subtree) and
    `copy_entity` (cross-match copy/transfer) remapped `part_of` but NOT the
    snake-segment back-pointer `__follows`, so a 2+ segment snake lost its chain
    past the first link when re-minted under an id collision. Fixed by remapping
    `__follows` via the same idmap in both paths (505; verified under a forced
    collision — worm→worm_2, s1's `__follows` s0→s0_2).
  - **Dice `kh0` negative-zero slice (HIGH).** `dice[-0:]` is the WHOLE list in
    Python, so `roll("NdMkh0")` returned the full sum instead of 0 (the `kl`
    branch `dice[:0]` was fine). Guarded `k==0` explicitly (498).
  - **Macro / foreach substitution (MED/LOW).** `_macro_subst` re-expanded a
    token appearing INSIDE an arg value ($@ pass then positional pass) and
    mis-parsed `$10`+ (the `$1` prefix). `_foreach_subst` only guarded
    `$name`-contains-token, not an id/x/y value containing a later token. Both
    rewritten as a SINGLE-pass `re.sub` (501; macro now also supports $10+).
  - **Mount `slot` binding (MED, two sites).** The mount-action dispatch
    `extra_ctx` never set the documented `slot` binding (read as None), and
    `_eval_slot_expr` hard-wired `slot` to the rider's CURRENT mount_slot (None
    on a fresh mount, stale on a switch) instead of the slot being EVALUATED.
    Fixed: bind `slot` in the action ctx; thread the evaluated slot param through
    `_eval_slot_expr` from `slot_cost_of`/`slot_condition_ok` (506).
  - **chain_targets relation anchor (user call → ORIGIN).** `relation`
    (hostile/ally/…) was judged vs the PREVIOUS link each hop, so a `hostile`
    chain flipped allegiance (enemy→ally→enemy). Now judged vs the ORIGIN
    `from_eid` (distance still measured from the previous link), so chain
    lightning bounces among the caster's enemies (502).
  - **swap + mounts (user call → REDIRECT).** `swap_entities` had no mount guard.
    Now applies `_mount_move_redirect` to both participants: a driver (a
    controls_movement slot) redirects the swap to its VEHICLE (riders carried), a
    passenger raises "dismount first" — mirroring tp/move_dirs (503).
  - **transform of a vehicle with riders (user call → gamerule, default block).**
    Replacing a vehicle's `slots` var wholesale orphaned its riders. New rule
    `transform_rider_mismatch_mode` (enum block|eject, default `block`): if EVERY
    rider's slot still exists in the new form they stay mounted; otherwise block
    (refuse, raise before any change) or eject (dismount all, then transform).
    `_release_riders` gained an explicit `mode` override for the eject path (504).
  - Re-verified CORRECT (no change): disguise POV gating + fog/footprint
    interaction, HP carry modes, modifier fold + caps + tag/grants, shields/
    absorb priority + drain, band()/roll_table boundary + weight handling,
    damage_spread apportionment, watcher edge-trigger + serialization, the access
    gate (no batch/foreach/macro bypass), nested-mount carry + cycle guards,
    rider-death corpse strip, `_find_dismount_cell`.

## Pass 7
- **Audit-pass-7 fixes: load-side shallow copies, ghost passives, max_level
  ceiling (scenarios 507-511).** A seventh sweep (three read-only survey agents across
  status/passive/event, movement/geometry/LOS, action/choice/dispatch/clamp;
  every candidate verified in code — and most behaviorally repro'd — before
  fixing). Five real bugs + two user design calls:
  - **Load-side shallow-copy (HIGH).** The SAVE side was deepcopied in passes
    5/6 (`Entity.to_dict` vars, `Match.to_dict` tiles, `_zone_to_dict`), but the
    LOAD side still re-shared nested data with the RETAINED snapshot:
    `Entity.from_dict` did `vars=dict(...)`, `_coerce_status_dict` did `dict(v)`
    per status, and `Match.from_dict` reused each tile dict by reference
    (`m.tiles[(x,y)] = val`). Since `from_dict` runs on `!history restore` /
    undo / action rollback (the snapshot stays in history), a later in-place
    `!ent set_var inv.x` / `!tile set` corrupted the saved snapshot, so a second
    restore returned the mutated value. Fixed all three with `copy.deepcopy`
    (zones' `_zone_from_dict` already deepcopied `data` + rebuilt `hooks`, so it
    was already safe). Scenarios 507-508.
  - **Ghost passives (MED).** In `fire_status_event`, `fire_hook`, and
    `emit_event`, global/team handlers fire BEFORE the entity's own handlers. If
    a global/team handler removed the entity (kill/remove — in a status hook the
    affected entity is bound as `self`, NOT `target`), the own-handler loop still
    ran on the just-removed entity, firing side effects from beyond the grave.
    Added an existence re-check (`id in self.entities`) before each own-handler
    loop, mirroring the loops' existing top-of-iteration guard. Scenarios
    510-511.
  - **`!status apply` crash when a hook removes the target (MED, pre-existing).**
    The command handler did `e = m.entities[eid]` right after `apply_status`,
    KeyError'ing if a lifecycle hook (e.g. an on_status_added passive that kills
    the entity) removed it mid-apply. Now reports "Applied ... which was then
    removed by a triggered effect" instead of crashing. Surfaced by scenario 510.
  - **max_level hard ceiling (user call → cap everywhere).** `max_level` capped
    only the `add_level` stacking mode; a FIRST application or a `replace` with
    an explicit level above max was uncapped (`!status apply h burn 10` on a
    fresh max_level=3 → level 10). User's call: make max_level a hard ceiling on
    the level field EVERYWHERE. New `Match._cap_status_level(sdef, lvl)` helper
    applied at all three apply sites (first / replace / add_level). Scenario 509.
  - **LOS-on-opaque (user call → KEEP current, no code change).** For the
    sight-aware line queries (`entities_on_los`, `entities_in_line_until`), an
    entity standing ON the FIRST opaque cell (e.g. an enemy at the near edge of
    smoke) is RETURNED as visible/hittable; only entities BEYOND the opaque cell
    are cut. CONFIRMED INTENDED (consistent with `has_los`'s "the target's own
    opacity never blocks" convention — you can see/shoot something at the wall
    surface, not past it). Documented here so the ambiguity doesn't recur; do
    NOT "fix" it to exclude the on-opaque entity.
  - Re-verified correct (no change): choice-replay RNG snapshot/restore +
    summon-budget reset + buffer reset, the `action._rollback_match` runtime-
    field list (incl. event-stack), clamp/death-check ordering in `write_var`,
    the dispatch gate, status counter auto-removal + cross-status removes/
    blocked_by, resistance mode-awareness + the force path, attached-part tick
    sharing, and (re-confirmed footprint-correct) all vision/LOS casts, distance
    gaps, and movement validation.

## Pass 8
- **Audit-pass-8 fix: ghost passives in the VAR-hook firing paths (scenario
  512).** An eighth sweep (three read-only survey agents across zones/clamps/
  tiles/aliases, death/corpse/parts/segments, and action/choice/formula/
  dispatch). Agents 1-2 found their subsystems correct (zones' anchored auras +
  footprint interaction, clamp ordering, tile precedence, alias resolution; the
  whole death/corpse/parts/segment/mount cluster incl. the part-destroy latch +
  revive subtree + mount-strip — all re-confirmed sound). Agent 3 found the ONE
  real bug: pass-7 guarded the ghost-passive case (a global/team handler removes
  the entity, then its OWN handlers must not fire from beyond the grave) in
  `fire_status_event` / `fire_hook` / `emit_event`, but MISSED the var-hook
  firing paths. `_fire_var_event_inner` (wave 1 = exact `on_var_{kind}`, wave 2
  = `on_var_written` catch-all) and `_fire_var_attempt_inner`
  (`on_var_write_attempt`) each fire global/team handlers then the entity's own
  passives WITHOUT re-checking the entity still exists. So a global var-hook that
  removes the affected entity left the own-passive loops iterating a stale `e`.
  Fixed with the same `if entity_id in self.entities:` guard before each own loop
  (three sites). NOTE for repro authors: in a VAR hook `self`/`this` =
  current_entity_id() (the active-turn entity), NOT the affected entity, and
  `target` is NOT bound (var-event extras expose changed_key/old_value/
  new_value/hook_name/intended_value/was_clamped only) — reference the affected
  entity by literal id or via `self` only when it IS the active entity. (This
  differs from STATUS hooks, where the affected entity is bound as `self`.)

## Pass 9
- **Audit-pass-9 fix: snake-trail coords shift with resize + cross-match copy
  (scenarios 513-514).** A ninth sweep — this one INTERACTION-focused (single
  subsystems are heavily swept now). Three read-only survey agents: (1)
  visibility/render/disguise × transform/mount/viewport/legend/fog, (2)
  modifier/status/team × transform/death/parts, (3) resize/transfer/choice-
  replay/undo. Agents 1-2 re-confirmed their interaction surfaces correct
  (disguise POV gating vs fog vs mechanics separation; viewport/legend window
  clipping; team-membership-change reads `e.team` fresh for modifiers+passives;
  status-removal drops modifiers live; transform wholesale-replaces status;
  part-on-different-team gets its own team passives; choice-replay preserves the
  event stack + resets summon budget; watchers fire only at the top-level
  command boundary, never mid-action). Agent 3 found the one real bug:
  - **`__seg_path` / `__seg_last` (the engine-managed snake-trail coordinate
    vars) were not shifted by `resize_grid` nor offset by `copy_entity`.** These
    are the ONLY coordinate-bearing ENTITY VARS the engine owns (head vars: a
    list of `[x,y]` cells the head has occupied + the head's last cell, used by
    `path`-follow-mode segments). resize_grid shifted entities/tiles/zones/
    explored/channel_views but missed them, and copy_entity remapped part_of +
    `__follows` but didn't offset them — so a path-mode snake re-laid its body at
    STALE cells after a center/edge-anchored resize, and a transferred snake
    re-laid at the SOURCE's coordinates in the destination match. Fixed with a
    shared `Match._shift_snake_path_vars(vars, ox, oy)` static helper called from
    the resize entity-shift loop and per-spawned-entity in copy_entity (delta =
    the same offset the anchor moves by). NOTE: copy_entity is a `MatchManager`
    method, so it calls the helper as `Match._shift_snake_path_vars(...)`, not
    `self.` (a transfer scenario now also guards that cross-match path against
    a crash). KNOWN pre-existing quirk surfaced (NOT fixed — separate from the
    coord bug): path-mode segments legitimately OVERLAP early (the trail is
    shorter than `(segments+1)*spacing`), and copy_entity re-validates occupancy
    on spawn, so transferring a snake whose trail hasn't spread yet fails with
    "cell occupied" — left as-is since overlapping located parts are themselves
    a questionable state.

## Pass 10
- **Audit-pass-10 fixes: turn-order skip-loop + mount push/pull footprint
  (scenarios 515-517).** The widest sweep yet — FIVE read-only interaction
  agents (mounts/vehicles deep; movement/block/opacity; turn-order & clocks;
  formula-sandbox safety; access-gate/dispatch) PLUS hand-written numeric
  assertion harnesses for the gnarliest primitives. Agents confirmed correct
  (no change): the whole movement/block/opacity surface (footprint-aware
  push/pull/swap/group, fail-open conditions, LOS symmetry/corner modes,
  raycast/first_opaque endpoints), the formula SANDBOX (no escapes — empty
  `__builtins__`, entity[X] mandatory-`.path`, every HOOK_CONTEXT name in
  `_who_arg`'s dynamic branch, `normalize_body_source` at every body boundary,
  kh0/explode/band/roll_table edges), and the ACCESS GATE (no batch/run/macro/
  foreach/alias/cmd bypass; overrides-before-rule precedence; approval re-gates
  with the approver's authority). My own numeric harnesses re-verified
  `damage_part` (every cap mode × percent × rounding × 0/0 passthrough × vital)
  and the `apply_modifiers` fold (add/inc%/more%/set/min/max tiers, priority
  bumps, op-order, stat caps) — all exact. Three real bugs fixed:
  - **Turn-order crash when a skip-status round-wrap empties the order
    (HIGH).** pass-3 guarded next_turn against an emptied order after the
    turn_end hooks and after `_advance_index`'s round-wrap, but NOT after
    `_skip_to_eligible` — whose OWN internal `_advance_index` (stepping over a
    `skips_turn` entity) can wrap and fire on_round_end/start hooks that remove
    the last entity. next_turn then did `turn_order[active_index]` on an empty
    list → IndexError (💥). Fixed: re-check `if not self.turn_order: return
    (None, log)` after BOTH `_skip_to_eligible` calls (opening-round + normal
    paths). Scenario 515.
  - **Skip-loop stale bound inflates round_number (MED).**
    `_skip_to_eligible` sampled `n = len(turn_order)` ONCE; if a round-wrap hook
    SHRANK the order mid-skip, the stale `n` let the loop keep cycling the
    survivors, firing on_round_end repeatedly and inflating round_number (a
    command-only repro: 3 entities, two removed on round_end, advanced round by
    3 instead of 1). Fixed: bound by the CURRENT order size each step
    (`if checked >= len(self.turn_order): return False`), keeping the initial
    `n` only as a hard cap against a skip-hook that GROWS the order. Identical
    behavior in the common no-shrink case. Scenario 516.
  - **push/pull validated the RIDER's footprint, not the vehicle's (HIGH).**
    `push_entity`/`pull_entity` walked the legal-prefix using the target
    entity's footprint, then committed via `e.move_dirs`, which redirects a
    mounted DRIVER to its vehicle (`_mount_move_redirect`). So pushing a 1×1
    pilot of a 2×2 tank validated the pilot's 1×1 path (which sat inside the
    tank's own cells → read as blocked → silent no-op) or, on a clear lane,
    committed the vehicle move that the prefix never validated (stop-early /
    mid-commit mismatch). Fixed: resolve `_mount_move_redirect()` at the START
    of push/pull (after the `n<=0` guard), so the VEHICLE's footprint is what's
    validated AND committed — and a non-driver PASSENGER raises "dismount
    first." Mirrors the tp/move_dirs/swap redirect (single-level, like swap).
    Scenario 517. (Process note: while reverting this for a pre-fix check I
    re-inserted the block at the wrong `dx,dy = DIRECTION_VECTORS[canon]`
    occurrence — that string also appears in `move_dirs` — briefly corrupting
    move_dirs. Lesson: a bare `s.find(old)` restore is unsafe when `old` isn't
    unique; prefer the Edit tool with surrounding context.)

## Pass 11
- **Audit-pass-11 fix: damage_spread fragment-mode crash + serialization
  consistency (scenario 518).** An eleventh sweep — three read-only interaction
  agents (serialization round-trip completeness; corpse/aura/time-hooks;
  status-tick/watchers/event-bus) PLUS hand-written numeric assertion harnesses
  for the primitives that still lacked one (`damage_spread`, the clamp
  chokepoint). The agents found NO confirmed correctness bugs (their flagged
  serialization items were FALSE POSITIVES — see below), but my numeric harness
  caught the real one:
  - **`damage_spread` fragment mode crashed without a `random_seed` (HIGH).**
    The `fragment` branch did `rng = getattr(self, "_rng", None) or random`, but
    `logic.py` never imported `random` (only `formula.py` did, for its
    `_active_rng`). `Match._rng` is None by default and only built when a
    `random_seed` is configured AND a formula roll initializes it — so under the
    DEFAULT (no-seed) config every `damage_spread(target, total, "fragment")`
    hit the `or random` fallback → `NameError: name 'random' is not defined` (a
    `❌ Runtime error` through the action/formula path). The weighted / uniform /
    main_only modes use no RNG, which is why scenarios 410-413 never caught it.
    Fix: `import random` at the top of logic.py (the fallback now mirrors
    formula's `_active_rng` exactly: seeded `_rng` when present, global `random`
    otherwise). Scenario 518.
  - **Serialization deepcopy consistency (NOT a live bug — defensive).**
    `Match.from_dict` restored `watchers` and `bound_channels` with a shallow
    `dict(v)` while `to_dict` deepcopied them. Two survey agents flagged this as
    the pass-5/6/7 load-side corruption class, but VERIFICATION showed it's a
    FALSE POSITIVE for correctness: both hold FLAT scalar dicts (`watchers`:
    condition/effect strings + bool `last` + bool `once`; `bound_channels`
    meta: `label`/`pov` strings), and the only mutations (`w["last"] = now`,
    `meta["pov"] = ...`) are TOP-LEVEL key reassignments on the already-
    independent `dict(v)` copy — they never reach the retained snapshot (which
    needs a NESTED in-place mutation to corrupt, as `vars`/tiles/zones had).
    Still, switched both to `copy.deepcopy(v)` for symmetry with the save side +
    every other dict field, so the inconsistency stops magnetizing audit
    re-investigation and a future nested field can't silently reintroduce the
    bug. (Documented as verified-safe so pass N+1 doesn't re-flag it.)
  - Numeric harnesses re-verified EXACT (no bugs): `damage_part` (every cap mode
    × percent × rounding × 0/0 passthrough × vital), the `apply_modifiers` fold
    (add/inc%/more%/set/min/max tiers, priority bumps, op-order, stat caps),
    `damage_spread` apportionment (largest-remainder shares sum to total across
    weighted/uniform/fragment/main_only/spatial-miss/all-zero-weights), and the
    CLAMP chokepoint (hard always clamps; soft engages only crossing from the
    legal side and stays DORMANT past the bound; max-before-min ordering).
  - OPEN QUESTION (RAISED → RESOLVED, shipped as the `suspend` mode below): a
    non-vital body PART destroyed by damage (hp→0 via `damage_part`) LINGERS
    attached-but-dead and does NOT route through `Entity.remove`, so its anchored
    AURA was never released. While investigating, established the current
    death-aura behavior: entity death/kill/despawn (and the part-death cascade)
    all route through `Entity.remove` → `_release_anchored_zones` → the
    `anchored_zone_on_anchor_loss` rule (delete/freeze); the ONE gap was the
    lingering-destroyed-limb case. The user's call: add a third mode that
    SUSPENDS the aura while the anchor is dead and RESUMES it on revive/heal,
    for BOTH entity death/revive and part destroy/heal. Shipped — see next entry.

## Pass 12
- **Audit-pass-12 (stability sweep, HANDS-ON): FormulaError-shadowing crash
  fixed (scenario 521).** First pass done entirely by hand per the standing
  directive (no survey-agent swarm) — reading subsystems + writing numeric/
  property assertion harnesses. Verified CORRECT with harnesses (no change):
  `side_hit`/`hit_location` geometry (4-way all facings × cardinals, 8-way
  corner detection, the 1×1 box==center invariant over 192 source/facing
  combos), `has_los` SYMMETRY (property test, ~7k cell-pairs × permissive/
  strict/open, 0 asymmetric), `raycast` straight-line, the serialization
  round-trip (idempotent `to_dict==to_dict(from_dict(to_dict))` on a complex
  match — multi-tile + parts + path-snake + mount + suspended aura + statuses
  + team + watchers + macros + fog + disguise — AND load-side deepcopy holds),
  `band()` boundaries (ranges / `n` / `lo+` / `-hi`), and dice (`kh`/`kl`/
  explode in range). One real bug fixed:
  - **`FormulaError` UnboundLocalError in `status_cmd` (HIGH, user-facing).**
    `vtt_commands.py` imports `FormulaError` at module level (line 23), but
    `status_cmd` had a REDUNDANT function-local `from formula import
    FormulaEngine, EvalCtx, FormulaError` deep in the handler (the counter
    path). That makes `FormulaError` a function-LOCAL for the WHOLE function,
    so the EARLIER `except FormulaError` in the `!status tick` validation path
    raised `UnboundLocalError: cannot access local variable 'FormulaError'`
    instead of the intended `❌ Invalid tick formula: ...`. So setting ANY
    invalid status-tick formula (typo / unknown identifier / bad syntax)
    crashed with a `💥`. Fixed by deleting the redundant local import; also
    hoisted `validate_formula` into the module-level import and removed the
    same redundant-local pattern from the tile-hook / zone-hook / status-tick
    handlers (they were latent versions of the same shadowing class). The
    remaining `from formula import ... as _FE/_vp/_FEng` aliased locals are
    SAFE (an alias doesn't shadow the module name). NOTE confirmed while here:
    a status-tick formula reads the instance level via `status_get(self,
    status_name, 'level')` — `level` is NOT a bare binding (only `status_name`
    is in the tick EvalCtx extras), so the validator correctly rejects a bare
    `level` (CLAUDE.md's earlier `5*level` shorthand was illustrative).
  - OPEN QUESTION raised with the user (LOS corner-mode consistency) → RESOLVED
    (corner-aware, shipped): `has_los` applied the `los_corner_mode` flanker
    check at diagonal crossings, but `first_opaque` / `raycast` walked the same
    thin `_line_cells` path WITHOUT it — so they only agreed in `open` mode.
    With an opaque corner-X (both flankers opaque), `has_los`=False (blocked)
    while `first_opaque`=None and `raycast` reported the beam reaching the
    target. The user's call: make first_opaque/raycast corner-aware so sight
    and beams agree. Fix: factored the corner-aware walk into the single shared
    `Match._los_stop(viewer, x1,y1,x2,y2)` → `(last_clear, blocked, blocker)`,
    and reimplemented all three over it — `has_los` = `not _los_stop(...)[1]`,
    `raycast` = `last_clear` (stops at the pre-corner cell on a corner-X block),
    `first_opaque` = the on-path `blocker` if any, else `last_clear` on a corner
    block, else None. One walk = one source of truth, so the three can never
    drift on the corner rule again. `_line_cells` stays for the geometry-only
    consumers (`entities_in_line_ignorelos` = walls-ignored by design;
    `entities_on_los` was already corner-aware via per-cell `has_los`).
    Verified: property sweep across permissive/strict/open — `first_opaque`
    None ⇔ `has_los` clear and `raycast`==target ⇔ clear, 0 mismatches; the
    has_los refactor left the symmetry property + full regression intact.
    Scenario 522.

## Pass 13
- **Audit-pass-13 (hands-on): ghost STATUS-TICK guard (scenario 523).** Another
  by-hand pass — numeric/behavioral harnesses, no agent swarm. Verified CORRECT
  with harnesses (no change), broadening the "primitives are exact" coverage:
  the AoE/area enumerators (`entities_in_cone`/`_rect`/`_area`/`_within`,
  `nearest_entity`, `chain_targets` — incl. footprint nearest-cell distance and
  the chain starting from the nearest to the origin, NOT including the origin),
  `transform` hp modes (percent/keep/full) + revert fidelity, shield/absorb
  (`absorb_damage`/`shield_total` — priority order, tag matching, penetration),
  the whole fog/vision stack (range, multi-tile sight UNION, fog_los blocking,
  fog_memory `full` vs `terrain` at remembered cells), status resistance
  (source-gating equipped-vs-inventory, sum/max/first stack, immunity, applied-
  level reduction, full-resist no-op, `force` bypass), and an edge/crash probe
  across dice / `roll_table` / `band` / coord extractors (every invalid input
  is a clean FormulaError, never a 💥), plus event-bus nested-payload integrity
  and the choice-replay exactly-once invariant (a side effect before two
  `choose()`s applies once net despite the rollback+replay per choice). One real
  bug fixed:
  - **Ghost status tick after a lethal tick (MED, the missed ghost-firing
    site).** `fire_status_tick` snapshots an entity's status NAMES so
    status-removal mid-tick doesn't break iteration, but it never re-checked the
    ENTITY still existed. So if status A's tick kills/removes the entity (a
    lethal DoT — or a part tick routing `damage_part` to a vital parent), its
    remaining statuses B, C, … still ticked "from beyond the grave": a tick
    writing to ANOTHER entity ghost-applied (e.g. a dead unit's aura still
    damaging others), and one reading `entity[self]` logged a spurious
    `⚠️ status_tick FAILED: Entity '<id>' not found` (caught, no crash). This is
    the same invariant the passes-7/8 ghost-passive guards enforce for the hook
    / event / var-hook firing sites; the status-TICK site was the one missed.
    Fix: `if eid not in self.entities: break` at the top of the per-status loop
    (after the name snapshot). A non-lethal multi-status tick still fires every
    status; only an actually-removed entity stops. Scenario 523.

## Pass 14
- **Audit-pass-14 (hands-on): FIRST CLEAN PASS — no bug found.** Continued the
  by-hand discipline (numeric/behavioral harnesses, no agent swarm). Hand-
  verified TEN subsystems/interaction-combos against assertion harnesses, ALL
  exact — no code change. This is the first pass that surfaced zero defects, a
  signal the harness-testable engine core is solid in these zones. Verified
  (so future passes can skip re-grinding these):
  1. **Multi-tile push/pull/swap geometry** — a 2×2 push stops exactly at the
     cell before a wall; push-to-edge clamps the anchor; swap of a 2×2 with a
     1×1 exchanges anchors; pull stops adjacent to a 2-wide body.
  2. **Segment sever modes** — `cascade` removes the cut + everything behind;
     `split` promotes the segment behind to a new independent head (cleared
     part/segment linkage, re-parented tail, stamped split-head template, added
     to turn order).
  3. **Macro/foreach substitution** — `$10`/`$11` parse as the 10th/11th arg
     (not `$1`+"0"), `$@` expands to all args, missing `$5`→"", no
     re-expansion of a `$2` appearing inside an arg value, foreach `$id`/`$name`.
  4. **Mount slot math** — capacity budget, per-rider `cost` formula, `condition`
     gate, re-seat doesn't self-block, mount-cycle guard.
  5. **Region parts** — facing-aware footprint-region projection (front/back/
     left/right/corners/center) rotates correctly with the parent's facing on a
     3×3.
  6. **resize_grid coordinate shift** — corpses-in-tile-data, anchored auras
     (re-stamped around the shifted anchor), mounts (vehicle + carried rider),
     and a subsequent revive all land at the shifted cells; no crash.
  7. **Recursion/limit guards** — event-bus re-emit bounded at
     `event_recursion_limit` (64), var-hook self-write bounded by the
     `_var_event_depth` guard, self-referential action bounded at the recursion
     limit (8) with a clean error — no hangs/crashes.
  8. **Multi-feature combos** — transforming into a larger footprint re-stamps
     the anchored aura's disc bigger (and revert shrinks it back); pushing a
     multi-tile vehicle carries its rider AND re-stamps its aura together.
  9. **Watchers** — edge-trigger (false→true fires once, no re-fire while true,
     re-fires after the condition resets), `once` removal, `last` serialization.
  10. **Corpse introspection** — `corpse_var`/`corpse_has` (nested paths +
      default), `corpse_status_has`/`get`/`names`, large-corpse `corpse_cells`
      footprint, and revive restoring the footprint.
  Note: numeric primitives (damage_part, modifier fold, damage_spread, clamps,
  side_hit, LOS/raycast, dice/band/roll_table, shields, status resistance, fog/
  vision) were already harness-verified exact in passes 11-13. With this pass,
  the harness-testable core is broadly covered; the likeliest remaining defect
  surface is the Discord adapter (not harness-testable) and genuinely new code.

## Pass 15
- **Audit-pass-15 (hands-on): SECOND CLEAN PASS — no bug found.** Swept the
  remaining untouched-by-hand areas; all exact, no code change. Verified:
  status interaction cluster (tags, cross-status `removes`/`blocked_by` by
  bare-name + `tag:` tokens, counters auto-removing at <=0 on `duration` AND
  custom fields); team-level state (resources `team_get`/`set`/`add` dotted,
  team `modifiers` aggregated per member with a non-member excluded, team
  passives firing on the acting member, membership-change picks up the new
  team's modifiers); tile precedence (instance `glyph`/`block`/`opaque` >
  template > rule) + tile time-hooks firing per placed instance; alias
  resolution (expands before the gate); batch undo grouping (a `!batch` reverts
  as ONE history entry) + single-command + tp undo; `!find` predicates
  (var compare, `team=`, `hp<`, `near:<id>:<r>`, `within:<x>:<y>:<r>` — all
  footprint/Chebyshev-correct). PROCESS NOTE for future harness authors: two
  "failures" this pass were BOTH test-harness errors, not engine bugs — (1)
  there is NO `ent damage` subcommand (damage is `ent hp <id> <-n>`), and (2)
  `restore_snapshot` (undo/history restore) REPLACES the Match object in
  `mgr.matches[mid]`, so a captured `m = mgr.matches[id]` reference goes STALE
  after an undo — always RE-FETCH `mgr.matches[id]` after a restore/undo or
  you'll read the pre-undo object and think undo is broken. Two clean passes
  (14-15) in a row → the harness-testable engine core is solid.

## Pass 16
- **Audit-pass-16 (hands-on, recent-features sweep): two fixes (scenarios
  549-550).** A by-hand interaction pass over the recent work (containers,
  inline `$()`, reveal_fog, macros, overlays) with numeric/behavioral harnesses.
  Verified CORRECT (no change): item_consume transactional rollback on action
  fail(), var_move deepcopy isolation + dest-overwrite, reveal_fog memory/expiry/
  serialization, macro `if`/`repeat` read-only gate (enforced at RUN time —
  mutating conditions rejected, entity survives), container edge cases (clamp on
  var_add, non-numeric/missing paths, var_sum_field skipping non-dict children).
  Two issues:
  - **Vital-var deletion (HIGH, FIXED).** item_consume / var_del / var_clear
    could DELETE a vital var (hp/max_hp/initiative) → corruption (a missing hp
    reads as 0 → entity counts dead). The `!ent delete_var` command already
    blocked it and var_del/var_clear docstrings CLAIMED remove_var protected
    vitals — but it DIDN'T. Fixed at the chokepoint: `Entity.remove_var(path,
    allow_protected=False)` raises VTTError on a protected var, so every formula
    deletion path + the command are uniform; the two vital-var property setters
    (max_hp/initiative = None) pass `allow_protected=True`. var_clear's existing
    `except VTTError: continue` now actually skips vitals as intended.
  - **Inline `$()` POV info-leak → mitigated by a gamerule (user-approved
    default: a toggle).** Inline `$()` evaluates read-only formulas with NO
    POV/fog filtering, so a non-host PLAYER could read hidden entity vars via a
    player-available command (`!dist $(entity[boss].hp) 1 1 1` returned the
    fogged boss's exact hp). NOT a bug in the engine's formula model (formulas
    always saw all), but it undercuts fog-of-war info-hiding. New rule
    `inline_args_access` (enum all|host, default `all` = unchanged): set to
    `host` and a non-host's command containing a `$()` token is REFUSED
    (`CommandRegistry._inline_args_blocked` + `_has_inline_token`, checked in
    both `run` and `dispatch_no_snapshot` so the batch/macro inner-line vector is
    covered too). No-op on an open match (no owner), an auto-approve/identity-
    less surface, or for hosts. The same "tighten reads for a fog match" lever as
    `command_access`. Default stays permissive; a fog GM opts into the lockdown.

## Pass 17
- **Audit-pass-17 (hands-on): CLEAN PASS — no bug found.** A by-hand
  interaction sweep over the recent-feature cross-products (graphics + fog/POV +
  mounts + status + transform + containers + macros), all exact, no code change.
  Verified (so future passes can skip re-grinding these):
  1. **Serialization idempotency** — `to_dict == to_dict(from_dict(to_dict))` on
     a match exercising EVERY recent serialized field at once (named tables,
     fog_reveals, border_show/color/opacity, render_mode, background, status-def
     overlay sprites, the overlays var, mounts, parts, container vars, control-
     flow macros, watchers, anchored zones, team data, sprite_layer var). No
     missing/dropped field.
  2. **`render_scene` POV/fog/disguise consistency** — for every POV the emitted
     entity + tile placements match the SAME `entity_visible_to` /
     `tile_visible_to` + `_fog_terrain_visible` predicates the ASCII path uses
     (no graphics-side info leak); a disguised entity shows the decoy name to
     enemies, the real name omniscient.
  3. **Status cluster** — dispel by `tag:` removes all matching + keeps others;
     transfer to an IMMUNE dest is consume-on-reject (gone from source, doesn't
     stick); counter to 0 on a CUSTOM field auto-removes.
  4. **Overlays render on a visible mounted rider** (the `_emit_entity_placement`
     overlay pass covers riders/region parts); item_consume inside a macro
     `repeat` decrements per iteration.
  5. **transform/revert** — id/pos/team preserved, statblock wholesale-swapped
     (old inventory dropped), revert restores it; needs a CAPTURED statblock
     (`store_entity_into_var` / a live id), NOT a flat hand-built dict.
  6. **Render resolution precedence** — per-tile `border_color` data >
     per-match border field > rule; per-tile `sprite_layer` data overrides the
     z-layer rule; inline `$()` negative result works in `!foreach`.
  7. **Macro control flow** — nested `if`/`repeat` + `$#` index, `macro_repeat_limit`
     clamp, `macro_step_limit` runaway abort, unbalanced-block rejected at `set`.
  Process note for future harness authors: two "failures" this pass were BOTH
  test errors — spawning a second entity ON a vehicle's occupied cell (the add
  silently fails, so the later mount has no rider), and using a FLAT transform
  template instead of a captured statblock (apply_statblock finds no `vars` →
  max_hp None). Neither is an engine defect.

## Pass 18
- **Audit-pass-18 (external-report triage): four fixes (scenarios 551-553).**
  A second Opus instance was asked to find flaws; all four it reported were
  verified against the code (repro'd where harness-reachable) and fixed:
  - **Vital-var WRITE corruption (MED, FIXED).** Any write path could set
    hp/max_hp/initiative to a non-number or nest under it (`!ent set_var h hp
    abc`, `var_set('h','hp','boom')`, `hp.x`, `var_add('h','hp.inner',5)`),
    after which the `int()`-based hp getter 💥'd `!list`/damage/is_alive — and it
    round-tripped through save/load. The DELETE path was already guarded
    (remove_var, pass-16); the symmetric WRITE path wasn't. Fixed at the
    `Entity.write_var` chokepoint: a write whose top-level key is a protected
    vital var is coerced to int (numeric strings/floats OK) or REJECTED
    (non-numeric / bool), and NESTING under a vital (`hp.x`) is refused
    (`_coerce_vital_value` helper). Skipped pre-bind (no match).
  - **`!ent clone` ignored parts/mounts (MED, FIXED).** Clone did `src.to_dict()
    → from_dict → spawn`, so it DROPPED a multi-part creature's limbs and copied
    protected relational fields raw — a mounted clone inherited `mounted_on`/
    `mount_slot` (a phantom rider bypassing slot capacity / on_mounted), a part
    clone inherited `part_of` (a phantom limb on the original's parent). Fixed to
    mirror `MatchManager.copy_entity`, same-match: clone the whole part SUBTREE
    (parents first, ids/`part_of`/`__follows` remapped, located-part offsets
    kept, `_restamp_parts_for`), STRIP `mounted_on`/`mount_slot`, and REFUSE
    cloning a body part ("clone its parent instead"). Gotcha fixed mid-work: the
    root must take the planned id, so `taken` excludes the plan's cid.
  - **Unguarded `int()` in `!ent hp/init/add/tp` (LOW, FIXED).** A non-numeric
    numeric arg (`!ent hp foe abc`) 💥'd pre-mutation where a clean ❌ belongs
    (sibling verbs already wrapped int()). Wrapped the four sites in try/except
    ValueError → ❌.
  - **Discord approval resolved against the wrong match (LOW, Discord-only,
    FIXED by inspection).** `_ApprovalView._match` popped/ran the request via the
    channel's ACTIVE match, but request ids are per-match sequential (r1, r2…),
    so a host switching matches mid-approval could pop a DIFFERENT match's
    same-id request. Fixed: `add_pending_request` now stores `match_id` on the
    request (runtime-only, harness-verified present); `_match` resolves against
    it, and the approve re-dispatch points the channel at the request's match
    before running (falls back to active for legacy requests).

## Pass 19
- **Audit-pass-19 (external-report triage): summon regression + var_get default
  + harness hardening (scenario 554).** A second Opus instance found two bugs
  plus a harness blindspot; all verified and fixed:
  - **The summon system was COMPLETELY BROKEN (HIGH, FIXED).** Every
    `summon` / `summon_near` / `summon_from` raised `❌ Runtime error: 'x'` and
    created NOTHING. `Match.summon_entity` called `Entity.from_dict(d)` BEFORE
    seeding `d["x"]/d["y"]` (they're set later from the resolved placement), but
    a template has its position STRIPPED and `from_dict` does `int(data["x"])`
    (subscript, no default) → `KeyError('x')`. Fixed: `d.setdefault("x", x)` /
    `setdefault("y", y)` before the from_dict probe-build (they're overwritten
    with place_x/place_y anyway). This had rotted undetected across ELEVEN of
    its own scenarios (317/318/321-324/336/340/351 + the summon-in-passive 319
    and summon-tile 320) because the harness only flagged 💥, not a top-level ❌.
  - **`var_get` rejected a default arg (LOW-MED, FIXED).** `var_get(eid, path)`
    was 2-arg, but the obvious create-or-read idiom `var_get('h','alarms',0)+1`
    (used by scenario 427's watcher effect, and mirroring `corpse_var`'s
    `default`) errored "takes 2 positional arguments but 3 were given". Added an
    optional `default`: returned on a missing path, still raises without one.
  - **Harness blindspot hardened (the meta-fix).** `run_scenarios.py` only
    flagged 💥 / "Syntax error", so a top-level `❌ Runtime error:` /
    `❌ Unexpected error:` (a core feature silently failing) slid through — the
    exact trap that hid the summon regression. Added `_ERROR_MARKERS` flagged
    UNLESS a scenario opts out with a `HARNESS-ALLOWS-ERRORS` tag in its
    Expected prose (for the few scenarios — 222 func-deletion, 551 vital-write
    rejection, 554 var_get-no-default — that DELIBERATELY surface a top-level
    error). 💥 / Syntax always flag regardless. So the whole suite now passing
    (551/551) is a real signal that no core feature is silently ❌-ing.

## Pass 20
- **Audit-pass-20 (hands-on): macro runaway backstops — two DoS/crash fixes
  (scenarios 555-556).** A by-hand sweep of the least-audited RECENT features
  (container prims, inline `$()`, macro control flow, reveal_fog, named tables,
  overlays, the Discord adapter) with behavioral harnesses. Verified CORRECT (no
  change): the container/inventory prims (item_consume drop-at-0 / over-consume /
  partial / custom field, var_add create-or-increment + non-numeric reject,
  var_move deepcopy isolation between entities), status dispel/transfer (tag
  dispel, max cap, consume-on-reject to an immune dest), transform/foreach/table/
  reveal_fog smoke on a multi-tile board, reveal_fog serialization + footprint
  `around`, the dice `kh0` guard (survived the roll_detail refactor), the inline
  `$()` classification (drift guard + validate_arg_safe reject path) and the
  raw_args opt-out set (ent/foreach/batch/run/macro/eval — exactly the meta
  commands), the ATB selection math + death-mid-turn robustness, and the Discord
  board/approval logic (pass-18's approval-match fix holds). Two real bugs in the
  MACRO control-flow interpreter (both let a macro hang/crash the bot, defeating
  the stated runaway backstop):
  - **Nested `repeat` over an empty body bypassed macro_step_limit (HIGH).**
    `_exec_macro` charged the step budget ONLY on `cmd` nodes, so a `repeat`
    whose body emits no command (an empty or directive-only block) never touched
    the budget. `repeat 1000 / repeat 1000 / end / end` ran 1e6 iterations (0.4s)
    and a third nesting level (1e9) would hang the bot for minutes — exactly the
    runaway macro_step_limit exists to stop. Fixed by charging one budget unit
    PER `repeat` ITERATION (in the `for k in range(...)` loop), so total loop
    work across all nesting is bounded by the limit regardless of body contents.
    macro_step_limit's meaning is now "dispatches PLUS loop iterations"; its desc
    updated. Tight legitimate loops are unaffected (default budget 10000).
  - **Recursive macros overflowed the Python stack (HIGH → 💥).** A macro line
    can `!macro run <other>`, and each `!macro run` allocated its OWN fresh step
    budget — so a self-recursive macro (`loop` = `macro run loop`) or a mutually-
    recursive pair recursed until `maximum recursion depth exceeded` (a caught
    but ugly 💥, and a lot of wasted work). Actions guard this with a recursion
    limit; macros had none. Fixed with a new `macro_recursion_limit` rule
    (default 20) + a `MatchManager._macro_depth` counter (on the manager = the
    dispatch stack, so it accumulates even if a macro line switches the active
    match), incremented around each `!macro run`'s `_exec_macro` and restored in
    `finally`. Beyond the limit the run aborts with a clean ❌, not a 💥. Legit
    nesting (`top`→`mid`→`leaf`, 3 deep) and 30 SEQUENTIAL inner runs both still
    work (depth returns to base between sequential cmds; resets between top-level
    runs). NOTE for future sweeps: `!batch` CANNOT recurse this way (its
    subcommands are literal text, no named indirection), but `!run <file>` on a
    self-referencing file has the same unbounded-recursion shape — left as-is
    (host-only disk I/O, far more exotic than a stored player-triggerable macro).

## Pass 21
- **Audit-pass-21 (hands-on): CLEAN PASS — no bug found.** A by-hand sweep
  targeting the recent-feature surface NOT already drilled in pass-20 (behavioral
  harnesses, no agent swarm); every check exact, no code change. Recording the
  verified combos so future passes can skip re-grinding them:
  1. **Container-prim vs clamp/vital interaction** — `var_add` on a clamped var
     RETURNS the pre-clamp computed value while STORING the clamped value; this
     is the SAME convention as `var_set` (both return the requested value, not
     the stored one) — NOT a bug (re-read via `var_get` for the stored value).
     `var_add`/`item_consume` route through `write_var`, so pass-18's vital-write
     coercion + the clamp system apply correctly (a write above max_hp clamps).
  2. **`ARG_SAFE_MATCH_FUNCS` classification is genuinely side-effect-free** —
     spot-audited the suspicious "safe" entries (`entity_snapshot` = pure
     template builder, `var_pick_random`/`roll_table`/`table_roll` = read-only
     except advancing the seeded RNG, which is intended/documented). No
     mis-classified mutator is reachable from inline `$()`.
  3. **`render_scene` vs `_render_ascii_impl` have NOT drifted** — both entity
     passes gate on `is_alive` + the SAME `entity_visible_to` / `tile_visible_to`
     / `zone_visible_to` / `corpse_visible_to` / `_fog_terrain_visible`
     predicates, skip the same glued/region/mounted parts, and are disguise-
     gated identically (the one deliberate difference: the scene region-part pass
     also honors a custom SPRITE, ASCII only a custom glyph). Overlay placements
     (`_emit_entity_placement`) are disguise-gated (no real-status-FX leak to an
     enemy POV) and follow the entity footprint/mode.
  4. **transform → save/load → revert round-trip** — a captured statblock stashed
     to a caller-chosen var survives `to_dict`/`from_dict` (form, part subtree,
     stash var all intact); revert on the RELOADED match restores the original
     name/hp/footprint and its inventory, and drops the transformed form's parts.
  5. **QoL commands** — `!find sort:<var>[:desc] show:<csv>` (missing var → `—`,
     sorts last; dotted paths), `!dist` (entity/cell/mixed, all metrics,
     footprint nearest-cell gap, `los`), `!roll` (NdM/kh/kl/explode, bad input →
     clean ❌). No crashes.
  6. **`roll_table_pick`** — weights validated (>=0, non-numeric → ❌), 0-weight
     filtered, cumulative selection sound (harmless dead `return total` after the
     final return; not worth a fix).
  7. **SpriteLoader path-traversal guard** — every `..`/absolute/mixed-separator
     key and non-PNG extension resolves to None (blocked); legit keys stay inside
     the sprites folder. (Defense-in-depth; sprites are server-side-only, no
     inbound upload path exists.)
  8. **reveal_fog** — a `turns=N` temporary reveal shows terrain AND live
     entities, then EXPIRES on the right round and is pruned from `fog_reveals`; a
     permanent reveal persists across rounds. Serializes + is footprint-aware
     (`around <eid>`).
  9. **Choice-replay through batch/foreach** — an action using `choose()`
     (transactional replay+rollback per choice) invoked inside `!batch` and
     `!foreach` with pre-supplied `answer=` tokens: a var write BEFORE the choice
     applies EXACTLY ONCE per invocation despite the replay, and the batch/foreach
     dispatch isn't corrupted by the rollback.
  Also re-confirmed clean by inspection: the Discord adapter board/approval/image
  logic (pass-18's per-match approval fix holds) and status dispel/transfer
  (tag dispel, max cap, consume-on-reject to an immune dest). NOTE for future
  harness authors: the default clamp caps hp at max_hp (soft, NO minimum — hp
  can go negative) and max_hp defaults to the spawn hp — so a bare `!ent add x
  X 30 ...` caps hp at 30; raise max_hp first or your "hp write is broken"
  repro is really the clamp working (this cost me a
  false lead this pass).

## Pass 22
- **Audit-pass-22 (hands-on): two "non-string → string-join" crash fixes
  (scenarios 558-559).** A by-hand sweep that (correctly) started with the
  freshest code — the just-added `!foreach` upgrades — and found a bug there,
  then a second of the SAME class elsewhere. Both are `💥`-level crashes where a
  non-string value reached a `", ".join(...)` / `re.sub` replacement that
  assumed strings:
  - **`_foreach_subst` `$team` (introduced by the foreach-upgrade change).** The
    `$team` token substituted the entity's team var without str-coercion (unlike
    `$i`/`$n`/`$x`/`$y`, which were `str()`d). A NUMERIC team var (`!ent set_var
    x team 5`) made the `re.sub` replacement lambda return an int →
    "sequence item 0: expected str instance, int found" (💥). Fixed by
    str()-coercing the replacement in the lambda (defensive for every token, not
    just `$team`). Regression added to scenario 558 (a numeric-team entity).
  - **`_tmpl_fmt_value` non-string DICT KEYS (pre-existing).** The `{placeholder}`
    template value formatter (used by `entity_line_format` / status-line / part-
    suffix templates) joined a dict var's keys with `", ".join(keys)` — but the
    sibling list branch already `str()`d its items, and the dict branch didn't.
    A formula can write a dict with non-string keys (`entity[a].loot = {1: 5}`);
    referencing that var in a template placeholder (e.g. `entity_line_format`
    `{loot}`) then crashed `!list`/`!state` with the same "expected str instance"
    💥. Fixed to `", ".join(str(k) for k in keys)` in both the truncated (>6) and
    full branches, mirroring the list branch. Scenario 559.
  Same-class sites deliberately LEFT (verified non-crashing or pathological-only):
  `!find sort:<var>` is type-safe by construction (a `(rank, number, string)`
  sort-key tuple, so mixed-type var values across entities never raise); the
  rule-name / slot-name joins (`logic.py` ~2545/6387) only take non-strings if a
  GM pathologically builds `slots = {1: ...}` with integer keys, which is far
  outside normal authoring. NOTE for future harness authors: the fastest bug this
  pass came from auditing the code I'd JUST written — fresh code is the highest-
  yield target, and a numeric team/var is a realistic GM input that scenarios
  rarely exercise.

## Pass 23
- **Audit-pass-23 (hands-on): CLEAN PASS — no bug found.** A by-hand sweep with
  exhaustive serialization checks, an event-bus review, broad input fuzzing, and
  numeric primitive re-verification; no code change. Verified combos (so future
  passes can skip re-grinding):
  1. **Serialization is airtight.** Built a match populating ~all 46 serialized
     fields (nested-var entities, multi-tile + parts + segments, status defs with
     overlay sprites + tags + modifiers, anchored zone, tiles, groups, aliases,
     macros, tables, watchers, team data + team passives, fog + memory + reveals,
     colors, layers, legend, border, render_mode, background, viewport). `to_dict
     == to_dict(from_dict(to_dict))` idempotent; and a LOAD-SIDE ISOLATION test
     (rebuild from a retained snapshot dict, mutate every nested structure on the
     loaded match IN PLACE, confirm the snapshot dict is untouched) passed — so
     `from_dict` deep-copies nested data, the passes-5/6/7/11 corruption class is
     fully closed.
  2. **Event bus (`emit_event`) is careful** — global handlers fire once; a
     directed event also fires the target's team + own handlers with existence
     re-checks (the passes-7/8 ghost-passive guards); `_event_stack` push/pop in
     try/finally; recursion capped by `event_recursion_limit` with a
     warning-latch drained at the outermost emit.
  3. **Input fuzzing (58 inputs) all handled cleanly (❌, never 💥).** 28
     numeric-arg commands fed non-numeric/huge/negative values (`!map resize/pan/
     center/view/border`, `!zone shift/fill`, `!tile line/fill`, `!part segment`,
     `!status apply/force/counter`, `!team add`, `!reveal_fog at/around`, `!ent
     hp/init/tp/add`, `!dist`, `!roll`, …) — the pass-18 int()-guarding holds
     broadly. 30 formula/structural edges (malformed passive/gpassive/func/clamp/
     status-tick/watch/action bodies, empty/vital-nesting var paths, vital
     deletion, ops on missing entities, self-referential alias/macro, a
     200-deep paren expr) — all clean.
  4. **Dice parser edge cases correct** — `1d1!`/`100d1!` don't infinite-loop
     (explosion skipped for sides==1), `0d6` is a clean FormulaError, `d6`
     implicit-1-die, combined suffixes (`2d6!kh1`, `3d6!kl1`), `kh` cap > dice
     count, negative groups — all exact.
  5. **Modifier fold is exact for the combat foundation** — status-instance +
     team + equipped sources aggregate together with correct `[source]` labels;
     `((10+5)+2)×(1+0.5)) = 25.5` to the decimal; a `modifier_stat_caps
     strength:0:20` rule (set before match creation so it's in the rules
     snapshot) clamps 110→20. NOTE: a status-instance modifier must be set via
     `!ent status <id> set <name> modifiers.<k>.<field> ...` (the status
     instance), NOT `!ent set_var <id> status.<name>...` (which writes a var
     named `status`, a different location — a false lead this pass).
  This is the third clean pass in the 20s (with 21); the harness-testable core +
  serialization + command-input robustness are solid. Likeliest remaining defect
  surface stays genuinely new code (as pass-22 showed — the bug was in the
  freshest change) and the Discord adapter.

## Pass 24
- **Audit-pass-24 (hands-on): the "round-keyed logic breaks under ATB" bug CLASS
  (scenario 560).** Drilled the history/undo subsystem (`match_history.py`, never
  hand-audited before) and found a defect class worth remembering: **ATB disables
  rounds, so `round_number` is frozen at 1 forever — every mechanism whose window
  / expiry / seed is keyed on `round_number` silently misbehaves under ATB.**
  Prior ATB work covered the LOUD cases (`round_number()`/`turn_index()`/round
  `schedule()` RAISE, and `_has_round_logic` warns about dormant round logic);
  these are the SILENT ones. Audit the whole class when touching ATB.
  - **Turn-autosave retention never pruned under ATB (REAL, FIXED).**
    `MatchHistory._prune_turns` kept `autosave_turn_retention_rounds` rounds'
    worth of turn snapshots by comparing `s.round_at_snapshot >= round_number -
    rounds + 1`. Under ATB that threshold never advances, so NOTHING was ever
    pruned and turn snapshots accumulated for the entire session — each one a
    FULL match state (measured: 40 ATB turns → 40 retained snapshots / 265 KiB,
    vs 5 in round mode; 150 turns → 150). An unbounded memory leak in the
    intended-for-long-sessions turn model. Fixed: under ATB, `_prune_turns`
    prunes by the `_turn_index` counter (the only clock that advances there)
    against a NEW `autosave_turn_retention_turns` rule (default 20 ≈ the
    round-mode default for a mid-sized party; -1 unlimited / 0 disable, mirroring
    the rounds rule). Round-based play is byte-for-byte unchanged (the ATB branch
    is gated on the `atb_enabled` rule). Verified: 150 turns → capped at 20,
    `!history undo turn` still restores correctly under ATB, turns keep advancing
    after an undo, and the 0 / -1 knobs behave.
  - **OPEN (raised with the user, NOT fixed — needs a design call): a
    `!reveal_fog ... turns=N` TEMPORARY reveal never expires under ATB.**
    `reveal_cells` stores `until = round_number + duration` and `_active_reveals`
    prunes when `until >= round_number`; with rounds frozen the reveal is
    permanent (verified: 30 ATB turns, cell still revealed, record still
    `until: 3`). Fixing it properly needs a monotonic TURN counter on Match —
    which does not exist (`history._turn_index` is unsuitable: it lives on the
    history object, which `to_dict` excludes by default, so it resets on
    save/load). That counter is exactly the "turn-elapsed counter to replace
    turn_index() under ATB" already flagged as a FUTURE item in the ATB entry, so
    its shape (field name, whether it's exposed as a formula prim, whether
    `turn_index()` returns it under ATB instead of raising, whether it counts in
    round mode too) is the user's design call — I asked rather than guess.
    RESOLVED — the counter shipped as `Match.turns_elapsed`; see the ATB
    turn-clock entry below.
  - Verified CORRECT in the same sweep (no change): the rest of the history
    subsystem — manual save → mutate → restore, restoring the SAME snapshot
    TWICE (no snapshot corruption, i.e. the pass-7 load-side deepcopy holds),
    `!history undo command N`, restore after an entity was removed (it comes
    back), `!history list`, `!history diff manual:a manual:b` (correct per-var
    old→new + additions), the confirmation-prompt gate, and linear-history
    truncation of the orphaned future. Also re-checked and judged BENIGN under
    ATB: the `random_stable` turn-order tiebreaker's `(match.id, round_number)`
    seed is frozen, but ATB picks the actor by charge bar (ties broken by
    time/rate/id), so turn_order ORDER doesn't drive selection — cosmetic only;
    and round `schedule()`s created before ATB was switched on never fire, which
    `_has_round_logic` already warns about by design.

## Pass 25
- **Audit-pass-25 (hands-on): the "dangling relational id" bug CLASS — transfer
  phantom mount fixed (scenario 561).** Entities reference each other by BARE ID
  (`mounted_on`, `part_of`, `__follows`, a zone's `anchor`). Any path that moves
  or copies an entity across a boundary must REMAP or STRIP those ids, or they
  dangle — and because an id is the engine's ONLY notion of entity identity, a
  dangling id silently LATCHES onto whatever later takes that id. Two prior
  fixes were instances of this (pass-6 corpse snapshot strips mounted_on;
  pass-18 `!ent clone` strips it); this pass found the remaining hole and a
  documentation error.
  - **`copy_entity` (`!ent transfer` / `!ent copy`) carried mount linkage across
    matches (REAL, FIXED).** It remapped `part_of` and `__follows` but left
    `mounted_on`/`mount_slot` pointing at the SOURCE match's vehicle. The
    transferred rider looked harmless at first (`is_mounted` defensively returns
    False when the host is absent), but the stale id persisted and SERIALIZED —
    so the moment the destination gained any entity with that id (a summon, a
    later transfer, a plain `!ent add`), the rider read as mounted on it. Verified
    harm: a rider "riding" an entity with NO `slots` var at all — bypassing slot
    existence, capacity, the slot `condition` gate and the on_mounted hook — and
    disappearing from the map/occupancy via the hidden-rider skip surface. Fixed
    by stripping both fields in `copy_entity`, matching the corpse/clone paths.
    Transferring a VEHICLE already ejects its riders (`_release_riders` via
    `Entity.remove`), so a rider and its vehicle can never travel together —
    arriving dismounted is the consistent outcome. Applies to `move=True`
    (transfer) and `move=False` (copy) alike.
  - **Suspended aura + id reuse: the DOCUMENTATION was wrong (amended, behavior
    left).** The `anchored_zone_on_anchor_loss` rule desc and this file both
    claimed a true despawn under `suspend` "leaves an inert bound zone that won't
    resume (nothing to revive)". Not true: the binding is by ID, so if a later
    entity is created with the despawned anchor's id, the suspended aura RESUMES
    around that unrelated entity the first time it moves (`_restamp_anchors_for`
    on `fire_entity_moved`). Verified. Left the BEHAVIOR as-is (there is no
    entity identity beyond the id, so distinguishing "same id, different entity"
    would need a new identity concept — a design call, not a bug fix) and
    corrected both docs per the standing "ambiguous wording MUST be amended"
    rule. Flag to the user if they'd rather change the semantics.
  - Verified CORRECT in the same sweep (no change): **scheduled effects** —
    `schedule_on` fires at the right turn in BOTH round mode and ATB (it's
    turn-based, so ATB is fine), entity removal drops its pending schedules,
    `cancel_schedule` works, schedules serialize, and round `schedule()` raises
    a clean pointer-to-`schedule_on` error under ATB. **The access gate /
    approval queue** (hand-verified for the first time, previously only
    agent-checked): a non-host's mutating command is QUEUED not executed, and
    there is NO bypass via `!batch`, `!foreach`, `!eval`, or `!macro` (each is
    gated at the top level before its inner ungated dispatch); approve
    re-dispatches with host authority, deny drops it, read-only subcommands pass
    for players, and `!map full` is correctly elevated by ELEVATED_ARGS.
    **Action transactional rollback** — an action that writes vars, damages hp,
    applies a status, calls `damage_part`, summons an entity and emits an event
    then `fail()`s reverts ALL of it (entity count included); a failing NESTED
    action rolls back its own effects while the outer continues (the GM checks
    `use_action`'s result if they want abort-on-failure); the recursion limit (8)
    errors cleanly rather than crashing; a succeeding action commits.

## Pass 26
- **Audit-pass-26 (hands-on): formula-sandbox dunder hole closed (scenario 562).**
  Hand-attacked the sandbox for the first time this era (31 escape attempts in
  expression mode + 20 in action mode). Expression mode rejected EVERYTHING
  (`__import__`/`eval`/`exec`/`open`/`globals`/`type`/comprehensions/lambda/
  subscript/`__builtins__` — all clean ❌, no 💥). Action mode leaked:
  - **THE BUG.** `_is_action_attr_passthrough` deliberately leaves ANY attribute
    chain rooted at a bare Name un-rewritten for Python's runtime attribute
    resolution — that's how `source.<path>`, `args.<key>` and Coord `target.x`
    work. But it also permitted PYTHON DUNDERS. Since `self` in an action body
    binds to a plain STRING (the entity id), `self.__class__` resolved to a live
    `type` object and `.__bases__` / `.__subclasses__` / `.__init__` walked
    further into Python internals. **Not RCE** — a Call's func must be a bare
    Name, so `...__subclasses__()` is rejected with "Only direct function calls
    are allowed" (verified). The REAL harm: those objects can be ASSIGNED INTO
    ENTITY VARS, and a var holding a `type` / bound method breaks
    `json.dumps` — verified `TypeError: Object of type type is not JSON
    serializable`, i.e. `!store save` fails and the match becomes unsaveable
    (the failure is caught and surfaced as a clean ❌ by `MatchManager.save`, so
    it's data-corruption/DoS, not a crash). Realistically hit by ACCIDENT (a
    typo'd `self.something`) as much as by intent.
  - **FIX:** reject Python dunder attribute names at VALIDATION time inside the
    action-mode passthrough walk (new module helper `_is_python_dunder`). The
    pattern is deliberately `__x__` — dunder on BOTH sides — because engine-
    reserved entity vars are LEADING-dunder only (`__follows`, `__segment`,
    `__part_located`, `__cell_stackable`, ...), so a legitimate `source.__follows`
    still validates and reaches runtime. Verified after the fix: all 51 escape
    attempts rejected; `source.<path>` (item container read), `args.<key>`, and
    location-target Coord `target.x`/`.y` all still work; vars stay clean and the
    match stays serializable. NOTE for future sweeps: authoring an action body
    goes through host-gated `!ent set_var`, so this was never player-reachable —
    but sandbox integrity is the whole point of the `_ALLOWED_NODES` whitelist +
    empty `__builtins__`, so the hole was worth closing regardless.
  - Verified CORRECT in the same sweep (no change): **no path mutates an entity
    id** except `copy_entity`'s remap (`!ent rename` changes the display NAME
    only), so the pass-25 dangling-id class is closed. **`!store save`/`load`
    round-trip** through MatchManager — two matches, a custom GameSystem with
    tweaked rules, active channel bindings, zones/anchored auras, macros, team
    data, nested inventory and a part subtree all survive into a FRESH manager,
    and the loaded match is fully functional (list/map/turn/move/macro/part all
    clean). (Path handling was "host-gated by design" here — SUPERSEDED in
    audit-pass-29: the gate was a no-op without an active match, so any user
    could read/write host files; now admin-only and confined to `saves/`.) **Event-log retention** is a plain count cap (not
    round-keyed, so unlike pass-24 it's ATB-safe): cap enforced, 0 = keep
    nothing, -1 = unlimited. **Dict-rule editors** — `!system set` correctly
    refuses dict rules and points at the dedicated editor; `!log format` (incl.
    clearing with `-` and custom `log()` types), `!gclamp`, `!system access` all
    validate and error cleanly; and per-match `access_overrides` SURVIVE a rule
    refresh and serialization (the explicitly-documented "don't fold into rules"
    invariant holds). **transform cross-products** — transforming a snake head
    drops its segments and revert restores them; transforming an aura anchor
    keeps the binding and re-stamps to the NEW footprint (25→36 cells);
    transforming a mounted rider preserves the mount; `!history undo` after a
    transform fully reverts name/footprint/inventory. OBSERVATION (not fixed, no
    design call made): transform does NOT re-validate the slot `condition` for a
    MOUNTED rider whose new form might no longer qualify — an asymmetry with the
    vehicle side, which got `transform_rider_mismatch_mode` in pass-6. Flag if
    the user wants symmetry.

## Pass 27
- **Audit-pass-27 (hands-on): CLEAN PASS — no bug found.** An adversarial /
  resource-exhaustion sweep (the class that produced the pass-20 macro runaway
  and the pass-24 ATB history leak), plus a re-entrancy audit. No code change.
  Verified:
  1. **No `$()` ARGUMENT INJECTION.** A `$()` result containing SPACES stays
     exactly ONE argument (`var_get` returning `'hello world'` lands as a single
     var value) — substitution is per-token and happens AFTER shlex, so nothing
     re-splits and a player can't smuggle extra args/subcommands through a
     computed value. A space-containing value fed to a numeric arg gives a clean
     `❌ hp amount must be a whole number ...`, not a split.
  2. **The `$()` read-only gate is airtight under attack** — every mutating
     function tried (`kill` / `var_set` / `summon` / `status_apply` /
     `damage_part`) is REJECTED with ZERO state change (entity still alive, hp
     untouched), user-defined `!func`s are banned as documented, and the
     rejection still holds when the `$()` is inside a MACRO line or a `!foreach`
     inner command (the two indirect dispatch paths).
  3. **No DEPTH-GUARD LEAKS** (the "one error wedges the feature forever"
     failure mode; audited because a leaked counter is silent). All six counters
     — `_action_depth`, `_var_event_depth` (both fire sites), `_event_depth` +
     `_event_stack`, `_death_processing`, `_alive_eval_depth`, and the new
     `MatchManager._macro_depth` — are restored in `try/finally`, and
     EMPIRICALLY: after 30 consecutive failures each (failing actions, an
     erroring var-hook passive, erroring event handlers, bad macro runs, and a
     malformed `alive_condition` across 5 kills) every counter read 0 and every
     subsystem still worked (a later good action/passive/event/macro fired, and
     death still processed).
  4. **Degenerate grids are safe** — a match created 0×0, 1×0 or with NEGATIVE
     dimensions handles `!map` / `!list` / `!state` / `!ent add` / `!map resize`
     / `!zone add` / `!tile set` without a 💥 (bounds checks reject placement
     cleanly and the renderer produces an empty map).
  - OBSERVATION (not fixed — flagged for the user): **grid dimensions are
    unvalidated and unbounded.** `!match new x X 20000 20000` and `!map resize`
    accept anything; creation is LAZY (instant, no allocation) so the cost lands
    at RENDER time, which is quadratic — measured 200²=0.01s/78 KiB,
    600²=0.05s/703 KiB, 1500²=0.45s/4.4 MiB, so 20000² would be minutes and
    ~780 MiB of output text. Discord is protected in practice (the viewport
    engages there and clips to viewport_width/height), but the CLI/gui render
    the whole grid. Deliberately NOT fixed: both commands are host-gated, the
    failure is immediate/obvious/recoverable (restart), and a `max_grid_dimension`
    default generous enough to be safe could still reject a legitimate large
    campaign map — that default is a design call. The codebase's own precedent
    (`macro_repeat_limit`'s "guards against a typo'd huge count") argues FOR
    adding one; say so and it's a small rule. RESOLVED — the `max_grid_dimension` rule (default 500)
    shipped; see the `!help find` / clone entry.
  - PROCESS NOTE for future harness authors: THREE apparent "failures" this pass
    were my own harness bugs, not engine defects — `!passive add` takes
    `target=`/`scope=` BEFORE the quoted formula (formula LAST; wrong order makes
    the formula parse as `target=hp` → "Syntax error"), and running probes via
    `bash -c "python -c ..."` mangles nested quotes inside `$()`/formulas. Write
    probes to a FILE and use the documented arg order.

## Pass 28
- **Audit-pass-28 (hands-on, LONG): snapshot `turn_order` aliasing + a DEAD
  gamerule (scenario 563).** Two new techniques carried this pass, both worth
  reusing: **systematic REGISTRY-vs-CODE invariant checks** and a **randomized
  stateful CHAOS test with structural invariant assertions**. Three findings.
  - **Snapshot aliasing of `turn_order` (HIGH, FIXED — the headline).**
    `Match.to_dict` stored `"turn_order": self.turn_order` BY REFERENCE, and
    `Entity.remove` mutates that list IN PLACE (`m.turn_order.remove(self.id)`,
    logic.py ~4270). So every entity removal retroactively SHRANK every
    already-taken snapshot, while each snapshot's `active_index` int stayed put
    — leaving snapshots internally INCONSISTENT (observed: `turn_order=['hero']`
    with `active_index=1`), and `!history undo` then restored a match whose turn
    cursor pointed past the end of its own turn order. This is the SAME
    corruption class as the pass-5/6/7/11 vars/tiles/zones/watchers fixes;
    `turn_order` was the one mutable field never copied. Fixed on BOTH sides
    (`to_dict` stores `list(...)`; `from_dict` copies on load so a restored
    match can't mutate the retained snapshot) plus a load-side CLAMP so a
    snapshot written by an older build still restores to a usable cursor.
    `rules` was also shared and is now deep-copied for symmetry (it is only ever
    rebound today, never mutated in place — copied so a future in-place edit
    can't silently reintroduce this). NOTE the diagnosis path: instrumenting
    every `active_index` writer found NOTHING, because no live method ever
    produced an out-of-range state — the snapshot was corrupted AFTER being
    taken. When live state looks fine but a restored one doesn't, suspect
    aliasing, not the writer.
  - **`var_hook_warning_verbosity` was a DEAD RULE (MED-LOW, FIXED).** Defined
    in RULES_REGISTRY, documented, listed by `!system rules`, settable — and
    read NOWHERE in the codebase. A GM could set it to `off` and warnings kept
    appearing (silent no-op config). Now honored at all three levels: `off`
    suppresses the var-hook recursion-limit warnings (both fire sites),
    `minimal` (default) keeps today's behavior, and `detailed` additionally
    annotates a DESTRUCTIVE write with the count of dropped keys. Gotcha worth
    remembering for anyone extending this: `_diff_subtree` collapses a
    structural shift (dict -> scalar) into ONE `changed` event carrying the old
    subtree as `old_value` — it does NOT emit per-leaf `removed` events — so the
    count walks `old_value` via `_walk_subtree_keys` and excludes the root
    (which survives, just holding a different value). Schema upgraded `str` ->
    `enum` so a typo'd level is rejected instead of silently meaning 'minimal'.
    New helper `Match.var_warn_verbosity()` (unrecognized value -> 'minimal',
    so a bad value can't silence real warnings).
  - **DOC FIX: `hit_location` has TWO modes, not three.** This file listed
    "modes uniform / weighted / aimed", but the engine accepts only `weighted`
    and `uniform` and RAISES on anything else — **aiming is a separate `aim`
    ARG**, not a mode, so a GM following the old wording would write
    `mode='aimed'` and get a rejection. Corrected in place, with the verified
    statistics recorded (see below).
  - **VERIFIED CLEAN — systematic invariant checks (new, reusable).** Every
    literal `rules.get("X")` name across ALL modules exists in RULES_REGISTRY
    (139 reads, no typos — a typo'd name silently returns the wrong default
    forever, so this check is cheap insurance); the only registry rules with no
    literal reference are the four `block_*`, built dynamically as
    `f"block_{mode}"`. All **189** `_MATCH_FUNC_NAMES` + **28** `_ALLOWED_FUNCS`
    are actually registered and callable (the empirical version of the
    documented "add it to `_MATCH_FUNC_NAMES` AND the namespace builder" trap —
    calling each with no args cleanly separates "unregistered" from "wrong
    arity"). `ARG_SAFE_MATCH_FUNCS` / `ARG_MUTATING_MATCH_FUNCS` exactly
    partition the match funcs. `_who_arg` handles HOOK_CONTEXT_NAMES by SET
    membership (`if slice_node.id in HOOK_CONTEXT_NAMES`), so that documented
    trap is structurally solved — new bindings are covered automatically.
  - **VERIFIED CLEAN — chaos + smoke + statistics.** A randomized STATEFUL
    chaos test (12 seeds x 200 steps over ~70 valid commands on a rich board:
    multi-tile, parts, segments, mounts, auras, statuses, fog, two matches)
    asserting structural invariants after EVERY step — no dangling
    `part_of`/`mounted_on`/`__follows`/zone-anchor/schedule id, turn_order a
    subset of entities, `active_index` in range, and state still
    JSON-serializable. It found the turn_order bug (4/12 seeds) and is clean
    after the fix. Also: 47 commands x 4 crash shapes (no args / bogus
    subcommand / no active match / both) = 188 invocations, zero 💥. Statistical
    verification of the RNG primitives (single-shot tests can't catch a bad
    weight normalization): `hit_location` weighted with weights 10/30/60 gives
    a 10/30/60 split; aiming at the 10 with the default `aim_weight` 3 gives
    30/(30+30+60) = 25% (measured 24.8%); a 0-weight side stays 0 and
    `aim_bonus=50` lifts it to ~36%; `roll_table 'a:1,b:3,c:6'` is exact and a
    0-weight entry is NEVER chosen. A NESTED aliasing audit (walking live vs
    snapshot in parallel to depth 4) now reports the snapshot fully independent
    at every level, and it stays byte-identical after mutating every live
    container — the whole corruption class is closed. Discord adapter by
    inspection: the three `" ".join(req["args"])` sites are safe (queued args
    are pre-substitution shlex strings — the gate runs before `$()`
    substitution) and `int(viewport_button_step)` is schema-guarded.

## Pass 29
- **Audit-pass-29 (hands-on, broad): gate bypass, cross-guild access, Discord
  rendering, undo (scenarios 570-575).** Started with a harness bug (scenarios
  230/232/236 never ran — no blank line before their headers; the harness now
  cross-checks header lines against parsed scenarios). Fixes:
  - **Read-only subcommand downgrade was GLOBAL → gate bypass (HIGH).**
    `READ_ONLY_SUBCOMMANDS` applied its words to EVERY host root, so for roots
    whose args[0] is content (`batch`, `emit`, `eval`, `run`) a player's
    `!batch list ; ent hp boss -40` ran unapproved. Now a per-root dict of the
    read-only subs each handler really dispatches, plus a module-end assert
    that every key is a registered command (570).
  - **Cross-guild / global-state access (CRITICAL, user design calls).** See
    §3 "Server workspaces" + "Disk access is confined to the server's saves folder":
    admin-only bot-wide commands, target-host checks, `saves/` confinement
    (572-574). Verified before the fix: token file readable via `!run`; one
    guild's user opened `ent` to all in another guild's match via `!system
    access`; `!match use <id>` gave an omniscient view of another guild's
    fogged board.
  - **Undo dropped the approval queue (MED).** `_restore_snapshot` builds a
    fresh Match; `pending_requests`/`_request_seq` are runtime-only, so every
    undo silently dropped queued player requests and restarted ids at r1 — a
    Discord Approve button still on screen then resolved a DIFFERENT request.
    Now carried across; `_ApprovalView._pop_own` also resolves by identity (571).
  - **Repeated `!undo command 1` was stuck (MED).** `truncate_after` kept the
    restored command snapshot (= the current state), so each later single undo
    re-restored it while reporting "Undid 1 command(s)". Command snapshots are
    now dropped on restore; round/turn ones stay (they mark a START) (575).
  - **Command autosaves had no count cap (MED, user default 100).** The turn
    window only prunes on turn advance, so a setup phase kept a full match
    snapshot per command (600 tile edits → 16 MB). New rule
    `autosave_command_retention_max` (575).
  - **Discord colored maps (MED, Discord-only).** A 30×30 viewport with ~16
    team-colored units is >2000 chars: `_split_for_discord` split INSIDE the
    ```ansi fence (garbage on both halves) and an over-cap board edit raised,
    silently DROPPING the auto-update board. Splitter is now fence-aware
    (property-tested: size, balanced fences, content preserved, no empty
    blocks); `_board_render` degrades (no color → no legend → a message naming
    the viewport rules); `render_ascii` emits one ANSI code per same-color RUN
    (a colored-terrain board 4478 → 2050 chars, cells decode identical); empty
    messages are skipped at the send chokepoint (Discord rejects them).
  - **Graphics canvas sizing (MED).** `render_match_png` rendered at full cell
    size then downscaled (80×80: an 8000² intermediate, 3.2 s); the GUI had no
    cap (40×40 at 4× zoom ≈ 1 GB). `sprite_render.fit_cell_size` sizes the
    canvas to the pixel budget first (0.33 s); GUI caps at 8000 px and pins its
    zoom readout.
  - **Fog reveal records piled up (LOW-MED perf).** One record per
    `!reveal_fog`; every fog check scans them (300 permanent → 166 ms per
    60×60 render). Records sharing an expiry now merge (23 ms).
  - **Undo vs channel bindings (MED, fog leak → gamerule, user call).** A
    snapshot carries `bound_channels` (incl. each channel's `pov`), but
    `MatchManager.active_by_channel` is not snapshotted. So undoing past a
    `!match bind pov=red` left the players' channel ACTIVE on the match but
    UNBOUND = omniscient: their `!map` showed the fogged board in full. New rule
    `undo_channel_bindings_mode` (enum, default `keep`): `keep` = the live
    bindings survive every undo (bindings are setup, not game state); `revert`
    = restore the snapshot's bindings AND detach every channel bound now but not
    in the snapshot (a channel unbound since the snapshot is re-bound, and
    re-pointed only if it isn't showing another match); `confirm` = when the
    bindings differ, refuse and list the differences. Every undo/restore accepts
    a one-call `bindings=keep|revert` override. Core: `_restore_snapshot(...,
    bindings, notes)` + `_resolve_bindings_mode` / `_binding_diff` in
    vtt_commands.py (577).
  - **Smaller fixes in the same pass (576 + probes).** `!log format` edits a
    SHARED system template → admin-only; `!log clear` → host (was open to all).
    `!run` nesting is bounded by `macro_recursion_limit` (a self-referencing file
    recursed until Python's stack limit). Team passives are labelled as team
    passives in failure warnings. `saves_path` treats a cross-drive path as
    outside `saves/` instead of raising. Discord approval buttons disable
    themselves and say so on timeout. `bot.py` names the file it really reads
    (`1bot_token.txt`).
  - **Player queries respect the channel POV (HIGH fog leak → gamerule, user
    call).** `!ent info <hidden id>` printed the full card, `!dist` located
    hidden units, `!find`/`!foreach` listed them, `!history diff` reported their
    changes, and `!part/!mount/!action/!passive/!clamp/!mod/!schedule` reads
    exposed them — while `!map`/`!list` hid them. New rule
    `pov_filters_queries` (bool, default on): under a team POV (channel binding
    or `!as view`), `_query_eid` makes a hidden entity read exactly like a
    missing one ("Entity '<typed>' not found"), listings skip hidden rows
    (`_pov_hides`), a hidden rider shows as `(unseen)` in `!mount list` (the
    capacity figure stays true), `!ent info` renders a disguise's decoy card,
    and `!history diff` is host-only while fog or entity_visibility_condition is
    active. Your own team's units — a body part counts as its root body's team
    — are never hidden (a hidden rider is still yours) while the
    `pov_own_team_visible` rule is on (default; audit-pass-35 made it a rule
    and extended it to the map). Helpers `_query_pov` /
    `_pov_hides` / `_query_eid` / `_acts_as_host` in vtt_commands.py. Also
    `!part info` (full var JSON, the data `!ent dump` is host-gated for) left
    READ_ONLY_SUBCOMMANDS — players keep `!part list` (579-580).
  - **Formula resource bounds (HIGH, bot-wide DoS).** A formula's cost followed
    its ARGUMENTS, and inline `$()` gives read-only formulas to every player:
    `!dist $(9**9**9) 1 1 1` froze the bot for every guild, `'a'*10**10`
    allocated 10 GB, and `cells_in_*` / `entities_in_rect/cone` / every sight
    line (`has_los`, `raycast`, `entities_on_los`, ...) walked as many cells as
    the arguments asked. `_ArithGuardTransformer` rewrites `** * + %` into
    bounded `__safe_*` helpers before EVERY compile (ints capped at Python's
    int-to-string digit limit, checked before computing; strings/lists at the
    new `formula_size_limit`; `%` on a string rejected); new
    `formula_cell_limit` caps geometry generation and `Match._check_line_budget`
    caps sight lines. A fuzzer over every `_ALLOWED_FUNCS` +
    `ARG_SAFE_MATCH_FUNCS` function with huge args finds nothing over 0.4 s.
    NEW FORMULA FUNCTIONS whose cost scales with an argument need the same
    budget (578).
  - **Discord adapter (probe-verified with stubs).** `!<alias>` never worked on
    Discord (only built-in roots are registered with discord.py; an alias died
    as CommandNotFound) — `on_command_error` now routes aliases of the
    channel's active match through `_dispatch`, ignoring other unknown `!words`
    so other bots' commands draw no reply. `!map image` / image boards ran
    `render_scene` in a worker thread while the event loop mutated the match
    (and render_scene switches on the shared `_vision_memo`, which a race could
    leave on for good) — the scene is now built on the loop (`scene_for_png`)
    and only the pixels in the thread (`render_scene_png`). An auto-update board
    kept refreshing after its channel was unbound, and unbound = omniscient, so
    a team board re-posted the whole fogged map — it is now retired. Text
    `!approve` ran the request in the HOST's channel (a player's `!match bind
    pov=blue` re-bound the host channel) and the Approve button permanently
    re-pointed the channel's active match — both now go through
    `run_approved_request` (requester's channel, pointer restored).
  - CLOSED (user: not a concern): with `random_seed` set, the seeded RNG's
    position isn't in snapshots, so after an undo/restore the sequence restarts
    from the seed. Leave it.

## Pass 30
- **Audit-pass-30 (hands-on, tool-driven): four reusable harnesses + fixes
  (scenarios 579 ext, 581-593).** Techniques worth reusing (all throwaway
  scripts, rebuild them from these descriptions):
  1. **POV LEAK DETECTOR** — a fogged board with distinctively-named hidden
     units (a part, a hidden rider, a corpse, an action, a status, a
     schedule, a zone, a tile, team data), then EVERY registered root x
     subcommand (from `registry._help[root]["subs"]`) x an argument pool run as
     a red-POV player; replies grepped for the hidden names/values/cells after
     stripping echoes of the typed args. ~136k invocations. Include list
     KINDS (`commands`/`turns`) in the pool — the first version missed
     `!history list commands` for lack of them.
  2. **STATEFUL CHAOS** — ~70 command shapes (host + a red-POV player
     context, undo/restore with bindings, transfer between two matches, ATB
     toggling, disguises) on a rich board, asserting after EVERY step: no
     dangling `part_of`/`mounted_on`/`__follows`/aura anchor, in-bounds
     (coordinates are 1-BASED: `1 <= x <= grid_width`), turn cursor in range,
     depth counters / vision memo / event stack at rest, JSON round-trip
     idempotent, and manual saves never lost. Wrap every registry handler to
     print the traceback of a non-VTTError. NOTE `!ent move <id> <n> <dir>`
     (count first) but `!ent push <id> <dir> [n]`.
  3. **FORMULA-FUNCTION FUZZER** — every `_ALLOWED_FUNCS`/`_MATCH_FUNC_NAMES`
     name x wrong-typed / missing-entity / wrong-arity args under a 1 s
     SIGALRM, classifying "Runtime error:" messages that are raw Python.
  4. **COMMAND FUZZER** — every root x subcommand x a junk/ids/numbers/paths/
     `$()` pool as HOST (~100k), flagging 💥 / Runtime / timeouts. Redirect
     `vtt_commands.SAVES_DIR` to a temp dir first (it writes save files).
  Also a MODEL-BASED undo test (random mutations + `undo command N`, checking
  each lands on the recorded state N commands back, under retention caps
  100/5/-1) — exact.
  Fixes:
  - **Failed action wiped the undo history (HIGH).** `action._rollback_match`
    rebuilds the match from a pre-state taken WITHOUT history and copied the
    empty `history` over the live one — every `fail()`/body exception AND
    every interactive `choose()` replay deleted all autosaves and manual
    saves. `history` is now preserved, and `_check_rollback_fields` raises if
    any Match field is neither serialized nor preserved (the list went stale
    twice) (584).
  - **Despawn left the part subtree behind (HIGH, dangling-id class).**
    `Entity.remove` (`!ent remove`, `!part remove`) didn't remove the body's
    parts, leaving `part_of` naming a gone id (a later same-id entity would
    inherit them). It now removes the subtree at the chokepoint (death and
    transform already did). Removing or detaching a middle snake SEGMENT now
    re-links the one behind it (`Match._splice_out_segment`); a detached
    segment loses `__segment`/`__follows`; `copy_entity` drops a `__follows`
    pointing outside the copied body; transform/revert keep the TARGET's own
    segment linkage (`_SEGMENT_LINK_VARS` — a unit's place in a chain is
    identity), and a stashed part that can't be re-placed on revert is
    reported and the chain closes around it (581, 582, 586).
  - **Crash replies after a lethal hook (MED).** `!ent move/push/pull`,
    `!mount dismount`, `!ent transform/revert` looked the unit up after an
    operation that can kill it (a lava tile's on_enter, a 0-hp transform) and
    💥'd. AND `CommandRegistry.run` skipped the post-command bookkeeping on
    both error branches, so a command that mutated then failed had no undo
    entry of its own; the bookkeeping (which records only on a real state
    change) now runs after errors too (582, 585).
  - **More POV leaks (pov_filters_queries):** bare `!turn` listed hidden
    units (now filtered like `!list`, disguise names; `!turn next` says "an
    unseen unit's turn"), and `!log` / `!history list <kind>` reported hidden
    units' events and verbatim command labels — now refused for non-hosts
    while the view is fogged or filtered, via the shared
    `_whole_board_read_blocked` (which also counts tile/zone/corpse visibility
    conditions) (579).
  - **Formula errors name the function (LOW).** `_cell_arg` for the sight
    prims (no more "invalid literal for int()"), a radius check in
    `entities_in_area`, `_runtime_msg` strips `FormulaEngine._namespace.
    <locals>._` from arity errors, and a nameless summon template is named
    after its minted id (was a bare "Runtime error: 'name'") (583).
  - **`saves_path` refuses names Windows can't hold as files** (`:` = an NTFS
    alternate data stream, CON/NUL/COM1…/LPT1… open DEVICES, `<>"|?*`,
    trailing dots/spaces) (573 ext).
  - **HOSTILE chaos mode** (the chaos harness with lethal global passives,
    lava tiles and killing zones layered on) found the "unit removed
    mid-operation" class, all fixed:
    - `move_dirs` kept walking a mover a tile hook had killed (it returned
      a log for a ghost). It now stops the moment the mover leaves
      `m.entities` and skips the stop/moved hooks.
    - `move_group_dirs` looked each member up with `entities[eid]` (KeyError
      once one died) and fired stop hooks for dead members. Removed members
      are now skipped (587).
    - `damage_spread` routed shares to parts the cascade had already removed
      (destroying a vital part kills the parent, which removes the siblings).
      The whole blast rolled back. It now skips gone parts and stops when the
      target itself is gone (588).
    - In kill mode, `resize_grid` computed the off-grid set once, so a unit
      that a death hook summoned into the cut region survived off-grid. It
      now loops until nothing is off-grid (589).
    - The `!turn next` / `!ent swap` replies say what happened when the
      actor was removed by its own effects.
  - **More POV:** `!map center <hidden id>` framed the camera on the hidden
    unit, revealing its position. It now reads as "no entity" (579 ext).
  - **Read-only forms of elevated commands.** ELEVATED_ARGS looked only at
    args[0], so `!map layer list` / `!map teamcolor list` were queued for
    approval with their mutating siblings. `_ELEVATED_READ_FORMS` names the
    exact read forms that stay player-available (590).
  - **Design calls the user made at the end of the pass (591-593):**
    - **A unit that dies to its own turn-start effects passes the turn on.**
      A dead unit can't hold the turn, so `Match._start_current_turn` (round
      mode; the ATB loop does the same by charge bar) starts the next unit's
      turn at once, logging "💀 `x` died at the start of its turn; the turn
      passes to the next unit." The successor is the first unit after the
      dead one in the order as it stood when its turn began; a wrap fires the
      round hooks. Bounded by the order size. When nobody is left, `!turn
      next` says so and shows the log (591).
    - **A displaced located part goes to the nearest free cell** on
      transform/revert, with a warning naming both cells; it is dropped only
      when no cell fits. Placement is two-phase so a displaced part can't
      take a later part's stored cell (586).
    - **`segment_removal_mode` rule** (close default | cascade | split |
      death; segment > head `__segment_removal_mode` > rule) decides what a
      GM despawn of a snake segment does to the body. `!ent remove`, `!part
      remove` and `remove_entity` go through `Match.despawn_entity`;
      internal removals (head removal, death, transform, transfer) and `!part
      detach` always close the chain (592).
    - **`team_data_visibility` rule** (own default | all | host) filters
      `!team list/get`; `!team get` is now player-available (593). `!log`
      stays host-only under a hidden view (no per-line filtering).
  - **`!match new <id> <name> <w> <h> <system>`** ignored the bare system
    name and silently made a default-system match; scenarios 32/106/146/147
    had never run under the systems they describe. The bare form now
    selects the system; any other unknown argument is an error.

## Pass 31
- **Audit-pass-31 (hands-on): the scenario suite as a bug detector
  (scenarios 594-595).** Technique worth reusing: list every `❌` / `⚠️`
  reply and every "X → Y" figure in the Expected prose, and check each
  against its scenario. Scenarios that "pass" while testing nothing hide
  real bugs. Fixes:
  - **Round 1 opened on the first-ADDED unit (HIGH).** `_rebuild_turn_order`
    kept the pointer on the current unit even before the match started, so
    whoever was added first took the first turn regardless of initiative
    (scenario 168's prose even called it "the spawn-order quirk"). The
    pointer now resets to the order's top until the first `!turn next`.
    KNOWN LIMIT: a pre-start `!turn set` is lost if units are added
    afterwards — set the opener last.
  - **Killing the unit whose turn it is skipped the next unit (HIGH).**
    Removal moved the pointer onto the successor; `!turn next` then fired
    the successor's turn-end hooks and advanced past it. `Entity.remove` now
    sets the serialized `Match.turn_vacated` ("same" | "wrap"); `next_turn`
    skips the gone actor's turn-end and starts the successor without
    advancing (wrapping the round if the dead unit was last);
    `_start_current_turn`, ATB and `!turn set` consume/clear it (594).
  - **`random_seed` ignored by `!roll` / `!table roll` / damage_spread**
    until some formula happened to run (the seeded RNG was built lazily by
    the formula engine). `Match.formula_rng()` is now the single accessor
    for every caller, and the choice-replay RNG snapshot calls it too.
  - **Silent failures in the command surface:** unknown subcommands printed
    only help (`_help_fallback` now leads with "❌ `!x y` isn't a valid
    command"); missing arguments did the same (now "is missing arguments");
    mistyped `key=` options were dropped (`_check_options`: clamp, reveal_fog
    `turns=`, store save, undo `bindings=`, bare `!map`, passive
    `target=`/`scope=`); stray trailing words were dropped (`_check_tail` on
    `!ent add/tp/hp/init/set_var` — an unquoted `hello world` stored
    "hello"); 21 error replies lacked the ❌ prefix. New: `!match list`
    (host-gated like bare `!match` back then, when it listed every match
    bot-wide; player-available since the server-workspace split) and
    `!match info` (this channel's match, player-available) (595).
  - **Rotten scenarios repaired** (each "passed" while testing nothing):
    removed subcommands (`!ent team`, `set_facing`, `list_vars`, `macro
    def`, `turn start`), seeds set on systems that didn't exist, units
    added before `!match use` or off-grid, value-less `set_var` used as a
    read, a truncated scenario (107), stranded stages (13, 26), and prose
    whose numbers were wrong (475, 545) or described the old turn order.
  - **`name` is a reserved var path (user call, scenario 596).** A unit's
    display name wasn't readable from formulas (`entity[x].name` errored).
    `logic.RESERVED_VAR_PATHS` = x / y / name: entity FIELDS that read like
    vars. `entity[x].name`, `var_get(x,'name')` and `var_has` read the real
    name (`formula._read_entity_path`); `!find` predicates / `show:` /
    `sort:` read the name SHOWN to the viewer's POV, so a disguise's decoy
    name is what a fooled team searches by (`_entity_path_value`). No var
    may exist at or under a reserved path: `Entity.write_var` refuses it
    (`reserved_var_path_error`, pointing at `!ent rename` / `!ent tp`), as
    do the formula writes and `!defvar add`. Renaming stays a command; a
    formula can't rename. A nested read (`name.first`) is a ❌.
  - Pre-start `!turn set` lost when units are added afterwards: the user
    chose to keep this (set the opener last).

## Pass 32
- **Audit-pass-32 (hands-on, freshest code first): `!again` recursion +
  `!ent hp` reply (scenarios 646-647).** Probes, the pass-30 chaos harness
  extended with every new command (pause/resume with the host rule
  toggled, `!again` / alias of it, `!cancel`, tile copy, hp forms, `!map
  cell`, `!find count/ids`, the new formula functions, clone; plus a
  "held commands only while paused" invariant — 48 seeds normal + hostile,
  clean), the formula fuzzer over the new functions (clean) and the
  command fuzzer over the touched roots (37.5k runs, clean). Fixes:
  - **`!again` recursed until Python's stack limit (HIGH).** Inside a
    batch / macro / foreach / `!run` file / action `cmd()`, the remembered
    command is the one CONTAINING the `again` line, so it reran its
    container forever (`!batch eval 1 ; again` → 💥). An alias of `again`
    was remembered under the alias name, so `!ag` reran itself. Now:
    `dispatch_no_snapshot` refuses `again` with a ❌; `run()` skips
    remembering when the ALIAS-RESOLVED name is again/as; a contextvar
    `_AGAIN_ACTIVE` refuses a replay that reaches `again` again (a name that
    became an alias of `again` after it was stored); `again` is in
    `_SELF_DISPATCHING_COMMANDS`. `_effective_access` returns `all` for
    `again` regardless of overrides: gating it queued the bare word, and
    approving that replayed the APPROVER's last command.
  - **Clicks were recorded as typed commands.** A Discord Approve button
    (via `run_approved_request`) and the Resume button ran `registry.run`
    at depth 0, so the click became the clicker's `!again` command. Both
    now raise `_RUN_DEPTH` to 1 first.
  - **Held commands ran against the holder's CURRENT match.** `!match
    resume` dispatched each held command in its channel, which might show
    another match by then. The re-point logic of `run_approved_request` is
    now the shared context manager `_channel_pointed_at(mgr, ch, mid)`,
    used by approvals, resume, and the Discord Resume button.
  - **`!ent hp` dropped hook output and deaths (pre-existing).** heal /
    damage went through the hp property, which discards write_var's log,
    so on_var passives fired silently (`!ent set_var` always showed them),
    and a lethal set said nothing about the death. The handler now writes
    once through `write_var`, appends the hook log, adds "`x` died." when
    the unit left the match, and rebuilds the turn order on an alive/dead
    flip the way heal_entity / damage_entity did. Also: a doubled sign
    (`+-5`, `--5`) is refused, an integer amount is read exactly (no float
    round trip for long numbers), and a zero change reads "hp unchanged".
  - `!ent move` / group move: "Unexpected token" replies lacked the ❌.
  - Verified clean: despawn-hook re-entrancy (`remove_entity(self)` /
    `kill(self)` in an on_entity_despawned handler), tile copy with corpses
    on both cells + undo, ATB prints no round line, POV of `!find
    count/ids` / `!map cell` / `!whoami` / `!match info`, `!roll odds`
    caps and edge specs, the skip loop.

## Pass 33
- **Audit-pass-33 (hands-on): all-or-nothing body spawns + input edges
  (scenarios 648-650).** The pass-30 POV leak detector re-run over every
  command added since (546k invocations, extended pool: `!map cell`
  coordinates, `for=`, `count`/`ids`, preview shapes, `status:`/`team=`/
  `near:` selectors) — no leak (two false hits: status DEFINITION names in
  `!status list` are match-wide setup like rules, and `!whoami` request
  ids that contained "999"). The full command fuzzer (98k runs, every
  root), the chaos harness (normal + hostile) and the model-based undo
  test were clean. Fixes:
  - **Multi-unit spawns are transactional (HIGH, dangling/duplicate
    class).** `copy_entity` (`!ent copy` / `transfer`) spawned the body
    into the destination unit by unit, so a located part that couldn't be
    placed left the parts before it there (a partial duplicate beside the
    untouched source); and a transferred unit killed by its own
    on_entity_despawned handler as it left lived on in the destination
    beside its corpse. The destination is now snapshotted and restored in
    place with `action._rollback_match` on any spawn failure, and when the
    unit dies on the way out (a NEW corpse with its id) — copy_entity then
    returns `(None, log)` and the command reports "died as it left".
    `!ent clone` claimed all-or-nothing but validated only anchor cells;
    a footprint / located-part / block refusal midway left the clones made
    so far. It now rolls the match back the same way. The transferred copy
    is the unit as it stood when the transfer began: writes a despawn
    handler makes to the leaving unit don't carry.
  - **Revive drops no limbs silently.** `revive_corpse` skipped a located
    part whose stored cell was taken, losing it for good with no message.
    It now uses transform's two-phase placement: parts that can return do,
    then displaced located parts (and the sub-parts glued to them) go to
    the nearest free cell with a ⚠️ line; dropped only when nothing fits,
    and said so.
  - **`_parse_scalar` (every `!ent set_var` / `!defvar` / `!team set`
    value) kept `inf` / `nan` / `Infinity` / `1e999` as non-finite
    floats** — a word became a number that poisons comparisons and sorts.
    Non-finite parses now stay strings. `_coerce_vital_value` caught only
    ValueError, so writing infinity to hp leaked "cannot convert float
    infinity to integer"; it now reads as "must be a finite number".
  - `!roll 1d6 1d6` joined into the malformed term `1d61d6` (the usage
    line even advertised `<dice> [<dice> ...]`): adjacent terms with no
    operator get a clear ❌, usage reads `<dice expression>`. `!roll odds`
    counted the span `hi - lo + 1` as "possible totals" (an exploding d6
    never totals 6); it counts reachable totals. The diff formatter
    (`!history diff`, undo preview, `!ent diff`) printed a missing side as
    `- 5` (read as minus five); it is now `5 -> (unset)`.
  - **User calls at the end of the pass (scenarios 651-653):**
    - **Formula changes show their output.** Every formula mutator
      (`summon*`, `kill`, `revive`, `transform`/`revert`, `remove_entity`,
      `mount`/`dismount`/`switch_slot`, `damage_part`/`damage_spread`,
      push/pull/swap, `move_entity`/steps, status apply/dispel/transfer,
      `emit`, `declare_winner`, and every var write / delete incl.
      `entity[x].path = ...`) discarded the log its Match method returned, so
      hook output and warnings from formula-driven changes never reached
      chat. They now go through `Match.surface_log`: inside an action, into
      its output buffer (shown on success, dropped with a rollback);
      otherwise into `logic.FORMULA_LOG_SINK`, a CONTEXT VARIABLE each
      `CommandRegistry.run` sets to a fresh list and shows at its end
      (`dispatch_no_snapshot` flushes after every inner line, so batch /
      macro output stays in order). A contextvar, not a per-match list: on
      Discord each message is its own asyncio task, and a shared list drained
      after a command could hand a host's hook lines (naming hidden units) to
      a player's command that ran while the host's awaited a send. A nested
      run (approval, `!again`) collects its own, so the lines land in its
      channel. No sink and no buffer (no command running) = dropped.
      NEW formula mutators must call `match.surface_log(<log>)`.
    - **Glued parts leave the roster by default.** Rule `roster_glued_parts`
      (bool, default False) + per-part `__roster_show` var (true/false, wins
      for any part, glued or not); a glued part with its own turn-order slot
      always lists (`Match.roster_shows`, used by `!list` / `!state`).
      `!part list` still shows every part. CLAUDE.md had claimed glued parts
      were hidden while the code listed them.
    - **`!turn next` reads in order:** turn-end / round lines and hook
      output first, "It is now X's turn" last.
    - **`!match clone` starts unpaused** (`clone_match` clears `paused`).
    - OPEN observation: an APPROVED player command runs in the requester's
      channel, so whatever it prints (including hook output naming units the
      requester's POV can't see) shows there. Pre-existing; formula output
      now adds to it. The pass-30 leak detector skipped queued commands, so
      approved replies were never checked.
      (On hold at the user's request.)

## Pass 34
- **Audit-pass-34 (hands-on): undo vs system rules, command interleaving,
  typo'd modes, unbounded areas (scenarios 654-662).** Two new harnesses
  worth reusing: a **RELOAD DIFFERENTIAL** (play the same random commands on
  a live manager and on a `save`/`load` copy taken mid-game, compare every
  reply and the full state each step — catches runtime-only state that
  changes behaviour; exclude the deliberately runtime-only queues: pending
  requests, held commands, `!again`) and a **HUGE-NUMBER TIMING FUZZER**
  (10⁹ / -10⁹ in every numeric position of every command and every
  state-changing formula function, under a SIGALRM). Fixes:
  - **Undo restored the snapshot's copy of `rules` (HIGH).** Undoing past a
    `!system set` ran that match on the old rules while the system kept the
    new ones, until a reload re-copied them. `_restore_snapshot` now
    re-copies the rules from the match's GameSystem (as `MatchManager.load`
    does), and a rules-only change is not an undo step
    (`_differs_in_undo_state`, `_NOT_UNDO_STATE` = paused + rules).
  - **Commands interleaved on Discord (HIGH).** Each message is its own
    asyncio task and handlers await their replies, so a second command ran
    inside the first: its changes landed in the first one's undo step
    (`!undo command 1` reverted both while naming one) and a `!batch` was
    not one unit. `CommandRegistry.run` holds a per-manager `asyncio.Lock`
    (`_command_lock(mgr)`, runtime-only), re-entrant within a task through
    the `_HOLDS_COMMAND_LOCK` context var (approvals, `!again`, held
    commands run nested). (Since the server-workspace split each server has
    its own manager, so its own lock: servers don't queue behind each other.)
  - **`!store load` was not all-or-nothing (HIGH).** It replaced the matches
    before parsing the systems, so loading a `!history export` file (no
    systems) wiped every match and then reported the error. `load` now
    builds everything into locals and commits at the end; a missing default
    system is refused; channel pointers at matches the file lacks are
    dropped. `!history import` names a whole-bot save and refuses a
    snapshot whose state doesn't load.
  - **A snapshot imported from ANOTHER match installed that match's id**
    under this match's key (two matches reporting one id; this match's
    approval requests resolved against the other). A foreign snapshot now
    restores only the board: id, name, owner, co-hosts, access overrides
    and bindings stay.
  - **Typo'd modes were silently read as a default.** `_rect_gap` (behind
    entities_within / nearest_entity / entities_in_area / `near:` /
    `within:` / `!dist`) read any unknown metric as Chebyshev — scenario 569
    had passed 'hostile' as entities_within's MODE (the relation is the
    FOURTH argument; note nearest_entity takes relation SECOND — the two
    orders differ) and passed with no filter. damage_spread split by weight
    on an unknown mode; a part's misspelled `to_main_cap` passed hits
    through uncapped. All refused now. `str` rules with `choices` are
    checked by `!system set` (they stored anything); `!ent set_var` warns
    on an engine-read var with fixed words (`_ENGINE_VAR_RULES`).
  - **Rule values are bounded.** Int rule schemas carry `min` / `max` /
    `unlimited` (-1 accepted), checked by `!system set` and shown by
    `!system rules`: `formula_cell_limit -1` used to be accepted and then
    refuse every sight line. The 16 formula-valued rules carry `"formula":
    "expression"|"program"` and are validated when set (they fail open at
    use: a typo'd visibility condition showed everything); a call to an
    unknown function is stored with a warning (a `!func` may come later).
    A NEW int or formula rule needs these schema keys.
  - **Cell conditions:** `!tile set / line / fill`, template data and `!zone
    set` warn when a `block` / `opaque` value is a broken formula;
    `_eval_block_spec` / `_eval_opaque_spec` take an `errors` list, and
    `!map cell ... for=<unit>` names each condition that failed at use and
    counted as "no".
  - **Output that was dropped:** damage_part discarded both hp writes' logs
    (a boss killed through its head died without a line) and wrote the
    part's hp on a detached object after that death removed it;
    `source.<path> = ...` in an action body dropped its hook output
    (SourceProxy); formula output now precedes the watcher poll, and `!turn
    next` puts turn-hook formula output before whose turn it is
    (`_take_formula_log`). Engine refusals inside a formula keep their own
    message; "Runtime error:" is kept for raw Python errors.
  - **Unbounded loops on user numbers:** `!reveal_fog` at/rect/around,
    `!tile fill` and `zone_fill_rect` walked the whole rectangle before
    keeping the on-grid part (a far corner hung the bot) — now clipped while
    built (`_grid_box`); `!tile line` checks formula_cell_limit;
    `_find_free_cell_near` stops at the farthest grid corner; damage_spread
    fragments are capped by formula_loop_limit. Also refused: step counts
    below 1 (`!ent move a 0 right` moved one step), a negative reveal
    radius, `turns=` below 1, negative status level / duration.
  - Verified clean: 40 reload-differential seeds, the POV leak detector
    (546k runs, only the two known false hits), the model-based undo test,
    chaos (normal + hostile), both huge-number fuzzers, the command fuzzer
    on every touched root, and the prose-quote check (every double-quoted
    ❌/⚠ reply in Expected prose appears in the actual output).

## Pass 35
- **Audit-pass-35 (hands-on): object aliasing, reload/parity differentials,
  four user calls (scenarios 663-670).** Two more reusable harnesses: the
  reload differential EXTENDED with formula commands that move dicts between
  units / teams / match vars / tiles (it reproduces the aliasing bug on the
  old code), and a COMMAND-VS-FORMULA PARITY harness (each operation run as
  `!command` and as its formula function on identical boards from several
  starting states; end states compared). Also a POV consistency check
  (`!list` vs `!find ids` vs `!ent info` under a fogged team view). Fixes:
  - **Stores shared objects (HIGH).** `entity[a].inv = entity[b].inv` /
    var_set(var_get(...)) / team_set / match_var_set / tile_set / zone_set /
    status_set stored the SAME dict, so a write to one changed the other
    with none of its hooks or clamps, and a save/load split them.
    `logic._own_value` deep-copies at every store, makes dict keys strings
    (`{1: 5}` was unreachable by the path `d.1` until a reload) and stores
    tuples as lists.
  - **Coordinates are lists (user call).** See the coord-return entry: a
    tuple-returning function's result went into a var as a tuple and came
    back from a reload as a list, so equality / `in` flipped.
    `formula._listify` wraps every built-in function's result
    (`_TUPLE_FREE_FUNCS`), the arith guard transformer compiles tuple VALUES
    as lists (loop targets and dict keys keep tuples). Output shows [3, 3].
  - **`move_step` teleported** (e.tp): no facing change, block_tp instead of
    block_walk, no on_entity_step, a snake head left its body behind, and a
    wall raised where the docs promise False. Now `move_dirs([(dir, 1)])`.
    The parity harness found it; every other pair (tp, hp, kill, revive,
    status apply/force/transfer/dispel/counter, mount/dismount, remove,
    push/pull/swap, face, team/tile/zone writes, transform/revert, aura
    anchor, var delete) matched.
  - **Own-team view (user call → rule `pov_own_team_visible`, default on).**
    `!list` dropped a team's own hidden passenger (entity_visible_to) while
    `!turn` / `!find` / `!ent info` kept it (_pov_hides); and the map applied
    entity_visibility_condition to a team's own units while the roster
    didn't. `Match.own_team_unit` now makes entity_visible_to true for the
    viewer's own units (root-body team) — map, scene, roster and queries
    agree; a hidden rider is listed but not drawn. Off = own units follow
    fog and the condition everywhere.
  - **Nested writes under a value are refused everywhere (user call).** `k.x`
    while k = 5 replaced the 5 in unit vars / team data / match vars but was
    refused in tiles / zones / statuses; `check_no_value_ancestor` refuses
    it in every store.
  - **`rounding_mode` rule (user call, default half_up).** damage_part's
    to-main share and transform's percent hp used round() (halves to even:
    50% of 5 -> 2). `Match.round_by_rule` on exact Fractions; half_up /
    half_even / floor / ceil. The formula round() is unchanged.
  - **Dotted paths with an empty segment** (`.lead`, `trail.`,
    `inventory..sword`) created keys named "" — refused by
    `check_store_path` in every store, `!defvar add` and template data.
  - **The bot never pings:** bot.py builds the bot with
    `AllowedMentions.none()` (replies echo unit names and typed arguments,
    so `@everyone` / `<@id>` text pinged the server).
  - **A loaded match always has every rule:** `Match.from_dict` overlays the
    snapshot's rules on DEFAULT_SYSTEM_SETTINGS (12 `rules.get(key, x)`
    fallbacks disagree with the registry; defensive, today's restore/load
    paths re-copy rules from the system anyway).
  - Verified clean: the command lock can't be inherited by a spawned task
    (only `to_thread` rendering is spawned); the Discord adapter path with
    two concurrent messages (stub bot, discord.py 2.7.1); the POV leak
    detector (546k runs, only the known false hits); the full command fuzzer
    (98k runs; 4 hits are `!eval` formulas subtracting from a string); the
    huge-number fuzzers; chaos normal + hostile; the reload differential.

## Pass 36
- **Audit-pass-36 (hands-on): wholesale var writes, stray words, a hidden
  NameError (scenarios 671-677).** Reusable harnesses: a STORE FUZZER (every
  read-only formula function's result written into a var, then JSON round
  trip and equality checked — clean), a STRAY-WORD DETECTOR (every scenario
  command re-run with an extra word appended; reports replies that are not
  errors and don't mention it — 181 command shapes before, 10 after, all
  variadic by design), and `pyflakes` (pip-installable; caught nothing new
  after the fix below, run it after edits to error paths). Fixes:
  - **Wholesale var writes skipped the write_var checks (HIGH).** Spawn
    (summon templates, part / segment templates) and transform set a unit's
    vars wholesale: a template `hp: 'abc'` made a unit that 💥'd `!list`, and
    a template var `x` / `name` made a var the engine can't reach.
    `logic.checked_unit_vars` (vital vars coerced to whole numbers or
    refused, reserved names refused, own copy with string keys) runs in
    `Entity.spawn` and `apply_statblock`; a transform checks BEFORE dropping
    the old parts, and refuses a form with no hp. Skipped template parts /
    segments now log a ⚠️ line. `!defvar add` refuses a non-number vital
    default.
  - **Status `level` / `duration` are number fields.** `!ent status a set
    burn level abc` was stored and the next `!status apply` 💥'd.
    `logic.checked_status_value` guards `!ent status set`, `!status data`
    and status_set; stacking keeps a float level (add_level used int()); a
    counter on a text field reports it (it read as 0 and removed the
    status). `!ent status ... add <name> <extra>` is refused.
  - **revive / transform / revert formula errors were a NameError (MED).**
    Their `except (VTTError, NotFound, OutOfBounds, Occupied)` named two
    classes formula.py never imported, so EVERY failure read "name
    'OutOfBounds' is not defined". Now `except VTTError`.
  - **Footprint (user call → refuse if it doesn't fit).** `!ent set_var a
    footprint_w 3` grew a over its neighbour (which vanished from the map)
    or off the grid, and `footprint_w 30000` hung the bot for every guild
    (every cell walk built 9*10^8 cells). `Match.check_body_fits` (only
    newly covered cells; glued / region parts and riders skipped) runs on a
    footprint write in write_var and on transform; `_cap_footprint` reads a
    side as at most grid side + 1.
  - **Negative part damage (user call → refuse).** See the locational-damage
    entry; also a text / infinite `to_main_percent` gives a clean message
    (was a raw Python error) and `absolute:<n>` needs n >= 0 (a negative cap
    healed the body on every hit). The destroyed-latch clear + aura resume
    moved from damage_part's heal branch to write_var (a plain hp write
    never cleared it).
  - **Stray words (user call → central check).** See §2 "Usage strings are
    authoritative". Annotated every unannotated subcommand (`!map` ×17,
    `!status` ×19, `!part`, `!table`, `!team`, `!watch`, `!macro`, `!mod`,
    ent copy/transfer, match win/outcome, tile line/fill, zone
    anchor/unanchor/sprite), rewrote ambiguous usages (`!reveal_fog`,
    `!ent status`, `!ent group`, `!system alias`, `!dist`, `!roll`), and
    added handler checks where an optional slot took any word: `!undo
    command zz` undid one command, `!ent copy a m 4` dropped the x, `!watch
    add ... onec` made a repeating watcher. Four scenario lines were
    themselves malformed (572 `!defvar add default ...`, 212, 569) and are
    fixed. `!map scene full` now works (it read only args[0]) and is
    host-only.
  - OPEN (user: decide later): status_counter_add on a missing (∞) duration
    removes the status — see the status-counters entry.

## Pass 37

Templates (entity + partial), right after partial templates shipped. Method:
two model-based fuzzers (random writes / deletes / gives / detaches /
copies on nested partial containers, checking after every step that the
saved file form materializes back to the unit's exact data, and after
random library edits — including edits inside nested references — that a
resync equals the pre-edit save re-materialized under the new library; 500
seeds × 400 steps), an entity-template save → spawn → save fuzzer on random
multi-tile units with glued / located / region parts, segments, statuses,
clamps and partial containers (300 seeds), a command fuzzer over every
`!template` subcommand, `!match templates` and the two formula functions
(~107k lines), and hand probes.
  - **Expansion bomb.** Four 10-key partials nested in each other expanded
    to 10^6 values: one `!template give` froze the bot 25 s (the next level
    ~4 min). New rule `partial_template_size_limit` (20000, owner ceiling):
    a node budget per materialization, charged per value built and per copy
    of a cached partial; a template that expands past it is refused at
    save / import too.
  - **Intermediate overrides weren't base.** A nested container's marker
    recorded the enclosing partial's own values (orc_base setting its axe's
    damage 8) as if the unit had changed them, so editing orc_base never
    reached saved or resynced orcs. Containers that come with a partial are
    now INHERITED (`"inherited": true`, empty lists): their base is what the
    enclosing partial holds there (`Match._container_base`), tracking
    measures against it, and a save writes only their own deviations as
    plain keys. A container copied out of its context (var_copy, a detached
    parent) is re-measured against its own partial (`rebase_written`,
    `_rebase`, by value).
  - **Save depended on the current library.** The file form compared
    against the library at save time, so a save or resync after a library
    edit misread a dict the unit rewrote wholesale or a removal inside a
    nested container. The file form is now a function of the markers alone:
    a dict the unit wrote is listed in `__removed` AND set (materialization
    applies removals first, so it replaces); removals inside a nested
    container are recorded in that container's marker; a missing path is
    merged into key by key so later library additions still arrive.
  - **Engine-kept vars.** `__part_located` / `__part_region` / snake links /
    the destroyed latch are set directly, not recorded: a resync of a unit's
    whole vars deleted them (and its default vars), a save dropped them. A
    save now writes them always and a root resync keeps them, the vitals and
    the system's default vars.
  - **Kind change.** Saving / importing / copying / restoring an entity
    template over a partial other templates reference broke every one of
    them; refused.
  - **Reserved names.** `__removed` (the file-form tombstone key) was a
    writable var name whose save then failed with a confusing message;
    reserved like `__template`. A partial carrying `x` / `name` into a
    whole-unit template was accepted and failed only at spawn; the expanded
    template is now checked at save / import.
  - **Hand-written markers** with empty or dotted-junk paths were accepted;
    marker paths are validated.
  - **Orphaned containers.** Deleting the last recorded field of a
    container the base lacks left an empty dict that the save dropped; the
    container is recorded.
  - **Smaller:** `!template save path=x` with no unit 💥'd; a repeated
    option was silently last-wins (refused); `!template refs` listed which
    vars of any visible unit changed to players although vars are host-only
    (now host-gated, a host read under pause); an undo clears the
    per-command partial cache (pins may differ).
  - OPEN: a snake whose head passed through its own body (the default)
    saves with a segment on the head's cell; spawn / revive / transform
    place that segment on the nearest free cell with a ⚠️.
