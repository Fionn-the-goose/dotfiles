import bpy
import os
import numpy as np
from bpy_extras import anim_utils
import bisect
# import sys

from bpy.app.handlers import persistent
from . import bake_ops
from . import subscriptions
from . import addon_updater_ops
from . import multikey

# --- Layer helpers ---------------------------------------------------------
# Post-hierarchical-migration invariant: obj.Anim_Layers is 1:1 with
# anim_data.nla_tracks in the same order. Each layer is a "real" NLA-backed
# layer; "group" identity is derived (a layer is a group iff something points
# to it via parent_layer). The helpers below are thin wrappers that exist for
# call-site readability and to keep one place to harden against bad state.

# Re-entrancy guard for mute/lock cascades (parent → descendants).
_group_cascade_skip = False

def row_to_layer_index(obj, row):
    """Row in obj.Anim_Layers -> NLA-track index. Identity since the mirror is 1:1."""
    if obj is None or row < 0:
        return -1
    if row >= len(obj.Anim_Layers):
        return -1
    return row

def layer_to_row_index(obj, layer_idx):
    """NLA-track index -> row in obj.Anim_Layers. Identity since the mirror is 1:1."""
    if obj is None or layer_idx < 0:
        return 0
    return min(layer_idx, max(0, len(obj.Anim_Layers) - 1))

def active_item(obj):
    """The currently-active Anim_Layers item, or None."""
    if obj is None:
        return None
    items = obj.Anim_Layers
    row = obj.als.layer_index
    if not items or row < 0 or row >= len(items):
        return None
    return items[row]

def is_layer_row_active(obj):
    """True if there's a valid active row. Kept as a thin wrapper because many
    operator poll() methods read clearer with this name."""
    return active_item(obj) is not None

def nla_idx(obj):
    """NLA-track index for the active row. Identity wrapper around layer_index
    (the row-vs-track distinction is gone post-migration). Returns -1 if no
    valid active row."""
    if obj is None:
        return -1
    items = obj.Anim_Layers
    row = obj.als.layer_index
    if not items or row < 0 or row >= len(items):
        return -1
    return row

# --- Hierarchical helpers (Phase 0 of GROUP-row -> hierarchical refactor) ---
# These operate on the new `parent_layer` string field. During Phase 0 they
# coexist with the legacy `parent_group`/`type=='GROUP'` model: parent_layer is
# empty until migration runs, so these helpers return trivial results for
# pre-migrated data — which is the correct answer for a flat hierarchy.

def layer_children(obj, layer_name):
    """Layers whose `parent_layer` equals `layer_name`. Pass '' for root-level layers."""
    if obj is None:
        return []
    return [it for it in obj.Anim_Layers if it.parent_layer == layer_name]

def layer_descendants(obj, layer_name):
    """All transitive descendants of `layer_name`, BFS order. Cycle-safe via visited set."""
    if obj is None or not layer_name:
        return []
    result = []
    queue = [layer_name]
    seen = {layer_name}
    while queue:
        current = queue.pop(0)
        for it in obj.Anim_Layers:
            if it.parent_layer == current and it.name not in seen:
                result.append(it)
                seen.add(it.name)
                queue.append(it.name)
    return result

def layer_ancestors(obj, layer_name):
    """Chain from immediate parent up to root, in that order. Stops on cycle or broken ref."""
    if obj is None or not layer_name:
        return []
    by_name = {it.name: it for it in obj.Anim_Layers}
    cur = by_name.get(layer_name)
    if cur is None:
        return []
    result = []
    seen = {cur.name}
    while cur.parent_layer:
        if cur.parent_layer in seen:
            break
        parent = by_name.get(cur.parent_layer)
        if parent is None:
            break
        result.append(parent)
        seen.add(parent.name)
        cur = parent
    return result

def subtree_root(obj, layer_name):
    """Topmost ancestor with no `parent_layer`. Returns the layer itself if already root, or None if not found."""
    if obj is None or not layer_name:
        return None
    by_name = {it.name: it for it in obj.Anim_Layers}
    cur = by_name.get(layer_name)
    if cur is None:
        return None
    seen = {cur.name}
    while cur.parent_layer:
        if cur.parent_layer in seen:
            break
        parent = by_name.get(cur.parent_layer)
        if parent is None:
            break
        seen.add(parent.name)
        cur = parent
    return cur

def is_group_layer(obj, layer_name):
    """True iff any other layer references `layer_name` as its `parent_layer`."""
    if obj is None or not layer_name:
        return False
    return any(it.parent_layer == layer_name for it in obj.Anim_Layers)

def _would_create_cycle(obj, child_name, new_parent_name):
    """True if setting child.parent_layer = new_parent would create a cycle.
    Returns False for no-op cases (empty new_parent, self-parenting, broken ref)."""
    if obj is None or not new_parent_name or new_parent_name == child_name:
        return False
    by_name = {it.name: it for it in obj.Anim_Layers}
    if new_parent_name not in by_name:
        return False
    cur = by_name[new_parent_name]
    seen = {new_parent_name}
    while cur.parent_layer:
        if cur.parent_layer == child_name:
            return True
        if cur.parent_layer in seen:
            return False  # pre-existing cycle (sanitization handles elsewhere)
        parent = by_name.get(cur.parent_layer)
        if parent is None:
            return False
        seen.add(parent.name)
        cur = parent
    return False

def layer_subtree_size(obj, layer_name):
    """Number of layers in the subtree rooted at `layer_name` (self + descendants)."""
    if obj is None or not layer_name:
        return 0
    if obj.Anim_Layers.find(layer_name) == -1:
        return 0
    return 1 + len(layer_descendants(obj, layer_name))

def subtree_size_above(obj, nla_tracks, nla_index):
    """Size of the subtree of the layer at `nla_index + 1`. Returns 0 if at top of NLA.
    Used by move-up logic to decide how many positions to shift the active subtree."""
    above = nla_index + 1
    if above < 0 or above >= len(nla_tracks):
        return 0
    return layer_subtree_size(obj, nla_tracks[above].name)

def subtree_size_below(obj, nla_tracks, nla_index):
    """Size of the subtree of the layer at `nla_index - 1`. Returns 0 if at bottom of NLA."""
    below = nla_index - 1
    if below < 0 or below >= len(nla_tracks):
        return 0
    return layer_subtree_size(obj, nla_tracks[below].name)

def sync_nla_to_layer_order(obj, context=None):
    """Ensure anim_data.nla_tracks is in the same order as the rows in
    obj.Anim_Layers. The addon's invariant post-hierarchical-migration is
    1:1 between collection and NLA (same order).

    Behavior:
      * If counts already match and orders agree -> no-op, returns False.
      * If counts differ (the collection-vs-NLA mirror has broken) -> rebuilds
        obj.Anim_Layers from nla_tracks via visible_layers() (cheap NLA-as-truth
        recovery) and returns True.
      * If counts match but orders differ -> reorders nla_tracks via
        move_layer() (operator-based, walks each track to its target slot) and
        returns True.

    Call this anywhere the LAYER/NLA pair might have drifted out of sync —
    start_animlayers, before loops that iterate the pair, etc. The function
    is a no-op in the common case so it's cheap to call defensively."""
    anim_data = anim_data_type(obj)
    if anim_data is None or not hasattr(anim_data, 'nla_tracks'):
        return False
    nla_tracks = anim_data.nla_tracks

    desired = [it.name for it in obj.Anim_Layers]

    # Counts disagree -> the LAYER<->NLA mirror is broken (e.g. blend just
    # loaded with stale state, or external NLA edits). Rebuild from NLA.
    if len(desired) != len(nla_tracks):
        visible_layers(obj, nla_tracks)
        return True

    # Fast path: already in sync.
    if all(nla_tracks[i].name == n for i, n in enumerate(desired)):
        return False

    if context is None:
        context = bpy.context

    # Selection-sort: for each target NLA position, walk the named track there.
    # Pass track_name to move_layer so it selects the right track by NAME,
    # avoiding any dependence on obj.als.layer_index (which may not correspond
    # to the same track when the LAYER<->NLA pair has just been desynced by
    # the caller).
    for target_idx, name in enumerate(desired):
        if nla_tracks[target_idx].name == name:
            continue
        current_idx = nla_tracks.find(name)
        if current_idx == -1 or current_idx == target_idx:
            continue
        if current_idx > target_idx:
            for _ in range(current_idx - target_idx):
                move_layer('DOWN', context, track_name=name)
        else:
            for _ in range(target_idx - current_idx):
                move_layer('UP', context, track_name=name)
    return True

def nla_layer_count(obj):
    """Number of layer rows in obj.Anim_Layers. Equals len(nla_tracks) when the
    collection is in sync (the post-migration invariant)."""
    if obj is None:
        return 0
    return len(obj.Anim_Layers)

# Cache for assigned_group enum items. Blender requires the items list
# returned by an `items` callback to be referenced by Python so strings
# aren't garbage-collected mid-frame.
_assigned_group_items_cache = {}

def layer_group_enum_items(self, context):
    """Dynamic enum items for AnimLayersItems.assigned_group: '(None)' plus
    every other layer (excluding self and the self's descendants — cycle
    prevention)."""
    obj = self.id_data
    items = [('NONE', '(None)', 'Root layer (no parent)', 'X', 0)]
    if obj is not None:
        excluded = {self.name} | {d.name for d in layer_descendants(obj, self.name)}
        idx = 1
        for it in obj.Anim_Layers:
            if it.name in excluded:
                continue
            items.append((it.name, it.name, f"Set parent to '{it.name}'",
                          'OUTLINER_COLLECTION', idx))
            idx += 1
    _assigned_group_items_cache[id(self)] = items
    return items

def layer_group_get(self):
    """Return enum index based on the current parent_layer string."""
    obj = self.id_data
    if obj is None or not self.parent_layer:
        return 0
    excluded = {self.name} | {d.name for d in layer_descendants(obj, self.name)}
    idx = 1
    for it in obj.Anim_Layers:
        if it.name in excluded:
            continue
        if it.name == self.parent_layer:
            return idx
        idx += 1
    return 0  # missing -> (None)

def layer_group_set(self, value):
    """Set parent_layer from enum index. Refuses cycles. After the change,
    enforces subtree contiguity in NLA so the layer's subtree sits adjacent
    to its new parent, and the just-linked layer takes the top slot among
    its siblings (above any other children of the same parent)."""
    obj = self.id_data
    if obj is None:
        return
    self_name = self.name
    if value == 0:
        self['parent_layer'] = ''
    else:
        excluded = {self.name} | {d.name for d in layer_descendants(obj, self.name)}
        idx = 1
        target_name = None
        for it in obj.Anim_Layers:
            if it.name in excluded:
                continue
            if idx == value:
                target_name = it.name
                break
            idx += 1
        if target_name is None:
            return
        # Cycle guard (redundant with descendants-excluded above, defensive).
        if _would_create_cycle(obj, self.name, target_name):
            return
        self['parent_layer'] = target_name

    # Reorder NLA. priority_name puts the just-linked layer at the top of its
    # parent's children stack — "above the other children".
    subscriptions.subscriptions_remove()
    try:
        enforce_subtree_contiguity(obj, priority_name=self_name)
    finally:
        if bpy.context.scene is not None:
            subscriptions.subscriptions_add(bpy.context.scene)

# --- End group helpers -----------------------------------------------------

@persistent
def loadanimlayers(self, context):
    '''When loading a file check if the current selected object is with animlayers, if not then check if there is something else turned on'''
    scene = bpy.context.scene
    anim_layer_objects = [AL_item.object for AL_item in scene.AL_objects]    
    #if the current object is not turned on, then check if another object is turned on
    subscribe = False
    #unhide and store all the related objects and collections because of bug prior to 3.6
    col_hide_viewlayer, col_hide_viewport, hidden_objs = unhide_collections_on_load(anim_layer_objects)

    for obj in bpy.data.objects:
        if obj is None:
            continue      
        if obj.als.turn_on and len(obj.users_scene):
            add_obj_to_animlayers(obj, anim_layer_objects)
            # One-time hierarchical-schema migration. Idempotent; bumps
            # obj.als.schema_version on success so subsequent loads skip the work.
            migrate_object_to_hierarchical(obj)
            #Make sure layer index is not more then the layers
            row_count = len(obj.Anim_Layers)
            if row_count and obj.als.layer_index > row_count - 1:
                obj.als['layer_index'] = row_count - 1
            subscribe = True
            if obj in bpy.context.view_layer.objects.values():
                start_animlayers(obj)
            else:
                load_none_view_layer(obj)
                
        elif obj in anim_layer_objects:
            obj.als.turn_on = False
            scene.AL_objects.remove(anim_layer_objects.index(obj))
            anim_layer_objects.remove(obj)

    hide_collections_on_load(col_hide_viewlayer, col_hide_viewport, hidden_objs)
    if subscribe:
        subscriptions.subscriptions_remove()
        subscriptions.subscriptions_add(scene)
    
def turn_animlayers_on(self, context):
    '''Turning on and off the NLA with obj.als.turn_on property'''
    obj = self.id_data
    scene = context.scene
    anim_data = anim_data_type(obj)
    #iterate through all selected objects, in case both were checked with alt + click
    if obj is None:
        return
    
    if self.turn_on:
        check_anim_data_start(self, obj, anim_data)
        #workaround for issues coming from version 4.1.0
        if bpy.app.version == (4, 1, 0):
            if scene.keying_sets.active is None:
                scene.keying_sets.active = scene.keying_sets_all['Location, Rotation & Scale']
            scene.tool_settings.use_keyframe_insert_keyingset = True
        subscriptions.frameend_update_callback()
    else:        
        #Remove object from animlayers collection
        i = 0
        while i < len(scene.AL_objects):
            if scene.AL_objects[i].object == obj or not scene.AL_objects[i].object:
                scene.AL_objects.remove(i)
            else:
                i += 1
       
        anim_data = anim_data_type(obj)
        
        if anim_data is None:
            #continue
            return
        tweak_mode_upper_stack(context, obj, anim_data, enter = False)
        anim_data.use_nla = False
        #iterate only over object animation, not shapekeys and apply the last replace layer
        for track in anim_data.nla_tracks:
            if not len(track.strips) or track.mute:
                continue
            #Deselect all the strips to avoid entering tweak mode from other objects
            track.strips[0].select = False
            
            #Assign the base layer to the active action
            if not anim_data.action:
                anim_data.action = track.strips[0].action
                if hasattr(anim_data, 'action_slot'):
                    anim_data.action_slot = track.strips[0].action_slot
                anim_data.action_blend_type = track.strips[0].blend_type
        
        #Remove the extra layer fcurves if they were turned on
        if self.view_all_keyframes:
            remove_multilayers_fcurves(obj, anim_data)

        #if there are no objects in AL_objects then subscriptions will be removed
        if not len(scene.AL_objects):
            obj.als.upper_stack = False
            scene.als.edit_all_layers_op = False
            
        #remove subscription only if there is no AL_objects in all the scene
        for scene in bpy.data.scenes:
            if len(scene.AL_objects):
                return
            
        if 'framerange_preview' in scene:
            del scene['framerange_preview']

        if hasattr(subscriptions, 'outofrange'):
            del subscriptions.outofrange

        subscriptions.subscriptions_remove()

def find_anim_datas(obj):
    anim_datas = []
    if hasattr(obj, 'animation_data'):
        anim_datas.append(obj.animation_data)
    if hasattr(obj.data, 'shape_keys'):
        if hasattr(obj.data.shape_keys, 'animation_data'):
            anim_datas.append(obj.data.shape_keys.animation_data)
            
    return anim_datas

def check_anim_data_start(self, obj, selected_anim_data):
    '''adds subtract layer and active action of the first layer to animation data that is currently not selected'''
    
    anim_datas = find_anim_datas(obj)
    #adding a boolean to check if the addon started already during cleanup
    start = False
    for anim_data in anim_datas:
        if not hasattr(anim_data, 'nla_tracks'):
            continue
        if anim_data != selected_anim_data:
            continue
        if nla_layer_count(obj) > len(anim_data.nla_tracks):
            obj.Anim_Layers.clear()

        if not nla_layer_count(obj) and len(anim_data.nla_tracks):
            start = True
            #Turn off until it gets a confirmation how to proceed
            self['turn_on'] = False
            bpy.ops.anim.clear_nla_warning('INVOKE_DEFAULT')
            continue
        if nla_layer_count(obj) and not action_search(anim_data.action, anim_data.nla_tracks) and anim_data.action:
            start = True
            self['turn_on'] = False
            bpy.ops.anim.clear_active_action_warning('INVOKE_DEFAULT')
            continue
        if check_override_tracks(obj, anim_data) or check_override_layers(obj):
            bpy.ops.message.layersoverride('INVOKE_DEFAULT')
            # continue
    
    row_count = len(obj.Anim_Layers)
    if row_count and obj.als.layer_index > row_count - 1:
        obj.als['layer_index'] = row_count - 1
        
    if not start:
        # subscriptions.subscriptions_remove()
        start_animlayers(obj)
        # subscriptions.subscriptions_add(bpy.context.scene)
    
def remove_old_setup(obj):
    'remove subtract track from the old addon setup'

    anim_datas = find_anim_datas(obj)
    for anim_data in anim_datas:
        if anim_data is None:
            continue
        if not len(anim_data.nla_tracks):
            continue
        if len(anim_data.nla_tracks) == nla_layer_count(obj):
            continue
        subtract = anim_data.nla_tracks[-1]
        if len(subtract.strips) != 1:
            continue

        if subtract.strips[0].blend_type == 'SUBTRACT' and len(anim_data.nla_tracks) > nla_layer_count(obj):
            tweak_mode_upper_stack(bpy.context, obj, anim_data, enter = False)
            anim_data.action = None
            anim_data.nla_tracks.remove(subtract)
            
def load_none_view_layer(obj):
    '''fix an nla bug which happens when a file is loaded and the object is excluded from view layer'''
    anim_data = anim_data_type(obj)
    if not len(obj.Anim_Layers) or not len(anim_data.nla_tracks):
        return
    i = obj.als.layer_index
    if obj.Anim_Layers[i].lock:
        return
    obj.als.viewlayer = False
    if not anim_data.use_tweak_mode:
        obj.als.upper_stack = False  
        anim_data.use_tweak_mode = True

def start_animlayers(obj):

    scene = bpy.context.scene
    AnimLayer_objects = [AnimLayers.object for AnimLayers in scene.AL_objects]
    remove_old_setup(obj)

    if obj not in AnimLayer_objects:
        add_obj_to_animlayers(obj, AnimLayer_objects)
    # Idempotent hierarchical-schema migration. The file-load path also calls
    # this from loadanimlayers; running again here covers the turn-on toggle
    # for objects that were saved with turn_on=False but still hold legacy
    # GROUP rows. The schema_version guard makes repeat calls free.
    migrate_object_to_hierarchical(obj)
    anim_data = anim_data_type(obj)
    if not hasattr(anim_data, 'nla_tracks'):
        return
    if not anim_data.use_nla:
        anim_data.use_nla = True
    
    if not len(anim_data.nla_tracks):
        return
    anim_data.nla_tracks[0].is_solo = False
    nla_tracks = anim_data.nla_tracks
    
    #check for tracks with duplicated names and assign with unique name
    track_names = [track.name for track in nla_tracks]
    for i, name in enumerate(track_names):
        if track_names.count(name) > 1:
            track_names[i] = unique_name(track_names, name)
            nla_tracks[i].name = track_names[i]
            if len(nla_tracks[i].strips) == 1:
                nla_tracks[i].strips[0].name = track_names[i]

    # If the saved obj.Anim_Layers ordering disagrees with the NLA-track order
    # (e.g. after loading a .blend whose NLA was edited externally), bring NLA
    # in line with the saved layer order before the iteration that follows.
    sync_nla_to_layer_order(obj)

    register_layers(obj, nla_tracks)

    # Check-up: enforce subtree contiguity every time start_animlayers runs.
    # Migration only fires once (schema_version guard); this catches any drift
    # introduced afterward — externally-edited NLA, Python-console parent_layer
    # writes that bypassed the dropdown setter, etc.
    enforce_subtree_contiguity(obj)
    
    frame_start, frame_end = subscriptions.get_frame_range(scene)
    #synchronize the temporary influence prorpery
    layer_items = [l for l in obj.Anim_Layers if l.type == 'LAYER']
    for i, layer in enumerate(layer_items):
        if i >= len(nla_tracks) or len(nla_tracks[i].strips) != 1:
            continue
        strip = nla_tracks[i].strips[0]

        if strip.action is None:
            continue
        layer.action_range = strip.action.frame_range
        if not layer.custom_frame_range:
            strip.frame_start = frame_start
            strip.frame_end = frame_end
        
    if len(obj.Anim_Layers):
        obj.als.upper_stack = False
        #run layer updates
        obj.als.layer_index = 0 if obj.als.layer_index < 0 else obj.als.layer_index
          
def add_obj_to_animlayers(obj, anim_layer_objects):
    '''Add the current object to the scene animation layers'''
    if obj in anim_layer_objects or obj is None or not obj.als.turn_on:
        return
    new_obj = bpy.context.scene.AL_objects.add()
    new_obj.object = obj
    new_obj.name = new_obj.object.name

def migrate_object_to_hierarchical(obj):
    """One-time migration: convert legacy GROUP-type phantom rows to real NLA-backed
    layers linked via the new `parent_layer` field. Idempotent; gated by
    `obj.als.schema_version` (0 = pre-hierarchical, 1 = post-migration).

    Returns True on success or no-op; False if migration could not run (e.g. the
    object is a library override and writes raise TypeError). On failure, the
    schema_version is NOT bumped, so the next file load will retry.

    Placement convention: each migrated group's new NLA track is created just
    below the lowest-NLA-index member of its subtree (parent at bottom). When
    the lowest member is already at NLA index 0, we fall back to creating the
    new track at index 1 (one above the lowest) — Phase 2's visible_layers
    contiguity repair will normalize this later.
    """
    try:
        if obj.als.schema_version >= 1:
            return True

        anim_data = anim_data_type(obj)
        if anim_data is None or not hasattr(anim_data, 'nla_tracks'):
            obj.als.schema_version = 1
            return True

        nla_tracks = anim_data.nla_tracks

        groups = [it for it in obj.Anim_Layers if it.type == 'GROUP']
        if not groups:
            # No GROUP rows: copy any straggler parent_group → parent_layer and bump version.
            for it in obj.Anim_Layers:
                if it.parent_group and not it.parent_layer:
                    it['parent_layer'] = it.parent_group
            obj.als.schema_version = 1
            return True

        # Snapshot GROUP data (the rows themselves will be removed).
        group_data = {}
        for g in groups:
            group_data[g.name] = {
                'expanded': g.expanded,
                'group_color': tuple(g.group_color),
                'mute': g.mute,
                'lock': g.lock,
                'solo': g.solo,
                'influence': g.influence,
                'parent_group': g.parent_group,
            }

        # Snapshot LAYER-row parent_group references (we restore these as parent_layer post-rebuild).
        layer_parents_snapshot = {}
        for it in obj.Anim_Layers:
            if it.type == 'LAYER' and it.parent_group:
                layer_parents_snapshot[it.name] = it.parent_group

        # Topological order: leaves first (groups not named as parent_group by any other group).
        # When a cycle exists, break it by treating one arbitrary remaining group as a leaf.
        sorted_groups = []
        remaining = {name: data['parent_group'] for name, data in group_data.items()}
        while remaining:
            is_parent = {pg for pg in remaining.values() if pg in remaining}
            leaves = [name for name in remaining if name not in is_parent]
            if not leaves:
                leaves = [next(iter(remaining))]
            for leaf in leaves:
                sorted_groups.append(leaf)
                del remaining[leaf]

        # Create one new NLA track per group, in leaves-first order.
        track_names_set = {t.name for t in nla_tracks}
        name_map = {}
        new_track_props = {}

        for old_name in sorted_groups:
            data = group_data[old_name]
            new_name = unique_name(list(track_names_set), old_name)
            track_names_set.add(new_name)
            name_map[old_name] = new_name

            # Collect this group's members (LAYERs by parent_group + any already-migrated nested groups).
            member_names = [ln for ln, pg in layer_parents_snapshot.items() if pg == old_name]
            for inner_old, inner_data in group_data.items():
                if inner_data['parent_group'] == old_name and inner_old in name_map:
                    member_names.append(name_map[inner_old])

            member_indices = [nla_tracks.find(mn) for mn in member_names]
            member_indices = [i for i in member_indices if i != -1]

            # Position: aim for "just below the lowest-index member" via nla_tracks.new(prev=lower_neighbor).
            prev_track = None
            if member_indices:
                min_idx = min(member_indices)
                if min_idx > 0:
                    prev_track = nla_tracks[min_idx - 1]
                else:
                    # No track below the lowest member exists; best-effort: insert just above the lowest.
                    # Phase 2's contiguity repair will normalize.
                    prev_track = nla_tracks[0]

            # Create an empty action so the strip is a non-META action strip (avoids auto-lock
            # by register_layers, which locks layers with !=1 strip or with a META strip).
            new_action = bpy.data.actions.new(name=new_name)
            if hasattr(new_action, 'id_root'):
                new_action.id_root = obj.als.data_type
            slot = add_action_slot(obj, new_action)

            new_track = nla_tracks.new(prev=prev_track)
            new_track.name = new_name
            new_strip = new_track.strips.new(name=new_name, start=0, action=new_action)
            if hasattr(new_strip, 'action_slot') and slot:
                new_strip.action_slot = slot
            new_strip.use_sync_length = False
            use_animated_influence(new_strip)

            # Reflect the legacy GROUP's mute on the new track so visible_layers picks it up.
            new_track.mute = data['mute']

            new_parent_layer = name_map.get(data['parent_group'], '') if data['parent_group'] else ''
            new_track_props[new_name] = {
                'expanded': data['expanded'],
                'group_color': data['group_color'],
                'mute': data['mute'],
                'lock': data['lock'],
                'solo': data['solo'],
                'influence': data['influence'],
                'parent_layer': new_parent_layer,
            }

        # Drop GROUP rows from the collection — they're now real NLA-backed layers.
        for i in range(len(obj.Anim_Layers) - 1, -1, -1):
            if obj.Anim_Layers[i].type == 'GROUP':
                obj.Anim_Layers.remove(i)

        # Rebuild collection from NLA. The Phase-0 visible_layers preserves the per-LAYER
        # snapshot (lock/frame_range/speed/etc.) but does NOT preserve parent_layer (the
        # snapshot only tracks parent_group). We patch parent_layer back in the next step.
        visible_layers(obj, nla_tracks)

        # Apply group-derived properties to the migrated layer rows.
        for new_name, props in new_track_props.items():
            idx = obj.Anim_Layers.find(new_name)
            if idx == -1:
                continue
            layer = obj.Anim_Layers[idx]
            layer['expanded'] = props['expanded']
            layer['group_color'] = props['group_color']
            layer['mute'] = props['mute']
            layer['lock'] = props['lock']
            layer['solo'] = props['solo']
            layer['influence'] = props['influence']
            layer['parent_layer'] = props['parent_layer']

        # Restore parent_layer for member LAYER rows (mapped through any name dedup).
        for member_name, old_parent in layer_parents_snapshot.items():
            new_parent = name_map.get(old_parent, '')
            if not new_parent:
                continue
            idx = obj.Anim_Layers.find(member_name)
            if idx != -1:
                obj.Anim_Layers[idx]['parent_layer'] = new_parent

        # Enforce subtree contiguity: each parent's descendants must sit
        # contiguously above it in NLA, with the parent at the lowest index of
        # its subtree. Members that pre-migration sat anywhere in the stack are
        # repositioned next to their parent. This is the hierarchical analogue
        # of the legacy "group members stay grouped" rule.
        enforce_subtree_contiguity(obj)

        obj.als.schema_version = 1
        return True
    except TypeError:
        # Library override or other write-restricted state; leave version untouched
        # so the next file load can retry once the override is removed.
        return False

def clean_AL_objects(scene):
    '''Cleaning AL objects list in case objects were removed from the scene'''
    i = 0
    while i < len(scene.AL_objects):
        obj = scene.AL_objects[i].object
        if obj not in scene.objects.values() or obj is None:
            scene.AL_objects.remove(i)
        else:
            i += 1

def register_layers(obj, nla_tracks):
    visible_layers(obj, nla_tracks)
    #apply the correct setup for the strips. If there are more then one strip then lock the layer
    layer_count = nla_layer_count(obj)
    for i, track in enumerate(nla_tracks):

        if len(track.strips) != 1 or track.strips[0].type == 'META' and layer_count > i + 1:
            row_idx = layer_to_row_index(obj, i)
            if 0 <= row_idx < len(obj.Anim_Layers):
                obj.Anim_Layers[row_idx].lock = True
            continue
        strip = track.strips[0]
        use_animated_influence(strip)
              
#updating the ui list with the nla track names
def visible_layers(obj, nla_tracks):
    '''Mirror obj.Anim_Layers 1:1 with NLA tracks (same order). Per-layer state
    (lock, frame range, speed, parent_layer, expanded, group_color, ...) is
    preserved across the rebuild by name. parent_layer refs that point at a
    missing layer are cleared; cycles are broken by clearing the offending ref.
    The collection-order = NLA-order invariant is the single source of truth;
    hierarchy is purely metadata on top of that order.'''

    def restore_influence(layer, strip):
        '''restore influence value if it has no keyframes and using the temprorary property'''
        if strip.fcurves:
            if len(strip.fcurves[0].keyframe_points):
                return
        layer['influence'] = strip.influence

    # 1. Snapshot per-layer state, keyed by name.
    layer_state = {}
    for it in obj.Anim_Layers:
        layer_state[it.name] = {
            'lock': it.lock,
            'frame_start': it.frame_start,
            'frame_end': it.frame_end,
            'speed': it.speed,
            'repeat': it.repeat,
            'offset': it.offset,
            'custom_frame_range': it.custom_frame_range,
            'parent_layer': it.parent_layer,
            'expanded': it.expanded,
            'group_color': tuple(it.group_color),
        }

    # 2. Rebuild one row per NLA track, preserving NLA order.
    obj.Anim_Layers.clear()
    for track in nla_tracks:
        layer = obj.Anim_Layers.add()
        layer['type'] = 0  # 'LAYER' — only one row type now (post-hierarchical migration).
        layer['name'] = track.name
        layer['mute'] = track.mute

        if len(track.strips):
            strip = track.strips[0]
            strip.name = track.name
            if strip.action is not None:
                layer['action'] = strip.action
            restore_influence(layer, strip)

        s = layer_state.get(track.name)
        if s is not None:
            layer['lock'] = s['lock']
            layer['frame_start'] = s['frame_start']
            layer['frame_end'] = s['frame_end']
            layer['speed'] = s['speed']
            layer['repeat'] = s['repeat']
            layer['offset'] = s['offset']
            layer['custom_frame_range'] = s['custom_frame_range']
            layer['parent_layer'] = s['parent_layer']
            layer['expanded'] = s['expanded']
            layer['group_color'] = s['group_color']
        else:
            # Fresh layer (not in the pre-rebuild snapshot). Init bracket keys
            # so subscriptions/ops that read via layer['key'] don't KeyError.
            layer['lock'] = False
            layer['frame_start'] = 0.0
            layer['frame_end'] = 0.0
            layer['speed'] = 1.0
            layer['repeat'] = 1.0
            layer['offset'] = 0.0
            layer['custom_frame_range'] = False
            layer['parent_layer'] = ''

    # 3. Sanitize parent_layer: clear refs to names that don't exist anymore,
    # and break any cycles by clearing the offending ref.
    existing_names = {it.name for it in obj.Anim_Layers}
    for it in obj.Anim_Layers:
        if it.parent_layer and it.parent_layer not in existing_names:
            it['parent_layer'] = ""
    # Cycle detection: walk each layer's ancestor chain; if we revisit, break.
    by_name = {it.name: it for it in obj.Anim_Layers}
    for it in obj.Anim_Layers:
        cur = it
        seen = {cur.name}
        while cur.parent_layer:
            if cur.parent_layer in seen:
                # Cycle: clear the closing-edge ref on `cur` to break it.
                cur['parent_layer'] = ""
                break
            parent = by_name.get(cur.parent_layer)
            if parent is None:
                break
            seen.add(parent.name)
            cur = parent


def enforce_subtree_contiguity(obj, context=None, priority_name=None):
    """Reorder anim_data.nla_tracks so each parent's subtree forms one
    contiguous block: parent at the LOWEST NLA index of the block, descendants
    stacked above recursively. obj.Anim_Layers follows via visible_layers().

    Sibling order at each level: ascending current NLA index by default — the
    sibling that was lower pre-reorder ends up closer to the parent. When
    `priority_name` is given, that layer is bumped to the top of its siblings
    (matches the interactive "just-linked layer goes above the other
    children" rule). Migration callers leave priority_name unset to preserve
    the user's original sibling NLA order.

    Idempotent: returns False if the NLA is already in the desired order.
    """
    anim_data = anim_data_type(obj)
    if anim_data is None or not hasattr(anim_data, 'nla_tracks'):
        return False
    nla_tracks = anim_data.nla_tracks
    n = len(nla_tracks)
    if n < 2:
        return False

    # Snapshot current NLA position of every track (used to break ties between siblings).
    current_index = {nla_tracks[i].name: i for i in range(n)}

    # Sort key: priority_name gets the highest rank within its sibling group;
    # all others sort by current NLA index ascending.
    def sibling_key(name):
        if priority_name is not None and name == priority_name:
            return n + 1  # higher than any real NLA index
        return current_index.get(name, 0)

    children_by_parent = {}
    for it in obj.Anim_Layers:
        children_by_parent.setdefault(it.parent_layer, []).append(it.name)
    for plist in children_by_parent.values():
        plist.sort(key=sibling_key)

    # DFS pre-order (parent first, then descendants): parent lands at lowest
    # NLA index of its subtree; descendants stack above.
    target = []
    visited = set()

    def emit_subtree(name):
        if name in visited or name not in current_index:
            return
        visited.add(name)
        target.append(name)
        for child in children_by_parent.get(name, []):
            emit_subtree(child)

    for root_name in children_by_parent.get('', []):
        emit_subtree(root_name)

    # Orphans (parent_layer pointing at a name that no longer exists).
    for i in range(n):
        nm = nla_tracks[i].name
        if nm not in visited:
            target.append(nm)
            visited.add(nm)

    current_order = [nla_tracks[i].name for i in range(n)]
    if current_order == target:
        return False

    # Reorder NLA. Try nla_tracks.move(from, to) per shift and verify it
    # actually took effect; otherwise fall back to channels_move via bpy.ops
    # (which always works under an NLA-editor context override).
    use_bpy_ops = False
    ctx = context if context is not None else bpy.context
    for target_idx, name in enumerate(target):
        current_idx = nla_tracks.find(name)
        if current_idx == -1 or current_idx == target_idx:
            continue
        moved = False
        if not use_bpy_ops:
            try:
                nla_tracks.move(current_idx, target_idx)
                # Verify the move stuck. If not, the API exists but isn't
                # actually shifting in this Blender version/context.
                if nla_tracks.find(name) == target_idx:
                    moved = True
                else:
                    use_bpy_ops = True
            except (AttributeError, RuntimeError, TypeError):
                use_bpy_ops = True
        if not moved:
            diff = current_idx - target_idx
            direction = 'DOWN' if diff > 0 else 'UP'
            for _ in range(abs(diff)):
                move_layer(direction, ctx, track_name=name)

    # Resync the collection to the new NLA order.
    visible_layers(obj, nla_tracks)
    return True


def use_animated_influence(strip):
    '''cleanup animated influence from the first keyframe behavior'''
    if strip.use_animated_influence:
        return
    fcu_len = len(strip.fcurves)
    strip.use_animated_influence = True
    
    if fcu_len != len(strip.fcurves):
        if len(strip.fcurves[0].keyframe_points):
            keyframes = strip.fcurves[0].keyframe_points
            # value = keyframes[0].co.y
            # print('influence value ', keyframes[0])
            if hasattr(keyframes, 'clear'):
                keyframes.clear()
            else:
                keyframe = keyframes[0]
                keyframes.remove(keyframe)
    
    strip.influence = 1

def check_override_layers(obj):
    if obj.override_library is None:
        return False
    if len(obj.override_library.reference.Anim_Layers):
        return True
    return False
    
def check_override_tracks(obj, anim_data):
    if obj.override_library is None:
        return []
    if anim_data is None:
        return []
    if anim_data == obj.animation_data:
        anim_data_ref = obj.override_library.reference.animation_data
    elif anim_data == obj.data.shape_keys.animation_data:
        anim_data_ref = obj.override_library.reference.data.shape_keys.animation_data
    if anim_data_ref is None:
        return []
    if len(anim_data_ref.nla_tracks):
        return anim_data_ref.nla_tracks
    else:
        return []
    
def check_overrides_ALobjects(obj):
    #check if an override object was added and already had animlayers turned on
    if not obj.override_library:
        return
    scene = bpy.context.scene
     
    if obj.name in scene.AL_objects:
        return
    if not scene.AL_objects:
        subscriptions.subscriptions_remove()
        subscriptions.subscriptions_add(scene)
    anim_layer_objects = [AL_item.object for AL_item in scene.AL_objects]
    add_obj_to_animlayers(obj, anim_layer_objects)   

#################################################### Multiply layer view FUNCTIONS ############################################################################
def get_fcu_layer_keyframes(obj, context, track):
    keyframes = []
    # fcurves = get_fcurves(track.strips[0].action)
    # fcurves = track.strips[0].action.fcurves
    fcurves = get_fcurves(obj, track.strips[0].action, obj.als.data_type)
    #store all the keyframe locations from the fcurves of the layer
    for fcu in fcurves:
        if fcu.group is not None:
            if fcu.group.name == 'Anim Layers': 
                continue
        #if only selected bones is used then check for the bones
        if selected_bones_filter(obj, fcu):
            continue
        keyframes = store_layer_frames(fcu, keyframes)
        
    # return sorted(set(keyframes))
    return np.sort(np.unique(keyframes))

def store_layer_frames(fcu, keyframes):
    '''storing the time also as the value, to be used for edit all keyframes'''
    length = len(fcu.keyframe_points)*2
    new_keyframes = np.zeros(length)
    fcu.keyframe_points.foreach_get('co', new_keyframes)
    keyframes = np.concatenate((keyframes, new_keyframes[::2]))
    return keyframes

def hide_view_all_keyframes(obj, anim_data):
    '''hide view all keyframes in the graph editor, to avoid the user changing the values
    and lock channels when edit all keyframes is turned off'''
    
    if anim_data.action is None:
        return
    # fcurves = get_fcurves(anim_data.action)
    # fcurves = anim_data.action.fcurves
    fcurves = get_fcurves(obj, anim_data.action)
    if not len(fcurves):
        return
    if obj.als.edit_all_keyframes:
        return
    if 'Anim Layers' in anim_data.action.groups:
        if anim_data.action.groups['Anim Layers'].lock:
            return
        anim_data.action.groups['Anim Layers'].lock = True
        return
    #if the group was not found or renamed iterate over the layers
    layer_items = [l for l in obj.Anim_Layers if l.type == 'LAYER']
    for i, layer in enumerate(layer_items):
        if layer.lock or obj.als.layer_index == i:
            continue
        fcu = fcurves.find(layer.name, index = i)
        if fcu is None:
            continue
        
        if not fcu.group.lock: #lock the groups if edit is not selected
            fcu.group.lock = True
            fcu.group.name = 'Anim Layers'
        if bpy.context.area:    
            if bpy.context.area.type != 'GRAPH_EDITOR': #hide the channels when using graph editor
                return
        
        if not fcu.hide:
            fcu.hide = True

def selected_bones_filter(obj, fcu):
    '''if only selected bones is used then check for the bones path in the fcurves data path'''
    if obj.als.only_selected_bones and obj.mode == 'POSE':
        selected_bones_path = [bone.path_from_id() for bone in bpy.context.selected_pose_bones]
        if fcu.data_path.split('].')[0]+']' not in selected_bones_path:
            return True
    return False

class EditAllLayersOperator(bpy.types.Operator):
    
    """Edits multiple layers"""
    bl_idname = "anim.edit_all_layers"
    bl_label = "Edit All Layers"
    bl_options = {'REGISTER', 'UNDO'}
    
    def invoke(self, context, event):
        
        self.obj = context.object
        anim_data = anim_data_type(self.obj)
        fcurves = get_fcurves(self.obj, anim_data.action)
        #Initialize self.fcu_layers:
        self.fcu_layers = dict()
        for i, layer in enumerate(self.obj.Anim_Layers): #look for the Anim Layers fcurve
            if layer.lock or anim_data.action is None or i == self.obj.als.layer_index:
                continue
            fcu_layer = fcurves.find(layer.name, index = i)
            if fcu_layer is None or not len(fcu_layer.keyframe_points):
                continue
            
            if fcu_layer.data_path not in self.fcu_layers:
                self.fcu_layers.update({fcu_layer.data_path : len(fcu_layer.keyframe_points)})
        
        #Detect if the mouse is using right click or left click for confirmation
        preferences = context.window_manager.keyconfigs.default.preferences
        if preferences:
            self.select_mouse = preferences.select_mouse + 'MOUSE'
            self.cancel_mouse = 'RIGHTMOUSE' if self.select_mouse == 'LEFTMOUSE' else 'LEFTMOUSE'
        else:
            self.select_mouse, self.cancel_mouse = 'LEFTMOUSE', 'RIGHTMOUSE'

        self.mouse_press = False
        context.scene.als.edit_all_layers_op = True
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}
    
    def modal(self, context, event):
        try:
            AL_objs = [AL_item.object for AL_item in context.scene.AL_objects]

            #Check if it's turned off in all anim layers objects
            if not any(obj.als.edit_all_keyframes for obj in AL_objs):
                bpy.context.scene.als.edit_all_layers_op = False
                # print('quitting modal operator, edit_all_keyframes turned off ')
                return{'FINISHED'}
            
            #Check if all anim layer objects are turned off
            if not any(obj.als.turn_on for obj in AL_objs):
                bpy.context.scene.als.edit_all_layers_op = False
                # print('quitting modal operator, anim layers turned off ')
                return{'FINISHED'}
            
            obj = context.object
            if obj is None:
                return{'PASS_THROUGH'}
            #If the current object is turned off then skip it
            if not obj.als.edit_all_keyframes or not obj.als.turn_on:
                return{'PASS_THROUGH'}
            
            if context.window_manager.operators:
                if context.window_manager.operators[-1].name == 'Delete Keyframes':
                    self.mouse_press = True
            
            # Checking for events, when to run the modal operator
            if event.type in {self.select_mouse, 'G', 'S', 'X'} and event.value != 'RELEASE':
                self.mouse_press = True
                return{'PASS_THROUGH'}
            
            # Cancelling the operation
            if self.mouse_press and event.type in {self.cancel_mouse, 'ESC'}:
                self.mouse_press = False
                return{'PASS_THROUGH'}
            
            if (event.value == 'RELEASE' or event.type in {'NONE', 'INBETWEEN_MOUSEMOVE'}) and self.mouse_press:
                self.mouse_press = False
            else:
                return{'PASS_THROUGH'}

            #If the object is different then the previous, then reseting fcu_layers
            if obj != self.obj:
                self.obj = obj
                self.fcu_layers = dict()

            anim_data = anim_data_type(obj)

            for i, layer in enumerate(self.obj.Anim_Layers): #look for the Anim Layers fcurve
                if layer.lock or anim_data.action is None or i == self.obj.als.layer_index:
                    continue

                fcurves = get_fcurves(self.obj, anim_data.action)
                fcu_layer = fcurves.find(layer.name, index = i)
                if fcu_layer is None or not len(fcu_layer.keyframe_points):
                    continue
                
                if fcu_layer.data_path not in self.fcu_layers:
                    self.fcu_layers.update({fcu_layer.data_path : len(fcu_layer.keyframe_points)})

                length = len(fcu_layer.keyframe_points)*2
                keyframes = np.zeros(length)
                # Getting all the keyframes that represent the layer. Each keyframe is a vector
                # Which show the current frame, and the second value is the frame before
                fcu_layer.keyframe_points.foreach_get('co', keyframes)
                # Creates columns from each pair of values (after and before)
                # and then flip them (before and after) using [:, [1, 0]]
                pairs = keyframes.reshape(-1, 2)[:, [1, 0]]
                org_pairs = pairs
                
                # Adding the offset from the layers and the current layers
                pairs = multilayer_reverse_offset(pairs, anim_data.nla_tracks[i].strips[0])
                pairs = multilayer_offset_evaluation(pairs, anim_data.nla_tracks[nla_idx(self.obj)].strips[0])

                #Creating a column from each pair
                changed_keys = pairs[pairs[:,0] != pairs[:,1]]
                # changed_keys = multilayer_reverse_offset(changed_keys, anim_data.nla_tracks[i].strips[0])

                #Get the frames and keyframes to compare
                frames = np.sort(pairs[:,0])
                # frames = multilayer_reverse_offset(frames, anim_data.nla_tracks[i].strips[0])

                #check if keyframes were deleted
                if self.fcu_layers.get(fcu_layer.data_path) != len(fcu_layer.keyframe_points):
                    fcurves = get_fcurves(self.obj, anim_data.nla_tracks[i].strips[0].action)
                    self.remove_keyframes(fcurves, frames)
                    self.fcu_layers[fcu_layer.data_path] = len(fcu_layer.keyframe_points)
                    continue
                        
                # memory_usage_bytes = sys.getsizeof(fcu_layers)
                # memory_usage_kb = memory_usage_bytes / 1024
                # print('memory_usage_kb', memory_usage_kb)
                
                if not changed_keys.size:
                    continue
                
                #iterate through the fcurves in the original action 
                fcurves = get_fcurves(self.obj, anim_data.nla_tracks[i].strips[0].action)   
                self.update_keyframes_postion(fcurves, changed_keys)

                
                # Reverting to the original values before the offsets
                pairs = org_pairs
                #updating the fcu layer keyframes so that value and time are equal again
                pairs[:,0] = pairs[:,1]
                fcu_layer.keyframe_points.foreach_set('co', pairs.flatten())

            # if removed_keyframes:
            #     bpy.ops.ed.undo_push(message = 'Removed Multi Layer Keyframes')
            # elif updated_keyframes:
            #     bpy.ops.ed.undo_push(message = 'Updated Multi Layer Keyframes')

            return {'PASS_THROUGH'}
        
        except Exception as e:
            # Log the error
            print("Error:", e)
            context.scene.als.edit_all_layers_op = False
            self.report({'ERROR'}, str(e) + '. Quitting Edit Multiple Layers')
            return {'CANCELLED'}
    
    def remove_keyframes(self, fcurves, frames):
        removed_keyframes = False

        for fcurve in fcurves: 
            if selected_bones_filter(self.obj, fcurve):
                continue
            if fcurve.group is None:
                continue
            if fcurve.group.name == 'Anim Layers':
                continue
            #del_keyframes = [keyframe for keyframe in fcurve.keyframe_points if keyframe.co[0] in del_keys]
            keyframe_points = list(fcurve.keyframe_points)
            while keyframe_points: # remove the keyframes from the original action
                if keyframe_points[0].co[0] not in frames:
                    fcurve.keyframe_points.remove(keyframe_points[0])
                    keyframe_points = list(fcurve.keyframe_points)
                    removed_keyframes = True
                else:
                    keyframe_points.pop(0)
            fcurve.update()
        
        return removed_keyframes
    
    def update_keyframes_postion(self, fcurves, changed_keys):
        update_keyframes = False

        for fcurve in fcurves:
            if selected_bones_filter(self.obj, fcurve):
                continue
            for keyframe in fcurve.keyframe_points:
                if keyframe.co[0] not in changed_keys[:, 0]:
                    continue

                #getting the index of the old key to get the new value from its pair
                index = np.where(changed_keys[:,0] == keyframe.co[0])[0]
                new_value = changed_keys[index, 1][0]
                #Getting the difference between the old and new key to add into the handles as well
                difference = new_value - keyframe.co[0]
                #Changing both values of the vector
                keyframe.co[0] = new_value
                if keyframe.interpolation == 'BEZIER':
                    keyframe.handle_left[0] += difference
                    keyframe.handle_right[0] += difference

                update_keyframes = True

        return update_keyframes

def remove_multilayers_fcurves(obj, anim_data):
    '''Remove old Anim Layers fcurves after viewing multiple layers'''
    nla_tracks = anim_data.nla_tracks
    tracknames = [track.name for track in nla_tracks]
    for track in (anim_data.nla_tracks):
        if len(track.strips) != 1:
            continue
        action = track.strips[0].action
        fcurves = get_fcurves(obj, action)
        if not len(fcurves):
            continue
        for i, trackname in enumerate(tracknames): 
            fcu = fcurves.find(trackname, index=i)   
            if not fcu: #remove all the fcurves/channels in the group and mark as removed
                continue
            if fcu.group.name != 'Anim Layers':
                continue
            fcu.group.lock = False
            for fcu_remove in fcu.group.channels:
                fcurves.remove(fcu_remove)

def multilayer_offset_evaluation(frames, strip):
            
    last_frame = strip.frame_start + (strip.frame_end - strip.frame_start) / strip.repeat
    offset = strip.frame_start - strip.action_frame_start * strip.scale
    
    frames = strip.frame_start * strip.scale + (frames - strip.frame_start) * strip.scale + offset# * strip.scale
    if strip.use_reverse:
        frames = last_frame - (frames - strip.frame_start)
    # frames = (strip.frame_start + (frames - strip.frame_start)) / strip.scale + offset / strip.scale

    return frames

def multilayer_reverse_offset(frames, strip):
    
    last_frame = strip.frame_start + (strip.frame_end - strip.frame_start) / strip.repeat
    offset = strip.frame_start - strip.action_frame_start * strip.scale
    
    frames = strip.frame_start * 1/strip.scale + (frames - strip.frame_start) * 1/strip.scale - offset * 1/strip.scale
    
    if strip.use_reverse:
        frames = last_frame - (frames - strip.frame_start)

    return frames

def view_all_keyframes(self, context):
    '''Creates new fcurves with the keyframes from the all the layers'''
    obj = self.id_data
    anim_data = anim_data_type(obj)
    nla_tracks = anim_data.nla_tracks
    #if animation layers is still not completly loaded then return
    if len(anim_data.nla_tracks) != nla_layer_count(obj) or anim_data.action is None:
        return
    
    #remove old Anim Layers fcurves
    remove_multilayers_fcurves(obj, anim_data)

    if not self.view_all_keyframes: #If the option is uncheck then finish edit and return
        self.edit_all_keyframes = False
        return

    for i, track in enumerate(nla_tracks):
        track_fcurves = get_fcurves(obj, track.strips[0].action)
        if i == obj.als.layer_index or track.strips[0].action is None or not len(track_fcurves) or obj.Anim_Layers[i].lock:
            continue
        #create a new fcurve with the name of the track
        fcurves = get_fcurves(obj, anim_data.action)
        fcu_layer = fcurves.new(track.name, index=i) #, action_group='Anim Layers'
        add_group_to_fcurve(obj, fcu_layer, 'Anim Layers')
        fcu_layer.update()
        fcu_layer.is_valid = True
        
        frames = get_fcu_layer_keyframes(obj, context, track)
        
        if not len(frames):
            continue
        
        frames = multilayer_offset_evaluation(frames, track.strips[0])
        frames = multilayer_reverse_offset(frames, nla_tracks[nla_idx(obj)].strips[0])

        keyframes = np.repeat(frames, 2)
        
        #create new keyframes for all the stored keys
        keyframes_amount = int(len(keyframes)*0.5)
        fcu_layer.keyframe_points.add(keyframes_amount)
        fcu_layer.keyframe_points.foreach_set('co', keyframes)
        fcu_layer.keyframe_points.foreach_set('type', [int(self.view_all_type)]*keyframes_amount)
            
        fcu_layer.hide = True
        fcu_layer.update()
        
        #Make sure lock is turned off when selecting new layer and edit is turned on
        if fcu_layer is not None and self.edit_all_keyframes:
            fcu_layer.group.lock = False 
            
    unlock_edit_keyframes(self, context)
    
def unlock_edit_keyframes(self, context):
    '''Lock or unlock the fcurves of the Multiple layers with the edit all keyframes property'''
    obj = self.id_data

    if not self.view_all_keyframes or obj is None:
        return
    
    anim_data = anim_data_type(obj)

    layer_items = [l for l in obj.Anim_Layers if l.type == 'LAYER']
    for i, layer in enumerate(layer_items): #look for the Anim Layers fcurve
        if layer.lock or anim_data.action is None or i == obj.als.layer_index:
            continue
        fcurves = get_fcurves(obj, anim_data.action)
        fcu = fcurves.find(layer.name, index = i)
        if fcu is None:
            continue
        if self.edit_all_keyframes:
            fcu.group.lock = False
        else:
            fcu.group.lock = True

    if self.edit_all_keyframes and not context.scene.als.edit_all_layers_op:
        bpy.ops.anim.edit_all_layers('INVOKE_DEFAULT')

###################################################### PROPERTY FUNCTIONS ################################################

def collect_children_collections(obj_col, col_hide_viewlayer, col_hide_viewport, layer_collection, col_checked = []):
    '''part of unhide objects collections, iterate over all the children in the viewlayer collections'''
    #iterate over all the children
    collections = bpy.data.collections
    
    for col in layer_collection.children:
        if obj_col.name != col.name and obj_col not in collections[col.name].children_recursive:
            continue
        if col in col_checked:
            continue
        
        if col.hide_viewport:
            col.hide_viewport = False
            col_hide_viewlayer.append(col)
            
        if collections[col.name].hide_viewport:
            collections[col.name].hide_viewport = False
            col_hide_viewport.append(collections[col.name])

        col_checked.append(col)
        
        #repeat the same function to iterate over the next children
        if len(col.children):
            col_hide_viewlayer, col_hide_viewport = collect_children_collections(obj_col, col_hide_viewlayer, col_hide_viewport, col, col_checked)
            
        
    return col_hide_viewlayer, col_hide_viewport

def unhide_collections_on_load(anim_layer_objects):
    '''unhide objects and collections during anim layers load to avoid errors with the nla'''
    #list of hidden realted collections in the view layer
    col_hide_viewlayer = []
    col_hide_viewport = []
    #use this for unmuting the eye icon
    hidden_objs = []
    #get all the collections related to the objects that have anim layers included
    obj_users_collection = []
    
    for obj in anim_layer_objects:
        if obj is None:
            continue
        #list of hidden related collections
        if obj.hide_viewport:
            obj.hide_viewport = False
            col_hide_viewport.append(obj)
 
        if obj.hide_get():
            hidden_objs.append(obj)
            obj.hide_set(False)
        
        obj_users_collection += [obj_col for obj_col in obj.users_collection]
        
    obj_users_collection = set(obj_users_collection)
        
    layer_collection = bpy.context.view_layer.layer_collection
        
    #get all the collections that influence the object
    for obj_col in obj_users_collection:
        col_hide_viewlayer, col_hide_viewport = collect_children_collections(obj_col, col_hide_viewlayer, col_hide_viewport, layer_collection)
    
    return col_hide_viewlayer, col_hide_viewport, hidden_objs

def hide_collections_on_load(col_hide_viewlayer, col_hide_viewport, hidden_objs):
    #revert back hidden layers, so they are hidden again
    for col in col_hide_viewlayer:
        col.hide_viewport = True     
    for col in col_hide_viewport:
        col.hide_viewport = True
    for obj in hidden_objs:
        obj.hide_set(True)
              
def tweak_mode_objs(scene):
    #store objects that are in tweak mode
    tweak_mode = {}
    i = 0
    while i < len(scene.AL_objects):
        obj = scene.AL_objects[i].object
        if obj is None:
            scene.AL_objects.remove(i)
            continue
        i += 1
        anim_data = anim_data_type(obj)
        if anim_data is None:
            continue
        tweak_mode[anim_data] = anim_data.use_tweak_mode
        
    return tweak_mode

def tweak_mode_upper_stack(context, obj, anim_data, enter = True):
    #override nla context, use a temporaray area
    #context = bpy.context
    #window = context.window_manager.windows[0]
    window = context.window
    screen = window.screen
    old_area = screen.areas[0].type
    ui_type = screen.areas[0].ui_type
    screen.areas[0].type = 'NLA_EDITOR'
    area = screen.areas[0]
    
    scene = context.scene
    #record tweak mode of other objects
    tweak_mode_objects = tweak_mode_objs(scene)
    #obj = anim_data.id_data
    # error = False
    with context.temp_override(window=window, area=area):
        # tweak mode needs to be turned on on animation data to be able to go in the context
        #if anim_data.use_tweak_mode:
        if scene.is_nla_tweakmode:
            try:
                bpy.ops.nla.tweakmode_exit()
            except RuntimeError:
                anim_data.use_tweak_mode = False
        if anim_data.use_tweak_mode:
            anim_data.use_tweak_mode = False
            
        if enter:
            #making sure there is no active action outside the nla
            if anim_data.action:
                anim_data.action = None
            
            try:
                bpy.ops.nla.tweakmode_enter(use_upper_stack_evaluation=True)
            except RuntimeError as e:
                print(obj.name, e)
                
            #check again if unhiding helped
            if anim_data.use_tweak_mode:
                obj.als.upper_stack = True
            else:
                obj.als.upper_stack = False
                #reset tweak mode that it's not appearing in Lower stack
                anim_data.use_tweak_mode = True
                anim_data.use_tweak_mode = False

    #restore tweak mode from other objects
    for obj_anim_data, value in tweak_mode_objects.items():
        if obj_anim_data == anim_data:
            continue
        obj_anim_data.use_tweak_mode = value
        
    screen.areas[0].type = old_area
    screen.areas[0].ui_type = ui_type
    
def update_layer_index(self, context):
    '''UIList selection changed (layer_index is the active row in obj.Anim_Layers).
    When the active row is a LAYER, switch NLA tweak mode and select the
    corresponding strip/track. When the active row is a GROUP, no NLA work to
    do — just skip the side effects.'''
    obj = self.id_data
    if obj is None:
        return
    if not self.turn_on:
        return
    if not len(obj.Anim_Layers):
        return
    anim_data = anim_data_type(obj)
    if anim_data is None or not hasattr(anim_data, 'nla_tracks'):
        return
    # If the active row is a GROUP, there's no NLA strip to switch to.
    nla_index = nla_idx(obj)
    if nla_index < 0 or nla_index >= len(anim_data.nla_tracks):
        return
    for track in anim_data.nla_tracks:
        track.select = False
        if len(track.strips):
            track.strips[0].select = False
    nla_track = anim_data.nla_tracks[nla_index]
    if not len(nla_track.strips):
        anim_data.use_tweak_mode = False
        return
    strip = nla_track.strips[0]
    # Update the fake influence property with the actual strip influence
    context.scene.als['influence'] = strip.influence

    if strip.action is None:
        anim_data.use_tweak_mode = False
        return

    #select and activate the strip and track
    strip.select = True
    nla_track.select = True
    anim_data.nla_tracks.active = nla_track

    active_row = self.layer_index
    if obj.Anim_Layers[active_row].lock:
        #anim_data.use_tweak_mode = False
        tweak_mode_upper_stack(context, obj, anim_data, enter = False)
        return

    if not obj.als.upper_stack:
        subscriptions.subscriptions_remove()
        tweak_mode_upper_stack(context, obj, anim_data)
        subscriptions.subscriptions_add(context.scene)
    else:
        anim_data.use_tweak_mode = False
        if not obj.Anim_Layers[active_row].lock:
            anim_data.use_tweak_mode = True

    if obj.als.view_all_keyframes:
        obj.als.view_all_keyframes = True

    

def layer_mute(self, context):
    """Sync self.mute to the NLA track. If the layer is a parent of others,
    cascade the mute change down its subtree (so descendants reflect the same
    effective mute state)."""
    obj = self.id_data
    global _group_cascade_skip
    anim_data = anim_data_type(obj)
    if anim_data is None:
        return

    # Sync own NLA track.
    layer_idx = anim_data.nla_tracks.find(self.name)
    if layer_idx != -1:
        anim_data.nla_tracks[layer_idx].mute = self.mute

    # Cascade to descendants. Guard against re-entry from the per-descendant
    # update callback firing again.
    if not _group_cascade_skip:
        _group_cascade_skip = True
        try:
            for d in layer_descendants(obj, self.name):
                if d.mute != self.mute:
                    d.mute = self.mute
        finally:
            _group_cascade_skip = False

    #Exclude muted layers from view all keyframes
    if obj.als.view_all_keyframes:
        obj.als.view_all_keyframes = True

def layer_solo(self, context):
    """Subtree-vs-subtree solo: muting every layer whose subtree-root differs
    from self's subtree-root. Internal mute/solo within self's own subtree is
    preserved. Toggling off restores each track's mute from the per-layer
    mute property."""
    obj = context.object
    if obj is None:
        return
    anim_data = anim_data_type(obj)
    if anim_data is None or not hasattr(anim_data, 'nla_tracks'):
        return
    nla_tracks = anim_data.nla_tracks
    global skip, _group_cascade_skip
    try:
        if skip:
            return
    except NameError:
        skip = False

    if _group_cascade_skip:
        return
    _group_cascade_skip = True
    try:
        if self.solo:
            # Turn off solo on every other layer so only one is the "soloed".
            for it in obj.Anim_Layers:
                if it.name == self.name:
                    continue
                if it.solo:
                    skip = True
                    it.solo = False
                    skip = False

            # Mute every layer whose subtree-root differs from self's
            # subtree-root; unmute every layer inside self's subtree-root.
            self_root = subtree_root(obj, self.name)
            self_root_name = self_root.name if self_root is not None else self.name
            self_subtree_names = {self_root_name} | {d.name for d in layer_descendants(obj, self_root_name)}
            for track in nla_tracks:
                if track.name in self_subtree_names:
                    track.mute = False
                else:
                    track.mute = True
        else:
            # Restore each track's mute from its layer-row mute (which itself
            # reflects ancestor-cascade state from layer_mute).
            for it in obj.Anim_Layers:
                track_idx = nla_tracks.find(it.name)
                if track_idx != -1:
                    nla_tracks[track_idx].mute = it.mute
    finally:
        _group_cascade_skip = False

def layer_lock(self, context):
    """Sync self.lock to the NLA track. Cascade to descendants (parent locked
    means everything inside is locked too). Forces re-lock when the track has
    an invalid strip layout (!=1 strip or META)."""
    obj = self.id_data
    global _group_cascade_skip
    anim_data = anim_data_type(obj)
    if anim_data is None:
        return
    nla_tracks = anim_data.nla_tracks

    layer_idx = nla_tracks.find(self.name)
    if layer_idx == -1:
        return

    # If the user tried to unlock but the strip layout is invalid, snap back.
    if not self.lock:
        if len(nla_tracks[layer_idx].strips) != 1 or nla_tracks[layer_idx].strips[0].type == 'META':
            self.lock = True
            return  # second update fires; let it through

    # Cascade to descendants.
    if not _group_cascade_skip:
        _group_cascade_skip = True
        try:
            for d in layer_descendants(obj, self.name):
                if d.lock != self.lock:
                    d.lock = self.lock
        finally:
            _group_cascade_skip = False

    # Get out of tweak mode if the locked row is the active one.
    if layer_idx == obj.als.layer_index:
        obj.als.layer_index = obj.als.layer_index

    #Exclude locked layers from view all keyframes
    if obj.als.view_all_keyframes:
        obj.als.view_all_keyframes = True
         
def only_selected_bones(self, context):
    '''assign selected bones to a global variable that will be checked in the handler'''
    view_all_keyframes(self, context)
    # if self.only_selected_bones:
    #     # global selected_bones
    #     # selected_bones = context.selected_pose_bones
    #     view_all_keyframes(self, context)
    # else:
    #     view_all_keyframes(self, context)
    #     #del selected_bones
    
def data_type_update(self, context):
    obj = self.id_data
    anim_data = anim_data_type(obj)
    obj.als['layer_index'] = 0
    if anim_data is None:
        obj.Anim_Layers.clear()
        return
    if not len(anim_data.nla_tracks):
        obj.Anim_Layers.clear()
        return
    register_layers(obj, anim_data.nla_tracks)
    
    #change bake method if working with shapekeys
    if self.baketype == 'NLA' and self.data_type == 'KEY':
        self.baketype = 'AL'

def layer_name_update(self, context):
    """Sync the renamed layer's NLA track + action names, and propagate the
    rename to every other layer's parent_layer reference."""
    obj = self.id_data
    if context.object is None:
        return

    # Deduplicate against sibling names.
    layer_names = [layer.name for layer in context.object.Anim_Layers if layer != self]
    if self.name in layer_names:
        self.name = unique_name(layer_names, self.name)
    anim_data = anim_data_type(obj)
    if not hasattr(anim_data, 'nla_tracks'):
        return
    nla_tracks = anim_data.nla_tracks
    override_tracks = check_override_tracks(obj, anim_data)
    layer_idx = nla_tracks.find(self.name)
    if layer_idx == -1:
        # Track name hasn't been synced yet. Locate by previous name via the
        # collection's row index.
        row = list(obj.Anim_Layers).index(self)
        if row >= len(nla_tracks):
            return
        layer_idx = row
    track = nla_tracks[layer_idx]
    if not len(track.strips):
        return
    strip = track.strips[0]
    # Capture the old name BEFORE renaming the track, so parent_layer refs
    # can be propagated. Prefer _prev_name (last observed name) and fall back
    # to the current track name (which still holds the pre-rename value here).
    rename_from = self.get('_prev_name', '') or track.name
    if self.name != track.name:
        if track.name in override_tracks:
            override_tracks[track.name].name = self.name
        track.name = self.name
        if len(track.strips) == 1:
            strip.name = self.name
    if rename_from and rename_from != self.name:
        for it in obj.Anim_Layers:
            if it is self:
                continue
            if it.parent_layer == rename_from:
                it['parent_layer'] = self.name
    self['_prev_name'] = self.name
    if strip.action is None:
        return
    if obj.als.auto_rename and strip.action.name != self.name:
        strip.action.name = self.name

def influence_get_strips(context):
    '''Get all the strips with the current layer as the first strip for influence operations'''
    obj = context.object
    anim_data = anim_data_type(obj)
    scene = context.scene
    current_strip = anim_data.nla_tracks[nla_idx(obj)].strips[0]
    strips = [current_strip]
    if scene.als.influence_global:
        strips += [strip for track in anim_data.nla_tracks for strip in track.strips]

    return strips

def influence_hide_keyframes(self, context):
    '''hide influence fcurves of the selected or all layers'''

    strips = influence_get_strips(context)

    hide = None

    #Assign selection to all strips
    for strip in strips:
        for fcu in strip.fcurves:
            if fcu.data_path != 'influence':
                continue
            if hide is None:
                hide = not fcu.hide

            fcu.hide = hide

def influence_mute_fcurves(self, context):
    '''hide influence fcurves of the selected or all layers'''

    strips = influence_get_strips(context)

    mute = None

    #Assign selection to all strips
    for strip in strips:
        for fcu in strip.fcurves:
            if fcu.data_path != 'influence':
                continue
            if mute is None:
                mute = not fcu.mute

            fcu.mute = mute

def influence_lock_keyframes(self, context):
    '''hide influence fcurves of the selected or all layers'''
    strips = influence_get_strips(context)

    lock = None

    #Assign selection to all strips
    for strip in strips:
        for fcu in strip.fcurves:
            if fcu.data_path != 'influence':
                continue
            if lock is None:
                lock = not fcu.lock
            fcu.lock = lock

def influence_update(self, context):

    # obj = self.id_data
    obj = context.object
    if not len(obj.Anim_Layers):
        return
    # index = obj.Anim_Layers.find(self.name)
    index = obj.als.layer_index
    anim_data = anim_data_type(obj)
    if not len(anim_data.nla_tracks[index].strips):
        return
    strip = anim_data.nla_tracks[index].strips[0]
    strip.influence = self.influence
    # if not self.influence:
    #     strip.influence = 0.000001
    strip.fcurves[0].update()
    
def blend_type_values(self, obj, strip):
    '''Changing the values for scale and rotation_quaternion when switching between blend modes'''
    if obj.als.data_type != 'OBJECT':
        return
    if obj.animation_data.action is None:
        return
    fcurves = get_fcurves(obj, obj.animation_data.action)
    if not len(fcurves):
        return
    strip_fcurves = get_fcurves(obj, strip.action)
    for fcu in strip_fcurves:
        if 'scale' not in fcu.data_path and 'rotation_quaternion' not in fcu.data_path:
            continue
        default_value = bake_ops.attr_default(obj, (fcu.data_path, fcu.array_index))[fcu.array_index]
        #switching from replace to add layer, needs to reduce value of 1 from the scale and rotation_quaternion
        for keyframe in fcu.keyframe_points:
            if strip.blend_type == 'REPLACE' and (self.blend_type == 'ADD' or self.blend_type == 'SUBTRACT'):
                keyframe.co[1] -= default_value
                keyframe.handle_right[1] -= default_value
                keyframe.handle_left[1] -= default_value
            elif (strip.blend_type == 'ADD' or strip.blend_type == 'SUBTRACT') and self.blend_type == 'REPLACE':
                keyframe.co[1] += default_value
                keyframe.handle_right[1] += default_value
                keyframe.handle_left[1] += default_value

def blend_type_update(self, context):
    '''synchronize the blend property with the NLA Blend'''
    obj = self.id_data
    anim_data = anim_data_type(obj)
    strip = anim_data.nla_tracks[nla_idx(obj)].strips[0]
    if self.blend_type == strip.blend_type:
        return
    if obj.als.auto_blend:
        blend_type_values(self, obj, strip)
    strip.blend_type = self.blend_type

def auto_rename(self, context):
    '''Use auto rename when Turning it on'''
    if not self.auto_rename:
        return
    obj = self.id_data
    if obj is None:
        return
    anim_data = anim_data_type(obj)
    if anim_data is None:
        return
    if not len(anim_data.nla_tracks):
        return
    if anim_data.action is None:
        return
    name = anim_data.action.name
    obj.Anim_Layers[obj.als.layer_index].name = name
    anim_data.nla_tracks[nla_idx(obj)].name = name
    anim_data.nla_tracks[nla_idx(obj)].strips[0].name = name
    
def auto_rename_default(obj):
    '''Apply the default auto renaming from the addon preferences'''
    folder_name = addon_folder_path()
    obj.als.auto_rename = bpy.context.preferences.addons[folder_name].preferences.auto_rename
    
def addon_folder_path():
    folder_path = os.path.dirname(os.path.realpath(__file__))
    folder_name = os.path.basename(folder_path)
    return folder_name
    
def add_inbetween_key(self, context):
    '''Adding a Breakdown keyframe that works also in Layers'''
    obj = self.id_data
    anim_data = anim_data_type(obj)
    strip = anim_data.nla_tracks[nla_idx(obj)].strips[0]
    frame = round(bake_ops.frame_evaluation(context.scene.frame_current, strip), 3)
    
    fcurves = get_fcurves(obj, anim_data.action)
    for fcu in fcurves:
        #filter selected bones
        # if obj.mode == 'POSE': #apply only to selected bones
        if selected_bones_filter(obj, fcu):
            continue
        if not multikey.filter_properties(obj, fcu):
            continue
        #get the last previous key
        for keyframe in fcu.keyframe_points:
            if round(keyframe.co[0], 3) > frame:
                key_after = keyframe
                break
            elif round(keyframe.co[0], 3) < frame:
                key_before = keyframe
            else:
                key_added = keyframe
            
        if 'key_after' not in locals() or 'key_before' not in locals():
            continue
        if 'key_added' not in locals():
            fcu.keyframe_points.add(1)
            key_added = fcu.keyframe_points[-1]
        value = key_before.co[1] + (key_after.co[1] - key_before.co[1]) * self.inbetweener
        key_added.co = (frame, value)
        fcu.update()
        del key_after
        del key_before
        del key_added
        
    self['inbetweener'] = 0.5     

def load_action(self, context):
    '''Load a new action from the layer list'''
    obj = self.id_data
    index = obj.Anim_Layers.find(self.name)
    anim_data = anim_data_type(obj)
    # if self.lock:
    #     return
    if self.action == 'None':
        return
    track = anim_data.nla_tracks[index]
    action = self.action
    if not len(track.strips):
        strip = track.strips.new(name = track.name, start=0, action = action)
        subscriptions.frameend_update_callback()
        strip.use_sync_length = False
        use_animated_influence(strip)
        return
    subscriptions.subscriptions_remove()
    strip = track.strips[0]
    #action = bpy.data.actions[self.action]
    if index != obj.als.layer_index:
        return
    if strip.action == action:
        return
    tweak_mode_upper_stack(context, obj, anim_data, enter = False)
    strip.action = action
    if action is None:
        return
    fcurves = get_fcurves(obj, action)
    if obj.als.auto_blend and len(fcurves):
        strip.blend_type = auto_blendtype(obj, action, strip.blend_type)
    #Auto rename
    if obj.als.auto_rename:
        obj.Anim_Layers[index].name = action.name
        track.name = action.name
        strip.name = action.name
    if self.lock:
        return
    obj.als.view_all_keyframes = obj.als.view_all_keyframes
    #anim_data.use_nla = True
    tweak_mode_upper_stack(context, obj, anim_data, enter = True)
    subscriptions.subscriptions_add(context.scene)

def auto_blendtype(obj, action, current_blend):
    '''apply blend type automatically'''
    if action is None:
        return
    fcurves = get_fcurves(obj, action)
    if not len(fcurves):
        return current_blend
    count = 0
    for fcu in fcurves:
        if not 'scale' in fcu.data_path and not 'rotation_quaternion' in fcu.data_path:
            continue
        default_value = bake_ops.attr_default(obj, (fcu.data_path, fcu.array_index))[fcu.array_index]
        if not default_value:
            continue
        count += 1
        for keyframe in fcu.keyframe_points:
            if keyframe.co[1] == 0:
                return 'ADD' 
    if count:
        return 'REPLACE'
    else:
        return current_blend

def layer_frame_start(self, context):
    '''synchronize action start and strip start'''
    if not self.custom_frame_range:
        return
    if self.frame_start > self.frame_end:
        self.frame_end = self.frame_start
    obj = self.id_data
    index = obj.Anim_Layers.find(self.name)
    anim_data = anim_data_type(obj)
    if not len(anim_data.nla_tracks[index].strips):
        return
    strip = anim_data.nla_tracks[index].strips[0]
    strip.frame_start = self.frame_start 

    if strip.repeat <= 1:
        strip_action_recalc(self, strip)
    else:
        self['offset'] =  round((strip.frame_start - strip.action_frame_start), 3)
        recalculate_repeat(self, strip)

def calculate_repeat_settings(self, strip):
    '''recalculate all the settings when repeat is applied'''

    strip.action_frame_start = strip.action.frame_range[0] #- self.offset
    strip.action_frame_end = strip.action.frame_range[1] #- self.offset
    action_range = (strip.action.frame_range[1] - strip.action.frame_range[0])
    strip.repeat = self.repeat
    strip.frame_start = self['frame_start'] = round(strip.action.frame_range[0] + self.offset, 2) #* strip.scale
    # update_action_frame_range(self.frame_start, self.frame_end, self, strip)
    strip.frame_end = self['frame_end'] = round(strip.action.frame_range[0] + (action_range * strip.repeat * strip.scale) + self.offset, 2) #* strip.scale


def recalculate_repeat(self, strip):
    '''get the repeat value from the frame range'''
    
    if self.repeat <= 1:
        return
    action_frame_range = strip.action_frame_end - strip.action_frame_start
    strip_frame_range = self.frame_end - self.frame_start
    strip.repeat = strip_frame_range / (action_frame_range * strip.scale)
    # strip.repeat = strip_frame_range / action_frame_range
    self ['repeat'] = strip.repeat
    
def layer_frame_end(self, context):
    '''synchronize action end and strip end'''
    if not self.custom_frame_range:
        return
    if self.frame_end < self.frame_start:
        self.frame_start = self.frame_end
    obj = self.id_data
    index = obj.Anim_Layers.find(self.name)
    anim_data = anim_data_type(obj)
    if not len(anim_data.nla_tracks[index].strips):
        return
    strip = anim_data.nla_tracks[index].strips[0]

    if strip.repeat <= 1:
        strip_action_recalc(self, strip)
    else:
        strip.frame_end = self.frame_end
        strip.action_frame_end = strip.action.frame_range[1]
        recalculate_repeat(self, strip)
        
def layer_frame_range(self, context):
    '''update the custom frame range when turned on and off'''
    obj = self.id_data
    anim_data = anim_data_type(obj)
    index = obj.Anim_Layers.find(self.name)
    strip = anim_data.nla_tracks[index].strips[0]
    
    if not self.custom_frame_range:
        # self['repeat'] = strip.repeat
        strip.repeat = 1 #change strip repeat but keep self.repeat value stored
        strip.use_reverse = False
        subscriptions.frameend_update_callback()
        layer_offset(self, context)
        return
    
    if len(anim_data.nla_tracks) != nla_layer_count(obj):
        return
    if self.repeat > 1:
        calculate_repeat_settings(self, strip)
        return
        # strip.repeat = self.repeat
        # calculate_repeat_settings(self, strip)

    if self.frame_end: #if there is a frame end defined restore previous settings
        strip_action_recalc(self, strip)  
    else:
        if not len(anim_data.nla_tracks[index].strips):
            return
        self.frame_start, self.frame_end = bake_ops.frame_start_end(context.scene)
        # self.frame_end = anim_data.nla_tracks[index].strips[0].frame_end - 10
        # self.frame_start = anim_data.nla_tracks[index].strips[0].frame_start 
        
        strip.extrapolation = 'NOTHING'

def layer_repeat(self, context):
    '''Multiply the action speed but keep strip limits the same'''
    
    obj = self.id_data
    index = obj.Anim_Layers.find(self.name)
    anim_data = anim_data_type(obj)
    if not len(anim_data.nla_tracks[index].strips):
        return
    strip = anim_data.nla_tracks[index].strips[0]
    if strip.repeat == self.repeat == 1:
        return
    
    calculate_repeat_settings(self, strip)
        
def layer_speed(self, context):
    '''Multiply the action speed but keep strip limits the same'''

    obj = self.id_data
    index = obj.Anim_Layers.find(self.name)
    anim_data = anim_data_type(obj)
    if not len(anim_data.nla_tracks[index].strips):
        return
    strip = anim_data.nla_tracks[index].strips[0]
    frame_end = strip.frame_end
    frame_start = strip.frame_start
    
    strip.scale = self.speed
    
    if not self.custom_frame_range:
        update_action_frame_range(frame_start, frame_end, self, strip)
        strip.frame_end = frame_end
        return

    if strip.repeat <= 1:
        strip_action_recalc(self, strip)
    else:
        recalculate_repeat(self, strip)
    
    if strip.use_sync_length:
        sync_frame_range(context)
    
def layer_offset(self, context):
    '''Offset the action keyframes but keep strip limits the same'''
    
    obj = self.id_data
    index = obj.Anim_Layers.find(self.name)
    anim_data = anim_data_type(obj)
    if not len(anim_data.nla_tracks[index].strips):
        return
    strip = anim_data.nla_tracks[index].strips[0]

    #changing only action frame start when custom frame range turned off
    if not self.custom_frame_range:
        frame_end = strip.frame_end
        update_action_frame_range(strip.frame_start, frame_end, self, strip)
        strip.frame_end = frame_end
        return
    
    if strip.repeat <= 1:
        strip_action_recalc(self, strip)
    else:
        calculate_repeat_settings(self, strip)

    if strip.use_sync_length:
        sync_frame_range(context)
    
def update_action_frame_range(frame_start, frame_end, layer, strip):
    '''calculating the offset and scale based on the start of the action frame range'''
    if strip.action is None:
        return
    #Get the begining of the action
    action_start = strip.action.frame_range[0]
    action_end = strip.action.frame_range[1]
    #Frame end depends on Frame Start and action frame range

    #Getting the offset from the strip to the actual action frame range, and multiplying the scale to it
    action_start_offset = (frame_start - action_start) * 1/strip.scale
    action_end_offset = (frame_end - action_end) * 1/strip.scale

    action_frame_start = action_start + action_start_offset - layer.offset * 1/strip.scale
    if strip.repeat > 1:
        strip.action_frame_end = action_end + action_end_offset - layer.offset * 1/strip.scale
    else:
        strip.action_frame_end = (frame_end - frame_start)*2 + (action_frame_start)
    
    # strip.action_frame_end = action_end + action_end_offset - layer.offset * 1/strip.scale
    strip.action_frame_start = action_frame_start

def strip_action_recalc(self, strip):
    
    strip.scale = self.speed
    strip.repeat = self.repeat
    
    update_action_frame_range(self.frame_start, self.frame_end, self, strip)

    strip.frame_start = self.frame_start
    # strip.frame_end_raw = self.frame_end
    strip.frame_end = self.frame_end


###################################################### HELPER FUNCTIONS ################################################                
def redraw_areas(areas):
    if not len(bpy.context.window_manager.windows):
        return
    for area in bpy.context.window_manager.windows[0].screen.areas:
        if area.type in areas:
            area.tag_redraw()

def anim_data_type(obj, toggle = False):    
    if obj.als.data_type == 'OBJECT' and not toggle:
        if not hasattr(obj, 'animation_data'):
            return None
        anim_data = obj.animation_data
    else:
        if not hasattr(obj.data.shape_keys, 'animation_data'):
            return None
        anim_data = obj.data.shape_keys.animation_data
    
    return anim_data

def anim_datas_append(obj):
    '''append shapekey animation data if it also exists'''
    anim_datas = [obj.animation_data]
    if hasattr(obj.data, 'shape_keys'):
        if hasattr(obj.data.shape_keys, 'animation_data'):
            #anim_datas = {obj.animation_data, obj.data.shape_keys.animation_data}
            anim_datas.append(obj.data.shape_keys.animation_data)
    return anim_datas

def unique_name(collection, name):
    '''add numbers to tracks if they have the same name'''
    if name not in collection:
        return name
    nr = 1
    if '.' in name:
        end = name.split('.')[-1]
        if end.isnumeric():
            nr = int(end)
            name = '.'.join(name.split('.')[:-1])
    while name + '.' + str(nr).zfill(3) in collection:
        nr += 1
    return name + '.' + str(nr).zfill(3)

#checks if the object has an action and if it exists in the NLA
def action_search(action, nla_tracks):
    '''returns True if action already exists in the nla_tracks'''
    if action is None:
        return  False
    for track in nla_tracks:
        for strip in track.strips:
            if strip.action == action:
                return True                   

    return False

def select_layer_bones(self, context):

    obj = context.object
    strips = obj.animation_data.nla_tracks[nla_idx(obj)].strips
    if len(strips) != 1 or strips[0].action is None:
        return
    fcurves = get_fcurves(obj, strips[0].action)

    # During nla bake shift doesn't exist
    if not hasattr(self, 'shift'):
        self.shift = False

    paths = {fcu.data_path.split('"')[1] for fcu in fcurves if len(fcu.data_path.split('"'))>1}
    
    bones = obj.pose.bones if bpy.app.version >= (5, 0, 0) else obj.data.bones

    for bone in bones:
        if bone.name in paths:
            bone.select = True
        elif not self.shift:
            bone.select = False

###################################################### CLASSES ###########################################################
class SelectBonesInLayer(bpy.types.Operator):
    """Select bones with keyframes in the current layer, use shift to add to the current selection"""
    bl_idname = "anim.bones_in_layer"
    bl_label = "Select layer bones"
    bl_icon = "BONE_DATA"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.object and context.object.mode == 'POSE'
    
    def invoke(self, context, event):
        self.shift = event.shift
        return self.execute(context)

    def execute(self, context):
        select_layer_bones(self, context)
        return {'FINISHED'}

class OverrideError(bpy.types.Operator):
    bl_idname = "message.layersoverride"
    bl_label = "WARNING!"
    bl_icon = "ERROR"
    
    confirm: bpy.props.BoolProperty(default=False)
    
    def execute(self, context):
        if not self.confirm:
            return {'FINISHED'}
        #use bpy.path for the absolute path
        filepath = bpy.path.abspath(context.object.override_library.reference.library.filepath)
        blenderpath = bpy.app.binary_path
        import subprocess
        subprocess.Popen([blenderpath, filepath])
        return {'FINISHED'}

    def invoke(self, context, event):
        wm = context.window_manager
        obj_name = context.object.name
        return wm.invoke_props_dialog(self, width=450+len(obj_name))

    def draw(self, context):
        layout = self.layout
        row = layout.row()
        obj_name = context.object.name
        row = layout.row()
        row.label(text="The object %s is an override library that uses layers or tracks in its source file."%obj_name)
        row = layout.row()
        row.label(text="Please clean the source file from animation layers or nla tracks.")
        layout.separator(factor = 2)
        row = layout.row()
        row.alignment = 'LEFT'
        row.prop(self, "confirm", text="Open the source file in a new Blender window")


class ClearNLA(bpy.types.Operator):
    bl_idname = "anim.clear_nla_warning"
    bl_label = "WARNING!"
    bl_icon = "ERROR"
    
    confirm: bpy.props.BoolProperty(default=True)
    
    def execute(self, context):
        
        # def draw_error(self, context):
        #     self.layout.label(text = 'Override NLA tracks are found and are not removable. Remove them inside the referenced file')
       
        obj = context.object
        anim_datas = anim_datas_append(obj)
        for anim_data in anim_datas:
            if anim_data is None:
                continue
            if self.confirm:
                #start to delete only after the library override referenced tracks
                nla_tracks = anim_data.nla_tracks
                if not len(nla_tracks):
                    continue
                clear_nla_tracks(obj, anim_data)
                
                if check_override_tracks(obj, anim_data) or check_override_layers(obj):
                    bpy.ops.message.layersoverride('INVOKE_DEFAULT')
                    continue
                obj.Anim_Layers.clear()
        
        subscriptions.subscriptions_remove()
        obj.als['turn_on'] = True
        start_animlayers(obj)
        subscriptions.subscriptions_add(context.scene)
            
        return {'FINISHED'}
    
    def invoke(self, context, event):
        wm = context.window_manager
        return wm.invoke_props_dialog(self, width=525)

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        obj_name = context.object.name
        col.label(text=obj_name+" has already tracks in the NLA editor, which have been created before using animation layers.")
        row = col.row()
        row.alignment = 'CENTER'
        row.prop(self, "confirm", text="Remove NLA tracks")

def clear_nla_tracks(obj, anim_data):
    '''remove all the nla tracks'''
    nla_tracks = anim_data.nla_tracks
    
    override_tracks = check_override_tracks(obj, anim_data)
    anim_data.use_tweak_mode = False
    for track in nla_tracks:
        track.is_solo = False
        if track.name not in override_tracks:
            anim_data.nla_tracks.remove(track)
    
class ClearActiveAction(bpy.types.Operator):
    bl_idname = "anim.clear_active_action_warning"
    bl_label = "WARNING!"
    bl_icon = "ERROR"

    # proceed: bpy.props.EnumProperty(name="Choose how to proceed", description="Select an option how to proceed with Anim Layers", override = {'LIBRARY_OVERRIDABLE'},
    #     items = [
    #         ('REMOVE_LAYERS', 'Remove old layers and continue with the current action', 'Remove previous Layers and continue with current action in the base layer', 0),
    #         ( 'REMOVE_ACTION', 'Remove current action and reload older Layers', 'Remove current action and continue with the previous layers', 1),
    #         ('ADD_ACTION', 'Add the current action as a new Layer', 'Keep previous Anim Layers and Add the active action as a new layer', 2),
    #         ]
    #     )
    
    def execute(self, context):
        self.proceed = context.preferences.addons[__package__].preferences.proceed
        obj = context.object
        anim_datas = anim_datas_append(obj)
        for anim_data in anim_datas:
            if anim_data is None:
                continue
            if self.proceed == 'REMOVE_LAYERS':
                #start to delete only after the library override referenced tracks
                if not len(anim_data.nla_tracks):
                    continue
                
                obj.als.layer_index = 0
                clear_nla_tracks(obj, anim_data)
                
                if check_override_tracks(obj, anim_data) or check_override_layers(obj):
                    bpy.ops.message.layersoverride('INVOKE_DEFAULT')
                    continue
                obj.Anim_Layers.clear()
                
            elif self.proceed == 'REMOVE_ACTION':
                anim_data.action = None
                
            elif self.proceed == 'ADD_ACTION':
                action = anim_data.action
                # anim_data.action = None
                index = nla_layer_count(obj) - 1  # NLA-track index
                obj.als.layer_index = layer_to_row_index(obj, max(0, index))
                #add_animlayer(layer_name = action.name , duplicate = False, index = 1, blend_type = 'COMBINE')
                add_animlayer(unique_name(obj.Anim_Layers, action.name), index = index, blend_type = 'REPLACE')
                obj.als.layer_index = layer_to_row_index(obj, index + 1)
                # new_track.strips[0].action = action
        
        subscriptions.subscriptions_remove()
        obj.als['turn_on'] = True
        start_animlayers(obj)
        subscriptions.subscriptions_add(context.scene)
            
        return {'FINISHED'}
    
    def invoke(self, context, event):
        wm = context.window_manager
        return wm.invoke_props_dialog(self, width = 450)

    def draw(self, context):
        layout = self.layout
        row = layout.row(align = True)
        obj_name = context.object.name
        row.alignment = 'CENTER'
        row.label(text=obj_name+" already has older layers and an active action that are not matching")
        # row = col.row()
        #col.alignment = 'CENTER'
        split = layout.split(factor = 0.25)
        split.label(text = 'How to Proceed')
        split.prop(context.preferences.addons[__package__].preferences, "proceed", text ='')

class AutoCustomFrameRange(bpy.types.Operator):
    bl_idname = "anim.custom_frame_range_warning"
    bl_label = "WARNING!"
    bl_icon = "ERROR"

    # auto_cfr: bpy.props.BoolProperty(name="Turn Custom Frame Range On", description="Automatically change custom frame range when strip settings are changed", default=False)
    confirm: bpy.props.BoolProperty(default=False)

    def invoke(self, context, event):
        obj = context.object
        anim_data = anim_data_type(obj) 
        index = obj.als.layer_index
        track = anim_data.nla_tracks[index]
        self.strip = track.strips[0]
        self.layer = obj.Anim_Layers[index]

        # Since the operator is running from a handler it can use the wrong context window
        # Which can cause the user to miss the operator dialogue. So it's checking for the view 3d
        wm = context.window_manager
        area = None
        for area in context.window.screen.areas:
            if area.type == 'VIEW_3D':
                return wm.invoke_props_dialog(self, width = 400)
            
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == 'VIEW_3D':
                    region = next((r for r in area.regions if r.type == 'WINDOW'), area.regions[0])
                    with bpy.context.temp_override(window=win, area=area, region=region):
                        return wm.invoke_props_dialog(self, width = 400)
                    
        # In case no View 3D was found
        return wm.invoke_props_dialog(self, width = 400)
        
    
    def draw(self, context):
        layout = self.layout
        layout.label(text=f"NLA strip settings have changed on layer {self.layer.name} !", icon = 'WARNING_LARGE')
        layout.label(text="Do you want to Turn Custom Frame Range on?")
        layout.prop(self, "confirm")

    def execute(self, context):
        # Get the current layer and strip
        if not self.confirm:
            self.restore(context)
            return {'FINISHED'}
        
        self.layer['custom_frame_range'] = True
        subscriptions.update_strip_layer_settings(self.strip, self.layer)
        redraw_areas(['VIEW_3D'])
        self.confirm = False
        subscriptions.subscriptions_add(context.scene)

        return {'FINISHED'}
    
    def cancel(self, context):
        self.restore(context)
        # return {'CANCELLED'}

    def restore(self, context):
        print('restore')
        subscriptions.frameend_update_callback()

        subscriptions.subscriptions_add(context.scene)
    
def update_action_list(scene):
    '''update all the objects layers with the updated action item list when a new layer was added'''
    for AL_object in scene.AL_objects:
        obj = AL_object.object
        if obj is None:
            continue
        anim_data = anim_data_type(obj)
        i = obj.als.layer_index
        if len(anim_data.nla_tracks[i].strips) != 1:
            return
        obj.Anim_Layers.update()
        layer = obj.Anim_Layers[0]
        layer.action = anim_data.nla_tracks[0].strips[0].action
        
def add_animlayer(layer_name = 'Anim_Layer' , duplicate = False, index = 1, blend_type = 'COMBINE', prev_track = None):
    '''Add an animation layer. `prev_track` overrides the default insertion
    anchor (just-above-the-active-track). Pass it when the caller needs the
    new track to land somewhere other than active+1 — for example, above the
    active layer's entire subtree.'''
    obj = bpy.context.object
    check_overrides_ALobjects(obj)
    anim_data = anim_data_type(obj)
    action = anim_data.action
    nla_tracks = anim_data.nla_tracks
    if obj.als.layer_index < 0 : obj.als['layer_index'] = 0
    if prev_track is not None:
        previous = prev_track
    else:
        previous = None if index == 0 else nla_tracks[nla_idx(obj)]

    new_track = nla_tracks.new(prev = previous)
    new_track.name = layer_name

    # Lock nla tracks for safety measures depending on the preferences
    if bpy.context.preferences.addons[__package__].preferences.lock_nlatracks:
        new_track.lock = True

    # If it's the first layer, then force is solo to turn off because
    # of some bug, might be related to ARP retargeter
    if not obj.als.layer_index:
        new_track.is_solo = False
    
    #if there is no action to duplicate then cancel duplication and create new layer
    if len(obj.Anim_Layers):
        if obj.Anim_Layers[obj.als.layer_index].action is None:
            duplicate = False

    #check if the object already has an action and if it exists in the NLA, if not create a new one
    if action is None or (action_search(action, anim_data.nla_tracks) and not duplicate): #
        action = bpy.data.actions.new(name=new_track.name)
        #update_action_list(bpy.context.scene)
    elif duplicate:
        action = obj.Anim_Layers[obj.als.layer_index].action

    slot = add_action_slot(obj, action)
    #This has to be assigned after creating the slot, otherwise it will create legacy slot automatically
    if hasattr(action, 'id_root'):
        action.id_root = obj.als.data_type

    #strip settings
    new_strip = new_track.strips.new(name = new_track.name,start=0, action = action)
    if hasattr(new_strip, 'action_slot') and slot:
        new_strip.action_slot = slot
    
    if duplicate:
        copy_strip_settings(new_strip, anim_data.nla_tracks[nla_idx(obj)].strips[0])
    else:
        new_strip.action_frame_start = 0
        visible_layers(obj, anim_data.nla_tracks)
        subscriptions.frameend_update_callback()

    #auto_rename(obj.als, bpy.context)
    new_strip.blend_type = blend_type
    new_strip.use_sync_length = False
    use_animated_influence(new_strip)

    return new_track
        
#adding a new track, action and strip       
class AddAnimLayer(bpy.types.Operator):
    """Add animation layer"""
    bl_idname = "anim.add_anim_layer"
    bl_label = "Add Animation Layer"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        obj = context.object
        #subscriptions.subscriptions_remove()
        anim_data = anim_data_type(obj)
        # addon_name = addon_folder_path()
        blend_type = context.preferences.addons[__package__].preferences.blend_type
        
        if subscriptions.check_handler in bpy.app.handlers.depsgraph_update_pre:
            bpy.app.handlers.depsgraph_update_pre.remove(subscriptions.check_handler)
            
        if obj.als.data_type == 'OBJECT':
            layer_name = 'Anim_Layer'
            base_name = 'Base_Layer'
            if anim_data is None:
                anim_data = obj.animation_data_create()
        elif obj.als.data_type == 'KEY':
            if not obj.data.shape_keys:
                obj.shape_key_add(name = 'Basis')
            layer_name = 'Shapekeys_Layer'
            base_name = 'Base_Shapekeys'
            if anim_data is None:
                anim_data = obj.data.shape_keys.animation_data_create()
        
        nla_tracks = anim_data.nla_tracks

        if not len(nla_tracks):
            #starting animation layers and getting the default sync layer names
            obj.als.auto_rename = context.preferences.addons[__package__].preferences.auto_rename
            #Adding base layer
            obj.als['layer_index'] = 0
            add_animlayer(base_name, index = 0, blend_type = blend_type)
            #using a temporary variable instead of calling update_track_list all the time with obj.als.layer_index

            index = 0

            if anim_data.action:
                add_animlayer(layer_name, index,  blend_type = blend_type)
                index += 1
                anim_data.action.use_fake_user = True
            anim_data.action = None
            new_track_name = anim_data.nla_tracks[index].name
            parent_layer_to_inherit = ''
        else:
            # Capture the active row's info BEFORE add_animlayer mutates state.
            # The new layer is always a SIBLING of the active row (inherits its
            # parent_layer). What changes based on whether the active has
            # children is only the INSERTION ANCHOR — a parent layer plus its
            # descendants is treated as a single unit, so the new layer goes
            # above the whole subtree instead of between parent and children.
            prev_active = active_item(obj)
            custom_prev_track = None
            parent_layer_to_inherit = ''
            if prev_active is not None:
                parent_layer_to_inherit = prev_active.parent_layer
                if is_group_layer(obj, prev_active.name):
                    descendants = layer_descendants(obj, prev_active.name)
                    top_desc_idx = -1
                    for d in descendants:
                        di = nla_tracks.find(d.name)
                        if di > top_desc_idx:
                            top_desc_idx = di
                    if top_desc_idx != -1:
                        custom_prev_track = nla_tracks[top_desc_idx]

            new_track = add_animlayer(unique_name(obj.Anim_Layers, layer_name),
                                      blend_type = blend_type,
                                      prev_track = custom_prev_track)
            new_track_name = new_track.name
            index = anim_data.nla_tracks.find(new_track_name)

        #register_layers(obj, nla_tracks)
        override_tracks = check_override_tracks(obj, anim_data)
        if override_tracks:
            bpy.ops.message.layersoverride('INVOKE_DEFAULT')
        #if override tracks exist then make sure selection is on top of them
        while anim_data.nla_tracks[index].name in override_tracks:
            index += 1
        new_track_name = anim_data.nla_tracks[index].name

        # Apply parent_layer to the freshly-added LAYER row.
        if parent_layer_to_inherit:
            row_idx = obj.Anim_Layers.find(new_track_name)
            if row_idx != -1:
                obj.Anim_Layers[row_idx]['parent_layer'] = parent_layer_to_inherit

        # Defensive: enforce contiguity in case the existing NLA was already
        # out of order. The prev_track we passed to add_animlayer puts the new
        # row in the right slot (just above the active's subtree), so in the
        # normal case this is a fast no-op. No priority_name — we do NOT want
        # the new layer bumped to the top of its sibling stack; we want it
        # exactly where prev_track placed it.
        subscriptions.subscriptions_remove()
        try:
            enforce_subtree_contiguity(obj, context)
        finally:
            if context.scene is not None:
                subscriptions.subscriptions_add(context.scene)

        # Pin selection on the new layer (its row index may have shifted
        # after enforce_subtree_contiguity ran).
        final_idx = obj.Anim_Layers.find(new_track_name)
        if final_idx != -1:
            obj.als.layer_index = final_idx

        subscriptions.animlayers_frame(context.scene, context)
        if subscriptions.check_handler not in bpy.app.handlers.depsgraph_update_pre:
            bpy.app.handlers.depsgraph_update_pre.append(subscriptions.check_handler)

        return {'FINISHED'}

def copy_strip_settings(new_strip, old_strip):
    '''Copy strip settings when duplicating them'''

    new_strip.use_reverse = old_strip.use_reverse
    new_strip.use_sync_length = old_strip.use_sync_length
    new_strip.extrapolation = old_strip.extrapolation
    new_strip.action_frame_start = old_strip.action_frame_start
    new_strip.action_frame_end = old_strip.action_frame_end
    new_strip.frame_start = old_strip.frame_start
    new_strip.frame_end = old_strip.frame_end

class DuplicateAnimLayer(bpy.types.Operator):
    """Duplicate animation layer"""
    bl_idname = "anim.duplicate_anim_layer"
    bl_label = "Duplicate Animation Layer"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.object is not None and is_layer_row_active(context.object)

    def execute(self, context):
        if subscriptions.check_handler in bpy.app.handlers.depsgraph_update_pre:
            bpy.app.handlers.depsgraph_update_pre.remove(subscriptions.check_handler)

        obj = context.object
        anim_data = anim_data_type(obj)
        nla_tracks = anim_data.nla_tracks
        i = nla_idx(obj)

        strip = nla_tracks[i].strips[0]
        blend = strip.blend_type
        track_name = nla_tracks[i].name

        # Capture source layer's parent_layer before duplication so the dup inherits it.
        source_parent_layer = ''
        a = active_item(obj)
        if a is not None:
            source_parent_layer = a.parent_layer

        name = unique_name(obj.Anim_Layers, track_name)
        new_track = add_animlayer(layer_name = name, duplicate = True, blend_type = blend)
        new_strip = new_track.strips[0]
        action = new_strip.action
        if obj.als.linked == False:
            tweak_mode_upper_stack(context, obj, anim_data, enter = False)
            new_strip.action = action.copy()


        register_layers(obj, nla_tracks)

        # Point the UIList at the newly-added layer by name.
        new_layer_name = new_track.name
        ridx = obj.Anim_Layers.find(new_layer_name)
        if ridx != -1:
            obj.als.layer_index = ridx
            if source_parent_layer:
                obj.Anim_Layers[ridx]['parent_layer'] = source_parent_layer

        #Turn on frame range if it was duplicated
        tweak_mode_upper_stack(context, obj, anim_data)
        if subscriptions.check_handler not in bpy.app.handlers.depsgraph_update_pre:
            bpy.app.handlers.depsgraph_update_pre.append(subscriptions.check_handler)

        return {'FINISHED'}

class ExtractSelection(bpy.types.Operator):
    """Extract selected bones to a new Layer"""
    bl_idname = "anim.extract_selected_bones"
    bl_label = "Extract Selected Bones"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        obj = context.object
        return obj and obj.type == 'ARMATURE' and obj.mode == 'POSE'

    def execute(self, context):     
        
        if subscriptions.check_handler in bpy.app.handlers.depsgraph_update_pre:
            bpy.app.handlers.depsgraph_update_pre.remove(subscriptions.check_handler)
        
        obj = context.object
        anim_data = anim_data_type(obj)
        nla_tracks = anim_data.nla_tracks
        

        blend = nla_tracks[nla_idx(obj)].strips[0].blend_type
        track_name = nla_tracks[nla_idx(obj)].name
        
        name = unique_name(obj.Anim_Layers, track_name + ' Extract')
        new_track = add_animlayer(layer_name = name, duplicate = True, blend_type = blend)
        bones_path = [bone.path_from_id() for bone in context.selected_pose_bones]
        bone_names = [bone.name for bone in context.selected_pose_bones]
        strip = new_track.strips[0]

        action = strip.action
        #create a new copy of the action
        new_action = action.copy()
        tweak_mode_upper_stack(context, obj, anim_data, enter = False)
        strip.action = new_action
        
        remove_empty_slots(new_action)
        #remove fcurves of the selected bones in the original layer
        fcurves = get_fcurves(obj, action)

        for fcu in list(fcurves):
            group = fcu.group.name if fcu.group is not None else None    
            if fcu.data_path.split(']')[0]+']' in bones_path or group in bone_names:
                # remove_fcurve_from_action(action, fcu)
                fcurves.remove(fcu)
        fcurves.update()

        # new_fcurves = get_fcurves(new_action)
        new_fcurves = get_fcurves(obj, new_action)

        #remove all bones that are not selected from the new extracted layer
        for fcu in list(new_fcurves):
            group = fcu.group.name if fcu.group is not None else None 
            if fcu.data_path.split(']')[0]+']' not in bones_path and group not in bone_names:
                # remove_fcurve_from_action(new_action, fcu)
                new_fcurves.remove(fcu)
            
        new_fcurves.update()

        register_layers(obj, nla_tracks)

        # Point the UIList at the newly-extracted layer.
        new_layer_name = new_track.name
        for ridx, it in enumerate(obj.Anim_Layers):
            if it.type == 'LAYER' and it.name == new_layer_name:
                obj.als.layer_index = ridx
                break

        tweak_mode_upper_stack(context, obj, anim_data)

        if subscriptions.check_handler not in bpy.app.handlers.depsgraph_update_pre:
            bpy.app.handlers.depsgraph_update_pre.append(subscriptions.check_handler)
           
        return {'FINISHED'}

class ExtractMarkers(bpy.types.Operator):
    """Extract keyframes from Markers. Usefull for mocap cleanup"""
    bl_idname = "anim.extract_markers"
    bl_label = "Extract Marked keyframes"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return len(context.scene.timeline_markers)

    def execute(self, context):
         
        if subscriptions.check_handler in bpy.app.handlers.depsgraph_update_pre:
            bpy.app.handlers.depsgraph_update_pre.remove(subscriptions.check_handler)
            
        obj = context.object
        anim_data = anim_data_type(obj)
        nla_tracks = anim_data.nla_tracks
   
        blend = nla_tracks[nla_idx(obj)].strips[0].blend_type
        track_name = nla_tracks[nla_idx(obj)].name
        
        name = unique_name(obj.Anim_Layers, track_name + ' Extract')
        new_track = add_animlayer(layer_name = name, duplicate = True, blend_type = blend)
        if obj.type == 'ARMATURE':
            bones_path = [bone.path_from_id() for bone in context.selected_pose_bones]
            bone_names = [bone.name for bone in context.selected_pose_bones]
        
        tweak_mode_upper_stack(context, obj, anim_data, enter = False)
        action = new_track.strips[0].action
        #create a new copy of the action
        new_action = action.copy()
        # new_action = copy_action(action)
        new_track.strips[0].action = new_action
        markers = context.scene.timeline_markers
        marked_frames = [marker.frame for marker in markers]

        remove_empty_slots(new_action)
        fcurves = get_fcurves(obj, new_action)
        
        #remove all bones that are not selected from the new extracted layer
        for fcu in list(fcurves):
            if obj.type == 'ARMATURE':
                group = fcu.group.name if fcu.group is not None else None 
                if fcu.data_path.split(']')[0]+']' not in bones_path and group not in bone_names:
                    fcurves.remove(fcu)
                    continue
            keyframes = fcu.keyframe_points
            
            #check the difference between the frames and the marked ones
            frames = np.zeros(len(keyframes)*2)
            keyframes.foreach_get('co', frames)
            missing_frames = set(marked_frames) - set(frames[::2])
            
            #add the missing keyframes
            for frame in missing_frames:                
                value = fcu.evaluate(frame)
                keyframes.insert(frame, value)
            
            #Create a duplicate of all the keyframes
            roundframes = []
            smartkeys = []
            for keyframe in keyframes:
                round_keyframe = round(keyframe.co[0])
                if round_keyframe in marked_frames and round_keyframe not in roundframes:
                    smartkey = bake_ops.smartkey(keyframe)
                    smartkeys.append(smartkey)
                    roundframes.append(round_keyframe)

            smartkeys = bake_ops.add_inbetween(smartkeys)
            
            for smartkey in smartkeys:
                smartkey.value = fcu.evaluate(smartkey.frame)
                smartkey.interpolation = 'BEZIER'
                
            i = 0
            roundframes = []
            while i < len(keyframes):
                round_keyframe = round(keyframes[i].co[0])
                if keyframes[i].co[0] not in marked_frames:# or round_keyframe in roundframes:
                    keyframes.remove(keyframes[i])
                else:
                    keyframes[i].interpolation = 'BEZIER'
                    roundframes.append(round_keyframe)
                    i += 1
                    
            bake_ops.add_interpolations(fcu, smartkeys)

        # context.scene.frame_set(current_frame)
        fcurves.update()

        register_layers(obj, nla_tracks)

        # Point the UIList at the newly-extracted layer.
        new_layer_name = new_track.name
        for ridx, it in enumerate(obj.Anim_Layers):
            if it.type == 'LAYER' and it.name == new_layer_name:
                obj.als.layer_index = ridx
                break

        tweak_mode_upper_stack(context, obj, anim_data)
        if subscriptions.check_handler not in bpy.app.handlers.depsgraph_update_pre:
            bpy.app.handlers.depsgraph_update_pre.append(subscriptions.check_handler)
        
        return {'FINISHED'}

class RemoveAnimLayer(bpy.types.Operator):
    """Remove animation layer. If the layer has children, prompts whether to delete the whole subtree or promote children to the parent."""
    bl_idname = "anim.remove_anim_layer"
    bl_label = "Remove Animation Layer"
    bl_options = {'REGISTER', 'UNDO'}

    children_action: bpy.props.EnumProperty(
        name='Children',
        description="What to do with descendants when removing a layer that has children",
        items=[
            ('PROMOTE', 'Promote children', "Re-parent direct children to this layer's parent (or root if it was a root layer)"),
            ('DELETE_SUBTREE', 'Delete subtree', "Delete this layer and all its descendants"),
        ],
        default='PROMOTE',
    )

    @classmethod
    def poll(cls, context):
        if context.object is None:
            return False
        anim_data = anim_data_type(context.object)
        if hasattr(anim_data, 'nla_tracks'):
            return len(anim_data.nla_tracks) > 0
        return False

    def invoke(self, context, event):
        obj = context.object
        a = active_item(obj)
        if a is None:
            return {'CANCELLED'}
        if is_group_layer(obj, a.name):
            # Has descendants — ask the user what to do.
            return context.window_manager.invoke_props_dialog(self)
        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        obj = context.object
        a = active_item(obj)
        if a is None:
            return
        descendants = layer_descendants(obj, a.name)
        layout.label(text=f"'{a.name}' has {len(descendants)} descendant layer(s).")
        layout.prop(self, 'children_action', expand=True)

    def execute(self, context):
        obj = context.object
        a = active_item(obj)
        if a is None:
            return {'CANCELLED'}
        anim_data = anim_data_type(obj)
        if anim_data is None or not hasattr(anim_data, 'nla_tracks'):
            return {'CANCELLED'}
        nla_tracks = anim_data.nla_tracks
        active_name = a.name
        active_parent = a.parent_layer

        # Decide which rows to remove based on the children policy.
        promoted_children = False
        if is_group_layer(obj, active_name):
            if self.children_action == 'DELETE_SUBTREE':
                names_to_remove = [active_name] + [d.name for d in layer_descendants(obj, active_name)]
            else:
                # PROMOTE: re-parent direct children to the active's parent.
                for child in layer_children(obj, active_name):
                    child['parent_layer'] = active_parent
                names_to_remove = [active_name]
                promoted_children = True
        else:
            names_to_remove = [active_name]

        override_tracks = check_override_tracks(obj, anim_data)
        for n in names_to_remove:
            if n in override_tracks:
                return {'CANCELLED'}

        # Blender 4.4 can crash when removing strips while in tweak mode.
        anim_data.use_tweak_mode = False

        try:
            for n in names_to_remove:
                idx = obj.Anim_Layers.find(n)
                if idx != -1:
                    obj.Anim_Layers.remove(idx)
        except TypeError:  # library overrides
            return {'CANCELLED'}

        for n in names_to_remove:
            track_idx = nla_tracks.find(n)
            if track_idx != -1:
                if len(nla_tracks) == 1:
                    tweak_mode_upper_stack(context, obj, anim_data, enter=False)
                nla_tracks.remove(nla_tracks[track_idx])

        # If children were promoted to the grandparent, their subtrees may now
        # be far from the new parent in NLA. Re-establish contiguity.
        if promoted_children:
            subscriptions.subscriptions_remove()
            try:
                enforce_subtree_contiguity(obj, context)
            finally:
                if context.scene is not None:
                    subscriptions.subscriptions_add(context.scene)

        new_count = len(obj.Anim_Layers)
        if obj.als.layer_index >= new_count:
            obj.als.layer_index = max(0, new_count - 1)
        return {'FINISHED'}

def share_layerkeys_items(self, context):
    '''create the layer items for the share keys excluding the current layer'''
    obj = self.id_data
    active = obj.Anim_Layers[obj.als.layer_index] if len(obj.Anim_Layers) else None
    return [(layer.name, layer.name, layer.name) for layer in obj.Anim_Layers
            if layer.type == 'LAYER' and layer != active]

class ShareLayerKeys(bpy.types.Operator):
    '''Share keyframes positions between layers'''
    bl_idname = "anim.share_layer_keys"
    bl_label = "Share Layer Keyframes"
    bl_options = {'REGISTER', 'UNDO'}
    

    @classmethod
    def poll(cls, context):
        return len(context.object.Anim_Layers)
    
    def execute (self, context):
        obj = context.object
        anim_data = anim_data_type(obj)
        fcu_frames = dict()
        current_strip = anim_data.nla_tracks[nla_idx(obj)].strips[0]
        #get the layer from the enumarator
        layer = obj.als.share_layer_keys
        if len(anim_data.nla_tracks[layer].strips) != 1:
            return {'CANCELLED'}
        action = anim_data.nla_tracks[layer].strips[0].action

        #store fcurves data path and array in a dictionary with all the frames
        fcurves = get_fcurves(obj, action)
        for fcu in fcurves:
            if selected_bones_filter(obj, fcu):
                continue
            #get all the keyframes
            keyframes = np.zeros([len(fcu.keyframe_points)*2])
            fcu.keyframe_points.foreach_get('co', keyframes)
            only_frames = keyframes[::2]
            #Store all the fcurve data in a dictionary
            fcu_frames.update({(fcu.data_path, fcu.array_index) : (only_frames, fcu.group)})
        
        #iterate over the stored fcurves and frames    
        for fcu_key, data in fcu_frames.items():
            frames, group = data
            
            value = None
            fcurves = get_fcurves(obj, anim_data.action)
            fcu = fcurves.find(data_path = fcu_key[0], index = fcu_key[1])
            if fcu is None:
                #if the fcurve doesn't exist then create it and assign the default value
                fcu = fcurves.new(data_path = fcu_key[0], index = fcu_key[1])
                value = bake_ops.attr_default(obj, fcu_key)[fcu_key[1]] if current_strip.blend_type in ['REPLACE', 'COMBINE'] else 0

            #if the group doesn't exist in the current layer then create one and assign it
            if fcu.group is None:
                new_group = add_group_to_fcurve(obj, fcu, group.name)
                # if group.name in anim_data.action.groups:
                #     new_group = anim_data.action.groups[group.name]
                # else:
                #     new_group = anim_data.action.groups.new(group.name)
                    
                # fcu.group = new_group   
            
            #exclude the frames that already exist in the current layer action fcurve
            if len(fcu.keyframe_points):
                keyframes = np.zeros([len(fcu.keyframe_points)*2])
                fcu.keyframe_points.foreach_get('co', keyframes)
                only_frames = set(keyframes[::2])
                frames = set(frames).difference(only_frames)
                
            #add all the keyframes
            for frame in frames:
                fcu.keyframe_points.add(1)
                #if there is no default value then get the value fromt the curve
                if value is None:
                    value = fcu.evaluate(frame)
                fcu.keyframe_points[-1].co = (frame, value)
            
            fcu.update()
        
        return{'FINISHED'}
        
def move_layer(dir, context, track_name=None):
    """Move a single NLA track via bpy.ops.anim.channels_move.

    If `track_name` is provided, that named track is the one moved. This is
    important when the addon's LAYER<->NLA mirror is temporarily out of sync
    (e.g. the caller just mutated obj.Anim_Layers manually and is about to
    sync NLA to match) — in that state, nla_idx(obj) cannot be trusted to
    point at the right track.

    Falls back to selecting the track at nla_idx(obj) (the active LAYER row)
    when no track_name is given — the legacy behavior used by
    MoveAnimLayerUp/Down's LAYER branches."""
    window = context.window
    screen = context.screen
    #Storing the first area in the screen
    old_area = screen.areas[0].type
    ui_type = screen.areas[0].ui_type

    area = screen.areas[0]
    area.type = 'NLA_EDITOR'
    region = area.regions[1]

    obj = context.object
    anim_data = anim_data_type(obj)

    #exit global tweakmode
    if context.scene.is_nla_tweakmode:
        tweak_mode_upper_stack(context, obj, anim_data, enter = False)

    #deselect all track strips
    for obj in context.scene.objects:
        anim_data = anim_data_type(obj)
        if anim_data is None:
            continue
        if not hasattr(anim_data, 'nla_tracks'):
            continue
        for track in anim_data.nla_tracks:
            track.select = False

    #select the track to move
    obj = context.object
    anim_data = anim_data_type(obj)
    if track_name is not None:
        idx_to_select = anim_data.nla_tracks.find(track_name)
        if idx_to_select == -1:
            # Restore area before bailing.
            screen.areas[0].type = old_area
            screen.areas[0].ui_type = ui_type
            return
    else:
        idx_to_select = nla_idx(obj)
    anim_data.nla_tracks[idx_to_select].select = True
    with context.temp_override(window=window, area=area, region=region):
        bpy.ops.anim.channels_expand()

        bpy.ops.anim.channels_move(direction=dir)

    #restoring the old area
    screen.areas[0].type = old_area
    screen.areas[0].ui_type = ui_type

    visible_layers(obj, anim_data.nla_tracks)

def _move_subtree_one_step(context, obj, subtree_root_name, direction):
    """Shift the subtree rooted at `subtree_root_name` past its next sibling
    subtree in `direction` ('UP' = higher NLA index, 'DOWN' = lower). Returns
    True if a move happened. Blocks at parent boundary (won't cross out of the
    current parent's subtree) and at NLA stack ends.

    Subtree contiguity invariant assumed: parent at lowest NLA index of its
    subtree, descendants immediately above, recursively. This is what the move
    operators themselves preserve; if the user manually breaks contiguity via
    the NLA editor, results are best-effort.
    """
    anim_data = anim_data_type(obj)
    if anim_data is None or not hasattr(anim_data, 'nla_tracks'):
        return False
    nla_tracks = anim_data.nla_tracks

    my_idx = nla_tracks.find(subtree_root_name)
    if my_idx == -1:
        return False
    my_row = obj.Anim_Layers.get(subtree_root_name)
    if my_row is None:
        return False
    my_parent = my_row.parent_layer
    my_size = layer_subtree_size(obj, subtree_root_name)

    # Find the sibling subtree just above/below my subtree in NLA.
    if direction == 'UP':
        above_idx = my_idx + my_size
        if above_idx >= len(nla_tracks):
            return False
        sibling_name = nla_tracks[above_idx].name
    else:  # DOWN
        if my_idx == 0:
            return False
        sibling_name = None
        for i in range(my_idx - 1, -1, -1):
            cand = obj.Anim_Layers.get(nla_tracks[i].name)
            if cand is None:
                continue
            if cand.parent_layer == my_parent:
                sibling_name = cand.name
                break
        if sibling_name is None:
            return False

    sibling_row = obj.Anim_Layers.get(sibling_name)
    if sibling_row is None or sibling_row.parent_layer != my_parent:
        # Crossing a parent boundary (or sibling row missing).
        return False
    sibling_size = layer_subtree_size(obj, sibling_name)

    # Move every member of my subtree past the sibling's subtree, one slot at
    # a time. Order matters: for UP, move top-of-subtree first so members
    # don't swap with each other; for DOWN, bottom-first.
    subtree_names = [subtree_root_name] + [d.name for d in layer_descendants(obj, subtree_root_name)]

    def _resort_by_position():
        pairs = [(nla_tracks.find(n), n) for n in subtree_names]
        pairs = [(i, n) for (i, n) in pairs if i != -1]
        if direction == 'UP':
            pairs.sort(key=lambda x: -x[0])
        else:
            pairs.sort(key=lambda x: x[0])
        return pairs

    for _ in range(sibling_size):
        for _, name in _resort_by_position():
            move_layer(direction, context, track_name=name)
    return True

class MoveAnimLayerUp(bpy.types.Operator):
    """Move the selected layer (and its subtree) up. Hold shift to move to the top of the parent's subtree."""
    bl_idname = "anim.layer_move_up"
    bl_label = "Move the selected Animation layer up."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.object
        if obj is None:
            return False
        anim_data = anim_data_type(obj)
        if hasattr(anim_data, 'nla_tracks'):
            return len(anim_data.nla_tracks) > 1
        return False

    def invoke(self, context, event):
        self.shift = event.shift
        return self.execute(context)

    def execute(self, context):
        obj = context.object
        a = active_item(obj)
        if a is None:
            return {'CANCELLED'}
        active_name = a.name

        subscriptions.subscriptions_remove()
        moved_any = False
        try:
            max_steps = len(obj.Anim_Layers) if self.shift else 1
            for _ in range(max_steps):
                if not _move_subtree_one_step(context, obj, active_name, 'UP'):
                    break
                moved_any = True
        finally:
            # Pin selection back on the moved layer by name.
            idx = obj.Anim_Layers.find(active_name)
            if idx != -1:
                obj.als.layer_index = idx
            subscriptions.subscriptions_add(context.scene)
        return {'FINISHED'} if moved_any else {'CANCELLED'}

class MoveAnimLayerDown(bpy.types.Operator):
    """Move the selected layer (and its subtree) down. Hold shift to move to the bottom of the parent's subtree."""
    bl_idname = "anim.layer_move_down"
    bl_label = "Move the selected animation layer down"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.object
        if obj is None:
            return False
        anim_data = anim_data_type(obj)
        if hasattr(anim_data, 'nla_tracks'):
            return len(anim_data.nla_tracks) > 1
        return False

    def invoke(self, context, event):
        self.shift = event.shift
        return self.execute(context)

    def execute(self, context):
        obj = context.object
        a = active_item(obj)
        if a is None:
            return {'CANCELLED'}
        active_name = a.name

        subscriptions.subscriptions_remove()
        moved_any = False
        try:
            max_steps = len(obj.Anim_Layers) if self.shift else 1
            for _ in range(max_steps):
                if not _move_subtree_one_step(context, obj, active_name, 'DOWN'):
                    break
                moved_any = True
        finally:
            idx = obj.Anim_Layers.find(active_name)
            if idx != -1:
                obj.als.layer_index = idx
            subscriptions.subscriptions_add(context.scene)
        return {'FINISHED'} if moved_any else {'CANCELLED'}

def copy_modifiers(modifier, mod_list):
    attr = {}
    for key in dir(modifier): #add all the attributes into a dictionary
        value = getattr(modifier, key)
        attr.update({key: value})    
    mod_list.append(attr)

    return mod_list

def paste_modifiers(fcu, mod_list):
    for mod in mod_list:
        if mod['type'] == 'CYCLES' and len(fcu.modifiers): #can add cycle modifier only as the first modifier
            continue
        new_mod = fcu.modifiers.new(mod['type'])
        if new_mod is None:
            continue
        for attr, value in mod.items():
            if type(value) is float or type(value) is int or type(value) is bool:
                if not new_mod.is_property_readonly(attr):
                    setattr(new_mod, attr, value)
                      
class CyclicFcurves(bpy.types.Operator):
    """Apply Cyclic Fcurve modifiers to all the selected bones and objects"""
    bl_idname = "anim.layer_cyclic_fcurves"
    bl_label = "Cyclic_Fcurves"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        anim_data = anim_data_type(context.object)
        if not hasattr(anim_data, 'action'):
            return False
        return anim_data.action is not None
    
    def execute(self, context):
        transform_types = ['location', 'rotation_euler', 'rotation_quaternion', 'scale']
        for obj in context.selected_objects:
            anim_data = anim_data_type(obj)
            fcurves = get_fcurves(obj, anim_data.action)
            for fcu in fcurves:
                if obj.mode == 'POSE': #apply only to selected bones
                    if selected_bones_filter(obj, fcu):
                        continue
                    if fcu.data_path in transform_types:
                        continue
                else:
                    if fcu.data_path not in transform_types and obj.als.data_type != 'KEY':
                        continue
                if not multikey.filter_properties(obj, fcu):
                    continue
                cycle_mod = False
                mod_list = []
                if len(fcu.modifiers):
                    #i = 0
                    while len(fcu.modifiers):
                        modifier = fcu.modifiers[0]
                        if modifier.type == 'CYCLES':
                            modifier.mute = False
                            cycle_mod = True
                            break
                        else: #if its a different modifier then store and remove it 
                            mod_list = copy_modifiers(modifier, mod_list)
                            fcu.modifiers.remove(fcu.modifiers[0])
                            #fcu.modifiers.update()
                if cycle_mod:
                    continue
                fcu.modifiers.new('CYCLES')
                fcu.update()
                if not len(mod_list):
                    continue #restore old modifiers
                paste_modifiers(fcu, mod_list)
                fcu.modifiers.update()                      
                                    
            redraw_areas(['GRAPH_EDITOR', 'VIEW_3D'])
                
        return {'FINISHED'}
    
class RemoveFcurves(bpy.types.Operator):
    """Remove Cyclic Fcurve modifiers from all the selected bones and objects"""
    bl_idname = "anim.layer_cyclic_remove"
    bl_label = "Cyclic_Remove"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        if not context.object:
            return False
        anim_data = anim_data_type(context.object)
        if not hasattr(anim_data, 'action'):
            return False
        return anim_data.action is not None
    
    def execute(self, context):
        transform_types = ['location', 'rotation_euler', 'rotation_quaternion', 'scale']
        for obj in context.selected_objects: 
            anim_data = anim_data_type(obj)
            fcurves = get_fcurves(obj, anim_data.action)
            for fcu in fcurves:
                if selected_bones_filter(obj, fcu):
                    continue
                # if obj.mode == 'POSE': #apply only to selected bones
                #     if obj.als.only_selected_bones:
                #         bones = [bone.path_from_id() for bone in context.selected_pose_bones]
                #         if fcu.data_path.split('].')[0]+']' not in bones:
                #             continue
                #     if fcu.data_path in transform_types:
                #         continue
                # pose mode always applies to bones and object mode to objects.
                elif obj.mode != 'POSE' and obj.als.data_type != 'KEY':
                    if fcu.data_path not in transform_types:
                        continue
                if not multikey.filter_properties(obj, fcu):
                    continue
                if len(fcu.modifiers):
                    for mod in fcu.modifiers:
                        
                        if mod.type == 'CYCLES':
                            fcu.modifiers.remove(mod)
                            fcu.update()
                            redraw_areas(['GRAPH_EDITOR', 'VIEW_3D'])
                            break
        return {'FINISHED'}

class ResetLayerKeyframes(bpy.types.Operator):
    """Add keyframes with 0 Value to the selected object/bones in the current layer, usefull for additive layers"""
    bl_idname = "anim.layer_reset_keyframes"
    bl_label = "Reset_Layer_Keyframes"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.object and len(context.object.Anim_Layers)

    def execute(self, context):
        obj = context.object
        anim_data = anim_data_type(obj)
        transform_types = ['location', 'rotation_euler', 'rotation_quaternion', 'scale']
        # fcurves = anim_data.action.fcurves
        fcurves = get_fcurves(obj, anim_data.action)
        frame_current = context.scene.frame_current
        strip = anim_data.nla_tracks[nla_idx(obj)].strips[0]
        frame_current = round(bake_ops.frame_evaluation(frame_current, strip), 3)
        
        for fcu in fcurves:
            if obj.type == 'ARMATURE': #apply only to selected bones
                if obj.mode == 'POSE' and fcu.data_path in transform_types: #skip 
                    continue
                elif selected_bones_filter(obj, fcu):
                    continue
                elif obj.mode == 'OBJECT' and fcu.data_path not in transform_types:
                    continue
            if not multikey.filter_properties(obj, fcu):
                continue
            key_exists = False
            blend_types = {'REPLACE', 'COMBINE'}
            value = bake_ops.attr_default(obj, (fcu.data_path, fcu.array_index))[fcu.array_index] if strip.blend_type in blend_types else 0
            
            #check if a key already exists on in the current frame
            for key in fcu.keyframe_points:
                if round(key.co[0], 3) == frame_current:
                    key.co[1] = value
                    key_exists = True
                    fcu.update()
                    continue
            if key_exists:
                continue
            #if key doesnt exists then add keyframes in current frame
            fcu.keyframe_points.add(1)
            try:
                fcu.keyframe_points[-1].co = (frame_current, value)
            except TypeError:
                print('Type Error ', fcu.data_path, frame_current, value)
            fcu.update() 
        return {'FINISHED'}
    
class AddAction(bpy.types.Operator):
    """Add a new action"""
    bl_idname = "anim.add_action"
    bl_label = "Add New Action"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        obj = context.object
        anim_data = anim_data_type(obj)
    
        tweak_mode_upper_stack(context, obj, anim_data, enter = False)
        #If there is no action get the layer name
        if anim_data.nla_tracks[nla_idx(obj)].strips[0].action is None:
            action = bpy.data.actions.new(obj.Anim_Layers[obj.als.layer_index].name)
        #otherwise get the previous action name
        else:
            action = obj.Anim_Layers[obj.als.layer_index].action
            action = bpy.data.actions[action.name].copy()

        add_action_slot(obj, action)
        obj.Anim_Layers[obj.als.layer_index].action = action

        #go into tweak mode
        obj.als.layer_index = obj.als.layer_index      
        return {'FINISHED'}

class RemoveAction(bpy.types.Operator):
    """remove the action from the layer"""
    bl_idname = "anim.remove_action"
    bl_label = "Remove Action"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        obj = context.object
        anim_data = anim_data_type(obj)
        
        tweak_mode_upper_stack(context, obj, anim_data, enter = False)
        anim_data.nla_tracks[nla_idx(obj)].strips[0].action = None
        obj.Anim_Layers[obj.als.layer_index].action = 'None'
        
        obj.als.layer_index = obj.als.layer_index      
        return {'FINISHED'}
    
class AddSlot(bpy.types.Operator):
    """Add a new slot"""
    bl_idname = "anim.add_slot"
    bl_label = "Add New Slot"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.object
    
    def execute(self, context):
        obj = context.object
        anim_data = anim_data_type(obj)
        if not anim_data:
            return {'FINISHED'}
        if not hasattr(anim_data, 'action_slot'):
            return {'FINISHED'}
        index = obj.als.layer_index
        if not len(anim_data.nla_tracks[index].strips):
            return {'FINISHED'}
        

        strip = anim_data.nla_tracks[index].strips[0]
        action = strip.action
        slot = action.slots.new(obj.als.data_type, obj.name)
        strip.action_slot = slot

        return {'FINISHED'}
    
class RemoveSlot(bpy.types.Operator):
    """Remove current slot"""
    bl_idname = "anim.remove_slot"
    bl_label = "Delete current slot"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.object
    
    def execute(self, context):
        obj = context.object
        anim_data = anim_data_type(obj)
        if not anim_data:
            return {'FINISHED'}
        if not hasattr(anim_data, 'action_slot'):
            return {'FINISHED'}
        index = obj.als.layer_index
        if not len(anim_data.nla_tracks[index].strips):
            return {'FINISHED'}
                
        strip = anim_data.nla_tracks[index].strips[0]
        action = strip.action
        action_slot = strip.action_slot

        action.slots.remove(action_slot)

        return {'FINISHED'}

def sync_frame_range(context):
    """Sync Frame Range to Action Length"""
    obj = context.object
    anim_data = anim_data_type(obj)
    use_frame_range = False  
    strip = anim_data.nla_tracks[nla_idx(obj)].strips[0]
    layer = obj.Anim_Layers[obj.als.layer_index]
    action = strip.action
    action_range = action.frame_range[1] - action.frame_range[0]
    #strip_range = strip.frame_end - strip.frame_start
    offset = layer.offset
    if action.use_frame_range:
        use_frame_range = True
        action.use_frame_range = False  
        
    strip.action_frame_start = action.frame_range[0]
    strip.action_frame_end = action.frame_range[1]
    layer.frame_start = action.frame_range[0] + offset
    layer.frame_end = action.frame_range[0] + offset + (action_range * strip.scale * strip.repeat)
    
    if use_frame_range:
        action.use_frame_range = True

class InfluenceNextKeyframe(bpy.types.Operator):
    """Select the influence keyframes of the current layer"""
    bl_idname = "anim.influence_next_key"
    bl_label = "Move to the next influence keyframe"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        anim_data = anim_data_type(context.object)
        return hasattr(anim_data,"nla_tracks") and len(anim_data.nla_tracks[nla_idx(context.object)].strips)
    
    def execute(self, context):
        
        strips = influence_get_strips(context)

        current_frame = context.scene.frame_current
        # The next keyframes from all the strips together
        next_keyframes = []

        #Assign selection to all strips
        for strip in strips:
            for fcu in strip.fcurves:
                if fcu.data_path != 'influence':
                    continue
                if not len(fcu.keyframe_points):
                    continue
                keys_len = len(fcu.keyframe_points)

                #Get the selection of the current keys or the next one in the list
                keyframes = np.zeros(keys_len*2)
                fcu.keyframe_points.foreach_get('co', keyframes)
                frames = keyframes[::2]
                next_i = bisect.bisect_right(frames, current_frame)
                if next_i >= len(frames):
                    continue
                next_frame = frames[next_i]
                next_keyframes.append(next_frame)

        if not len(next_keyframes):
            return {'CANCELLED'}  
        next_keyframes.sort()

        next_i = bisect.bisect_right(next_keyframes, current_frame)
        if next_i >= len(next_keyframes):
            return {'CANCELLED'}
        
        next_frame = next_keyframes[next_i]

        context.scene.frame_set(int(next_frame))

        return {'FINISHED'}  

class InfluencePrevKeyframe(bpy.types.Operator):
    """Select the influence keyframes of the current layer"""
    bl_idname = "anim.influence_prev_key"
    bl_label = "Move to the previous influence keyframe"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        anim_data = anim_data_type(context.object)
        return hasattr(anim_data,"nla_tracks") and len(anim_data.nla_tracks[nla_idx(context.object)].strips)
    
    def execute(self, context):
        
        strips = influence_get_strips(context)

        current_frame = context.scene.frame_current
        # The prev keyframes from all the strips together
        prev_keyframes = []

        #Assign selection to all strips
        for strip in strips:
            for fcu in strip.fcurves:
                if fcu.data_path != 'influence':
                    continue
                if not len(fcu.keyframe_points):
                    continue
                keys_len = len(fcu.keyframe_points)

                #Get the selection of the current keys or the prev one in the list
                keyframes = np.zeros(keys_len*2)
                fcu.keyframe_points.foreach_get('co', keyframes)
                frames = keyframes[::2]
                prev_i = bisect.bisect_left(frames, current_frame)
                if prev_i:
                    prev_i -= 1 
                prev_frame = frames[prev_i]
                prev_keyframes.append(prev_frame)

        if not len(prev_keyframes):
            return {'CANCELLED'}  
        prev_keyframes.sort()

        prev_i = bisect.bisect_left(prev_keyframes, current_frame)
        if prev_i:
            prev_i -= 1 
        
        prev_frame = prev_keyframes[prev_i]
        context.scene.frame_set(int(prev_frame))

        return {'FINISHED'}  


class SelectInfluenceKeys(bpy.types.Operator):
    """Select the influence keyframes of the current layer"""
    bl_idname = "anim.select_influence_keys"
    bl_label = "Select influence keyframes"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        anim_data = anim_data_type(context.object)
        return hasattr(anim_data,"nla_tracks") and len(anim_data.nla_tracks[nla_idx(context.object)].strips)
    
    def execute(self, context):
        
        strips = influence_get_strips(context)

        current_keys = None

        #Assign selection to all strips
        for strip in strips:
            for fcu in strip.fcurves:
                if fcu.data_path != 'influence':
                    continue
                if not len(fcu.keyframe_points):
                    continue
                keys_len = len(fcu.keyframe_points)

                #Get the selection of the current keys or the next one in the list
                if current_keys is None:
                    #Check if there are already keys selected
                    current_keys = np.zeros(keys_len, dtype = bool)
                    fcu.keyframe_points.foreach_get('select_control_point', current_keys)

                if all(current_keys):
                    #All the keyframes are already selected, then Deselect them
                    selected_keys = np.zeros(keys_len, dtype = bool)
                    fcu.keyframe_points.foreach_set('select_control_point', selected_keys)
                    fcu.keyframe_points.foreach_set('select_right_handle', selected_keys)
                    fcu.keyframe_points.foreach_set('select_left_handle', selected_keys)
                else:
                    selected_keys = np.ones(keys_len, dtype = bool)
                    fcu.keyframe_points.foreach_set('select_control_point', selected_keys)
                    fcu.keyframe_points.foreach_set('select_right_handle', selected_keys)
                    fcu.keyframe_points.foreach_set('select_left_handle', selected_keys)

        return {'FINISHED'}  
    
class SyncActionLength(bpy.types.Operator):
    """Sync Frame Range to Action Length"""
    bl_idname = "anim.sync_frame_range"
    bl_label = "Sync to Action"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        anim_data = anim_data_type(context.object)
        if not hasattr(anim_data,"nla_tracks"):
            return False
        if not len(anim_data.nla_tracks[nla_idx(context.object)].strips):
            return False
        return not anim_data.nla_tracks[nla_idx(context.object)].strips[0].use_sync_length
        
    def execute(self, context):
        sync_frame_range(context)
        return {'FINISHED'}

class ToggleGroupExpanded(bpy.types.Operator):
    """Toggle the expanded/collapsed state of a parent layer's subtree in the UI"""
    bl_idname = "anim.toggle_group_expanded"
    bl_label = "Toggle Expand"
    bl_options = {'REGISTER', 'UNDO'}

    group_name: bpy.props.StringProperty(name='Layer Name', default='')

    def execute(self, context):
        obj = context.object
        if obj is None:
            return {'CANCELLED'}
        idx = obj.Anim_Layers.find(self.group_name)
        if idx == -1:
            return {'CANCELLED'}
        obj.Anim_Layers[idx].expanded = not obj.Anim_Layers[idx].expanded
        return {'FINISHED'}

class UnlinkFromParent(bpy.types.Operator):
    """Remove this layer from its parent (make it a root). The layer keeps
    its NLA neighborhood; unrelated subtrees are not disturbed."""
    bl_idname = "anim.unlink_from_parent"
    bl_label = "Unlink From Parent"
    bl_options = {'REGISTER', 'UNDO'}

    layer_name: bpy.props.StringProperty(name='Layer Name', default='')

    @classmethod
    def poll(cls, context):
        return context.object is not None

    def execute(self, context):
        obj = context.object
        if not self.layer_name:
            return {'CANCELLED'}
        idx = obj.Anim_Layers.find(self.layer_name)
        if idx == -1:
            return {'CANCELLED'}
        layer = obj.Anim_Layers[idx]
        if not layer.parent_layer:
            return {'CANCELLED'}  # already a root

        layer['parent_layer'] = ''

        subscriptions.subscriptions_remove()
        try:
            # No priority_name — the unlinked layer stays where it is in NLA;
            # we don't want it jumping to the top of the root layers stack.
            enforce_subtree_contiguity(obj, context)
        finally:
            if context.scene is not None:
                subscriptions.subscriptions_add(context.scene)

        # Pin selection on the unlinked layer (rows may have shifted).
        new_idx = obj.Anim_Layers.find(self.layer_name)
        if new_idx != -1:
            obj.als.layer_index = new_idx
        return {'FINISHED'}

class LinkToLayer(bpy.types.Operator):
    """Make the currently active layer a child of this layer.

    Bound to the `+` button on each row of the UIList. `target_name` is the
    layer whose row was clicked. The active layer (obj.Anim_Layers[layer_index])
    gets its parent_layer set to `target_name`. Refuses self-parenting and
    cycles. After linking, the active subtree is repositioned so it sits at
    the top of `target_name`'s children stack (above any existing children)."""
    bl_idname = "anim.link_to_layer"
    bl_label = "Link Active Layer As Child"
    bl_options = {'REGISTER', 'UNDO'}

    target_name: bpy.props.StringProperty(name='Target Layer', default='')

    @classmethod
    def poll(cls, context):
        return context.object is not None and active_item(context.object) is not None

    def execute(self, context):
        obj = context.object
        a = active_item(obj)
        if a is None or not self.target_name:
            return {'CANCELLED'}
        if a.name == self.target_name:
            self.report({'WARNING'}, "Can't link a layer to itself")
            return {'CANCELLED'}
        if obj.Anim_Layers.find(self.target_name) == -1:
            self.report({'WARNING'}, f"Layer '{self.target_name}' not found")
            return {'CANCELLED'}
        if _would_create_cycle(obj, a.name, self.target_name):
            self.report({'WARNING'}, "Can't link: target is a descendant of the active layer")
            return {'CANCELLED'}
        if a.parent_layer == self.target_name:
            # Already linked to this parent — no-op rather than an error.
            return {'CANCELLED'}

        active_name = a.name
        a['parent_layer'] = self.target_name

        # Reorder NLA so the newly-linked layer sits at the top of the
        # target's children stack (priority_name), with its subtree contiguous.
        subscriptions.subscriptions_remove()
        try:
            enforce_subtree_contiguity(obj, context, priority_name=active_name)
        finally:
            if context.scene is not None:
                subscriptions.subscriptions_add(context.scene)

        # Pin selection back on the moved layer (its row index has shifted).
        idx = obj.Anim_Layers.find(active_name)
        if idx != -1:
            obj.als.layer_index = idx
        return {'FINISHED'}

class AssignLayerToGroup(bpy.types.Operator):
    """Set the active layer's parent layer (empty name = root). Refuses cycles."""
    bl_idname = "anim.assign_layer_to_group"
    bl_label = "Set Parent Layer"
    bl_options = {'REGISTER', 'UNDO'}

    group_name: bpy.props.StringProperty(name='Parent Name', default='')

    @classmethod
    def poll(cls, context):
        return context.object is not None and active_item(context.object) is not None

    def execute(self, context):
        obj = context.object
        a = active_item(obj)
        if a is None:
            return {'CANCELLED'}
        if self.group_name:
            if obj.Anim_Layers.find(self.group_name) == -1:
                self.report({'WARNING'}, f"Layer '{self.group_name}' not found")
                return {'CANCELLED'}
            if _would_create_cycle(obj, a.name, self.group_name):
                self.report({'WARNING'}, f"Cannot set parent: would create a cycle.")
                return {'CANCELLED'}
        layer_name = a.name
        a['parent_layer'] = self.group_name
        # Reorder NLA: the just-linked layer takes the top slot among its
        # new siblings (priority_name), with its subtree adjacent to the parent.
        subscriptions.subscriptions_remove()
        try:
            enforce_subtree_contiguity(obj, context, priority_name=layer_name)
        finally:
            if context.scene is not None:
                subscriptions.subscriptions_add(context.scene)
        # Pin selection back on the moved layer (visible_layers shuffles rows).
        idx = obj.Anim_Layers.find(layer_name)
        if idx != -1:
            obj.als.layer_index = idx
        return {'FINISHED'}

class LAYERS_UL_list(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, reversed):
        # filter_items below computes the full display order (hierarchical
        # walk, newest-first per level), so disable Blender's built-in reverse.
        self.use_filter_sort_reverse = False
        if self.layout_type not in {'DEFAULT', 'COMPACT'}:
            return

        obj = data
        anim_data = anim_data_type(obj)
        nla_tracks = anim_data.nla_tracks if anim_data else None

        # Depth-based indentation. Computed via parent_layer chain.
        depth = len(layer_ancestors(obj, item.name))
        has_children = is_group_layer(obj, item.name)

        row = layout.row(align=True)
        for _ in range(depth):
            row.label(text='', icon='BLANK1')

        if has_children:
            tri = 'TRIA_UP' if item.expanded else 'DISCLOSURE_TRI_RIGHT'
            op = row.operator('anim.toggle_group_expanded', text='', icon=tri, emboss=False)
            op.group_name = item.name
        # else:
        #     row.label(text='', icon='BLANK1')

        layer_idx = nla_tracks.find(item.name) if nla_tracks else -1
        if nla_tracks is None or layer_idx == -1:
            row.prop(item, 'name', text='', emboss=False)
            return

        track = nla_tracks[layer_idx]

        # `-` button on child rows (left of solo). Spacer for root rows so the
        # solo/name columns stay aligned regardless of hierarchy depth.
        if item.parent_layer:
            unlink_op = row.operator('anim.unlink_from_parent', text='', icon='REMOVE', emboss=False)
            unlink_op.layer_name = item.name
        # else:
        #     row.label(text='', icon='BLANK1')

        solo_icon = 'SOLO_ON' if track.is_solo else 'SOLO_OFF'
        row.prop(track, 'is_solo', text='', invert_checkbox=False, icon=solo_icon, emboss=False)
        row.prop(item, "name", text="", emboss=False)

        sub_row_right = row.row(align=True)
        sub_row_right.alignment = 'RIGHT'
        # `+` button: re-parent the currently-active layer under this row's layer.
        link_op = sub_row_right.operator('anim.link_to_layer', text='', icon='ADD', emboss=False)
        link_op.target_name = item.name
        
        if len(track.strips):
            blend_type = track.strips[0].blend_type
            sub_row_right.label(text=blend_type[0] + '  ')


        # Bind mute to item.mute (not track.mute) so the layer_mute update
        # callback fires and cascades to descendants. layer_mute keeps
        # track.mute in sync.
        mute_icon = 'HIDE_ON' if item.mute else 'HIDE_OFF'
        sub_row_right.prop(item, 'mute', text='', invert_checkbox=False, icon=mute_icon, emboss=False)
        lock_icon = 'LOCKED' if item.lock else 'UNLOCKED'
        sub_row_right.prop(item, 'lock', text='', invert_checkbox=False, icon=lock_icon, emboss=False)

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        n = len(items)
        flt_flags = [self.bitflag_filter_item] * n

        # Build parent → list-of-child-indices map (NLA-index ascending, since
        # collection order == NLA order). Sort descending below for "newest-first" UI.
        children_by_parent = {}
        name_to_idx = {}
        for i, it in enumerate(items):
            name_to_idx[it.name] = i
            children_by_parent.setdefault(it.parent_layer, []).append(i)
        for plist in children_by_parent.values():
            plist.sort(reverse=True)  # highest NLA index first per level

        # Hide descendants of any collapsed parent.
        collapsed = set()
        for i, it in enumerate(items):
            if not it.expanded and children_by_parent.get(it.name):
                collapsed.add(it.name)
        if collapsed:
            for i, it in enumerate(items):
                cur = it
                while cur.parent_layer:
                    if cur.parent_layer in collapsed:
                        flt_flags[i] &= ~self.bitflag_filter_item
                        break
                    pidx = name_to_idx.get(cur.parent_layer)
                    if pidx is None:
                        break
                    cur = items[pidx]

        # Visual order: depth-first walk where each parent emits its
        # descendants (newest-first per level) BEFORE itself. Result is
        # children above their parent in the UI list, matching the
        # "parent at lowest NLA index of its subtree" invariant.
        visual_order = []
        visited = set()

        def emit_subtree(idx):
            if idx in visited:
                return
            visited.add(idx)
            it = items[idx]
            for child_idx in children_by_parent.get(it.name, []):
                emit_subtree(child_idx)
            visual_order.append(idx)

        for root_idx in children_by_parent.get('', []):
            emit_subtree(root_idx)

        # Orphans (parent_layer set but parent missing): emit by NLA-descending index.
        leftover = [i for i in range(n) if i not in visited]
        leftover.sort(reverse=True)
        for i in leftover:
            visual_order.append(i)

        neworder = [0] * n
        for display_pos, original_idx in enumerate(visual_order):
            neworder[original_idx] = display_pos
        return flt_flags, neworder

def panelFactory(space_type):
    '''Adding Anim layers panel to different space types'''

    class ANIMLAYERS_PT_Panel:
        bl_space_type = space_type
        bl_region_type = "UI"
        bl_category = "Animation"
        #bl_options = {"DEFAULT_CLOSED"}

        @classmethod
        def poll(cls, context):
            return context.object is not None

    class ANIMLAYERS_PT_List(ANIMLAYERS_PT_Panel, bpy.types.Panel):
        bl_label = "Animation Layers"
        bl_idname = f"ANIMLAYERS_PT_{space_type}_List"

        def draw(self, context):
            obj = context.object
            anim_data = anim_data_type(obj)
            layout = self.layout
            
            addon_updater_ops.check_for_update_background()

            # call built-in function with draw code/checks
            addon_updater_ops.update_notice_box_ui(self, context)
                            
            row = layout.row()
            row.prop(obj.als, 'turn_on')
            
            if not obj.als.turn_on:
                return
            #action type
            if hasattr(obj.data, 'shape_keys'):
                split = layout.split(factor=0.4, align = True)
                split.label(text = 'Data Type:')
                split.prop(obj.als, 'data_type', text ='')
            
            row = layout.row()
            row.template_list("LAYERS_UL_list", "", context.object, "Anim_Layers", context.object.als, "layer_index", rows=2)
            col = row.column(align=True)
            col.operator('anim.add_anim_layer', text="", icon = 'ADD')
            col.operator('anim.remove_anim_layer', text="", icon = 'REMOVE')
            col.separator()
            col.operator("anim.layer_move_up", text="", icon = 'TRIA_UP')
            col.operator("anim.layer_move_down", text="", icon = 'TRIA_DOWN')

            if not hasattr(anim_data, 'nla_tracks') or not len(obj.Anim_Layers):# or obj.Anim_Layers[obj.als.layer_index].lock:
                return

            # Skip layer-specific controls when the active row is a group header.
            if not is_layer_row_active(obj):
                return

            track = anim_data.nla_tracks[nla_idx(obj)]

            # col=layout.column(align = True)
            row = layout.row(align = True)

            if not len(track.strips):
                return
            strip = track.strips[0]
            if not len(strip.fcurves):
                return

            #Drawing the influence slider
            if len(strip.fcurves[0].keyframe_points) and not strip.fcurves[0].mute:
                row.prop(strip, 'influence', slider = True, text = 'Influence')
                row.operator('anim.influence_prev_key', text = '', icon = 'PREV_KEYFRAME')
                row.separator(factor = 0.1)
                row.operator('anim.influence_next_key', text = '', icon = 'NEXT_KEYFRAME')
            else:
                row.prop(context.scene.als, 'influence', slider = True, text = 'Influence')
                # row.prop(obj.Anim_Layers[obj.als.layer_index], 'influence', slider = True, text = 'Influence')
            
            #Influence SETTINGS
            row.separator(factor = 1.5)
            row.prop(context.scene.als, 'influence_settings', text ='', icon = 'SETTINGS')
            if context.scene.als.influence_settings:
                split = layout.split(factor = 0.4)
                global_local = 'Global' if context.scene.als.influence_global == True else 'Local'
                split.prop(context.scene.als, 'influence_global', text = global_local, toggle = True)

                row = split.row(align = True)
                row.alignment = 'RIGHT'
                #Drawing Select influence keys
                row.operator('anim.select_influence_keys', text="", icon = 'RESTRICT_SELECT_OFF')

                #Hide and unhide from the graph editor
                hide_icon = 'HIDE_ON' if strip.fcurves[0].hide else 'HIDE_OFF'
                row.prop(obj.als, 'influence_hide', icon = hide_icon, text ='')

                #Drawing the mute influence button
                mute_icon = 'MUTE_IPO_OFF' if track.strips[0].fcurves[0].mute else  'MUTE_IPO_ON'
                row.prop(obj.als, 'influence_mute', icon_only=True, icon = mute_icon)
                
                row.separator(factor = 1.5)
                lock_icon = 'DECORATE_LOCKED' if strip.fcurves[0].lock else 'DECORATE_UNLOCKED'
                row.prop(obj.als, 'influence_lock', icon_only=True, icon =  lock_icon)

            row = layout.row()
            row.prop(track.strips[0], 'blend_type', text = 'Blend')
                
    class ANIMLAYERS_PT_Ops(ANIMLAYERS_PT_Panel, bpy.types.Panel):
        bl_label = "Bake Operators"
        bl_idname = f"ANIMLAYERS_PT_{space_type}_Ops"
        bl_parent_id = f"ANIMLAYERS_PT_{space_type}_List"
        bl_options = {"DEFAULT_CLOSED"}

        def draw(self, context):
            obj = context.object
            if obj is None:
                return
            if not obj.als.turn_on:
                return
            anim_data = anim_data_type(obj)
            if not hasattr(anim_data, 'nla_tracks') or not len(obj.Anim_Layers):# or obj.Anim_Layers[obj.als.layer_index].lock:
                return
            if not is_layer_row_active(obj):
                return
            layout = self.layout
            merge_layers = layout.column()
            #merge_layers.operator("anim.layers_merge_down", text="New Baked Layer", icon = 'NLA')
            merge_layers.operator("anim.layers_merge_down", text="Merge / Bake", icon = 'NLA_PUSHDOWN')

            duplicateanimlayer = layout.row(align=True)
            duplicateanimlayer.operator('anim.duplicate_anim_layer', text="Duplicate Layer", icon = 'SEQ_STRIP_DUPLICATE')
            icon = 'LINKED' if obj.als.linked else 'UNLINKED'
            duplicateanimlayer.prop(obj.als, 'linked', icon_only=True, icon = icon)

            extract = layout.row(align=True)
            extract.operator('anim.extract_selected_bones', text="Extract Selected Bones", icon = 'SELECT_SUBTRACT')
            markers = layout.row(align=True)
            markers.operator('anim.extract_markers', text="Extract Marked Keyframes", icon = 'MARKER_HLT')

    class ANIMLAYERS_PT_Tools(ANIMLAYERS_PT_Panel, bpy.types.Panel):
        bl_label = "Layer Tools"
        bl_idname = f"ANIMLAYERS_PT_{space_type}_Tools"
        bl_parent_id = f"ANIMLAYERS_PT_{space_type}_List"
        bl_options = {"DEFAULT_CLOSED"}

        def draw(self, context):
            obj = context.object
            if obj is None:
                return
            if not obj.als.turn_on:
                return
            if not is_layer_row_active(obj):
                return
            if len(obj.Anim_Layers):
                if obj.Anim_Layers[obj.als.layer_index].lock:
                    return
            
            layout = self.layout
            
            row = layout.row()
            row.operator("anim.bones_in_layer", text="Select Bones in Layer", icon = 'BONE_DATA')
            row = layout.row()
            row.separator()
            row = layout.row()
            split = row.split(factor=0.9, align = True)
            #if obj.mode == 'POSE':
            split.prop(obj.als, 'only_selected_bones', text = 'Affect Only Selected Bones')#, icon = 'GROUP_BONE'
            #else:
            #    split.label(text = 'Filter')
            split.operator('fcurves.filter', icon ='FILTER', text = '')
            box = layout.box()
            row = box.row()
            row.operator("anim.layer_reset_keyframes", text="Reset Key Layer ", icon = 'KEYTYPE_MOVING_HOLD_VEC')
            row = box.row()
            row.prop(obj.als, 'inbetweener', text = 'Inbetweener', slider = True)

            layout.separator(factor = 0.2)
            row = layout.row()
            row.operator('anim.share_layer_keys', text = 'Share Layer Keys')
            row.prop(obj.als, 'share_layer_keys', text = '')
            layout.separator(factor = 0.2)

            box = layout.box()
            row = box.row()
            row.alignment = 'CENTER'
            row.label(text = 'Multikey - Edit Multiple Keyframes')
            row = box.row()
            row.prop(context.scene.multikey, 'scale', text = 'Scale', slider = True)
            row.prop(context.scene.multikey, 'randomness', text = 'Random', slider = True)      
            box.operator("fcurves.multikey", icon = 'ACTION_TWEAK')

            layout.separator(factor = 0.2)
            row = layout.row()
            row.operator("anim.layer_cyclic_fcurves", text="Cyclic Fcurves", icon = 'FCURVE')
            row.operator("anim.layer_cyclic_remove", text="Remove Fcurves", icon = 'X')
            layout.separator(factor = 0.2)
            box = layout.box()
            row = box.row()
            #row.label(text= 'Keyframes From Multiple Layers:')
            row.prop(obj.als, 'view_all_keyframes', text = 'View Multiple Layer Keyframes')
            
            if obj.als.view_all_keyframes:
                row = box.row()
                split = row.split(factor=0.4, align = True)
                split.prop(obj.als, 'edit_all_keyframes')
                split.prop_menu_enum(obj.als, 'view_all_type') 
        
    class ANIMLAYERS_PT_Settings(ANIMLAYERS_PT_Panel, bpy.types.Panel):
        bl_label = "Layer Settings"
        bl_idname = f"ANIMLAYERS_PT_{space_type}_Settings"
        bl_parent_id = f"ANIMLAYERS_PT_{space_type}_List"
        bl_options = {"DEFAULT_CLOSED"}

        def draw(self, context):
            obj = context.object
            if obj is None:
                return
            if not obj.als.turn_on:
                return
            # if len(obj.Anim_Layers):
            #     if obj.Anim_Layers[obj.als.layer_index].lock:
            #         return

            anim_data = anim_data_type(obj)
            if not hasattr(anim_data, 'nla_tracks'):
                return
            nla_tracks = anim_data.nla_tracks
            if not len(nla_tracks):
                return

            layout = self.layout

            track = nla_tracks[nla_idx(obj)]
            strip = track.strips[0] if len(track.strips) else anim_data
            layer = obj.Anim_Layers[obj.als.layer_index]

            # Parent assignment dropdown. Shown whenever there's more than one
            # layer (so there's a potential parent to pick).
            if len(obj.Anim_Layers) > 1:
                grp_box = layout.box()
                row = grp_box.row(align=True)
                row.label(text='Parent:', icon='OUTLINER_COLLECTION')
                row.prop(layer, 'assigned_group', text='')
                if is_group_layer(obj, layer.name):
                    desc_count = len(layer_descendants(obj, layer.name))
                    grp_box.prop(layer, 'group_color', text='Group Color')
                    grp_box.label(text=f"{desc_count} descendant layer(s)")

            box = layout.box()

            if anim_data is not None:
                row = box.row(align = True)
                split = box.split(factor=0.3, align = True)
                # row.alignment = 'CENTER'
                split.label(text = 'Active Action: ')
                split.template_ID(layer, "action", new="anim.add_action") #, new="action.new", unlink="action.unlink"

                #Active Slot only available from Blender 4.4
                if hasattr(strip, 'action_slot'):
                    split = box.split(factor=0.3, align = True)
                    split.label(text = 'Active Slot: ')
                    split = split.split(factor=0.7, align = True)
                    split.template_search(
                        strip, "action_slot",
                        strip, "action_suitable_slots",
                        # new="anim.slot_new_for_id",
                        # unlink="anim.slot_unassign_from_id",
                    )
                    split.operator("anim.add_slot", icon = 'DUPLICATE', text = '')
                    split.operator("anim.remove_slot", icon = 'X', text = '')

                # row = box.row(align = True)
                split = box.split(factor=0.6, align = True)
                split.prop(obj.als, 'auto_rename', text = 'Sync Layer/Action Names')
                split.prop(obj.als, 'auto_blend')
            
            box = layout.box()
            ##Custom Frame Range
            row = box.row()
            row.prop(layer,'custom_frame_range', icon = 'TIME') 
            
            frame_range_settings = context.preferences.addons[__package__].preferences.frame_range_settings
            if layer.custom_frame_range:
                # row.operator("anim.sync_frame_range", text="", icon = 'FILE_REFRESH')
                row = box.row()
                
                if frame_range_settings == 'ANIMLAYERS':
                    row.prop(layer,'frame_start', text = 'Frame Start')
                    row.prop(layer,'frame_end', text = 'Frame End')
                    if len(track.strips):
                        row.prop(track.strips[0],'extrapolation', text = '')
                    row = box.row()
                    row.prop(track.strips[0],'use_sync_length', text = 'Always Sync')
                    row.operator("anim.sync_frame_range", text="Sync to Action", icon = 'FILE_REFRESH')
                    row = box.row()
                    row.prop(track.strips[0],'use_reverse')
                    #row.prop(track.strips[0],'repeat')
                    row.prop(layer,'repeat')
                elif len(track.strips):
                    row.prop(track.strips[0],'frame_start', text = 'Frame Start')
                    row.prop(track.strips[0],'frame_end', text = 'Frame End')
                    row.prop(track.strips[0],'extrapolation', text = '')
                    row = box.row()
                    row.prop(track.strips[0],'action_frame_start', text = 'Action Start')
                    row.prop(track.strips[0],'action_frame_end', text = 'Action End')
                    row.prop(track.strips[0],'scale', text = 'Scale')
                    row = box.row()
                    row.prop(track.strips[0],'use_sync_length', text = 'Always Sync')
                    row.operator("anim.sync_frame_range", text="Sync to Action", icon = 'FILE_REFRESH')
                    row = box.row()
                    row.prop(track.strips[0],'use_reverse')
                    row.prop(layer,'repeat')

            row = box.row()
            # row.prop(track.strips[0],'scale', text = 'Scale')
            if frame_range_settings == 'ANIMLAYERS' or not layer.custom_frame_range:
                row.prop(layer,'speed', text = 'Speed ')
                row.prop(layer,'offset', text = 'Offset')

            # row = box.row()
            # for mod in strip.modifiers:
            layout.template_modifiers()
            # split = layout.split(factor=0.6, align = True)
            # split.label(text="Default Blend Type ")
            # split.prop(context.scene.als,'blend_type', text = '')

    # Set the class name dynamically
    ANIMLAYERS_PT_List.__name__ = f"ANIMLAYERS_PT_{space_type}_List"
    ANIMLAYERS_PT_Ops.__name__ = f"ANIMLAYERS_PT_{space_type}_Ops"
    ANIMLAYERS_PT_Tools.__name__ = f"ANIMLAYERS_PT_{space_type}_Tools"
    ANIMLAYERS_PT_Settings.__name__ = f"ANIMLAYERS_PT_{space_type}_Settings"

    return ANIMLAYERS_PT_List, ANIMLAYERS_PT_Ops, ANIMLAYERS_PT_Tools, ANIMLAYERS_PT_Settings

def copy_action(action):

    new_action = action.copy()
    if not hasattr(action, 'layers'):
        return new_action
    
    for slot in action.slots:
        slot_id = slot.identifier
        users = slot.users()
        for user in users:
            user.animation_data.action_slot = new_action.slots[slot_id]

    return new_action            
 
def get_obj_slot(obj, action, data_type = None):
    '''Get the slot in the action that this object is using either it's object, or shapekeys'''
    
    if data_type is None:
        data_type = obj.als.data_type

    if not hasattr(action, 'slots'):
        return None
    
    if not len(action.slots):
        # If no slots exist, create one for the object and return it
        slot = add_action_slot(obj, action)
        return slot
    
    # data_type = obj.als.data_type 
    for slot in action.slots:
        if slot.target_id_type != data_type:
            continue

        if data_type == 'KEY' and obj.data.shape_keys in slot.users():
            return slot
        elif obj in slot.users():
            return slot
        
    return add_action_slot(obj, action)
    
def get_fcurves(obj: bpy.types.Object, action: bpy.types.Action, data_type = None):

    if data_type is None:
        data_type = obj.als.data_type

    if hasattr(action, 'layers'):
        slot = get_obj_slot(obj, action, data_type)
        if slot:
            channelbag = anim_utils.action_get_channelbag_for_slot(action, slot)
            if not channelbag and hasattr(anim_utils, 'action_ensure_channelbag_for_slot'):
                channelbag = anim_utils.action_ensure_channelbag_for_slot(action, slot)
            if channelbag:
                return channelbag.fcurves

    # action.fcurves not available anymore from Blender 5.0
    if hasattr(action, 'fcurves'):
        return action.fcurves
    return []

def get_channelbag(obj: bpy.types.Object, action: bpy.types.Action, data_type = None):
    '''Getting the container of the fcurves, either the action or channelbag
    Using this when adding a new group to the action'''

    if data_type is None:
        data_type = obj.als.data_type
        
    if hasattr(action, 'layers'):
        slot = get_obj_slot(obj, action, data_type)
        channelbag = None
        if slot:
            channelbag = anim_utils.action_get_channelbag_for_slot(action, slot)
        if channelbag is None:
            # action_ensure_channelbag_for_slot works only from Blender 5
            if hasattr(anim_utils, 'action_ensure_channelbag_for_slot'):
                channelbag = anim_utils.action_ensure_channelbag_for_slot(action, slot)
            else:
                channelbag = action
        return channelbag
    else:
        return action

def add_group_to_fcurve(obj, fcu, groupname):
    '''Add an fcurve group based on the fcurve container, either action or channelbag'''

    action = fcu.id_data
    #get the container which is either a channelbag or a group
    channelbag = get_channelbag(obj, action)
    group = channelbag.groups.get(groupname)
    if group is None:
        group = channelbag.groups.new(groupname)

    fcu.group = group
    
    return group

def add_action_slot(obj, action):
    '''Adding a new slot to an action, Relevant only for Blender 4.4 +'''

    if not action:
        return None
    if not hasattr(action, 'layers'):
        return None
    
    if action.slots:
        for slot in action.slots:
            if obj.als.data_type != slot.target_id_type:
                continue

            # Shapekey slot users are the shapekey data, object slot users are the objects
            item = obj.data.shape_keys if hasattr(obj.data, 'shape_keys') and obj.als.data_type == 'KEY' else obj
            if item in slot.users():
                return slot

    slot = action.slots.new(obj.als.data_type, obj.name)
    return slot

def remove_empty_slots(action):
    '''removing empty slots without users, using when extracting from a layer'''
    if not action:
        return
    if not hasattr(action, 'layers'):
        return
    if not action.slots:
        return
    
    for slot in action.slots:
        if not len(slot.users()):
            action.slots.remove(slot)


classes = (AutoCustomFrameRange, ResetLayerKeyframes, LAYERS_UL_list, AddAnimLayer, ExtractSelection, ExtractMarkers, DuplicateAnimLayer, RemoveAnimLayer, CyclicFcurves, RemoveFcurves, MoveAnimLayerUp,
    MoveAnimLayerDown, SelectBonesInLayer, ClearNLA, ClearActiveAction, OverrideError, AddAction, SyncActionLength, RemoveAction, ShareLayerKeys, SelectInfluenceKeys,
    AddSlot, RemoveSlot, EditAllLayersOperator, InfluencePrevKeyframe, InfluenceNextKeyframe,
    ToggleGroupExpanded, AssignLayerToGroup, LinkToLayer, UnlinkFromParent)

spaceTypes = ['VIEW_3D', 'GRAPH_EDITOR', 'DOPESHEET_EDITOR', 'NLA_EDITOR']
# panel_classes = (cls for spaceType in spaceTypes for cls in panelFactory(spaceType))
panel_classes = []

def register_panels():

    prefs = bpy.context.preferences.addons[__package__].preferences
    for space_type in spaceTypes:
        if space_type in prefs.enabled_editors:
            panel_classes.extend(panelFactory(space_type))

def unregister_panels():
    for panel in reversed(panel_classes):
        try:
            bpy.utils.unregister_class(panel)
        except:
            pass
    panel_classes.clear()

def register():
    from bpy.utils import register_class
    register_panels()

    for cls in classes:
        register_class(cls)

    bpy.app.handlers.load_post.append(loadanimlayers)
    
    
def unregister():
    from bpy.utils import unregister_class
    unregister_panels()

    for cls in classes:
        unregister_class(cls)
    
    if loadanimlayers in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(loadanimlayers)

    if subscriptions.check_handler in bpy.app.handlers.depsgraph_update_pre:
        bpy.app.handlers.depsgraph_update_pre.remove(subscriptions.check_handler)
    if subscriptions.animlayers_frame in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(subscriptions.animlayers_frame)
    bpy.msgbus.clear_by_owner(bpy.context.scene)

       