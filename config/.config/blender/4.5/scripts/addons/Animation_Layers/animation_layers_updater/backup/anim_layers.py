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
            #Make sure layer index is not more then the layers
            if obj.als.layer_index > len(obj.Anim_Layers) - 1:
                obj.als['layer_index'] = len(obj.Anim_Layers) - 1
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
        if len(obj.Anim_Layers) > len(anim_data.nla_tracks):
            obj.Anim_Layers.clear()
        
        if not len(obj.Anim_Layers) and len(anim_data.nla_tracks):
            start = True
            #Turn off until it gets a confirmation how to proceed
            self['turn_on'] = False
            bpy.ops.anim.clear_nla_warning('INVOKE_DEFAULT')
            continue
        if len(obj.Anim_Layers) and not action_search(anim_data.action, anim_data.nla_tracks) and anim_data.action:
            start = True
            self['turn_on'] = False
            bpy.ops.anim.clear_active_action_warning('INVOKE_DEFAULT')
            continue
        if check_override_tracks(obj, anim_data) or check_override_layers(obj):
            bpy.ops.message.layersoverride('INVOKE_DEFAULT')
            # continue
    
    if len(obj.Anim_Layers) and obj.als.layer_index > len(obj.Anim_Layers) - 1:
        obj.als['layer_index'] = len(obj.Anim_Layers) - 1
        
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
        if len(anim_data.nla_tracks) == len(obj.Anim_Layers):
            continue
        subtract = anim_data.nla_tracks[-1]
        if len(subtract.strips) != 1:
            continue
         
        if subtract.strips[0].blend_type == 'SUBTRACT' and len(anim_data.nla_tracks) > len(obj.Anim_Layers):
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
    
    register_layers(obj, nla_tracks)
    
    frame_start, frame_end = subscriptions.get_frame_range(scene)
    #synchronize the temporary influence prorpery
    for i, layer in enumerate(obj.Anim_Layers):
        if len(nla_tracks[i].strips) != 1:
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
    for i, track in enumerate(nla_tracks):
            
        if len(track.strips) != 1 or track.strips[0].type == 'META' and len(obj.Anim_Layers) > i+1:
            obj.Anim_Layers[i].lock = True
            continue
        strip = track.strips[0]
        use_animated_influence(strip)
              
#updating the ui list with the nla track names
def visible_layers(obj, nla_tracks):
    '''Creates a list of all the tracks without the top subtrack for the UI List'''
    
    def restore_influence(layer, strip):
        '''restore influence value if it has no keyframes and using the temprorary property'''
        if strip.fcurves:
            if len(strip.fcurves[0].keyframe_points):
                return
        layer['influence'] = strip.influence

    lock = []
    frame_range = []

    #store all the layer properties
    for layer in obj.Anim_Layers:
        lock.append(layer.lock)
        frame_range.append((layer.frame_start, layer.frame_end, layer.speed, layer.repeat, layer.offset, layer.custom_frame_range))
    
    #check if a layer was removed and adjust the stored properties
    if len(nla_tracks) < len(obj.Anim_Layers):
        removed = 0
        for i, layer in enumerate(obj.Anim_Layers):
            if layer.name not in nla_tracks:
                lock.pop(i - removed)
                frame_range.pop(i - removed)
                removed += 1
               
    #check if a layer was added and adjust the stored properties
    if len(nla_tracks) > len(obj.Anim_Layers):
        obj.Anim_Layers.update()
        for i, track in enumerate(nla_tracks):
            if track.name not in obj.Anim_Layers: 
                lock.insert(i, False)
                frame_range.insert(i, (0, 0, 1, 1, 0, False))         
    #write layers             
    obj.Anim_Layers.clear()
    #check if there are still layers because of overrides
    length = len(obj.Anim_Layers)
    
    for i, track in enumerate(nla_tracks):
        if length > i:
            continue
        layer = obj.Anim_Layers.add()
        layer['name'] = track.name
        layer['mute'] = track.mute

        if len(track.strips):
            strip = track.strips[0]
            strip.name = track.name
            if strip.action != None:
                layer['action'] = strip.action
            restore_influence(layer, strip)

        if not lock: #check if the list is appended and lock list is not empty
            continue
        layer['lock'] = lock[i]
        layer['frame_start'] = frame_range[i][0]
        layer['frame_end'] = frame_range[i][1]
        layer['speed'] = frame_range[i][2]
        layer['repeat'] = frame_range[i][3]
        layer['offset'] = frame_range[i][4]
        layer['custom_frame_range'] = frame_range[i][5]


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
    for i, layer in enumerate(obj.Anim_Layers):
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
                pairs = multilayer_offset_evaluation(pairs, anim_data.nla_tracks[self.obj.als.layer_index].strips[0])

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
    if len(anim_data.nla_tracks) != len(obj.Anim_Layers) or anim_data.action is None:
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
        frames = multilayer_reverse_offset(frames, nla_tracks[obj.als.layer_index].strips[0])

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

    for i, layer in enumerate(obj.Anim_Layers): #look for the Anim Layers fcurve
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
    '''select the new action clip when there is a new selection in the ui list and make all the updates for this Layer'''
    obj = self.id_data
    if obj is None:# or context.object is None:
        return
    if not self.turn_on:
        return
    if not len(obj.Anim_Layers):
        return
    anim_data = anim_data_type(obj)
    for track in anim_data.nla_tracks:
        track.select = False
        if len(track.strips):
            track.strips[0].select = False
    nla_track = anim_data.nla_tracks[self.layer_index]
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
        
    if obj.Anim_Layers[self.layer_index].lock:
        #anim_data.use_tweak_mode = False
        tweak_mode_upper_stack(context, obj, anim_data, enter = False)
        return
            
    if not obj.als.upper_stack:
        subscriptions.subscriptions_remove()
        tweak_mode_upper_stack(context, obj, anim_data)
        subscriptions.subscriptions_add(context.scene)
    else:
        anim_data.use_tweak_mode = False
        if not obj.Anim_Layers[self.layer_index].lock:
            anim_data.use_tweak_mode = True
            
    if obj.als.view_all_keyframes:
        obj.als.view_all_keyframes = True

    

def layer_mute(self, context):
    obj = self.id_data
    index = list(obj.Anim_Layers).index(self)
    anim_data = anim_data_type(obj)
    anim_data.nla_tracks[index].mute = self.mute
    
    #Exclude muted layers from view all keyframes
    if obj.als.view_all_keyframes:
        obj.als.view_all_keyframes = True
    
def layer_solo(self, context):
 
    obj = context.object
    anim_data = anim_data_type(obj)
    #added a skip boolean so that when layer.solo = False it doesnt iterate through all the layers because of the call, since only one layer can be solo
    global skip
    try:
        if skip:
            return
    except NameError:
        skip = False

    if self.solo:
        for i, layer in enumerate(obj.Anim_Layers):
            if layer != self:
                skip = True
                layer.solo = False
                anim_data.nla_tracks[i].mute = True
            else:
                anim_data.nla_tracks[i].mute = False
        skip = False
    else:
        #when turned off restore track mute from the layers mute property
        for i, track in enumerate(anim_data.nla_tracks):
            track.mute = obj.Anim_Layers[i].mute
   
def layer_lock(self, context):
    obj = self.id_data
    index = list(obj.Anim_Layers).index(self)
    anim_data = anim_data_type(obj)
    nla_tracks = anim_data.nla_tracks
    
    if not self.lock:
        if len(nla_tracks[index].strips) != 1 or nla_tracks[index].strips[0].type == 'META':
            self.lock = True
    #Get out of tweak mode
    if index == obj.als.layer_index:
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
    
    #if layer name exists then add a unique name
    obj = self.id_data
    if context.object is None:
        return
    layer_names = [layer.name for layer in context.object.Anim_Layers if layer != self]
    if self.name in layer_names:
        self.name = unique_name(layer_names, self.name)
    anim_data = anim_data_type(obj)
    if not hasattr(anim_data, 'nla_tracks'):
        return
    nla_tracks = anim_data.nla_tracks
    override_tracks = check_override_tracks(obj, anim_data)
    index = list(obj.Anim_Layers).index(self)
    track = nla_tracks[index]
    if not len(track.strips):
        return
    strip = track.strips[0]
    if self.name != track.name:
        #synchronize override_tracks
        if track.name in override_tracks:
            override_tracks[track.name].name = self.name
        track.name = self.name
        if len(track.strips) == 1:
            strip.name = self.name
    if strip.action is None:
        return
    if obj.als.auto_rename and strip.action.name != self.name:
        strip.action.name = self.name

def influence_get_strips(context):
    '''Get all the strips with the current layer as the first strip for influence operations'''
    obj = context.object
    anim_data = anim_data_type(obj)
    scene = context.scene
    current_strip = anim_data.nla_tracks[obj.als.layer_index].strips[0]
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
    strip = anim_data.nla_tracks[obj.als.layer_index].strips[0]
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
    anim_data.nla_tracks[obj.als.layer_index].name = name
    anim_data.nla_tracks[obj.als.layer_index].strips[0].name = name
    
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
    strip = anim_data.nla_tracks[obj.als.layer_index].strips[0]
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
    
    if len(anim_data.nla_tracks) != len(obj.Anim_Layers):
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
    strips = obj.animation_data.nla_tracks[obj.als.layer_index].strips
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
                index = len(obj.Anim_Layers) - 1
                obj.als.layer_index = index
                #add_animlayer(layer_name = action.name , duplicate = False, index = 1, blend_type = 'COMBINE')
                add_animlayer(unique_name(obj.Anim_Layers, action.name), index = index, blend_type = 'REPLACE')
                obj.als.layer_index += 1
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
        
def add_animlayer(layer_name = 'Anim_Layer' , duplicate = False, index = 1, blend_type = 'COMBINE'):
    '''Add an animation layer'''
    obj = bpy.context.object
    check_overrides_ALobjects(obj)
    anim_data = anim_data_type(obj)
    action = anim_data.action
    nla_tracks = anim_data.nla_tracks
    if obj.als.layer_index < 0 : obj.als['layer_index'] = 0
    previous = None if index == 0 else nla_tracks[obj.als.layer_index]
    
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
        copy_strip_settings(new_strip, anim_data.nla_tracks[obj.als.layer_index].strips[0])
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
        else:
            add_animlayer(unique_name(obj.Anim_Layers, layer_name), blend_type = blend_type)
            index = obj.als.layer_index + 1
        #register_layers(obj, nla_tracks)
        override_tracks = check_override_tracks(obj, anim_data)
        if override_tracks:
            bpy.ops.message.layersoverride('INVOKE_DEFAULT')
        #if override tracks exist then make sure selection is on top of them
        while anim_data.nla_tracks[index].name in override_tracks:
            index += 1
        obj.als.layer_index = index

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
    
    def execute(self, context):    
        if subscriptions.check_handler in bpy.app.handlers.depsgraph_update_pre:
            bpy.app.handlers.depsgraph_update_pre.remove(subscriptions.check_handler)
            
        obj = context.object
        anim_data = anim_data_type(obj)
        nla_tracks = anim_data.nla_tracks    
        i = obj.als.layer_index
        
        strip = nla_tracks[i].strips[0]
        blend = strip.blend_type
        track_name = nla_tracks[i].name
        
        
        name = unique_name(obj.Anim_Layers, track_name)
        new_track = add_animlayer(layer_name = name, duplicate = True, blend_type = blend)
        new_strip = new_track.strips[0]
        action = new_strip.action
        if obj.als.linked == False:
            tweak_mode_upper_stack(context, obj, anim_data, enter = False)
            new_strip.action = action.copy()  
                
        
        register_layers(obj, nla_tracks)
        
        obj.als.layer_index += 1
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
        

        blend = nla_tracks[obj.als.layer_index].strips[0].blend_type
        track_name = nla_tracks[obj.als.layer_index].name
        
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
        
        obj.als.layer_index += 1  
        
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
   
        blend = nla_tracks[obj.als.layer_index].strips[0].blend_type
        track_name = nla_tracks[obj.als.layer_index].name
        
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
        
        obj.als.layer_index += 1   
        
        tweak_mode_upper_stack(context, obj, anim_data)
        if subscriptions.check_handler not in bpy.app.handlers.depsgraph_update_pre:
            bpy.app.handlers.depsgraph_update_pre.append(subscriptions.check_handler)  
        
        return {'FINISHED'}

class RemoveAnimLayer(bpy.types.Operator):
    """Remove animation layer"""
    bl_idname = "anim.remove_anim_layer"
    bl_label = "Remove Animation Layer"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        anim_data = anim_data_type(context.object) if context.object else None
        if hasattr(anim_data, 'nla_tracks'):
            return len(anim_data.nla_tracks)
    
    def execute(self, context):

        obj = context.object
        anim_data = anim_data_type(obj)
        nla_tracks = anim_data.nla_tracks
        track = nla_tracks[obj.als.layer_index]
        override_tracks = check_override_tracks(obj, anim_data)
        if track.name in override_tracks:
            return {'CANCELLED'}
        try:
            obj.Anim_Layers.remove(obj.als.layer_index)
        except TypeError: #library overrides currently can not remove items
            return {'CANCELLED'}
        
        #Since Blender 4.4 Blender is crashing when removing strips while in tweak mode
        anim_data.use_tweak_mode = False

        if len(nla_tracks) == 1:
            tweak_mode_upper_stack(context, obj, anim_data, enter = False)

        nla_tracks.remove(track) 
        #update the ui list item's index
        if obj.als.layer_index != 0:
            obj.als.layer_index -= 1
        else:
            obj.als.layer_index = 0

        return {'FINISHED'}  

def share_layerkeys_items(self, context):
    '''create the layer items for the share keys excluding the current layer'''    
    obj = self.id_data
    return [(layer.name, layer.name, layer.name) for layer in obj.Anim_Layers if layer != obj.Anim_Layers[obj.als.layer_index]]

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
        current_strip = anim_data.nla_tracks[obj.als.layer_index].strips[0]
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
        
def move_layer(dir, context):
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
    
    #select only the current track strip
    obj = context.object
    anim_data = anim_data_type(obj)
    anim_data.nla_tracks[obj.als.layer_index].select = True 
    with context.temp_override(window=window, area=area, region=region):
        bpy.ops.anim.channels_expand()
        
        bpy.ops.anim.channels_move(direction=dir)
    
    #restoring the old area 
    screen.areas[0].type = old_area
    screen.areas[0].ui_type = ui_type

    visible_layers(obj, anim_data.nla_tracks)
    
class MoveAnimLayerUp(bpy.types.Operator):
    """Move the selected layer up. Hold shift to move to the top"""
    bl_idname = "anim.layer_move_up"
    bl_label = "Move the selected Animation layer up."
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        obj = context.object
        anim_data = anim_data_type(obj) if obj else None
        if hasattr(anim_data, 'nla_tracks'):
            return len(anim_data.nla_tracks) > 1
        
    def invoke(self, context, event):
        self.shift = event.shift
        return self.execute(context)
    
    def execute(self, context):
        
        obj = context.object
        index = obj.als.layer_index
        if index >= len(obj.animation_data.nla_tracks)-1:
            return {'CANCELLED'}
        
        subscriptions.subscriptions_remove()
        if self.shift:
            top_layer = len(obj.animation_data.nla_tracks)-1
            obj.Anim_Layers.move(index, top_layer)    
            move_layer('TOP', context)
            obj.als.layer_index = top_layer
        else:
            obj.Anim_Layers.move(index, index + 1)    
            move_layer('UP', context)
            obj.als.layer_index += 1
        subscriptions.subscriptions_add(context.scene)
                 
        return {'FINISHED'}
    
class MoveAnimLayerDown(bpy.types.Operator):
    """Move the selected layer down. Hold shift to move to the bottom"""
    bl_idname = "anim.layer_move_down"
    bl_label = "Move the selected animation layer down"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        anim_data = anim_data_type(context.object) if context.object else None
        if hasattr(anim_data, 'nla_tracks'):
            return len(anim_data.nla_tracks) > 1
    
    def invoke(self, context, event):
        self.shift = event.shift
        return self.execute(context)
    
    def execute(self, context):
        
        obj = context.object
        index = obj.als.layer_index
        if index == 0:
            return {'CANCELLED'}
        
        subscriptions.subscriptions_remove()
        if self.shift:
            bottom_layer = 0
            obj.Anim_Layers.move(index, bottom_layer)    
            move_layer('BOTTOM', context)
            obj.als.layer_index = bottom_layer
        else:
            obj.Anim_Layers.move(index, index -1)
            move_layer('DOWN', context)
            obj.als.layer_index -= 1
        subscriptions.subscriptions_add(context.scene)
        
        return {'FINISHED'}

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
        strip = anim_data.nla_tracks[obj.als.layer_index].strips[0]
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
        if anim_data.nla_tracks[obj.als.layer_index].strips[0].action is None:
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
        anim_data.nla_tracks[obj.als.layer_index].strips[0].action = None
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
    strip = anim_data.nla_tracks[obj.als.layer_index].strips[0]
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
        return hasattr(anim_data,"nla_tracks") and len(anim_data.nla_tracks[context.object.als.layer_index].strips)
    
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
        return hasattr(anim_data,"nla_tracks") and len(anim_data.nla_tracks[context.object.als.layer_index].strips)
    
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
        return hasattr(anim_data,"nla_tracks") and len(anim_data.nla_tracks[context.object.als.layer_index].strips)
    
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
        if not len(anim_data.nla_tracks[context.object.als.layer_index].strips):
            return False
        return not anim_data.nla_tracks[context.object.als.layer_index].strips[0].use_sync_length
        
    def execute(self, context):
        sync_frame_range(context)
        return {'FINISHED'}  
  
class LAYERS_UL_list(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, reversed):
        obj = bpy.context.object
        anim_data = anim_data_type(obj)
        nla_tracks = anim_data.nla_tracks
        self.use_filter_sort_reverse = True
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align = True)
            icon = 'SOLO_ON' if nla_tracks[index].is_solo else 'SOLO_OFF'
            row.prop(nla_tracks[index], 'is_solo', text = '', invert_checkbox=False, icon = icon, emboss=False)
            row.prop(item, "name", text="", emboss=False)
            
            split = row.split(factor=0, align = True)
            sub_row_right = row.row(align=True)
            sub_row_right.alignment = 'RIGHT'
            if len(nla_tracks[index].strips):
                blend_type = nla_tracks[index].strips[0].blend_type
                sub_row_right.label(text = blend_type[0] + '  ')
                # sub_row_right.prop_menu_enum(nla_tracks[index].strips[0], 'blend_type', text = blend_type[0])
                
            icon = 'HIDE_ON' if nla_tracks[index].mute else 'HIDE_OFF'
            sub_row_right.prop(nla_tracks[index], 'mute', text = '', invert_checkbox=False, icon = icon, emboss=False)
            
            icon = 'LOCKED' if item.lock else 'UNLOCKED'
            sub_row_right.prop(item,'lock', text = '', invert_checkbox=False, icon = icon, emboss=False)
            # split = row.split(factor=0, align = True)
            
        elif self.layout_type in {'GRID'}:
            pass
        
    # def invoke(self, context, event):
    #     pass 

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

            track = anim_data.nla_tracks[obj.als.layer_index]
                
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
            track = nla_tracks[obj.als.layer_index]
            strip = track.strips[0] if len(track.strips) else anim_data
            layer = obj.Anim_Layers[obj.als.layer_index]
            layout = self.layout 
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
    AddSlot, RemoveSlot, EditAllLayersOperator, InfluencePrevKeyframe, InfluenceNextKeyframe)

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

       