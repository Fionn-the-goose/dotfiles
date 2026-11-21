import bpy
from . import anim_layers
from . import subscriptions
from mathutils import Vector, Quaternion
import numpy as np
# import time

def frame_start_end(scene):
    if scene.use_preview_range:
        frame_start = scene.frame_preview_start
        frame_end = scene.frame_preview_end
    else:
        frame_start = scene.frame_start
        frame_end = scene.frame_end     
    return float(frame_start), float(frame_end)

def smart_start_end(smartkeys, frame_start, frame_end):
    '''add the first and last frame of the scene if necessery'''
    frame_start = float(frame_start)
    frame_end = float(frame_end)
    if not len(smartkeys):
        return smartkeys
    frames = [key.frame for key in smartkeys]
    #if min(frames) < frame_start:
    if frame_start not in frames:
        keystart = smartkey()
        keystart.startendkeys(frame_start)
        smartkeys.append(keystart)      
    else: 
        for keystart in smartkeys:
            if keystart.frame == frame_start:
                keystart.startendkeys(frame_start)
                break
        
    #if max(frames) > frame_end:
    if frame_end not in frames:
        keyend = smartkey()
        keyend.startendkeys(frame_end)
        smartkeys.append(keyend)     
    else:
        for keyend in reversed(smartkeys):
            if keyend.frame == frame_end: 
                keyend.startendkeys(frame_end)
                break       
    smartkeys.sort()
    
    return smartkeys

def remove_outofrange_keys(smartkeys, frame_start, frame_end):
    '''remove smartkeys that are outside of the timeline'''
    i = 0
    while i < len(smartkeys):
        if smartkeys[i].frame < frame_start or smartkeys[i].frame > frame_end:
            smartkeys.pop(i)
        else:
            i += 1
    return smartkeys

def smart_repeat(smartkeys, fcu, strip):
    '''duplicates the smartkeys strip repeat'''
    strip_end = strip.action_frame_end * strip.scale
    #last_frame = (strip.frame_end - strip.frame_start) / strip.repeat + strip.frame_start
    # fcu_range = (strip_end - fcu.range()[0])
    fcu_range = (strip_end - strip.action_frame_start * strip.scale)
    #duplicate the keys on the cycle after      
    keyframes_dup = []

    for i in range(1, int(strip.repeat)):
        for key in smartkeys[1:]:
            keydup = smartkey(key)
            keydup.frame += round(fcu_range*(i), 2)

            #duplicate the tangents tuple values
            if hasattr(key, 'handle_left'):
                keydup.handle_left = Vector([key.handle_left[0] + fcu_range*(i+1), key.handle_left[1]])
            #if it's the last keyframe then the right handle get the value from the first keyframes
            if hasattr(key, 'handle_right'):
                keydup.handle_right = Vector([key.handle_right[0] + fcu_range*(i+1), key.handle_right[1]])
            keyframes_dup.append(keydup)
               
    #merge the keyframes from the cycle with the original keyframes
    smartkeys.extend(keyframes_dup)
    smartkeys.sort()
    
    return smartkeys

def smart_cycle(smartkeys, fcu, frame_start, frame_end):
    '''duplicates the smartkeys cycle'''
    
    for mod in fcu.modifiers:
        if mod.type != 'CYCLES' or mod.mute is True:
            continue
        fcu_range = round(fcu.range()[1] - fcu.range()[0])
        if not fcu_range:
            return smartkeys
        if not mod.cycles_after and mod.mode_after != 'None':
            #if it's an iternal cycle then duplicate the smartkeys until the scene frame end
            cycle_end_dup = int((frame_end - fcu.range()[1])/fcu_range)+2
            if mod.use_restricted_range and mod.frame_end < frame_end:
                cycle_end_dup = int((mod.frame_end - fcu.range()[1])/fcu_range)+2
        elif mod.mode_after != 'None':
            cycle_end_dup = mod.cycles_after
            if mod.use_restricted_range and mod.frame_end < (fcu.range()[1] + fcu_range * cycle_end_dup):
                cycle_end_dup = int((mod.frame_end - fcu.range()[1])/fcu_range)+2
                
        #copy the the right handle of the first keyframe to the last, and the left handle from the last keyframe to the first
        smartkeys[-1].handle_right_type = smartkeys[0].handle_left_type
        smartkeys[0].handle_left_type = smartkeys[-1].handle_right_type
        # if smartkeys[-1].interpolation == 'BEZIER':
        if hasattr(smartkeys[-1], 'handle_right'):
            smartkeys[-1].handle_right = [smartkeys[0].handle_right[0] + fcu_range, smartkeys[-1].handle_right[1]]
        # if smartkeys[0].interpolation == 'BEZIER':
        if hasattr(smartkeys[0], 'handle_left'):
            smartkeys[0].handle_left = [smartkeys[-1].handle_left[0] - fcu_range, smartkeys[0].handle_left[1]]
        #duplicate the keys on the cycle after      
        keyframes_dup = []
        for key in smartkeys[1:]:
            for i in range(cycle_end_dup):
                keydup = smartkey(key)
                keydup.frame += float(round(fcu_range, 2)*(i+1))
                
                if hasattr(keydup, 'handle_left'):
                    #duplicate the tangents tuple values
                    keydup.handle_left = Vector([key.handle_left[0] + fcu_range*(i+1), key.handle_left[1]])
                if hasattr(keydup, 'handle_right'):
                    #if it's the last keyframe then the right handle get the value from the first keyframes
                    keydup.handle_right = Vector([key.handle_right[0] + fcu_range*(i+1), key.handle_right[1]])
                if keydup not in keyframes_dup:
                    keydup.frame = round(keydup.frame, 2)
                    keyframes_dup.append(keydup)
        #if it's an iternal cycle then duplicate the keyframes before the cycle keyframes 
        if not mod.cycles_before and mod.mode_before != 'None':
            cycle_start_dup = int((fcu.range()[0] - frame_start) /fcu_range)+2
            if mod.use_restricted_range and mod.frame_start > frame_start:
                cycle_start_dup = int((fcu.range()[0]-mod.frame_start)/fcu_range)+2
        elif mod.mode_before != 'None':
            cycle_start_dup = mod.cycles_before
            if mod.use_restricted_range and mod.frame_start > (fcu.range()[0] + fcu_range * cycle_start_dup):
                cycle_start_dup = int((fcu.range()[0]-mod.frame_start)/fcu_range)+2     
        #duplicate the keys on the cycle before            
        for key in smartkeys[:-1]:
            for i in range(cycle_start_dup):
                keydup = smartkey(key)
                keydup.frame -= fcu_range*(i+1)
                if hasattr(keydup, 'handle_left'):
                    #duplicate the tangents
                    keydup.handle_left = [key.handle_left[0] - fcu_range*(i+1), key.handle_left[1]]
                if hasattr(keydup, 'handle_right'):
                    keydup.handle_right = [key.handle_right[0] - fcu_range*(i+1), key.handle_right[1]]

                if keydup not in keyframes_dup:
                    keydup.frame = round(keydup.frame, 2)
                    keyframes_dup.append(keydup)
        #merge the keyframes from the cycle with the or iginal keyframes
        smartkeys.extend(keyframes_dup)
        smartkeys = list(set(smartkeys))
        smartkeys.sort()

        if mod.use_restricted_range:
            smartkeys = smart_start_end(smartkeys, mod.frame_start, mod.frame_end)
            smartkeys = smart_start_end(smartkeys, mod.frame_start+1, mod.frame_end-1)
            
    return smartkeys

def smart_bake(context):
    #record all the keyframes into smartkeys
    obj = context.object
    frame_start, frame_end = context.scene.als.bake_range
    fcu_smartkeys = {}
    anim_data = anim_layers.anim_data_type(obj)

    # Initialize the progress bar
    wm = context.window_manager
    total_iterations = 0
    for track in anim_data.nla_tracks:
        if track.mute:
            continue
        if len(track.strips) != 1 or track.strips[0].action is None:
            continue
        fcurves = anim_layers.get_fcurves(obj, track.strips[0].action)
        total_iterations += len(fcurves)

    wm.progress_begin(0, total_iterations) 
    processed = 0

    for layer, track in zip(obj.Anim_Layers, anim_data.nla_tracks):
        if track.mute:
            continue
        if len(track.strips) != 1 or track.strips[0].action is None:
            continue
        strip = track.strips[0]
        strip_start = strip.action_frame_start
        strip_end = strip.action_frame_end
        # include influence keyframes in the smartbake
        for strip_fcu in strip.fcurves:
            strip_keyframes = [keyframe for keyframe in strip_fcu.keyframe_points if len(strip_fcu.keyframe_points) and not strip_fcu.mute]
        
        fcurves = anim_layers.get_fcurves(obj, strip.action)
        for fcu in fcurves:
            if not fcu.is_valid or fcu.mute or selected_bones_filter(obj, fcu.data_path):
                continue
            smartkeys = []
            #duplicate all the keyframes into a new keyframes class duplicate
            for key in list(fcu.keyframe_points) + strip_keyframes:
                keyframe = smartkey(key)
                #add the handle values only if it's in combine or replace layers
                if strip.blend_type in {'COMBINE', 'REPLACE'}:
                    keyframe.handle_left = Vector(key.handle_left)
                    keyframe.handle_right = Vector(key.handle_right)
                    keyframe.handle_left[0] = strip_start * layer.speed + (keyframe.handle_left[0] - strip_start) * layer.speed + layer.offset
                    keyframe.handle_right[0] = strip_start * layer.speed + (keyframe.handle_right[0] - strip_start) * layer.speed + layer.offset

                #keyframe.frame += layer.offset
                keyframe.frame = strip_start * layer.speed + (keyframe.frame - strip_start) * layer.speed + layer.offset# * layer.speed
                keyframe.frame = round(keyframe.frame, 2)
                if keyframe not in smartkeys:
                    smartkeys.append(keyframe)
            
            if not smartkeys:
                continue
            #remove duplicate or any subframe from first and end frames for clean cyclic
            if len(fcu.modifiers) and obj.als.mergefcurves:
                smartkeys[0].frame = float(round(smartkeys[0].frame))
                smartkeys[-1].frame = float(round(smartkeys[-1].frame))
            
            #remove extra keyframes
            smartkeys = list(set(smartkeys))
            smartkeys.sort()
            if len(fcu.modifiers) and obj.als.mergefcurves:
                smartkeys = smart_cycle(smartkeys, fcu, frame_start, frame_end)

            #apply strip action settings
            last_frame = (strip.frame_end - strip.frame_start) / strip.repeat + strip.frame_start
            if strip.use_reverse:
                for key in smartkeys:
                    key.frame = last_frame - (key.frame - strip.frame_start)     
            if strip.repeat > 1:
                smartkeys = smart_start_end(smartkeys, last_frame , last_frame+1) #strip.frame_start
                smartkeys = remove_outofrange_keys(smartkeys, strip.frame_start, last_frame+1)  #+ layer.offset
                smartkeys = smart_repeat(smartkeys, fcu, strip)
            if layer.custom_frame_range:
                smartkeys = smart_start_end(smartkeys, strip.frame_start, strip.frame_end)
                smartkeys = remove_outofrange_keys(smartkeys, strip.frame_start, strip.frame_end)

                #if the strip is cutting with a different strip, then add keyframes in the cut    
                for layercut in obj.Anim_Layers:
                    if layercut.mute or not layercut.custom_frame_range or layercut == layer:
                        continue
                    if strip_start < layercut.frame_start < strip_end:
                        smartkeys = smart_start_end(smartkeys, (layercut.frame_start-1), strip.frame_end-1)
                    if strip_start < layercut.frame_end < strip_end:
                        smartkeys = smart_start_end(smartkeys, (layercut.frame_end+1), strip.frame_end-1)

            #if the list of keyframes exists in a different track list then add them
            if (fcu.data_path, fcu.array_index) in fcu_smartkeys:
                smartkeys = list(set(fcu_smartkeys[(fcu.data_path, fcu.array_index)]+smartkeys))
            #Merge all duplicated keyframes
            smartkeys = list(set(smartkeys))

            smartkeys.sort()
            
            fcu_smartkeys.update({(fcu.data_path, fcu.array_index):smartkeys})

            processed += 1
            wm.progress_update(processed)

    wm.progress_end() 

    #add inbetweens
    for fcu, smartkeys in fcu_smartkeys.items():
        if not smartkeys:
            continue
        if min(smartkeys).frame < frame_start or max(smartkeys).frame > frame_end:
            smartkeys = smart_start_end(smartkeys, frame_start, frame_end)
            smartkeys = remove_outofrange_keys(smartkeys, frame_start,  frame_end)
        if context.scene.als.handles_type not in {'ALIGNED', 'VECTOR', 'AUTO', 'AUTO_CLAMPED'}:
            smartkeys = add_inbetween(smartkeys)
    return fcu_smartkeys
 
def add_inbetween(smartkeys):
    i = 0
    while i < len(smartkeys)-1:
        if smartkeys[i].inbetween:
            i += 1
            continue
        if (smartkeys[i+1].frame - smartkeys[i].frame) <= 1:
            i += 1
            continue
        key1 = smartkey()
        key1.frame = smartkeys[i].frame + (smartkeys[i+1].frame - smartkeys[i].frame)*1/3
        key1.inbetween = True
        
        key2 = smartkey()
        key2.frame = smartkeys[i].frame + (smartkeys[i+1].frame - smartkeys[i].frame)*2/3
        key1.frame = round(key1.frame, 2)
        key2.frame = round(key2.frame, 2)

        key2.inbetween = True
        smartkeys.insert(i+1, key1)
        smartkeys.insert(i+2, key2)
        i += 3 
    return smartkeys

class smartkey:
    def __init__(self, key = None):
        if not key:
            return
        if hasattr(key, 'co'):
            self.frame = round(float(key.co[0]), 1)
        elif hasattr(key, 'frame'):
            self.frame = round(float(key.frame), 1)
        self.interpolation = key.interpolation
        self.handle_left_type = key.handle_left_type
        self.handle_right_type = key.handle_right_type
        # if hasattr(key, 'handle_left'):
        #     self.handle_left = Vector(key.handle_left)
        # if hasattr(key, 'handle_right'):
        #     self.handle_right = Vector(key.handle_right)
        self.easing = key.easing
        self.inbetween = False

    def startendkeys(self, frame):
        self.frame = round(float(frame), 1)
        self.interpolation = 'BEZIER'
        self.handle_left_type = 'VECTOR'
        self.handle_right_type = 'VECTOR'
        self.easing = 'AUTO'
        self.inbetween = False

    def __lt__(self, other):
        return self.frame < other.frame

    def __hash__(self):
        return hash(self.frame)

    def __eq__(self, other):
        if not isinstance(other, type(self)): return NotImplemented
        return self.frame == other.frame
    
    def __str__(self):
        return str(self.frame)

def mute_unbaked_layers(layer_index, nla_tracks, additive):
    obj = bpy.context.object
    #a list to record which layers that are not merged were muted
    mute_rec = [] 
    #mute the layers that are not going to be baked
    if obj.als.direction == 'ALL':
        return mute_rec

    for index, track in enumerate(nla_tracks):
        #if running into a replace layer during additive bake then exclude the rest of the layers from the bake
        if track.mute:
            mute_rec.append(track)
            continue
        if additive and track.strips[0].blend_type == 'REPLACE' and index >= layer_index:
            layer_index = len(nla_tracks)
            #layer_index = len(nla_tracks)-1
        if obj.als.direction == 'DOWN' and index > layer_index:
            track.mute = True
            track.select = False
        if obj.als.direction == 'UP' and index < layer_index:
            track.mute = True
            track.select = False
      
    return mute_rec

def mute_modifiers(obj, nla_tracks):
    #disable modifiers if merge fcurves is false
    modifier_rec = []
    extrapolations = []
    for track in nla_tracks:
        if len(track.strips) != 1 or track.strips[0].action is None:
            continue
        fcurves = anim_layers.get_fcurves(obj, track.strips[0].action)
        for fcu in fcurves:
            if selected_bones_filter(obj, fcu.data_path):
                continue
            if fcu.extrapolation == 'LINEAR':
                extrapolations.append((fcu.data_path, fcu.array_index))
            if fcu.lock:
                fcu.lock = False
            if fcu.group is not None:
                if fcu.group.lock:
                    fcu.group.lock = False
            if not fcu.is_valid:
                continue
            if len(fcu.modifiers) and not obj.als.mergefcurves:
                for mod in fcu.modifiers:
                    if mod.mute == False:
                        modifier_rec.append(mod)
                        mod.mute = True                          

    return modifier_rec, extrapolations

def unmute_modifiers(obj, nla_tracks, modifier_rec):
    #Turn on fcurve modifiers if merge fcurves is false
    for track in nla_tracks:
        if track.strips[0].action is None:
            continue
        fcurves = anim_layers.get_fcurves(obj, track.strips[0].action)
        for fcu in fcurves:
            if not fcu.is_valid or selected_bones_filter(obj, fcu.data_path):
                continue
            if not len(fcu.modifiers):
                continue
            for mod in fcu.modifiers:
                if mod in modifier_rec:
                    mod.mute = False
                elif obj.als.mergefcurves and track == nla_tracks[obj.als.layer_index]:
                    mod.mute = True

def invisible_layers(b_layers):
    #Store the current invisible layer bones and make them visible for baking
    layers_rec = []
    for i in range(len(b_layers)):
        if b_layers[i] == False:
            layers_rec.append(i)
            b_layers[i] = True
    return layers_rec

def select_keyframed_bones(self, context, obj):
    #Select all keyframed bones in layers if not only selected
    if obj.als.onlyselected:
        return
    if obj.mode != 'POSE':
        bpy.ops.object.posemode_toggle()
    bpy.ops.pose.select_all(action='DESELECT')
    for i in range(0, obj.als.layer_index+1):
        obj.als['layer_index'] = i
        anim_layers.select_layer_bones(self, context)

def mute_constraints(obj):
    #Mute constraints if are not cleared during bake
    constraint_rec = [] 
    if obj.als.clearconstraints:
        return constraint_rec
    for bone in bpy.context.selected_pose_bones:
        for constraint in bone.constraints:
            if constraint.mute == False:
                constraint_rec.append(constraint)
                constraint.mute = True
    return constraint_rec

def smartbake_apply(obj, nla_tracks, fcu_keys, extrapolations):
    #apply smartbake for blenders bake
    #smart bake - delete unnecessery keyframes:
    # transform_types = ['location', 'rotation_euler', 'rotation_quaternion', 'scale']
    strip = nla_tracks[obj.als.layer_index].strips[0]
    # if strip.action is None:
    #     return
         
    # action_range = strip.frame_end - strip.frame_start
    fcurves = anim_layers.get_fcurves(obj, strip.action)
    for fcu in fcurves:
        if not fcu.is_valid:
            continue
        if selected_bones_filter(obj, fcu.data_path):
            continue
        fcu_key = (fcu.data_path, fcu.array_index)
        if fcu_key not in fcu_keys.keys():
            fcurves.remove(fcu)
            continue
        #get all the frames of the smart keys
        smartkeys = fcu_keys[fcu_key]
        #Get all the inbetween values
        for smartkey in smartkeys:
            if smartkey.inbetween:
                smartkey.value = round(fcu.evaluate(smartkey.frame), 2)         
        smart_frames = [key.frame for key in smartkeys if not key.inbetween]        
 
        #get all the frames of the baked keyframes
        key_frames = [key.co[0] for key in fcu.keyframe_points]
        for smart_key in smartkeys:
            if smart_key.frame > strip.action.frame_range[1]:
                break
            if smart_key.inbetween:
                continue
            #add keyframes that are missing from the bake but included in the smart bake
            if smart_key.frame not in key_frames:
                value = fcu.evaluate(smart_key.frame)
                fcu.keyframe_points.add(1)
                fcu.keyframe_points[-1].co = (smart_key.frame, value)
                fcu.update()        
                    
        i = 0
        while i < len(fcu.keyframe_points):
            key = fcu.keyframe_points[i]
            
            if key.co[0] not in smart_frames:
                fcu.keyframe_points.remove(key)
            else:
                key.co[1] = round(key.co[1], 3)

                i += 1
        fcu.update()
        #add_interpolations(fcu, smartkeys)
        if (fcu.data_path, fcu.array_index) in extrapolations:
            fcu.extrapolation = 'LINEAR'
        fcu.update()

def armature_restore(obj, b_layers, layers_rec, constraint_rec):
    if obj.type != 'ARMATURE':
        return
    #Turn off previous invisible bone layers
    if bpy.app.version < (4, 0, 0) :
        for i in range(len(b_layers)):
            if i in layers_rec:
                b_layers[i] = False
    else:
        for key, value in layers_rec.items():
            key.is_visible = value
            
                    
    #Turn on constraints
    if not obj.als.clearconstraints:
        for constraint in constraint_rec:
            constraint.mute = False

def attr_default(obj, fcu_key):
    '''Returns the default value or default array value in a list'''
    #check if the fcurve source belongs to a bone or obj
    if fcu_key[0][:10] == 'pose.bones':
        transform = fcu_key[0].split('.')[-1]
        attr = fcu_key[0].split('"')[-2]
        bone = fcu_key[0].split('"')[1]
        if bone in obj.pose.bones:
            source = obj.pose.bones[bone]
        #if the bone not found still calculate the default based on the path
        elif '.rotation_quaternion' in fcu_key[0]:
            return [1.0, 0.0, 0.0, 0.0]
        elif '.scale' in fcu_key[0]:
            return [1.0, 1.0, 1.0]
        else:
            return [0]
        
    #in case of shapekey animation        
    elif fcu_key[0][:10] == 'key_blocks':
        attr = fcu_key[0].split('"')[1]
        if attr not in obj.data.shape_keys.key_blocks:
            return [0]
        shapekey = obj.data.shape_keys.key_blocks[attr]
        return [0] if shapekey.slider_min <= 0 else shapekey.slider_min
    #in case of transforms in object mode
    else:# fcu_key[0] in transform_types:
        source = obj
        transform = fcu_key[0]
    
    #check when it's transform property of Blender
    if transform in source.bl_rna.properties.keys():
        if hasattr(source.bl_rna.properties[transform], 'default_array'):
            if len(source.bl_rna.properties[transform].default_array) > fcu_key[1]:
                attrvalue = source.bl_rna.properties[transform].default_array#[fcu_key[1]]
                return attrvalue

    #in case of property on object
    elif len(fcu_key[0].split('"')) > 1:
        if fcu_key[0].split('"')[1] in obj.keys():
            attr = fcu_key[0].split('"')[1]

    if 'attr' not in locals():
        # print(fcu_key[0], 'has no attributes returning 0')
        return [0]  

    #since blender 3 access to custom property settings changed
    if attr in source:
        if not isinstance(source[attr], float) and not isinstance(source[attr], int):
            return [0]
        id_attr = source.id_properties_ui(attr).as_dict()
        attrvalue = id_attr['default']
        return [attrvalue]
    
    return [0]

def selected_bones_filter(obj, fcu_data_path):
    '''using obj.als.onlyselected property for the bake'''
    if not obj.als.onlyselected:
        return False
    if obj.mode != 'POSE':
        return True
    transform_types = ['location', 'rotation_euler', 'rotation_quaternion', 'scale']
    #filter selected bones if option is turned on
    bones = [bone.path_from_id() for bone in bpy.context.selected_pose_bones]
    if fcu_data_path.split('].')[0]+']' not in bones and fcu_data_path not in transform_types:
        return True
  
def evaluate_combine(data_path, added_array, eval_array, array_default, influence):
    
    if data_path.endswith('scale'):
        eval_array = eval_array * (added_array / array_default) ** influence
    elif data_path.endswith('rotation_quaternion'):
        #multiply first the influence with the w separatly
        added_array[0] = added_array[0] + (1- added_array[0])*(1 - influence)
        added_array[1:] *= influence 
        eval_array = np.array(Quaternion(eval_array) @ Quaternion(added_array))# ** influence
    #if it's a custom property
    elif not data_path.endswith('rotation_euler') and not data_path.endswith('location'):
        eval_array = eval_array + (added_array - array_default) * influence
        
    return eval_array    
    
def frame_evaluation(frame, strip):
    frame_eval = frame
    #change the frame if the strip is on hold    
    if frame < strip.frame_start:
        if strip.extrapolation == 'HOLD':
            frame_eval = strip.frame_start
    elif frame >= strip.frame_end:
        if strip.extrapolation == 'HOLD' or strip.extrapolation == 'HOLD_FORWARD':
            frame_eval = strip.frame_end
            
    last_frame = strip.frame_start + (strip.frame_end - strip.frame_start) / strip.repeat
    offset = strip.frame_start - strip.action_frame_start * strip.scale
    
    if strip.repeat > 1 and (frame) >= last_frame:
        action_range = (strip.action_frame_end * strip.scale - strip.action_frame_start * strip.scale)
        frame_eval = (((frame_eval - strip.frame_start) % (action_range)) + strip.frame_start)
                
    if strip.use_reverse:
        frame_eval = last_frame - (frame_eval - strip.frame_start)
    frame_eval = strip.frame_start * 1/strip.scale + (frame_eval - strip.frame_start) * 1/strip.scale - offset * 1/strip.scale
    
    return frame_eval
    
def clean_no_user_slots(action):
    '''Remove all the slots that are connected to the action and object but not part of the action slot'''
    if not hasattr(action, 'slots'):
        return
    for slot in action.slots:
        if slot is None:
            continue
        if len(slot.users()):
            continue

        action.slots.remove(slot)

def AL_bake(frame_start, frame_end, nla_tracks, fcu_keys, additive, step, actioncopy, baked_layer = None):
    
    obj = bpy.context.object
    if obj is None:
        return
    anim_data = anim_layers.anim_data_type(obj)
    # baked_action = anim_data.action
    track = nla_tracks[obj.als.layer_index]
    baked_action = track.strips[0].action
    clean_no_user_slots(baked_action)
    #create the baked fcurve
    # baked_channelbag = anim_layers.get_channelbag(obj, baked_action, obj.als.data_type)
    baked_channelbag = anim_layers.get_channelbag(obj, baked_action)
    baked_fcurves = baked_channelbag.fcurves


    if not obj.als.smartbake:
        interpolation_types =  ('CONSTANT', 'LINEAR', 'BEZIER', 'SINE', 'QUAD', 'CUBIC', 'QUART', 'QUINT', 'EXPO', 'CIRC', 'BACK', 'BOUNCE', 'ELASTIC')
        handle_types = ('FREE', 'ALIGNED', 'VECTOR', 'AUTO', 'AUTO_CLAMPED')
        # Get the index position to assign with foreach_set
        interpolation = interpolation_types.index(bpy.context.preferences.edit.keyframe_new_interpolation_type)
        handle_type = handle_types.index(bpy.context.preferences.edit.keyframe_new_handle_type)

    if obj.als.operator == 'MERGE':# and not additive: # and anim_data.action is not None: #and obj.als.onlyselected
        #overwrite action
        anim_data.use_tweak_mode = False
        
        #create a duplicate of the action on the merged layer to have a clean action in order to not write over the calculation
        action_copy = bpy.data.actions[baked_action.name].copy()
        
        track.strips[0].action = action_copy

    
    if hasattr(baked_action, 'id_root'):
        baked_action.id_root = obj.als.data_type

    blend_types = {'ADD' : '+', 'SUBTRACT' : '-', 'MULTIPLY' : '*'}
    fcu_paths = []

    # Initialize the progress bar
    wm = bpy.context.window_manager
    fcu_set = {fcu_key[0] for fcu_key in fcu_keys.keys()}
    total_iterations = len(fcu_set)
    wm.progress_begin(0, total_iterations)  # (start, end range)
    processed = 0

    for fcu_key in fcu_keys.keys():
        if fcu_key[0] in fcu_paths:
            continue
        else:
            fcu_paths.append(fcu_key[0])
            
        if selected_bones_filter(obj, fcu_key[0]):
            continue
        extrapolation = False
    
        #adding default value to the array
        array_default = np.array(attr_default(obj, fcu_key))

        array_length = array_default.shape[0]

        if isinstance(array_default[0], str):
            continue
        #find or create the fcurve in the new action
        baked_fcus = []
        smart_frames = {}
        mod_copy = []
        mod_list = {}
        #Create smart frames and new fcurves for all layers, apply their groups and modifiers
        for i in range(array_length):
            if (fcu_key[0], i) not in fcu_keys:
                continue
            smart_frames.update({i : [key.frame for key in fcu_keys[fcu_key[0], i]]})
            if not smart_frames[i]:
                continue
            
            #find the groups and modifiers
            for track in nla_tracks:
                if track.mute:
                    continue
                if not len(track.strips):
                    continue
                if track.strips[0].action is None:
                    continue
                # if track == baked_layer:
                #     continue
                #finding the channel group of array
                channelbag = anim_layers.get_channelbag(obj, track.strips[0].action)
                if channelbag is None:
                    continue

                fcurves = channelbag.fcurves
                fcu = fcurves.find(fcu_key[0], index = i)
                # print(f'track {track.name} fcurves {len(fcurves)}')
                if fcu is None:
                    # print('fcu is none', fcu_key[0], track.name)
                    continue
                group = fcu.group if fcu.group is not None else None

                if group is not None:
                    if group.name in baked_channelbag.groups:
                        group = baked_channelbag.groups[group.name]
                    else:
                        group = baked_channelbag.groups.new(group.name)
                        
                #copy and append Modifiers into mod_list. Mute them if turned on
                if len(fcu.modifiers) and not obj.als.mergefcurves:
                    for mod in fcu.modifiers:
                        mod_copy = anim_layers.copy_modifiers(mod, mod_copy)
                        mod_list.update({i: mod_copy})
                        #turn off modifier after copying it and append it
                        if mod.mute == False:
                            # modifier_rec.append(mod)
                            mod.mute = True
                extrapolation = True if fcu.extrapolation == 'LINEAR' else False

            #### Creating or overwritting (during merge) the new baked fcurves####

            baked_fcu = ensure_fcurves_bversion(baked_fcurves, fcu_key[0], i)
                
            baked_fcu.color_mode = 'AUTO_RGB'

            if group is not None:
                if baked_fcu.group != group:
                    baked_fcu.group = group

            if extrapolation:
                baked_fcu.extrapolation = 'LINEAR'

            baked_fcus.append(baked_fcu)
        
        #select smart bake frame range or every frame in the range       
        if obj.als.smartbake:
            #merge all the smartframe arrays
            frame_range = set([item for sf in smart_frames.values() for item in sf])
        else:
            frame_range = range(frame_start, frame_end+1, step)
        
        #Evaluate the value of the current frame from all the unmuted tracks
        for frame in frame_range:
            
            eval_array = np.zeros(array_length) if additive else array_default
            layers_count = 0
            
            #ITERATE through all the layers to evaluate
            for track in nla_tracks:
                if not len(track.strips):
                    continue

                if track.mute or track == baked_layer or track.strips[0].action is None:
                    continue
                strip = track.strips[0]
                fcurves = anim_layers.get_fcurves(obj, strip.action)
                if not len(fcurves):
                    continue
                if (frame < strip.frame_start or frame > strip.frame_end) and strip.extrapolation == 'NOTHING':
                    layers_count += 1
                    continue
                
                #get the influence value either from the attribute or the fcurve
                if not strip.fcurves[0].mute and len(strip.fcurves[0].keyframe_points):
                    influence = strip.fcurves[0].evaluate(frame)
                    #if there are influence keyframes then make sure layers_count is not 1 to recalculate the handles
                    layers_count = 2
                else:
                    influence = strip.influence
                
                #evaluate the frame according to the strip settings
                frame_eval = frame_evaluation(frame, strip)

                blend_type = strip.blend_type
                    
                #Create the array of the current layer if a value is missing then add the default value
                added_array = []
                missing = 0
                for i in range(array_length):
                    
                    fcu = fcurves.find(fcu_key[0], index = i)
                    #if the fcurve is not found then get the default value
                    if fcu is None:
                        missing += 1
                        #getting the previous value if the fcurve is missing instead of just default
                        #the other option would be to use an array for the influence as well
                        value = eval_array[i] if blend_type in ('REPLACE', 'COMBINE') else 0
                    else:
                        value = fcu.evaluate(frame_eval)
                    added_array.append(value)
                if missing == array_length:
                    continue
                #convert the array to a numpy array
                added_array = np.array(added_array)
                
                ###EVALUATION###
                if blend_type =='COMBINE':
                    if 'location' in fcu_key[0] or 'rotation_euler' in fcu_key[0]:
                        blend_type = 'ADD'
                if blend_type =='REPLACE':
                    eval_array = eval_array * (1 - influence) + added_array * influence
                elif blend_type =='COMBINE':
                    eval_array = evaluate_combine(fcu_key[0], added_array, eval_array, array_default, influence)
                else:
                    eval_array = eval('eval_array' + blend_types[blend_type] +' added_array' + '*' + str(influence))
                layers_count += 1     
            
            if not eval_array.size:
                continue
            
            #WRITING the keyframes:
            for baked_fcu in baked_fcus:
                
                i = baked_fcu.array_index
                if obj.als.smartbake:
                    #find the smartkey in the fcu_keys -smartkeys and array of the fcurves
                    if frame not in smart_frames[i]:
                        continue
                    
                    smartkey = fcu_keys[(fcu_key[0], i)][smart_frames[i].index(frame)]
                    smartkey.value = eval_array[i]
                    if smartkey.inbetween:
                        continue
                
                baked_fcu.keyframe_points.add(1)
                keyframe = baked_fcu.keyframe_points[-1]
                keyframe.co = (frame, eval_array[i])
            
        for baked_fcu in baked_fcus:
            if not len(baked_fcu.keyframe_points):
                continue
            baked_fcu.update()
            i = baked_fcu.array_index
            if (fcu_key[0], i) not in fcu_keys:
                continue
            if obj.als.smartbake:
                add_interpolations(baked_fcu, fcu_keys[(fcu_key[0], i)], layers_count)
            else:
                # Add the interpolation and handle types from the preferences defaults
                set_all_handles(baked_fcu, interpolation, handle_type)

            baked_fcu.update()
            
            #paste the modifiers to the new baked fcurve
            if i in mod_list and not len(baked_fcu.modifiers):
                anim_layers.paste_modifiers(baked_fcu, mod_list[i])

        processed += 1
        wm.progress_update(processed)
    wm.progress_end()

    if not actioncopy and obj.als.operator == 'MERGE':
        bpy.data.actions.remove(action_copy)
                
    return baked_action

def set_all_handles(baked_fcu, interpolation, handle_type):
    '''Set all the handle types and interpolations in an fcurve using foreach_set and numpy
    should use numerical value instead of a string'''
    keyframes_len = len(baked_fcu.keyframe_points)
    interpolations = np.full(keyframes_len, interpolation)
    handles = np.full(keyframes_len, handle_type)
    kp = baked_fcu.keyframe_points
    kp.foreach_set('interpolation', interpolations)
    kp.foreach_set('handle_left_type', handles)
    kp.foreach_set('handle_right_type', handles)

def ensure_fcurves_bversion(fcurves, data_path, i):
    '''Either use ensure in Blender 5.0 and above or use find and new in older version'''

    if hasattr(fcurves, 'ensure'):
        baked_fcu = fcurves.ensure(data_path, index = i)
    else:
        baked_fcu = fcurves.find(data_path, index = i)
        if baked_fcu is None:            
            baked_fcu = fcurves.new(data_path, index = i)


    if len(baked_fcu.keyframe_points):
        baked_fcu.keyframe_points.clear()

    return baked_fcu

def non_recalc_handle_type(baked_keys):
    
    handles_type = bpy.context.scene.als.handles_type
    if handles_type not in {'ALIGNED', 'VECTOR', 'AUTO', 'AUTO_CLAMPED'}:
        return False
    for key in baked_keys:
        key.handle_left_type = handles_type
        key.handle_right_type = handles_type
    
    return True
    
def add_interpolations(baked_fcu, smartkeys, layers_count = 0):
    '''Add the interpolation or control points between every two smartkeys'''

    baked_keys = baked_fcu.keyframe_points
    keys = [key for key in smartkeys if not key.inbetween]
        
    if non_recalc_handle_type(baked_keys):
        return

    #the index for the inbetweens
    P1index = 1
    P2index = 2
    if len(baked_keys) != len(keys):
        print('unequal length of keys ',baked_fcu.data_path, len(baked_keys), len(keys))
        print('keys ', [key.frame for key in keys])
        print('baked keys ', [round(key.co[0], 2) for key in baked_keys])
        print('set difference ', set([key.frame for key in keys]).difference(set([key.co[0] for key in baked_keys])))

    baked_keys[0].handle_left_type = 'VECTOR'
    baked_keys[-1].handle_right_type = 'VECTOR'
    
    #keys are smartkeys without the inbetween keyframes
    for i, key in enumerate(keys[:-1]):
        skip = False
        #if the fcurve was counted only once, then just copy the old handle values instead of recalculating
        if layers_count == 1:# and key != keys[0] and i < len(keys) - 3:
            apply_handle_types(baked_keys, keys, i)
            if hasattr(key, 'handle_right'):
                baked_keys[i].handle_right = key.handle_right
            if hasattr(keys[i+1], 'handle_left'):
                baked_keys[i+1].handle_left = keys[i+1].handle_left
            if hasattr(key, 'handle_right') and hasattr(keys[i+1], 'handle_left'): 
                skip = True
        
        if key.interpolation != 'BEZIER':
            baked_keys[i].interpolation = key.interpolation
            baked_keys[i].easing = key.easing
            if keys[-1] == key:
                baked_keys[i+1].interpolation = keys[i+1].interpolation
                baked_keys[i+1].easing = keys[i+1].easing
            skip = True
            
        if not smartkeys[P1index].inbetween :
            P1index += 1
            P2index += 1
            continue 
        
        if skip:
            P1index += 3
            P2index += 3
            continue
        
        #skip if value not found in smartkey (bug that need to be solved)
        if not hasattr(smartkeys[P1index], 'value') or not hasattr(smartkeys[P2index], 'value'):
            apply_handle_types(baked_keys, keys, i)
            print(baked_fcu.data_path, 'missing smartkey value on frame', baked_keys[i].co[0])
            P1index += 3
            P2index += 3
            continue

        P0 = baked_keys[i].co[1]
        P3 = baked_keys[i+1].co[1]

        P1 = smartkeys[P1index].value
        P2 = smartkeys[P2index].value

        cp1 = (1/6)*( -5*P0 + 18*P1 - 9*P2 + 2*P3)
        cp2 = (1/6)*(  2*P0 -  9*P1 +18*P2 - 5*P3)

        apply_handle_types(baked_keys, keys, i)

        baked_keys[i].handle_right = [smartkeys[P1index].frame, cp1]
        baked_keys[i+1].handle_left = [smartkeys[P2index].frame, cp2]
        
        #iterate through the inbetween smartkeys
        P1index += 3
        P2index += 3
    
        #add in-betweener


def apply_handle_types(baked_keys, smartkeys, i):
    
    handles_type = bpy.context.scene.als.handles_type
    if handles_type == 'PRESERVE':
        if hasattr(smartkeys[i], 'handle_right_type'):
            baked_keys[i].handle_right_type = smartkeys[i].handle_right_type
        if hasattr(smartkeys[i+1], 'handle_left_type'):
            baked_keys[i+1].handle_left_type = smartkeys[i+1].handle_left_type
        else:
            baked_keys[i].handle_right_type = 'FREE'
            baked_keys[i+1].handle_left_type = 'FREE'
    else:
        baked_keys[i].handle_right_type = handles_type
        baked_keys[i+1].handle_left_type = handles_type
    
def bake_range_type(self, context):
    if self.bake_range_type == 'SCENE':
        frame_start, frame_end = frame_start_end(context.scene)
        self.bake_range = (int(frame_start), int(frame_end))
        
    if self.bake_range_type == 'KEYFRAMES':
        obj = context.object
        anim_data = anim_layers.anim_data_type(obj)
        frame_end = []
        frame_start = []
        posebones = context.selected_pose_bones
        #if baking only selected bones then find the longest fcurves for the range
        if obj.als.onlyselected and posebones:
            bonespath = [bone.path_from_id() for bone in posebones]
            #get the frame range from 
            if not bonespath:
                return 
        for track in anim_data.nla_tracks:
            if not len(track.strips):
                continue
            action = track.strips[0].action
            if obj.als.onlyselected and posebones:
                # Get fcurve range from the selected objects
                fcurves = anim_layers.get_fcurves(obj, action)
                for fcu in fcurves:
                    #check if the fcurve is in the selected bones
                    if any(path in fcu.data_path for path in bonespath):
                        frame_start.append(fcu.range()[0])
                        frame_end.append(fcu.range()[1])
            else:
                # Get the action range
                action = track.strips[0].action
                frame_start.append(action.frame_range[0])
                frame_end.append(action.frame_range[1])

        # Checking for the longest action in all the actions
        if frame_start:
            self.bake_range = (int(min(frame_start)), int(max(frame_end)))

class MergeAnimLayerDown(bpy.types.Operator):
    """Merge and bake the layers from the current selected layer down to the base"""
    bl_idname = "anim.layers_merge_down"
    bl_label = "Merge_Layers_Down"
    bl_options = {'REGISTER', 'UNDO'}
    
    step: bpy.props.IntProperty(name='Step', description='Bake every number of frame steps', default=1)
    actioncopy: bpy.props.BoolProperty(name='Copy original merged action', description='Create a copy of the original action that is being overwritten', default = False)

    def invoke(self, context, event):
        obj = context.object
        bake_range_type(context.scene.als, context)
        if obj.mode != 'POSE':
            obj.als.onlyselected = False
        wm = context.window_manager
        return wm.invoke_props_dialog(self, width = 200)

    def draw(self, context):
        obj = context.object
        layout = self.layout
        box = layout.box()
        if obj.als.data_type == 'OBJECT':
            split = box.split(factor=0.5, align = True)
            split.label(text = 'Bake Type :')
            split.prop(obj.als, 'baketype')
        split = box.split(factor=0.5, align = True)
        split.label(text = 'Bake Operator :')
        split.prop(obj.als, 'operator')
        split = box.split(factor=0.5, align = True)
        split.label(text = 'Bake Direction :')
        split.prop(obj.als, 'direction')
        box.prop(obj.als, 'mergefcurves')
        
        split = box.split(factor=0.5, align = True)
        if obj.als.baketype == 'AL':
            split.prop(obj.als, 'smartbake')
            if obj.als.smartbake:
                split.prop_menu_enum(context.scene.als, 'handles_type')
            else:
                split.prop(self, 'step')
        else:
            split.prop(self, 'step')
            
        if obj.mode == 'POSE':
            box.prop(obj.als, 'onlyselected')
        if obj.als.baketype == 'NLA':
            # split.prop(self, 'direction')
            box.prop(obj.als, 'clearconstraints')
        if obj.als.operator == 'MERGE':
            box.prop(self, 'actioncopy')
        
        split = box.split(factor=0.5, align = True)
        split.label(text = 'Frame Range :')
        split.prop(context.scene.als, 'bake_range_type', text = '')
        #split = box.split(factor=0.2, align = True)
        if context.scene.als.bake_range_type == 'CUSTOM':
            row = box.row()
            row.prop(context.scene.als, 'bake_range', text = '')
        
    def execute(self, context):
        obj = bpy.context.object
        if obj is None:
            return {'CANCELLED'}
        anim_data = anim_layers.anim_data_type(obj)
        nla_tracks = anim_data.nla_tracks
        
        if obj.als.direction == 'DOWN' and not obj.als.layer_index:
            return {'CANCELLED'}

        #disable baking up from Blender's bake
        if obj.als.direction == 'UP' and obj.als.layer_index == len(nla_tracks)-1:
            return {'CANCELLED'}
        
        if obj.als.baketype == 'NLA':
            obj.als.smartbake = False
        
        subscriptions.subscriptions_remove()
        # start = time.time()
        #define the start and end frame of the bake, according to scene or preview length 
        frame_start, frame_end = context.scene.als.bake_range
        
        # Incase the strips are shorter then the keyframe range (because scene is shorter)
        # Then updating the strips length
        for layer, track in zip(obj.Anim_Layers, anim_data.nla_tracks):
            if layer.custom_frame_range:
                continue
            if len(track.strips) != 1:
                continue
            strip = track.strips[0]
            strip.frame_start =frame_start
            anim_layers.update_action_frame_range(frame_start, frame_end, layer, strip)
            strip.scale = layer.speed
            strip.frame_end = frame_end

        obj.als.view_all_keyframes = False
        if context.scene.frame_current > frame_end:
            context.scene.frame_current = frame_end                   

        layer_index = obj.als.layer_index
        blendings = [track.strips[0].blend_type for track in nla_tracks[layer_index:] if len(track.strips) == 1]
        
        #define if the new baked layer is going to be additive or replace
        additive = False
        
        if obj.als.direction == 'UP' and 'REPLACE' not in blendings and obj.als.baketype == 'AL' and layer_index:
            if 'COMBINE' in blendings: 
                blend = 'COMBINE'
            else:
                blend = 'ADD'
                additive = True
        else:
            blend = 'REPLACE'
        
        mute_rec = mute_unbaked_layers(layer_index, nla_tracks, additive)

        fcu_keys = smart_bake(context)

        if obj.als.operator == 'MERGE':
            if obj.als.direction == 'DOWN':     
                obj.als.layer_index = 0
            baked_layer = None
            strip = anim_data.nla_tracks[obj.als.layer_index].strips[0]
            action = strip.action
            if hasattr(strip, 'action_slot'):
                action_slot = strip.action_slot
            if action is not None: action_name = action.name
            
        #if baking to a new layer then setup the new index and layer
        elif obj.als.operator == 'NEW':
            self.actioncopy = False
            if  obj.als.direction == 'UP' and additive and 'REPLACE' in blendings:
                obj.als.layer_index =  layer_index + blendings.index('REPLACE') - 1
            elif obj.als.direction == 'UP' or obj.als.direction == 'ALL':
                obj.als.layer_index = len(obj.Anim_Layers)-1

            layer_names = [layer.name for layer in obj.Anim_Layers]
            baked_layer = anim_layers.add_animlayer(layer_name = anim_layers.unique_name(layer_names, 'Baked_Layer') , duplicate = False, index = obj.als.layer_index, blend_type = blend)
            anim_layers.register_layers(obj, nla_tracks)
            
            obj.als.layer_index += 1
        
        #remove subsciption again after adding a layer there was new subsciption applied
        subscriptions.subscriptions_remove()

        track = nla_tracks[obj.als.layer_index]
        #use internal bake
        if obj.als.baketype =='NLA':
            modifier_rec, extrapolations = mute_modifiers(obj, nla_tracks)
            if obj.als.smartbake and not obj.als.mergefcurves:
                #apply the last frame of the smart bake instead of the whole action when it has a smaller value
                smart_end = max(([value.frame for fcu_values in fcu_keys.values() for value in fcu_values]))
                if smart_end < frame_end : frame_end = smart_end
            if obj.type == 'ARMATURE':
                if bpy.app.version < (4, 0, 0) :
                    b_layers = obj.data.layers
                    layers_rec = invisible_layers(b_layers)
                else:
                    b_layers = None
                    layers_rec = dict()
                    for col in obj.data.collections:
                        layers_rec.update({col : col.is_visible})
                        col.is_visible = True
                # Select the bones from all the layers
                self.shift = True
                select_keyframed_bones(self, context, obj)
                        
                constraint_rec = mute_constraints(obj)
            if obj.als.onlyselected:
                bake_type = {'POSE'}
            else:
                bake_type = {'OBJECT', 'POSE'}
            if obj.als.smartbake:
                self.step = 1
            
            if not obj.select_get():
                obj.select_set(True)
            bpy.ops.nla.bake(frame_start = frame_start, frame_end = frame_end, only_selected = True, visual_keying=True, clear_constraints=obj.als.clearconstraints, bake_types = bake_type, step = self.step)
            # anim_data.action.fcurves.update()
            strip = track.strips[0]
            old_action = strip.action
            
            action = anim_data.action
            action_name = old_action.name
            #reset the strip settings, remove old and create new
            track.strips.remove(strip)
            strip = track.strips.new(track.name, 0, action)
            anim_layers.tweak_mode_upper_stack(context, obj, anim_data, enter = False)
            
            if obj.als.smartbake: 
                smartbake_apply(obj, nla_tracks, fcu_keys, extrapolations)
            if obj.type == 'ARMATURE':
                armature_restore(obj, b_layers, layers_rec, constraint_rec)
            unmute_modifiers(obj, nla_tracks, modifier_rec)
            
            # anim_data.action = None
            #bpy.data.actions.remove(old_action)
            if self.actioncopy:
                old_action.name = action_name + '_old'
            else:
                bpy.data.actions.remove(old_action)
            # strip.action.name = action_name
            if blendings.count('COMBINE') == len(blendings) and len(blendings) and obj.als.direction == 'UP':
                track.strips[0].blend_type = 'COMBINE'
            
                
        else: #use anim layers bake
            action = AL_bake(frame_start, frame_end, nla_tracks, fcu_keys, additive, self.step, self.actioncopy, baked_layer)
            anim_layers.tweak_mode_upper_stack(context, obj, anim_data, enter = False)
            track.strips.remove(track.strips[0])
            strip = track.strips.new(track.name, 0, action)
            strip.blend_type = blend
            
        #removing layers after merge
        if obj.als.operator == 'MERGE':
            if hasattr(strip, 'action_slot'):
                if action_slot in strip.action.slots.values():
                    strip.action_slot = action_slot
                else:
                    # During NLA Bake slot doesn't exist need to find a new on
                    strip.action_slot = anim_layers.get_obj_slot(obj, action)

            #reset layer settings
            baked_layer = obj.Anim_Layers[obj.als.layer_index]  
            baked_layer.repeat, baked_layer.speed, baked_layer.offset = 1, 1, 0
            strip.use_sync_length = False
            if baked_layer.custom_frame_range:
                baked_layer.custom_frame_range = False
                baked_layer.frame_start = frame_start
                baked_layer.frame_end = frame_end
                
            if obj.als.baketype == 'AL':
                #Rename the old action with a number
                bpy.data.actions[action_name].use_fake_user = False
                bpy.data.actions[action_name].name = anim_layers.unique_name(bpy.data.actions, action_name)
                #Rename the current action to the old action
                action.name = action_name

            #delete the baked layers except for the base layer
            if obj.als.direction == 'DOWN':
                while layer_index  > 0:
                    nla_tracks.remove(nla_tracks[layer_index])
                    layer_index -= 1
                
            if obj.als.direction == 'UP':
                layer_index += 1
                while layer_index < len(nla_tracks):
                    if additive and nla_tracks[layer_index].strips[0].blend_type == 'REPLACE':
                        break
                    nla_tracks.remove(nla_tracks[layer_index])
            
            if obj.als.direction == 'ALL':
                obj.als.layer_index = 0
                index = 0
                merged_track = nla_tracks[layer_index]
                while len(nla_tracks) > 1:
                    if nla_tracks[index] != merged_track:
                        nla_tracks.remove(nla_tracks[index])
                    else:
                        index += 1
            
        #turn the tracks back on if necessery
        if obj.als.direction != 'ALL':
            for track in nla_tracks:
                if track in mute_rec:
                    track.mute = True
                else:
                    track.mute = False
        action.use_fake_user = True
        anim_layers.register_layers(obj, nla_tracks)
            
        #refresh tweak mode for all objects
        for al in context.scene.AL_objects:
            obj = al.object
            obj.als.layer_index = obj.als.layer_index
        
        subscriptions.frameend_update_callback()
        subscriptions.subscriptions_add(context.scene)
        
        # end = time.time()
        # print("time ", end-start)
        return {'FINISHED'}

def register():
    bpy.utils.register_class(MergeAnimLayerDown)

def unregister():
    bpy.utils.unregister_class(MergeAnimLayerDown)
