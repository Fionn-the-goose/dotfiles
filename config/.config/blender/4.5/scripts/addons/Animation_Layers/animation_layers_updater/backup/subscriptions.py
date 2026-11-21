import bpy

from . import anim_layers
from . import bake_ops
import numpy as np
import time
import inspect

def subscriptions_remove(handler = True):
    #clear all handlers and subsciptions
    # if scene is None : scene = bpy.context.scene

    global subscriptions_owner
    if 'subscriptions_owner' in globals():
        bpy.msgbus.clear_by_owner(subscriptions_owner)
        del subscriptions_owner

    global influence_keys, selected_bones

    if 'influence_keys' in globals():
        del influence_keys
    if 'selected_bones' in globals():
        del selected_bones

    if not handler:
        return
    if check_handler in bpy.app.handlers.depsgraph_update_pre:
        bpy.app.handlers.depsgraph_update_pre.remove(check_handler)
    if animlayers_frame in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(animlayers_frame)

def subscriptions_add(scene, handler = True):
    global func_running

    func_running = False
    global subscriptions_owner
    if 'subscriptions_owner' in globals():
        bpy.msgbus.clear_by_owner(subscriptions_owner)

    subscriptions_owner = object()

    #Checking if frame range preview was turned on when pressing P

    subscribe_to_preview_frame_end(scene)
    subscribe_to_track_name(subscriptions_owner)
    subscribe_to_action_name(subscriptions_owner)
    subscribe_to_strip_settings(subscriptions_owner)
    subscribe_to_influence(subscriptions_owner)
    if bpy.app.version >= (4, 4, 0):
        subscribe_to_action_slot(scene)

    if not handler:
        return

    if check_handler not in bpy.app.handlers.depsgraph_update_pre:
        bpy.app.handlers.depsgraph_update_pre.append(check_handler)    
    if animlayers_frame not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(animlayers_frame)

def animlayers_frame(scene, context):

    current = scene.frame_current_final
    check_scene()
    #During Particles bake screen attribute is empty
    if bpy.context.screen is None:
        return
    #Make sure the animation is playing and not just running a motion path
    if not bpy.context.screen.is_animation_playing:
        return
    #Checking if preview range was turned on or off, when using hotkey P it doesn't recognize
    #only during the frame handler
    if scene.get('framerange_preview') != scene.use_preview_range:
        scene['framerange_preview'] = scene.use_preview_range
        frameend_update_callback()
        return
    frame_start, frame_end = bake_ops.frame_start_end(scene)
    # frame_start, frame_end = get_frame_range(scene)
    reset_subscription = False
    if 'outofrange' not in globals():
        global outofrange
        outofrange = False if 0 <= current < frame_end else True
    
    if 0 <= current < frame_end:
        if outofrange:
            frameend_update_callback()
        outofrange = False
        return
    
    outofrange = True

    #In case of running into empty objects then clean AL_objects
    clean_AL_objects = False
    #iterate only through objects with anim layers turned on
    objects = [obj.object for obj in scene.AL_objects]
    for obj in objects:
        if obj is None:
            clean_AL_objects = True
            continue
        anim_data = anim_layers.anim_data_type(obj) 
        if anim_data is None:
            return
        nla_tracks = anim_data.nla_tracks
        if not len(nla_tracks):
            return
        for i, track in enumerate(nla_tracks):
            if len(track.strips) != 1:
                continue
            #checks if the layer has a custom frame range
            layer = obj.Anim_Layers[i]
            if layer.custom_frame_range:
                continue
            if not reset_subscription:
                subscriptions_remove(handler = False)
                reset_subscription = True
            strip = track.strips[0]
            
            if current < 0:
                # anim_layers.strip_action_recalc(layer, track.strips[0])
                strip.frame_start = current
                # track.strips[0].action_frame_start = current * 1/layer.speed - layer.offset * 1/layer.speed
                anim_layers.update_action_frame_range(current, frame_end, layer, strip)
                strip.frame_end = frame_end + 10.0

            elif current >= frame_end:
                if strip.frame_start < 0:
                    strip.frame_start = 0
                    anim_layers.update_action_frame_range(0, frame_end, layer, strip)
                    return
                anim_layers.update_action_frame_range(strip.frame_start, current + 10.0, layer, strip)
                strip.frame_end = current + 10.0

    if clean_AL_objects:
        anim_layers.clean_AL_objects(scene)
        
    if reset_subscription:
        subscriptions_add(scene, handler = False)
   
def check_handler(scene):
    '''A main function that performs a series of checks using a handler'''
    # scene = bpy.context.scene
    #Timer for handler
    # if 'last_check_time' not in globals():
    #     global last_check_time
    #     last_check_time = 0
    # current_time = time.time()
    # if current_time - last_check_time < 0.01:
    #     return
    # last_check_time = current_time

    #if there are no objects included in animation layers then return
    if not len(scene.AL_objects):
        return
    obj = bpy.context.object
    #if the object was removed from the scene, then remove it from anim layers object list
    if obj is None:
        anim_layers.clean_AL_objects(scene)
        return
    if not obj.als.turn_on:
        return
    anim_data = anim_layers.anim_data_type(obj)
    if anim_data is None:
        return
    if not anim_data.use_nla:
        obj.als.turn_on = False
        return
    if not len(obj.Anim_Layers):
        return    
    if not hasattr(anim_data, 'nla_tracks') or not obj.als.turn_on: #obj.select_get() == False or
        return
    anim_layers.add_obj_to_animlayers(obj, [item.object for item in scene.AL_objects])
    nla_tracks = anim_data.nla_tracks
    layer = obj.Anim_Layers[obj.als.layer_index]
    active_action_update(obj, anim_data, nla_tracks)
    #check if a keyframe was removed
    if bpy.context.active_operator is not None:
            
        if bpy.context.active_operator.name == 'Enter Tweak Mode':
            if not bpy.context.active_operator.properties['use_upper_stack_evaluation']:
                obj.als.upper_stack = False
                
        if bpy.context.active_operator.name == 'Move Channels':
            anim_layers.visible_layers(obj, nla_tracks)

    # Making sure that scene.als.edit_all_layers_op is not somehow turned on
    if not any(item.object.als.edit_all_keyframes for item in scene.AL_objects if item.object is not None) and scene.als.edit_all_layers_op:
        scene.als.edit_all_layers_op = False

    # check if track and layers are synchronized
    if track_layer_synchronization(obj, nla_tracks):
        return

    track = nla_tracks[obj.als.layer_index]

    sync_frame_range(scene, track, layer)
    # sync_strip_range(scene)
    always_sync_range(track, layer)
    
    if anim_data.use_tweak_mode and layer.lock:
        layer['lock'] = False
    elif not anim_data.use_tweak_mode and not layer.lock:
        layer['lock'] = True

    influence_sync(scene, obj, nla_tracks)
          
    # continue if locked
    if layer.lock:
        return
    
    #In case a keyframe was added and a new action slot was added to anim_data
    #Check that it's synchornized with the strip action slot
    strip = track.strips[0]
    if hasattr(strip, 'action_slot') and strip.action:
        if strip.action_slot != anim_data.action_slot:
            strip.action_slot = anim_data.action_slot

    if obj.als.view_all_keyframes:
        anim_layers.hide_view_all_keyframes(obj, anim_data)
        check_selected_bones(obj)
        
    influence_check(nla_tracks[obj.als.layer_index])

def track_layer_synchronization(obj, nla_tracks):
    '''check if track and layers are synchronized, running only when adding/removing tracks via the nla'''

    if len(nla_tracks) == len(obj.Anim_Layers):
        return False
    
    new_layers_names = set(track.name for track in nla_tracks).difference(set(layer.name for layer in obj.Anim_Layers))
    anim_layers.visible_layers(obj, nla_tracks)
    if obj.als.layer_index > len(obj.Anim_Layers)-1:
        obj.als.layer_index = len(obj.Anim_Layers)-1
    
    if not bpy.context.preferences.addons[__package__].preferences.auto_custom_range:
        return
    
    #update new layer with strip settings
    frame_start, frame_end = get_frame_range(bpy.context.scene)

    for layer_name in new_layers_names:
        if len(nla_tracks[layer_name].strips) != 1:
            continue
        strip = get_strip_in_meta(nla_tracks[layer_name].strips[0])
        layer = obj.Anim_Layers[layer_name]
        if not layer.custom_frame_range:
            continue
        if (strip.frame_start, strip.frame_end) != (frame_start, frame_end):
            subscriptions_remove()
            bpy.ops.anim.custom_frame_range_warning('INVOKE_DEFAULT')
            return
        
        update_strip_layer_settings(strip, layer)
        layer['action'] = strip.action
    return True

def active_action_update(obj, anim_data, nla_tracks):
    '''updating the active action into the selected layer'''
    if obj.Anim_Layers[obj.als.layer_index].lock:
        if anim_data.action != None:
            subscriptions_remove()
            anim_data.use_tweak_mode = False
            anim_data.action = None
            subscriptions_add(bpy.context.scene)
        return
    if anim_data.action == nla_tracks[obj.als.layer_index].strips[0].action:
        return
    if not len(nla_tracks[obj.als.layer_index].strips):
        return
    if not anim_data.action or anim_data.is_property_readonly('action'):
        return
    subscriptions_remove()
    action = anim_data.action
    anim_data.action = None
    obj.Anim_Layers[obj.als.layer_index].action = action
    subscriptions_add(bpy.context.scene)

def get_strip_in_meta(strip):
    '''check if it's meta strip then access the last strip inside meta hierarchy'''  
    while len(strip.strips):
        strip = strip.strips[0]
    return strip

def sync_frame_range(scene, track, layer):
    '''Nla strips are not updating with msgbus when changing frame range in the ui
    so it checks again during check handler if the frame range is changed and syncs it'''
    
    if bpy.context.screen.is_animation_playing:
        return
    # scene = bpy.context.scene
    if not len(track.strips):
        return

    strip = track.strips[0]

    #In case of Custom frame range
    if layer['custom_frame_range']:
        if (strip.frame_start, strip.frame_end) != (layer.frame_start, layer.frame_end):
            update_strip_layer_settings(strip, layer)

    else:
        #In case of None custom frame range, make the strips adjust to scene frame range
        frame_start, frame_end = get_frame_range(scene)
        
        #defining global frame range to check if it was changed in the handler,
        # msgbus subsciption is not updated before
        if 'frame_range' not in globals():
            global frame_range
            frame_range = (frame_start, frame_end)

        if frame_range != (frame_start, frame_end):
            frame_range = (frame_start, frame_end)
            frameend_update_callback()
            return

        #Turn on custom frame range if the current strip is not following the scene frame range
        # Should be activated when nla strips are edited manually in the nla editor, only when auto custom range is turned on, otherwise just update the strip frame range to the scene frame range
        if (round(strip.frame_start, 2), round(strip.frame_end, 2)) != (round(frame_start, 2), round(frame_end, 2)):
            if bpy.context.preferences.addons[__package__].preferences.auto_custom_range:
                subscriptions_remove()
                # print('321 custom frame range')
                bpy.ops.anim.custom_frame_range_warning('INVOKE_DEFAULT')
                return
            else:
                frameend_update_callback()
                return

def sync_strip_range(scene):
    '''Checking all the strips if a value was changed in the nla (not including UI changes)
    Similiar to sync custom frame range but iterating through all the layers
    Currently disabled'''

    frame_start, frame_end = get_frame_range(scene)
    if 'frame_range' not in globals():
        global frame_range
        frame_range = (frame_start, frame_end)

    clean_AL_objects = False
    objects = [obj.object for obj in scene.AL_objects]
    for obj in objects:
        if obj is None:
            #Turn on to clean AL_objects
            clean_AL_objects = True
            continue
        anim_data = anim_layers.anim_data_type(obj) 
        if anim_data is None:
            continue
        nla_tracks = anim_data.nla_tracks
        if not len(nla_tracks):
            continue
        for i, track in enumerate(nla_tracks):
            if len(track.strips) != 1:
                continue
            layer = obj.Anim_Layers[i]
            if layer['custom_frame_range']:
                if (strip.frame_start, strip.frame_end) != (layer.frame_start, layer.frame_end):
                    update_strip_layer_settings(strip, layer)
                continue

            strip = track.strips[0]
            strip_frame_start = strip.frame_start
            strip_frame_end = strip.frame_end
            
            if (strip_frame_start, round(strip_frame_end, 2)) != (frame_start, float(frame_end)):
                subscriptions_remove()
                # print('357 custom_frame_range_warning ')
                bpy.ops.anim.custom_frame_range_warning('INVOKE_DEFAULT')
                return

    if clean_AL_objects:
        anim_layers.clean_AL_objects(scene)

def always_sync_range(track, layer):
    '''sync frame range when always sync turned on'''
    if not len(track.strips):
        return
    if not layer.custom_frame_range:
        if track.strips[0].use_sync_length:
            track.strips[0].use_sync_length = False
        return
    if not track.strips[0].use_sync_length:
        if tuple(layer.action_range) != (0.0, 0.0): #reset action range when turned off
            layer.action_range = (0.0, 0.0)
        return   
    strip = track.strips[0]
    if tuple(layer.action_range) == tuple((strip.action.frame_range[0], strip.action.frame_range[1])):
        return
    anim_layers.sync_frame_range(bpy.context)
    layer.action_range = strip.action.frame_range
    
def influence_sync(scene, obj, nla_tracks):
    
    #Tracks that dont have keyframes are locked
    for i, track in enumerate(nla_tracks):
        if obj.Anim_Layers[i].lock:
            continue
        if not len(track.strips):
            continue
        strip = track.strips[0]
        if not len(strip.fcurves):
            if not strip.use_animated_influence:
                influence = strip.influence
                anim_layers.use_animated_influence(strip)
                strip.influence = influence
            continue

        if not len(strip.fcurves[0].keyframe_points):
            #apply the influence property to the temp property when keyframes are removed (but its still locked)
            if not strip.fcurves[0].lock:
                # obj.Anim_Layers[i]['influence'] = track.strips[0].influence
                scene.als['influence'] = strip.influence
            track.strips[0].fcurves[0].lock = True

    if scene.animation_data is None:
        return
    action = scene.animation_data.action
    if action is None:
        return
    
    # strip.use_animated_influence = True
    #if a keyframe was found in the temporary property then add it to the
    # data_path = 'Anim_Layers[' + str(obj.als.layer_index) + '].influence'
    data_path = 'als.influence'
    fcurves = anim_layers.get_fcurves(scene, action, data_type = 'SCENE')
    if not len(fcurves):
        return
    # fcurves = action.fcurves
    fcu_influence = fcurves.find(data_path)
    if fcu_influence is None:
        return
    if not len(fcu_influence.keyframe_points):
        return
    #remove the temporary influence
    fcurves.remove(fcu_influence)
    #if the action was created just for the influence because of empty object data type then remove the action
    if action.name == scene.name + 'Action' and not len(scene.animation_data.nla_tracks) and not len(fcurves):
        bpy.data.actions.remove(action)

    strip = nla_tracks[obj.als.layer_index].strips[0]
    if strip.fcurves[0].mute:
        return
    strip.fcurves[0].lock = False
    
    # if not strip.influence:
    #     strip.influence = 0.0001
    strip.keyframe_insert('influence')
    strip.fcurves[0].update()

def influence_check(selected_track):
    '''update influence when a keyframe was added without autokey'''
    #skip the next steps if a strip is missing or tracks were removed from the nla tracks
    if len(selected_track.strips) != 1:# or obj.als.layer_index > len(nla_tracks)-2:
        return
    strip = selected_track.strips[0]
    if not len(strip.fcurves):
        return

    global influence_keys
    if strip.fcurves[0].mute or not len(strip.fcurves[0].keyframe_points) or bpy.context.scene.tool_settings.use_keyframe_insert_auto:
        if 'influence_keys' in globals():
            del influence_keys
        return  #when the fcurve doesnt have keyframes, or when autokey is turned on, then return
    
    #update if the influence keyframes are changed. influence_keys are first added in influence_update_callback
    if 'influence_keys' not in globals():
        initialize_influence_keys(strip)
        return
    wm = bpy.context.window_manager
    if not len(wm.operators):
        return
    if "ANIM_OT_keyframe_insert" not in wm.operators[-1].bl_idname:
        return
    
    length = len(strip.fcurves[0].keyframe_points)*2
    keyframes = np.zeros(length)
    strip.fcurves[0].keyframe_points.foreach_get('co', keyframes)
    # Comparing only the values, because if it updates while duplicating  or moving frames than it's crashing
    if np.array_equal(influence_keys, keyframes):
        return
    
    selected_track.strips[0].fcurves[0].update()
    influence_keys = keyframes


def check_selected_bones(obj):
    '''running in the handler and checking if the selected bones were changed during view multiply layer keyframes'''
    if not obj.als.only_selected_bones:
        return
    global selected_bones
    try: 
        selected_bones
    except NameError:
        selected_bones = bpy.context.selected_pose_bones
        return
    else:
        if selected_bones != bpy.context.selected_pose_bones:
            selected_bones = bpy.context.selected_pose_bones
            obj.als.view_all_keyframes = True    

def check_scene():
    '''update strip frame end after scene change, this is part of the animlayers_frame handler'''
    if 'current_scene' not in globals():
        global current_scene
        current_scene = bpy.context.scene
        return
    if current_scene != bpy.context.scene:
        #remove old scene from subscriptions
        subscriptions_remove(handler = False)
        frameend_update_callback()
        current_scene = bpy.context.scene
        #Add the new scene to subscriptions
        subscriptions_add(current_scene, handler = False)

########################### MSGBUS SUBSCRIPTIONS #############################
    
#Callback function for Scene frame end

def get_frame_range(scene):
    '''Getting the frame range also when outside of scene frame range'''
    frame_start, frame_end = bake_ops.frame_start_end(scene)
    
    #if it's out of range add 10 frames to the current frame, else add 10 frames to the scene frame end
    frame_end = scene.frame_current_final + 10.0 if scene.frame_current_final >= frame_end else frame_end + 10.0

    frame_start = scene.frame_current_final if scene.frame_current_final < 0 else 0.0

    return frame_start, frame_end

def frameend_update_callback():
    '''End the strips at the end of the scene or scene preview'''
    scene = bpy.context.scene
    if not scene.AL_objects:
        return

    subscriptions_remove(handler = False)
    frame_start, frame_end = get_frame_range(scene)

    clean_AL_objects = False
    #Iterating through all the tracks
    for AL_item in scene.AL_objects:
        obj = AL_item.object
        if obj is None or obj not in scene.objects.values():
            clean_AL_objects = True
            continue
        #anim_data = anim_data_type(obj)
        anim_datas = anim_layers.anim_datas_append(obj)
        
        for anim_data in anim_datas:
            if anim_data is None:
                continue
            if len(anim_data.nla_tracks) != len(obj.Anim_Layers):
                continue
            for layer, track in zip(obj.Anim_Layers, anim_data.nla_tracks):
                if layer.custom_frame_range:
                    continue
                if len(track.strips) != 1:
                    continue
                strip = track.strips[0]
                strip.frame_start = frame_start
                anim_layers.update_action_frame_range(frame_start, frame_end, layer, strip)
                strip.scale = layer.speed
                strip.frame_end = frame_end

    if clean_AL_objects:
        anim_layers.clean_AL_objects(scene)
    subscriptions_add(scene, handler = False)
    
#Subscribe to the scene frame_end
def subscribe_to_preview_frame_end(scene):
    '''subscribe_to_preview_frame_end and frame preview end'''
    global subscriptions_owner
    
    # subscribe_end = scene.path_resolve("frame_end", False)
    # Subscribing to preview frame end since it's not registering in the depsgraph
    subscribe_preview_end = scene.path_resolve("frame_preview_end", False)
    subscribe_use_preview = scene.path_resolve("use_preview_range", False)
    for subscribe in [subscribe_preview_end, subscribe_use_preview]:
        
        bpy.msgbus.subscribe_rna(
        key=subscribe,
        owner=subscriptions_owner,
        args=(),
        notify=frameend_update_callback,)
        
        # bpy.msgbus.publish_rna(key=subscribe)
    
# def action_framestart_update_callback(*args):
#     ''' update the strip start with the action start'''
    
    
def track_update_callback():
    '''update layers with the tracks name'''
    # global initial_call
    # if initial_call:
    #     return
    if not bpy.context.selected_objects:
        return
    obj = bpy.context.object
    if obj is None:
        return
    if not obj.als.turn_on:
        return
    current_anim_data = anim_layers.anim_data_type(obj)
    anim_datas = anim_layers.anim_datas_append(obj)
    for anim_data in anim_datas:
        if anim_data is None:
            return
        nla_tracks = anim_data.nla_tracks
        if not len(nla_tracks):# or len(nla_tracks[:-1]) != len(obj.Anim_Layers):
            return
        override_tracks = anim_layers.check_override_tracks(obj, anim_data)
        for i, track in enumerate(nla_tracks):
            if anim_data != current_anim_data:
                continue
            #make sure there are no duplicated names
            if track.name != obj.Anim_Layers[i].name:
                #If its an override track, then make sure the reference object name is also synchronized
                if obj.Anim_Layers[i].name in override_tracks:
                    override_tracks[obj.Anim_Layers[i].name].name = track.name
                obj.Anim_Layers[i].name = track.name
                if len(track.strips) == 1:
                    track.strips[0].name = track.name
    
def subscribe_to_track_name(subscriptions_owner):
    '''Subscribe to the name of track'''
    
    #subscribe_track = nla_track.path_resolve("name", False)
    subscribe_track = (bpy.types.NlaTrack, 'name')
    
    bpy.msgbus.subscribe_rna(
        key=subscribe_track,
        # owner of msgbus subcribe (for clearing later)
        owner=subscriptions_owner,
        # Args passed to callback function (tuple)
        args=(),
        # Callback function for property update
        notify=track_update_callback,)
        
    # bpy.msgbus.publish_rna(key=subscribe_track)

def action_name_callback():
    '''update layers with the tracks name'''
    # global initial_call
    # if initial_call:
    #     return
    obj = bpy.context.object
    if obj is None:
        return
    if not obj.als.turn_on:
        return
    anim_data = anim_layers.anim_data_type(obj)
    #anim_datas = anim_layers.anim_datas_append(obj)
    if anim_data is None:
        return
    nla_tracks = anim_data.nla_tracks
    if not len(nla_tracks):
        return
    layer = obj.Anim_Layers[obj.als.layer_index]
    if not len(nla_tracks[obj.als.layer_index].strips):
        return
    action = nla_tracks[obj.als.layer_index].strips[0].action
    if action is None:
        return
    if not obj.als.auto_rename or layer.name == action.name:
        return
    layer.name = action.name
    
def subscribe_to_action_name(subscriptions_owner):
    '''Subscribe to the name of track'''
    
    #subscribe_track = nla_track.path_resolve("name", False)
    subscribe_action = (bpy.types.Action, 'name')
    bpy.msgbus.subscribe_rna(
        key=subscribe_action,
        # owner of msgbus subcribe (for clearing later)
        owner=subscriptions_owner,
        # Args passed to callback function (tuple)
        args=(),
        # Callback function for property update
        notify=action_name_callback,)
        
    # bpy.msgbus.publish_rna(key=subscribe_action)
    
def influence_update_callback():
    '''update influence'''
    # global initial_call
    
    if not bpy.context.selected_objects:
        return
    obj = bpy.context.object
    scene = bpy.context.scene
    #checking if the object has nla tracks, when I used undo it was still calling the property on an object with no nla tracks
    if obj is None:
        return
    if not obj.als.turn_on:
        return
    anim_data = anim_layers.anim_data_type(obj)
    if anim_data is None:
        return
    if not len(anim_data.nla_tracks):
        return
    i = obj.als.layer_index
    track = anim_data.nla_tracks[i]
    if len(track.strips) != 1:
        return
    strip = track.strips[0]
    scene.als['influence'] = strip.influence
    # obj.Anim_Layers[i]['influence'] = strip.influence

    if strip.fcurves[0].mute or strip.fcurves[0].lock:
        return
    if not len(track.strips[0].fcurves[0].keyframe_points):
        return
    
    # This is relevant only for autokey update
    if not bpy.context.scene.tool_settings.use_keyframe_insert_auto:
        return
    #if the influence property and fcurve value are not the same then store the keyframes to check in the handler for a change

    if len(track.strips[0].fcurves[0].keyframe_points):
        strip.keyframe_insert('influence')
        strip.fcurves[0].update()
        return
    
def initialize_influence_keys(strip):
    '''Setting up the influence keys'''
    global influence_keys

    length = len(strip.fcurves[0].keyframe_points)*2
    keyframes = np.zeros(length)
    strip.fcurves[0].keyframe_points.foreach_get('co', keyframes)
    influence_keys = keyframes


def subscribe_to_influence(subscriptions_owner):
    '''Subscribe to the influence of the track'''
    subscribe_influence = (bpy.types.NlaStrip, 'influence')
    bpy.msgbus.subscribe_rna(
        key=subscribe_influence,
        # owner of msgbus subcribe (for clearing later)
        owner=subscriptions_owner,
        # Args passed to callback function (tuple)
        args=(),
        # Callback function for property update
        notify=influence_update_callback,)
        

def subscribe_to_action_slot(subscriptions_owner):
    '''Subscribe to the influence of the track'''
    subscribe_slot = (bpy.types.NlaStrip, 'action_slot')
    
    bpy.msgbus.subscribe_rna(
        key=subscribe_slot,
        # owner of msgbus subcribe (for clearing later)
        owner=subscriptions_owner,
        # Args passed to callback function (tuple)
        args=(),
        # Callback function for property update
        notify=slot_update_callback,)

def slot_update_callback():
    '''Always updating action slot in the active action when updated in the strip'''

    if not bpy.context.selected_objects:
        return
    obj = bpy.context.object
    if obj is None:
        return
    anim_data = anim_layers.anim_data_type(obj)
    if anim_data is None:
        return
    if anim_data.action is None:
        return
    if not len(anim_data.nla_tracks):
        return
    if not len(obj.Anim_Layers):
        return

    if not len(anim_data.nla_tracks[obj.als.layer_index].strips):
        return

    strip = anim_data.nla_tracks[obj.als.layer_index].strips[0]
    anim_data.action_slot = strip.action_slot


def subscribe_to_strip_settings(subscriptions_owner):
    '''Subscribe to the strip settings of the track'''

    frame_start = (bpy.types.NlaStrip, 'frame_start')
    frame_end = (bpy.types.NlaStrip, 'frame_end')
    action_frame_start = (bpy.types.NlaStrip, 'action_frame_start')
    action_frame_end = (bpy.types.NlaStrip, 'action_frame_end')
    scale = (bpy.types.NlaStrip, 'scale')
    repeat = (bpy.types.NlaStrip, 'repeat')
    
    attributes = [frame_start, frame_end, action_frame_start, action_frame_end, scale, repeat, frame_start, frame_end]

    if bpy.app.version >= (3, 3, 0):
        #this properties exist only after Blender 3.2
        frame_start_ui = (bpy.types.NlaStrip, 'frame_start_ui')
        frame_end_ui = (bpy.types.NlaStrip, 'frame_end_ui')
        attributes += [frame_start_ui, frame_end_ui]
    
    for key in attributes:
        bpy.msgbus.subscribe_rna(
            key=key,
            # owner of msgbus subcribe (for clearing later)
            owner=subscriptions_owner,
            # Args passed to callback function (tuple)
            args=(),
            # Callback function for property update
            notify=strip_settings_callback,)

def update_strip_layer_settings(strip, layer):
    if not strip.action:
        return

    if strip.repeat <= 1:
        #Reversing the offset calculation based on the action start frame, strip start and scale
        action_start = strip.action.frame_range[0]
        offset = strip.frame_start - action_start - (strip.action_frame_start - action_start) * strip.scale
    else:
        #During repeat the offset is based on the distance from the action first keyframe
        offset = strip.frame_start - strip.action.frame_range[0]
    
    layer['speed'] = strip.scale
    layer['offset'] = round(offset, 3)
    
    #If custom frame range is turned off return to not lose frame range values
    if not layer.custom_frame_range:
        return
    layer['frame_end'] = strip.frame_end
    layer['frame_start'] = strip.frame_start
    layer['repeat'] = strip.repeat
    
      
def strip_settings_callback():
    '''subscribe_to_strip_settings callback'''
    if not bpy.context.selected_objects:
        return
    obj = bpy.context.object
    if obj is None:
        return
    anim_data = anim_layers.anim_data_type(obj)
    if anim_data is None:
        return
    if not len(anim_data.nla_tracks):
        return
    if not len(obj.Anim_Layers):
        return
    
    # sync_strip_range()
    if not len(anim_data.nla_tracks[obj.als.layer_index].strips):
        return
    strip = anim_data.nla_tracks[obj.als.layer_index].strips[0]
    layer = obj.Anim_Layers[obj.als.layer_index]

    update_strip_layer_settings(strip, layer)
    anim_layers.redraw_areas([ 'VIEW_3D'])
