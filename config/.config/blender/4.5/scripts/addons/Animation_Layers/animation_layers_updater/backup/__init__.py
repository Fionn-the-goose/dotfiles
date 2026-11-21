# ***** BEGIN GPL LICENSE BLOCK *****
#
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# ***** END GPL LICENCE BLOCK *****

bl_info = {
    "name": "Animation Layers",
    "author": "Tal Hershkovich",
    "version" : (2, 4, 0),
    "blender" : (3, 2, 0),
    "location": "View3D - Properties - Animation Panel",
    "description": "Simplifying the NLA editor into an animation layers UI and workflow",
    "wiki_url": "",
    "category": "Animation"}

if "bpy" in locals():
    import importlib
    if "bake_ops" in locals():
        importlib.reload(bake_ops)
    if "anim_layers" in locals():
        importlib.reload(anim_layers)
    if "subscriptions" in locals():
        importlib.reload(subscriptions)
    if "multikey" in locals():
        importlib.reload(multikey)
    if "addon_updater_ops" in locals():
        importlib.reload(addon_updater_ops)

import bpy
from . import addon_updater_ops
from . import anim_layers
from . import bake_ops
from . import subscriptions
from . import multikey
from bpy.utils import register_class
from bpy.utils import unregister_class

class AnimLayersSceneSettings(bpy.types.PropertyGroup):
    bake_range_type: bpy.props.EnumProperty(name = 'Bake Range', description="Use either scene, actions length or custom frame range", default = 'SCENE', update = bake_ops.bake_range_type,
        items = [('SCENE', 'Scene Range', 'Bake to the scene range'), ('KEYFRAMES', 'Keyframes Range', 'Bake all the keyframes in the layers'), ('CUSTOM', 'Custom', 'Enter a custom frame range')], override = {'LIBRARY_OVERRIDABLE'})
        
    bake_range: bpy.props.IntVectorProperty(name='Frame Range', description='Bake to a custom frame range', size = 2)
    handles_type: bpy.props.EnumProperty(name="Handle Types", description="Select handle type before recalculating the handle values", override = {'LIBRARY_OVERRIDABLE'}, default = 'FREE',
        items = [
            ('PRESERVE', 'Preserve', 'Preserves previous Bezier handlers types before recalculation', 0),
            ( 'FREE', 'Free', 'Bezier handlers get Free handle types before recalculation', 1),
            ('ALIGNED', 'Aligned', 'Bezier handlers get Aligned handle types before recalculation', 2),
            ('VECTOR','Vector', 'Bezier handlers get Vector handle types', 3),
            ('AUTO', 'Auto', 'Bezier handlers get Auto handle types before recalculation',4),
            ('AUTO_CLAMPED', 'Auto Clamped', 'Bezier handlers get Free handle types before recalculation', 5)
            ]
        )
    
    influence: bpy.props.FloatProperty(name="Layer Influence", description="Layer Influence", min = 0.0, options={'ANIMATABLE'}, max = 1.0, default = 1.0, precision = 3, update = anim_layers.influence_update)
    influence_settings: bpy.props.BoolProperty(name="Influence Settings", description="Opens Influence settings menu", default=False)
    influence_global: bpy.props.BoolProperty(name="Influence Global/Local", description="Influence options affect current layer or all layers", default=False)
    edit_all_layers_op: bpy.props.BoolProperty(name="Edit All Layers Check Property", description="Operator to check if edit all layers is running", default=False)
    

class AnimLayersSettings(bpy.types.PropertyGroup):
    turn_on: bpy.props.BoolProperty(name="Turn Animation Layers On", description="Turn on and start Animation Layers", default=False, options={'HIDDEN'}, update = anim_layers.turn_animlayers_on, override = {'LIBRARY_OVERRIDABLE'})
    layer_index: bpy.props.IntProperty(update = anim_layers.update_layer_index, options={'LIBRARY_EDITABLE'}, default = 0, override = {'LIBRARY_OVERRIDABLE'})
    linked: bpy.props.BoolProperty(name="Linked", description="Duplicate a layer with a linked action", default=False, options={'HIDDEN'}, override = {'LIBRARY_OVERRIDABLE'})
    
    #Bake settings
    smartbake: bpy.props.BoolProperty(name="Smart Bake", description="Stay with the same amount of keyframes after merging and baking", default=False, options={'HIDDEN'}, override = {'LIBRARY_OVERRIDABLE'})
    onlyselected: bpy.props.BoolProperty(name="Only selected Bones", description="Bake only selected Armature controls", default=False, options={'HIDDEN'}, override = {'LIBRARY_OVERRIDABLE'})
    clearconstraints: bpy.props.BoolProperty(name="Clear constraints", description="Clear constraints during bake", default=False, options={'HIDDEN'}, override = {'LIBRARY_OVERRIDABLE'})
    mergefcurves: bpy.props.BoolProperty(name="Merge Cyclic & Fcurve modifiers", description="Include Fcurve modifiers in the bake", default = True, options={'HIDDEN'}, override = {'LIBRARY_OVERRIDABLE'})

    view_all_keyframes: bpy.props.BoolProperty(name="View", description="View keyframes from multiple layers, use lock and mute to exclude layers", default=False, update = anim_layers.view_all_keyframes, override = {'LIBRARY_OVERRIDABLE'})
    edit_all_keyframes: bpy.props.BoolProperty(name="Edit", description="Edit keyframes from multiple layers", default=False, update = anim_layers.unlock_edit_keyframes, override = {'LIBRARY_OVERRIDABLE'})
    only_selected_bones: bpy.props.BoolProperty(name="Only Selected Bones", description="Edit and view only selected bones", default=True, update = anim_layers.only_selected_bones, override = {'LIBRARY_OVERRIDABLE'})
    view_all_type: bpy.props.EnumProperty(name="Type", description="Select visibiltiy type of keyframes", update = anim_layers.view_all_keyframes, override = {'LIBRARY_OVERRIDABLE'}, default = '2',
        items = [
            ('0', 'Breakdown', 'select Breakdown visibility'),
            ('1', 'Jitter', 'select Jitter visibility'),
            ('2', 'Moving Hold', 'select Moving Hold visibility'),
            ('3', 'Extreme', 'select Extreme visibility'),
            ('4', 'Keyframe', 'select Keyframe visibility')
        ]
    )
    baketype : bpy.props.EnumProperty(name = '', description="Type of Bake", items = [('AL', 'Anim Layers','Use Animation Layers Bake',0), ('NLA', 'NLA Bake', 'Use Blender internal NLA Bake',1)], override = {'LIBRARY_OVERRIDABLE'})
    direction: bpy.props.EnumProperty(name = '', description="Select direction of merge", items = [('UP', 'Up','Merge upwards','TRIA_UP',1), ('DOWN', 'Down', 'Merge downwards','TRIA_DOWN',0), ('ALL', 'All', 'Merge all layers')], override = {'LIBRARY_OVERRIDABLE'})
    operator : bpy.props.EnumProperty(name = '', description="Type of bake", items = [('NEW', 'New Baked Layer','Bake into a New Layer','NLA',1), ('MERGE', 'Merge', 'Merge Layers','NLA_PUSHDOWN',0)], override = {'LIBRARY_OVERRIDABLE'})
    blend_type :  bpy.props.EnumProperty(name = 'Blend Type', description="Blend Type", 
        items = [('REPLACE', 'Replace', 'Use as a Base Layer'), ('ADD', 'Add', 'Use as an Additive Layer'), ('SUBTRACT', 'Subtract', 'Use as an Subtract Layer')], update = anim_layers.blend_type_update , override = {'LIBRARY_OVERRIDABLE'})
    data_type :  bpy.props.EnumProperty(name = 'Data Type', description="Select type of action data", default = 'OBJECT', update = anim_layers.data_type_update,
        items = [('KEY', 'Shapekey', 'Switch to shapekey animation layers'), ('OBJECT', 'Object', 'Switch to object animation')], override = {'LIBRARY_OVERRIDABLE'})#, update = anim_layers.blend_type_update
    auto_rename: bpy.props.BoolProperty(name="Auto Rename Layer", description="Rename layer to match to selected action", default = True, update = anim_layers.auto_rename, options={'HIDDEN'}, override = {'LIBRARY_OVERRIDABLE'})
    auto_blend: bpy.props.BoolProperty(name="Auto Blend", description="Apply blend type automatically based on scale and rotation values", default = False, options={'HIDDEN'}, override = {'LIBRARY_OVERRIDABLE'})
    fcurves: bpy.props.IntProperty(name='fcurves', description='helper to check if fcurves are changed', default=0, override = {'LIBRARY_OVERRIDABLE'})
    upper_stack : bpy.props.BoolProperty(name="Upper Stack Evaluation", description="Checks if tweak mode uses upper stack", default=False, override = {'LIBRARY_OVERRIDABLE'})
    
    #Tools    
    inbetweener : bpy.props.FloatProperty(name='Inbetween Keyframe', description="Adds an inbetween Keyframe between the Layer's neighbor keyframes", soft_min = 0, soft_max = 1, default=0.5, options = set(), override = {'LIBRARY_OVERRIDABLE'}, update = anim_layers.add_inbetween_key)
    share_layer_keys: bpy.props.EnumProperty(name = 'Share Layer Keys', description="Share keyframes positions between layers", items = anim_layers.share_layerkeys_items, override = {'LIBRARY_OVERRIDABLE'})
    
    influence_hide: bpy.props.BoolProperty(name="Hide Influence", description="Hide Influence Fcurves", default=False, update = anim_layers.influence_hide_keyframes, override = {'LIBRARY_OVERRIDABLE'})
    influence_lock: bpy.props.BoolProperty(name="Lock Influence", description="Lock Influence Fcurves", default=False, update = anim_layers.influence_lock_keyframes, override = {'LIBRARY_OVERRIDABLE'})
    influence_mute: bpy.props.BoolProperty(name="Mute Influence", description="Mute Influence Fcurves", default=False, update = anim_layers.influence_mute_fcurves, override = {'LIBRARY_OVERRIDABLE'})

class AnimLayersItems(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="AnimLayer", override = {'LIBRARY_OVERRIDABLE'}, update = anim_layers.layer_name_update)
    mute: bpy.props.BoolProperty(name="Mute", description="Mute Animation Layer", default=False, options={'HIDDEN'}, override = {'LIBRARY_OVERRIDABLE'}, update = anim_layers.layer_mute)
    lock: bpy.props.BoolProperty(name="Lock", description="Lock Animation Layer", default=False, options={'HIDDEN'}, override = {'LIBRARY_OVERRIDABLE'}, update = anim_layers.layer_lock)
    solo: bpy.props.BoolProperty(name="Solo", description="Solo Animation Layer", default=False, options={'HIDDEN'}, override = {'LIBRARY_OVERRIDABLE'}, update = anim_layers.layer_solo)
    influence: bpy.props.FloatProperty(name="Layer Influence", description="Layer Influence", min = 0.0, options={'ANIMATABLE'}, max = 1.0, default = 1.0, precision = 3, update = anim_layers.influence_update) #

    action: bpy.props.PointerProperty(name = 'action', description = "Select action", type=bpy.types.Action, update = anim_layers.load_action, override = {'LIBRARY_OVERRIDABLE'})

    action_range: bpy.props.FloatVectorProperty(name='action range', description="used to check if layer needs to update frame range", override = {'LIBRARY_OVERRIDABLE'}, size = 2)
    custom_frame_range: bpy.props.BoolProperty(name="Custom Frame Range", description="Use a custom frame range per layer instead of the scene frame range", default=False, options={'HIDDEN'}, override = {'LIBRARY_OVERRIDABLE'}, update = anim_layers.layer_frame_range)
    
    frame_start: bpy.props.FloatProperty(name='Action Start Frame', description="First frame of the layer's action",min = 0, default=0, override = {'LIBRARY_OVERRIDABLE'}, update = anim_layers.layer_frame_start)
    frame_end: bpy.props.FloatProperty(name='Action End Frame', description="End frame of the layer's action", default=0, override = {'LIBRARY_OVERRIDABLE'}, update = anim_layers.layer_frame_end)
    speed: bpy.props.FloatProperty(name='Speed of the action', description="Speed of the action strip", default = 1, override = {'LIBRARY_OVERRIDABLE'}, update = anim_layers.layer_speed)
    offset: bpy.props.FloatProperty(name='Offset when the action starts', description="Offseting the whole layer animation", default = 0, precision = 2, override = {'LIBRARY_OVERRIDABLE'}, update = anim_layers.layer_offset)
    repeat: bpy.props.FloatProperty(name="Repeat", description="Repeat the action", min = 0.1, default = 1, options={'HIDDEN'}, override = {'LIBRARY_OVERRIDABLE'}, update = anim_layers.layer_repeat)


class AnimLayersObjects(bpy.types.PropertyGroup):

    object: bpy.props.PointerProperty(name = "object", description = "objects with animation layers turned on", type=bpy.types.Object, override = {'LIBRARY_OVERRIDABLE'})

    

# Add-ons Preferences Update Panel
# Define Panel classes for updating
panels = anim_layers.panel_classes
def update_panel(self, context):
    anim_layers.unregister_panels()
    anim_layers.register_panels()

    message = "AnimationLayers: Updating Panel locations has failed"
    try:
        for panel in panels:
            if "bl_rna" in panel.__dict__:
                bpy.utils.unregister_class(panel)

        for panel in panels:
            panel.bl_category = context.preferences.addons[__name__].preferences.category
            bpy.utils.register_class(panel)

    except Exception as e:
        print("\n[{}]\n{}\n\nError:\n{}".format(__name__, message, e))
        pass
    
@addon_updater_ops.make_annotations
class AnimLayersAddonPreferences(bpy.types.AddonPreferences):
    # this must match the addon name, use '__package__'
    # when defining this in a submodule of a python package.
    bl_idname = __package__
    
    auto_rename: bpy.props.BoolProperty(name="Sync Layer/Action Names", description="Rename layer to match to selected action", default = True)
    blend_type : bpy.props.EnumProperty(name = 'Default Blend Type', description="Default Blend Type when adding layers",  default = 'COMBINE',
        items = [('COMBINE', 'Combine', 'Use Combine as the default blend type'), ('ADD', 'Add', 'Use Add as the default blend type'), ('REPLACE', 'Replace', 'Use Replace as the default blend type'), 
            ('SUBTRACT', 'Subtract', 'Use as an Subtract Layer')])
    
    frame_range_settings : bpy.props.EnumProperty(name = 'Frame Range Settings', description="Use Either Anim Layers Custom Frame Range Settings or NLA Settings",  default = 'ANIMLAYERS',
        items = [('ANIMLAYERS', 'Anim Layers Settings', 'Use Anim Layers properties to adjust custom frame range'), 
                 ('NLA', 'NLA Settings', 'Use the nla properties to adjust custom frame range')])
    
    lock_nlatracks: bpy.props.BoolProperty(name="Automatically lock the NLA tracks", description="Automatically lock nla tracks when creating layers for safety", default = True)
    auto_custom_range: bpy.props.BoolProperty(name="Switch automatically to custom frame range when editing NLA Strips", description="Automatically use custom frame range when adjusting NLA Strips manually", default = False)

    #Property for ClearActiveAction
    proceed: bpy.props.EnumProperty(name="Choose how to proceed", description="Select an option how to proceed with Anim Layers", override = {'LIBRARY_OVERRIDABLE'},
        items = [
            ('REMOVE_LAYERS', 'Remove old layers and continue with the current action', 'Remove previous Layers and continue with current action in the base layer', 0),
            ( 'REMOVE_ACTION', 'Remove current action and reload older Layers', 'Remove current action and continue with the previous layers', 1),
            ('ADD_ACTION', 'Add the current action as a new Layer', 'Keep previous Anim Layers and Add the active action as a new layer', 2),])
    
    enabled_editors: bpy.props.EnumProperty(
        name="Enabled Editors",
        description="Select which editors should show animation layers panels",
        items=[('VIEW_3D', "3D View", ""), ('GRAPH_EDITOR', "Graph Editor", ""), ('DOPESHEET_EDITOR', "Dope Sheet", ""),('NLA_EDITOR', "NLA Editor", ""),],
        options={'ENUM_FLAG'},
        default={'VIEW_3D', 'NLA_EDITOR', 'DOPESHEET_EDITOR', 'GRAPH_EDITOR'},
        update=update_panel
    )

    category: bpy.props.StringProperty(
        name="Tab Category",
        description="Choose a name for the category of the panel",
        default="Animation",
        update=update_panel
    )
    
    # addon updater preferences from `__init__`, be sure to copy all of them
    auto_check_update: bpy.props.BoolProperty(
        name = "Auto-check for Update",
        description = "If enabled, auto-check for updates using an interval",
        default = True,
    )

    updater_interval_months: bpy.props.IntProperty(
        name='Months',
        description = "Number of months between checking for updates",
        default=0,
        min=0
    )
    updater_interval_days: bpy.props.IntProperty(
        name='Days',
        description = "Number of days between checking for updates",
        default=7,
        min=0,
    
    )
    updater_interval_hours: bpy.props.IntProperty(
        name='Hours',
        description = "Number of hours between checking for updates",
        default=0,
        min=0,
        max=23
    )
    updater_interval_minutes: bpy.props.IntProperty(
        name='Minutes',
        description = "Number of minutes between checking for updates",
        default=0,
        min=0,
        max=59
    )

    def draw(self, context):
        layout = self.layout
        addon_updater_ops.update_settings_ui(self, context)
        
        col = layout.column()

        col.label(text="Tab Category:")
        col.prop(self, "category", text="")
        row = layout.row()
        row.prop(self, "enabled_editors")
        col = layout.column()
        col.separator(factor = 1)
        col = col.box()
        col.label(text="Defaults:")
        split = col.split(factor=0.5, align = True)
        split.prop(self, "auto_rename")
        
        split.label(text="Default Blend Type: ")
        split.prop(self,'blend_type', text = '')
        
        row = col.row()
        row.label(text = "Custom Frame Range Settings")
        row.prop(self, "frame_range_settings", text = '')

        row = col.row()
        row.prop(self, "auto_custom_range")
        row.prop(self, "lock_nlatracks")

classes = (AnimLayersSettings, AnimLayersSceneSettings, AnimLayersItems, AnimLayersObjects)

addon_keymaps = []
    
def register():
    addon_updater_ops.register(bl_info)
    register_class(AnimLayersAddonPreferences)
    addon_updater_ops.make_annotations(AnimLayersAddonPreferences) # to avoid blender 2.8 warnings
    if bpy.app.version < (3, 2, 0):
        return
        
    bake_ops.register()
    anim_layers.register()
    multikey.register()
    
    for cls in classes:
        register_class(cls)
    
    bpy.types.Object.als = bpy.props.PointerProperty(type = AnimLayersSettings, options={'LIBRARY_EDITABLE'}, override = {'LIBRARY_OVERRIDABLE'})
    bpy.types.Scene.als = bpy.props.PointerProperty(type = AnimLayersSceneSettings, options={'LIBRARY_EDITABLE'}, override = {'LIBRARY_OVERRIDABLE'})
    bpy.types.Object.Anim_Layers = bpy.props.CollectionProperty(type = AnimLayersItems, override = {'LIBRARY_OVERRIDABLE', 'USE_INSERTION'})
    bpy.types.Scene.AL_objects = bpy.props.CollectionProperty(type = AnimLayersObjects, options={'LIBRARY_EDITABLE'}, override = {'LIBRARY_OVERRIDABLE', 'USE_INSERTION'})
    
    update_panel(None, bpy.context)
    #update_tweak_keymap()
    
    #Make sure TAB hotkey in the NLA goes into full stack mode
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    km = kc.keymaps.new(name= 'NLA Generic', space_type= 'NLA_EDITOR')
    if 'nla.tweakmode_enter' not in km.keymap_items:
        kmi = km.keymap_items.new('nla.tweakmode_enter', type= 'TAB', value= 'PRESS')
        kmi.properties.use_upper_stack_evaluation = True
        addon_keymaps.append((km, kmi))
        
    
def unregister():
    addon_updater_ops.unregister()
    unregister_class(AnimLayersAddonPreferences)
    if bpy.app.version < (3, 2, 0):
        return
    
    for cls in classes:
        unregister_class(cls)
    bake_ops.unregister()
    anim_layers.unregister()
    multikey.unregister()
    del bpy.types.Object.als
    del bpy.types.Object.Anim_Layers
    del bpy.types.Scene.AL_objects
    #removing keymaps
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()

if __name__ == "__main__":
    register()