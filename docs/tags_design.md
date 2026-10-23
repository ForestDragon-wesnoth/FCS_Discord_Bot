# Tags: design

The agreed design for widening the tag system (#260), settled with the user
before any code. Status: PRs 1 and 2 built (§1-§5; CLAUDE.md §7.20);
PR 3 (§6, §7) to come. Action tags (#234) shipped first; this extends tags to
units, corpses, var containers (items), teams, maps and the template /
prefab libraries, makes every tag field match the same way, and adds an
optional vocabulary. Ask before deviating from it.

## 1. One matching rule

- **A tag** is one word: no spaces, no commas (`logic.check_tag`). The
  words `and`, `or`, `not` are reserved and can't be tags.
- **A tag filter** is either a LIST (a list, or comma / space text) or an
  EXPRESSION. A list means "any of": `[fire, cold]` matches a thing tagged
  `fire` or `cold`. "All of", "none of" and anything else is an
  expression: `fire and magic`, `animal and not (undead or construct)`.
  `not` binds tightest, then `and`, then `or`; parentheses group.
- A text value is read as an expression when it holds one of the three
  words or a parenthesis, else as a list. Commands that store a filter
  parse it and refuse a malformed one; a filter stored in free-form vars
  is parsed at use, and a malformed one matches nothing, with the error
  named where the field is reported (`!action info`, the refusal).
- The same rule everywhere a tag filter appears: zone / tile reads and
  `tag=` filters, status tag reads, action restriction lists (#234),
  modifier and shield `tags`, and every new field below. Modifier and
  shield `tags` lists change from "all of" to "any of" (user call: one
  rule); records that need every tag write `a and b`. `not_tags` stays
  on modifiers and shields as "none of": a record matches when its `tags`
  filter matches AND none of its `not_tags` is present, so the two never
  conflict (`tags: "fire or cold"`, `not_tags: [magic]` = fire or cold,
  but not magical). It is a second way to write `and not (...)`, kept
  because it costs nothing.
- **Status `tag:` tokens** (`removes`, `blocked_by`, immunity,
  resistance, dispel) take a filter after `tag:`; an expression goes in
  parentheses so the token list's commas stay unambiguous: `removes:
  tag:(fire and magic), stunned`. A bare `tag:fire` stays a single tag.
- Pure helper `tags_match(tags, filter)`: whether a tag list matches a
  filter, for formulas that compose their own checks.

## 2. Unit tags

- **The tags var**, named by a new var-name rule `tags_var` (default
  `tags`, in `VAR_NAME_RULES`): the unit's own tags, a list. Ordinary var
  writes add and remove them (templates, partials, prefabs carry it like
  any var).
- **Granted tags** come from sources and last exactly as long as the
  source gives them; they go only by removing the source or editing it
  (user call: no separate tag remover):
  - a status: its definition's `grants_unit_tags` (an instance holding
    the key replaces the definition's, as in #234);
  - an equipped container: `grants_unit_tags` on a var container under a
    scanned source root, rule `unit_tag_sources` (default `equipped`,
    with per-unit replace / add vars, the shape of `modifier_sources`);
  - the unit's team: `grants_unit_tags` in team data;
  - its body root: a part has its root's tags (all of them, granted ones
    included).
- **Engine tags**, reserved: `corpse`, `part`, `segment`, `rider`,
  `vehicle`, `map_carrier`. A part takes its root's own and granted tags,
  not the root's engine tags (it has its own: `part`). Derived live from the state the engine
  tracks (a unit stops being `rider` when it dismounts); no tags var,
  grant or vocabulary entry can hold one (refused at the write).
- **A unit's tags** (`Match.unit_tags`): own, then granted (statuses,
  containers, team, root), then engine tags, each once.
- **Corpses** keep the tags their snapshot gives (own and the statuses /
  containers it was stored with, its team's grants) plus `corpse`.
- **Reads** (POV-filtered like every unit query): `unit_tags(eid)`,
  `unit_has_tag(eid, filter)`, `units_with_tag(filter, map=)`,
  `corpse_tags(cid)`, a `tag=` filter on `entities_within`,
  `entities_in_area`, `nearest_entity` and the other unit searches, a
  `tag:<filter>` selector for `!find` / `!foreach`, a `{tags}`
  placeholder in the roster formats, and the tags with their sources in
  `!ent info`.

## 3. Actions and statuses against unit tags

- **Action fields** `target_requires_tags` and `target_refuses_tags`
  (filters): an entity / corpse target must match the first and must not
  match the second. Checked with immunity (#234) before the body: a
  single target is refused with the reason, a list target drops the
  units that fail, with a note naming each. (Corpse targets are checked
  here, since a corpse carries tags; the #234 immunity skip for corpses
  stays as it is.)
- **Status field** `cannot_affect_units_with_tag` (a filter, name
  approved): the status
  can't be applied to a unit whose tags match (fear on `mindless`).
  Checked in `apply_status` with immunity, named by
  `status_apply_block_reason`; `status_force` ignores it like immunity.
- Both read the target's full tag set (§2), so a status can grant a tag
  that opens or closes an action (a `zombified` status makes a unit
  `undead`, and banish now works on it).

## 4. Containers (items)

- `tags` on any var container: that container's own tags.
- `inner_tags`: tags reaching every container nested below it (a loot
  bag's `inner_tags: [stolen]` makes each item in it `stolen`; the bag
  itself takes none of them).
- `exclude_inherited_tags` (`all` / `*` or a list) cuts what arrives from
  above, for that container and everything below it.
- `grants_unit_tags`: the holder's tags while the container is under a
  scanned source root (§2).
- None of these are action tags: actions keep `tags` / `action_tags`
  (#234).
- Reads: `container_tags(eid, path)` (own, then inherited, nearest
  first), `container_has_tag(eid, path, filter)`,
  `containers_with_tag(eid, filter, root=)` (the paths of matching
  containers, e.g. the `metal` items in an inventory).

## 5. Teams and maps

- **Teams:** team data `tags` (the team's own) and `grants_unit_tags`
  (given to every member). `team_tags(team)`, `team_has_tag`.
- **Maps:** a map var named by a new rule `map_tags_var` (default
  `tags`, in `MAP_VAR_NAME_RULES`). Units on a map don't take its tags.
  `map_tags(map)`, `map_has_tag(map, filter)`, `maps_with_tag(filter)`.

## 6. Libraries

- **Unit templates:** a template's tags are its saved unit's tags var (no
  second field). `!template list tag=<filter>`.
- **Partials and prefabs:** a metadata `tags` field, set with `!template
  tags <name> ...` / `!prefab tags <name> ...`; `list tag=<filter>` on
  both.

## 7. Vocabulary

- A system library `tag_vocabulary` (tag -> description), inherited
  entry by entry like the other libraries; `!system tags
  <add|remove|list> <system> ...`. Read live from the system (it only
  explains and warns).
- Rule `tag_vocabulary_mode`: `off` (default) / `warn`. With `warn`, a
  command storing an undeclared tag adds a ⚠️. `!system lint` lists
  undeclared tags whenever a vocabulary exists. Tab completion and `!help
  tag <tag>` show the declared tags and their descriptions. Nothing is
  ever refused for being undeclared.
- No implications between tags (`skeleton` -> `undead`) for now (user
  call).

## 8. Build order

1. **PR 1:** the matcher (§1, without converting the old fields yet),
   unit and corpse tags with their sources and engine tags (§2), the
   reads, the action and status fields of §3.
2. **PR 2:** every existing tag field on the one rule (modifiers and
   shields to "any of", expressions everywhere, scenarios rewritten),
   containers (§4), teams and maps (§5).
3. **PR 3:** library tags (§6) and the vocabulary (§7).

Each PR: scenarios per behaviour with a failure case, the multi-tile
smoke test (a part's root tags, a rider's `rider` tag, a 2x2 body in a
`tag=` area search), the POV leak and existence-oracle detectors on the
new reads, the replay differential and usage drift.

## 9. Open questions

None left from the first discussion: `not_tags` stays beside expressions
(§1), status `tag:` tokens take expressions in parentheses (§1), and the
status-side field is `cannot_affect_units_with_tag` (§3).
