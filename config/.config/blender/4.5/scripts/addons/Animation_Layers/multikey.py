# if "bpy" in locals():
#     import importlib
#     if "multikey" in locals():
#         importlib.reload()
        
import bpy
import random
import numpy as np
from mathutils import Quaternion
from . import bake_ops
from . import anim_layers

def attr_default(obj, fcu_key):
    #check if the fcurve source belongs to a bone or obj
    if  fcu_key[0][:10] == 'pose.bones':
        transform = fcu_key[0].split('.')[-1]
        attr = fcu_key[0].split('"')[-2]
        bone = fcu_key[0].split('"')[1]
        source = obj.pose.bones[bone]
        
    #in case of shapekey animation        
    elif fcu_key[0][:10] == 'key_blocks':
        attr = fcu_key[0].split('"')[1]
        shapekey = obj.data.shape_keys.key_blocks[attr]
        return 0 if shapekey.slider_min <= 0 else shapekey.slider_min
    #in case of transforms in object mode
    else:# fcu_key[0] in transform_types:
        source = obj
        transform = fcu_key[0]
        
    #check when it's transform property of Blender
    if transform in source.bl_rna.properties.keys():
        if hasattr(source.bl_rna.properties[transform], 'default_array'):
            if len(source.bl_rna.properties[transform].default_array) > fcu_key[1]:
                attrvalue = source.bl_rna.properties[transform].default_array[fcu_key[1]]
                return attrvalue

    #in case of property on object
    elif fcu_key[0].split('"')[1] in obj.keys():
        attr = fcu_key[0].split('"')[1]

    if 'attr' not in locals():
        return 0  
      
    #since blender 3 access to custom property settings changed
    if attr in source:
        id_attr = source.id_properties_ui(attr).as_dict()
        attrvalue = id_attr['default']
        return attrvalue
    
    return 0

def store_handles(key):
    #storing the distance between the handles bezier to the key value
    handle_r = key.handle_right[1] - key.co[1]
    handle_l = key.handle_left[1] - key.co[1]
    
    return handle_r, handle_l

def apply_handles(key, handle_r, handle_l):
    key.handle_right[1] = key.co[1] + handle_r
    key.handle_left[1] = key.co[1] + handle_l

def filter_properties(obj, fcu):
    'Filter the W X Y Z attributes of the transform properties'
    
    transformations = ["rotation_quaternion","rotation_euler", "location", "scale"]
    #check if the fcurve data path ends with any of the transformations
    if not any(fcu.data_path.endswith(transform) for transform in transformations):
        return True
    transform = fcu.data_path.split('"].')[1] if obj.mode == 'POSE' else fcu.data_path
    index = fcu.array_index
    if 'rotation' in transform:
        if transform == 'rotation_euler':
            index -= 1
        transform = 'rotation'
    transform = 'filter_' + transform
    #in case of channels like bbone_scalein that are no included then return
    if not hasattr(bpy.context.scene.multikey, transform):
        return True
    attr = getattr(bpy.context.scene.multikey, transform)
    return True if attr[index] else False
        
def add_value(key, value):
    if key.select_control_point:
        #store handle values in relative to the keyframe value
        handle_r, handle_l = store_handles(key)              
        
        key.co[1] += value
        apply_handles(key, handle_r, handle_l)
                
#calculate the difference between current value and the fcurve value                
def add_diff(obj, fcurves, path, current_value, eval_array):
    '''Get the difference value and add it to all selected keyframes'''

    if eval_array is None:
        return
    
    array_value = current_value - eval_array

    if not any(array_value):
        return

    for i, value in enumerate(array_value):
        fcu = fcurves.find(path, index = i)
        if fcu is None or not filter_properties(obj, fcu):
            continue
        for key in fcu.keyframe_points:
            add_value(key, value)
        fcu.update()

class ScaleValuesOp(bpy.types.Operator):
    """Modal operator used while scale value is running before release"""
    bl_idname = "anim.multikey_scale_value"
    bl_label = "Scale Values"
    bl_options = {'REGISTER', 'UNDO'}
    
    def invoke(self, context, event):
        #reset the values for dragging
        self.stop = False
        scene = context.scene
        global is_dragging
        is_dragging = True

        self.avg_value = dict()
        #dictionary of the keyframes and their original INITIAL values
        self.keyframes_values = dict()
        self.keyframes_handle_right = dict()
        self.keyframes_handle_left = dict()
        
        #the average value for each fcurve
        self.keyframes_avg_value = dict()

        for obj in context.selected_objects:
            if obj.animation_data.action is None:
                continue
            action = obj.animation_data.action
            
            fcurves = anim_layers.get_fcurves(obj, action)
            for fcu in fcurves:
                if anim_layers.selected_bones_filter(obj, fcu):
                    continue                       
                if not filter_properties(obj, fcu):
                    continue
                
                #avg and value list per fcurve
                avg_value = []
                value_list = []
                for key in fcu.keyframe_points: 
                    if key.select_control_point: 
                        value_list.append(key.co[1])
                        self.keyframes_values.update({key : key.co[1]})
                        self.keyframes_handle_right.update({key : key.handle_right[1]})
                        self.keyframes_handle_left.update({key : key.handle_left[1]})
                        
                if len(value_list)>1:
                    #the average value with the scale property added to it
                    avg_value = sum(value_list) / len(value_list)
                    
                    for key in fcu.keyframe_points: 
                        if key.select_control_point:
                            self.keyframes_avg_value.update({key : avg_value})
                
        if not self.keyframes_avg_value:
            if 'is_dragging' in globals():
                del is_dragging
            scene.multikey['scale'] = 1
            anim_layers.redraw_areas(['VIEW_3D'])
            return('CANCELLED')
                
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}
        
    def modal(self, context, event):
        
        global is_dragging

        try:
            scene = context.scene
            scale = scene.multikey.scale
            #Quit the modal operator when the slider is released
            if self.stop:
                del is_dragging
                scene.multikey['scale'] = 1
                anim_layers.redraw_areas(['VIEW_3D'])
                #modal is being cancelled because of undo issue with the modal running through the property
                return {'FINISHED'}
            if event.value == 'RELEASE':  # Stop the modal on next frame. Don't block the event since we want to exit the field dragging
                self.stop = True
                return {'PASS_THROUGH'}
            
            for key, key_value in self.keyframes_values.items():
                if not key.select_control_point:
                    continue
                if key not in self.keyframes_avg_value:
                    continue
                avg_value = self.keyframes_avg_value[key]
                handle_right_value = self.keyframes_handle_right[key]
                handle_left_value = self.keyframes_handle_left[key]
                
                #add the value of the distance from the average * scale factor
                key.co[1] = avg_value + ((key_value - avg_value)*scale)
                key.handle_right[1] = avg_value + ((handle_right_value - avg_value)*scale)
                key.handle_left[1] = avg_value + ((handle_left_value - avg_value)*scale)

            return {'PASS_THROUGH'}
        
        except Exception as e:
            # Log the error
            print("Error:", e)
            self['scale'] = 1
            self.stop = True
            del is_dragging
            return {'CANCELLED'}
    
def scale_value(self, context):

    if 'is_dragging' in globals():
        if is_dragging:
            return
    
    obj = context.object
    
    if obj is None:
        self['scale'] = 1
        return
    action = obj.animation_data.action
    
    if action is None:
        self['scale'] = 1
        return
    
    if context.mode == 'POSE' and not context.selected_pose_bones:
        self['scale'] = 1
        return
    bpy.ops.anim.multikey_scale_value('INVOKE_DEFAULT')
    
def random_value(self, context):
    
    for obj in context.selected_objects:
        if obj.animation_data.action is None:
            continue
        action = obj.animation_data.action
        fcurves = anim_layers.get_fcurves(obj, action)
        for fcu in fcurves:
            # if obj.mode == 'POSE':
            if anim_layers.selected_bones_filter(obj, fcu):
                continue
            if not filter_properties(obj, fcu):
                continue
            value_list = []
            threshold = bpy.context.scene.multikey.randomness                        
            for key in fcu.keyframe_points: 
                if key.select_control_point == True: 
                    value_list.append(key.co[1])
                    
            if len(value_list) > 0:
                value = max(value_list)- min(value_list)
                for key in fcu.keyframe_points:
                    add_value(key, value * random.uniform(-threshold, threshold))
                fcu.update()
                
    self['randomness'] = 0.1

def evaluate_combine(data_path, added_array, eval_array, array_default, influence):
    
    if 'scale' in data_path:
        eval_array = eval_array * (added_array / array_default) ** influence
    elif 'rotation_quaternion' in data_path:
        #multiply first the influence with the w separatly
        added_array[0] = added_array[0] + (1- added_array[0])*(1 - influence)
        added_array[1:] *= influence 
        eval_array = np.array(Quaternion(eval_array) @ Quaternion(added_array))# ** influence
    #if it's a custom property
    elif 'rotation_euler' not in data_path and 'location' not in data_path:
        eval_array = eval_array + (added_array - array_default) * influence
        
    return eval_array

def evaluate_array(fcurves, fcu_path, frame, array_default = [0, 0, 0]):
    '''Create an array from all the indexes'''
    
    array_len = len(array_default)
    
    #assigning the default array in case 
    fcu_array = array_default.copy()
    #get the missing arrays in case quaternion is not complete
    for i in range(array_len):
        fcu = fcurves.find(fcu_path, index = i)
        if fcu is None:
            continue
        fcu_array[i] = fcu.evaluate(frame)

    return np.array(fcu_array)
    
def evaluate_layers(context, obj, anim_data, fcu, array_default):
    '''Calculate the evaluation of all the layers when using the nla'''
    
    if not hasattr(anim_data, 'nla_tracks') or not anim_data.use_nla:
        return None
    nla_tracks = anim_data.nla_tracks
    if not len(nla_tracks):
        return None
    frame = context.scene.frame_current
    blend_types = {'ADD' : '+', 'SUBTRACT' : '-', 'MULTIPLY' : '*'}
    fcu_path = fcu.data_path
    
    eval_array = array_default.copy()

    for track in nla_tracks:
        if track.mute:
            continue
        if not len(track.strips):
            continue
        for strip in track.strips: 
            if not strip.frame_start <= frame <= strip.frame_end:
                continue
            action = strip.action
            if action is None:
                continue
            blend_type = strip.blend_type 
            
            #get the influence value either from the attribute or the fcurve. function coming from bake
            influence = strip.influence
            if len(strip.fcurves):
                if not strip.fcurves[0].mute and len(strip.fcurves[0].keyframe_points):
                    influence = strip.fcurves[0].evaluate(frame)
            
            #evaluate the frame according to the strip settings
            frame_eval = frame

            #change the frame if the strip is on hold    
            if frame < strip.frame_start:
                if strip.extrapolation == 'HOLD':
                    frame_eval = strip.frame_start
            elif frame >= strip.frame_end:
                if strip.extrapolation == 'HOLD' or strip.extrapolation == 'HOLD_FORWARD':
                    frame_eval = strip.frame_end
                    
            last_frame = strip.frame_start + (strip.frame_end - strip.frame_start) / strip.repeat
            
            if strip.repeat > 1 and (frame) >= last_frame:
                action_range = (strip.action_frame_end * strip.scale - strip.action_frame_start * strip.scale)
                frame_eval = (((frame_eval - strip.frame_start) % (action_range)) + strip.frame_start)
                        
            if strip.use_reverse:
                frame_eval = last_frame - (frame_eval - strip.frame_start)
            offset = (strip.frame_start * 1/strip.scale - strip.action_frame_start) * strip.scale
            frame_eval = strip.frame_start * 1/strip.scale + (frame_eval - strip.frame_start) * 1/strip.scale - offset * 1/strip.scale
            fcurves = anim_layers.get_fcurves(obj, action)
            eval_array = evaluate_blend_type(fcurves, eval_array, fcu_path, frame_eval, influence, array_default, blend_type, blend_types)

    #Adding an extra layer from the action outside and on top of the nla
    tweak_mode = anim_data.use_tweak_mode
    if tweak_mode:
        anim_data.use_tweak_mode = False

    action = anim_data.action
    if action:
        influence = anim_data.action_influence
        blend_type = anim_data.action_blend_type
        
        fcurves = anim_layers.get_fcurves(obj, action)
        eval_array = evaluate_blend_type(fcurves, eval_array, fcu_path, frame, influence, array_default, blend_type, blend_types)
    anim_data.use_tweak_mode = tweak_mode

    return eval_array


def evaluate_blend_type(fcurves, eval_array, fcu_path, frame, influence,
                         array_default, blend_type, blend_types):
    '''Calculate the value based on the blend type'''

    fcu_array = evaluate_array(fcurves, fcu_path, frame, array_default)
    if fcu_array is None:
        return eval_array
        ###EVALUATION###
    if blend_type =='COMBINE':
        if 'location' in fcu_path or 'rotation_euler' in fcu_path:
            blend_type = 'ADD'

    if blend_type =='REPLACE':
        eval_array = eval_array * (1 - influence) + fcu_array * influence
    elif blend_type =='COMBINE':
        eval_array = evaluate_combine(fcu_path, fcu_array, eval_array, array_default, influence)
    else:
        eval_array = eval('eval_array' + blend_types[blend_type] + 'fcu_array' + '*' + str(influence))

    return eval_array

def evaluate_value(self, context):

    for obj in context.selected_objects:
        
        anim_data = obj.animation_data
        if anim_data is None:
            return
        if anim_data.action is None:
            return
        
        action = obj.animation_data.action
        # fcu_paths = []
        transformations = ["rotation_quaternion","rotation_euler", "location", "scale"]
        if obj.mode == 'POSE': 
            bonelist = context.selected_pose_bones if obj.als.onlyselected else obj.pose.bones
        
        fcurves = anim_layers.get_fcurves(obj, action)
        for fcu in fcurves:
            # if fcu in fcu_paths:
            #     continue
            current_value = None
            if not filter_properties(obj, fcu):
                continue
            if obj.mode == 'POSE':   
                if anim_layers.selected_bones_filter(obj, fcu):
                    continue

                for bone in bonelist:
                    #find the fcurve of the bone
                    if fcu.data_path.rfind(bone.name) != 12 or fcu.data_path[12 + len(bone.name)] != '"': 
                        continue
                    path_split = fcu.data_path.split('"].')

                    if len(path_split) <= 1:
                        continue
                    else:
                        transform = fcu.data_path.split('"].')[1]
                    if transform not in transformations:
                        continue
                    
                    current_value = getattr(obj.pose.bones[bone.name], transform)
            else:
                transform = fcu.data_path
                current_value = getattr(obj, transform)
            #In case it was completly filtered out and not current value available
            if not current_value:
                continue

            array_default = np.array(bake_ops.attr_default(obj, (fcu.data_path, fcu.array_index)))
            eval_array = evaluate_layers(context, obj, anim_data, fcu, array_default)
            if eval_array is None:
                fcurves = anim_layers.get_fcurves(obj, action)
                eval_array = evaluate_array(fcurves, fcu.data_path, context.scene.frame_current, array_default)
            
            #calculate the difference between current value and the fcurve value
            add_diff(obj, fcurves, fcu.data_path, np.array(current_value), eval_array)
                            
class MULTIKEY_OT_Multikey(bpy.types.Operator):
    """Edit all selected keyframes"""
    bl_label = "Edit Selected Keyframes"
    bl_idname = "fcurves.multikey"
    bl_options = {'REGISTER', 'UNDO'}
    # bl_description = ('Select keyframes, move your bone or objecet and press the operator. Does not work with Autokey')

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.animation_data and bpy.context.scene.tool_settings.use_keyframe_insert_auto == False
      
    def execute(self, context):
        evaluate_value(self, context)
        return {'FINISHED'} 
 
class MultikeyProperties(bpy.types.PropertyGroup):
    
    #selectedbones: bpy.props.BoolProperty(name="Affect only selected bones", description="Affect only selected bones", default=True, options={'HIDDEN'})
    #handletype: bpy.props.BoolProperty(name="Keep handle types", description="Keep handle types", default=False, options={'HIDDEN'})
    scale: bpy.props.FloatProperty(name="Scale Values Factor", description="Scale percentage from the average value", default=1.0, soft_max = 10, soft_min = -10, step=0.1, precision = 3, update = scale_value)
    randomness: bpy.props.FloatProperty(name="Randomness", description="Random Threshold of keyframes", default=0.1, min=0.0, max = 1.0, update = random_value)
    # is_dragging: bpy.props.BoolProperty(default = False)

    #filters
    filter_location: bpy.props.BoolVectorProperty(name="Location", description="Filter Location properties", default=(True, True, True), size = 3, options={'HIDDEN'})
    filter_rotation: bpy.props.BoolVectorProperty(name="Rotation", description="Filter Rotation properties", default=(True, True, True, True), size = 4, options={'HIDDEN'})
    filter_scale: bpy.props.BoolVectorProperty(name="Scale", description="Filter Scale properties", default=(True, True, True), size = 3, options={'HIDDEN'})

class FilterProperties(bpy.types.Operator):
    """Filter Location Rotation and Scale Properties"""
    bl_idname = "fcurves.filter"
    bl_label = "Filter Properties               W    X    Y    Z"
    bl_options = {'REGISTER', 'UNDO'}
    
    def invoke(self, context, event):
        wm = context.window_manager
        return wm.invoke_props_dialog(self, width = 200)
    
    def draw(self, context):
        layout = self.layout
        row = layout.row()
        row.label(text = 'Location')
        row.prop(context.scene.multikey, 'filter_location', text = '')
        row = layout.row()
        row.label(text = 'Rotation')
        row.prop(context.scene.multikey, 'filter_rotation', text = '')
        row = layout.row()
        row.label(text = 'Scale')
        row.prop(context.scene.multikey, 'filter_scale', text = '')
    
    def execute(self, context):
        return {'CANCELLED'}
              
classes = (MultikeyProperties, FilterProperties, MULTIKEY_OT_Multikey, ScaleValuesOp)


def register():
    from bpy.utils import register_class
    for cls in classes:
        register_class(cls)
    bpy.types.Scene.multikey = bpy.props.PointerProperty(type = MultikeyProperties, options={'LIBRARY_EDITABLE'}, override = {'LIBRARY_OVERRIDABLE'})

def unregister():
    from bpy.utils import unregister_class
    for cls in classes:
        unregister_class(cls)
    del bpy.types.Scene.multikey                            