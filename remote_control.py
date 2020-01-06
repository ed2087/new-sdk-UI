#!/usr/bin/env python3

# Copyright (c) 2018 Anki, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License in the file LICENSE.txt or at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Control Vector using a webpage on your computer.

This example lets you control Vector by Remote Control, using a webpage served by Flask.
"""

import io
import json
import sys
import time
from enum import Enum
from lib import flask_helpers

import anki_vector
from anki_vector import util
from anki_vector import annotate

try:
    from flask import Flask, request
except ImportError:
    sys.exit("Cannot import from flask: Do `pip3 install --user flask` to install")

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit("Cannot import from PIL: Do `pip3 install --user Pillow` to install")


def create_default_image(image_width, image_height, do_gradient=False):
    """Create a place-holder PIL image to use until we have a live feed from Vector"""
    image_bytes = bytearray([0x70, 0x70, 0x70]) * image_width * image_height

    if do_gradient:
        i = 0
        for y in range(image_height):
            for x in range(image_width):
                image_bytes[i] = int(255.0 * (x / image_width))   # R
                image_bytes[i + 1] = int(255.0 * (y / image_height))  # G
                image_bytes[i + 2] = 0                                # B
                i += 3

    image = Image.frombytes('RGB', (image_width, image_height), bytes(image_bytes))
    return image


flask_app = Flask(__name__)
_default_camera_image = create_default_image(320, 240)
_is_mouse_look_enabled_by_default = False


def remap_to_range(x, x_min, x_max, out_min, out_max):
    """convert x (in x_min..x_max range) to out_min..out_max range"""
    if x < x_min:
        return out_min
    if x > x_max:
        return out_max
    ratio = (x - x_min) / (x_max - x_min)
    return out_min + ratio * (out_max - out_min)


class DebugAnnotations(Enum):
    DISABLED = 0
    ENABLED_VISION = 1
    ENABLED_ALL = 2


# Annotator for displaying RobotState (position, etc.) on top of the camera feed
class RobotStateDisplay(annotate.Annotator):
    def apply(self, image, scale):
        d = ImageDraw.Draw(image)

        bounds = [3, 0, image.width, image.height]

        def print_line(text_line):
            text = annotate.ImageText(text_line, position=annotate.AnnotationPosition.TOP_LEFT, outline_color='black', color='lightblue')
            text.render(d, bounds)
            TEXT_HEIGHT = 11
            bounds[1] += TEXT_HEIGHT

        robot = self.world.robot  # type: robot.Robot

        # Display the Pose info for the robot
        pose = robot.pose
        print_line('Pose: Pos = <%.1f, %.1f, %.1f>' % pose.position.x_y_z)
        print_line('Pose: Rot quat = <%.1f, %.1f, %.1f, %.1f>' % pose.rotation.q0_q1_q2_q3)
        print_line('Pose: angle_z = %.1f' % pose.rotation.angle_z.degrees)
        print_line('Pose: origin_id: %s' % pose.origin_id)

        # Display the Accelerometer and Gyro data for the robot
        print_line('Accelmtr: <%.1f, %.1f, %.1f>' % robot.accel.x_y_z)
        print_line('Gyro: <%.1f, %.1f, %.1f>' % robot.gyro.x_y_z)


class RemoteControlVector:

    def __init__(self, robot):
        self.vector = robot

        # don't send motor messages if it matches the last setting
        self.last_lift = None
        self.last_head = None
        self.last_wheels = None

        self.drive_forwards = 0
        self.drive_back = 0
        self.turn_left = 0
        self.turn_right = 0
        self.lift_up = 0
        self.lift_down = 0
        self.head_up = 0
        self.head_down = 0

        self.go_fast = 0
        self.go_slow = 0

        self.is_mouse_look_enabled = _is_mouse_look_enabled_by_default
        self.mouse_dir = 0

        all_anim_names = self.vector.anim.anim_list
        all_anim_names.sort()
        self.anim_names = []

        # Hide a few specific test animations that don't behave well
        bad_anim_names = [
            "ANIMATION_TEST",
            "soundTestAnim"]

        for anim_name in all_anim_names:
            if anim_name not in bad_anim_names:
                self.anim_names.append(anim_name)

        default_anims_for_keys = ["anim_turn_left_01",  # 0
                                  "anim_blackjack_victorwin_01",  # 1
                                  "anim_pounce_success_02",  # 2
                                  "anim_feedback_shutup_01",  # 3
                                  "anim_knowledgegraph_success_01",  # 4
                                  "anim_wakeword_groggyeyes_listenloop_01",  # 5
                                  "anim_fistbump_success_01",  # 6
                                  "anim_reacttoface_unidentified_01",  # 7
                                  "anim_rtpickup_loop_10",  # 8
                                  "anim_volume_stage_05"]  # 9

        self.anim_index_for_key = [0] * 10
        kI = 0
        for default_key in default_anims_for_keys:
            try:
                anim_idx = self.anim_names.index(default_key)
            except ValueError:
                print("Error: default_anim %s is not in the list of animations" % default_key)
                anim_idx = kI
            self.anim_index_for_key[kI] = anim_idx
            kI += 1

        all_anim_trigger_names = self.vector.anim.anim_trigger_list
        self.anim_trigger_names = []

        bad_anim_trigger_names = [
            "InvalidAnimTrigger",
            "UnitTestAnim"]

        for anim_trigger_name in all_anim_trigger_names:
            if anim_trigger_name not in bad_anim_trigger_names:
                self.anim_trigger_names.append(anim_trigger_name)

        self.selected_anim_trigger_name = self.anim_trigger_names[0]

        self.action_queue = []
        self.text_to_say = "Hi I'm Vector"

    def set_anim(self, key_index, anim_index):
        self.anim_index_for_key[key_index] = anim_index

    def handle_mouse(self, mouse_x, mouse_y):
        """Called whenever mouse moves
            mouse_x, mouse_y are in in 0..1 range (0,0 = top left, 1,1 = bottom right of window)
        """
        if self.is_mouse_look_enabled:
            mouse_sensitivity = 1.5  # higher = more twitchy
            self.mouse_dir = remap_to_range(mouse_x, 0.0, 1.0, -mouse_sensitivity, mouse_sensitivity)
            self.update_mouse_driving()

            desired_head_angle = remap_to_range(mouse_y, 0.0, 1.0, 45, -25)
            head_angle_delta = desired_head_angle - util.radians(self.vector.head_angle_rad).degrees
            head_vel = head_angle_delta * 0.03
            if self.last_head and head_vel == self.last_head:
                return
            self.last_head = head_vel
            self.vector.motors.set_head_motor(head_vel)

    def set_mouse_look_enabled(self, is_mouse_look_enabled):
        was_mouse_look_enabled = self.is_mouse_look_enabled
        self.is_mouse_look_enabled = is_mouse_look_enabled
        if not is_mouse_look_enabled:
            # cancel any current mouse-look turning
            self.mouse_dir = 0
            if was_mouse_look_enabled:
                self.update_mouse_driving()
                self.update_head()

    def update_drive_state(self, key_code, is_key_down, speed_changed):
        """Update state of driving intent from keyboard, and if anything changed then call update_driving"""
        update_driving = True
        if key_code == ord('W'):
            self.drive_forwards = is_key_down
        elif key_code == ord('S'):
            self.drive_back = is_key_down
        elif key_code == ord('A'):
            self.turn_left = is_key_down
        elif key_code == ord('D'):
            self.turn_right = is_key_down
        else:
            if not speed_changed:
                update_driving = False
        return update_driving

    def update_lift_state(self, key_code, is_key_down, speed_changed):
        """Update state of lift move intent from keyboard, and if anything changed then call update_lift"""
        update_lift = True
        if key_code == ord('R'):
            self.lift_up = is_key_down
        elif key_code == ord('F'):
            self.lift_down = is_key_down
        else:
            if not speed_changed:
                update_lift = False
        return update_lift

    def update_head_state(self, key_code, is_key_down, speed_changed):
        """Update state of head move intent from keyboard, and if anything changed then call update_head"""
        update_head = True
        if key_code == ord('T'):
            self.head_up = is_key_down
        elif key_code == ord('G'):
            self.head_down = is_key_down
        else:
            if not speed_changed:
                update_head = False
        return update_head

    def handle_key(self, key_code, is_shift_down, is_alt_down, is_key_down):
        """Called on any key press or release
           Holding a key down may result in repeated handle_key calls with is_key_down==True
        """

        # Update desired speed / fidelity of actions based on shift/alt being held
        was_go_fast = self.go_fast
        was_go_slow = self.go_slow

        self.go_fast = is_shift_down
        self.go_slow = is_alt_down

        speed_changed = (was_go_fast != self.go_fast) or (was_go_slow != self.go_slow)

        update_driving = self.update_drive_state(key_code, is_key_down, speed_changed)

        update_lift = self.update_lift_state(key_code, is_key_down, speed_changed)

        update_head = self.update_head_state(key_code, is_key_down, speed_changed)

        # Update driving, head and lift as appropriate
        if update_driving:
            self.update_mouse_driving()
        if update_head:
            self.update_head()
        if update_lift:
            self.update_lift()

        # Handle any keys being released (e.g. the end of a key-click)
        if not is_key_down:
            if ord('9') >= key_code >= ord('0'):
                anim_name = self.key_code_to_anim_name(key_code)
                self.queue_action((self.vector.anim.play_animation, anim_name))
            elif key_code == ord(' '):
                self.queue_action((self.vector.behavior.say_text, self.text_to_say))
            elif key_code == ord('X'):
                self.queue_action((self.vector.anim.play_animation_trigger, self.selected_anim_trigger_name))

    def key_code_to_anim_name(self, key_code):
        key_num = key_code - ord('0')
        anim_num = self.anim_index_for_key[key_num]
        anim_name = self.anim_names[anim_num]
        return anim_name

    def func_to_name(self, func):
        if func == self.vector.behavior.say_text:
            return "say_text"
        if func == self.vector.anim.play_animation:
            return "play_anim"
        return "UNKNOWN"

    def action_to_text(self, action):
        func, args = action
        return self.func_to_name(func) + "( " + str(args) + " )"

    def action_queue_to_text(self, action_queue):
        out_text = ""
        i = 0
        for action in action_queue:
            out_text += "[" + str(i) + "] " + self.action_to_text(action)
            i += 1
        return out_text

    def queue_action(self, new_action):
        if len(self.action_queue) > 10:
            self.action_queue.pop(0)
        self.action_queue.append(new_action)

    def update(self):
        """Try and execute the next queued action"""
        if self.action_queue:
            queued_action, action_args = self.action_queue[0]
            if queued_action(action_args):
                self.action_queue.pop(0)

    def pick_speed(self, fast_speed, mid_speed, slow_speed):
        if self.go_fast:
            if not self.go_slow:
                return fast_speed
        elif self.go_slow:
            return slow_speed
        return mid_speed

    def update_lift(self):
        lift_speed = self.pick_speed(8, 4, 2)
        lift_vel = (self.lift_up - self.lift_down) * lift_speed
        if self.last_lift and lift_vel == self.last_lift:
            return
        self.last_lift = lift_vel
        self.vector.motors.set_lift_motor(lift_vel)

    def update_head(self):
        if not self.is_mouse_look_enabled:
            head_speed = self.pick_speed(2, 1, 0.5)
            head_vel = (self.head_up - self.head_down) * head_speed
            if self.last_head and head_vel == self.last_head:
                return
            self.last_head = head_vel
            self.vector.motors.set_head_motor(head_vel)

    def update_mouse_driving(self):
        drive_dir = (self.drive_forwards - self.drive_back)

        turn_dir = (self.turn_right - self.turn_left) + self.mouse_dir
        if drive_dir < 0:
            # It feels more natural to turn the opposite way when reversing
            turn_dir = -turn_dir

        forward_speed = self.pick_speed(150, 75, 50)
        turn_speed = self.pick_speed(100, 50, 30)

        l_wheel_speed = (drive_dir * forward_speed) + (turn_speed * turn_dir)
        r_wheel_speed = (drive_dir * forward_speed) - (turn_speed * turn_dir)

        wheel_params = (l_wheel_speed, r_wheel_speed, l_wheel_speed * 4, r_wheel_speed * 4)
        if self.last_wheels and wheel_params == self.last_wheels:
            return
        self.last_wheels = wheel_params
        self.vector.motors.set_wheel_motors(*wheel_params)


def get_anim_sel_drop_down(selectorIndex):
    html_text = """<select onchange="handleDropDownSelect(this)" name="animSelector""" + str(selectorIndex) + """">"""
    i = 0
    for anim_name in flask_app.remote_control_vector.anim_names:
        is_selected_item = (i == flask_app.remote_control_vector.anim_index_for_key[selectorIndex])
        selected_text = ''' selected="selected"''' if is_selected_item else ""
        html_text += """<option value=""" + str(i) + selected_text + """>""" + anim_name + """</option>"""
        i += 1
    html_text += """</select>"""
    return html_text


def get_anim_sel_drop_downs():
    html_text = ""
    for i in range(10):
        # list keys 1..9,0 as that's the layout on the keyboard
        key = i + 1 if (i < 9) else 0
        html_text += str(key) + """: """ + get_anim_sel_drop_down(key) + """<br>"""
    return html_text

def get_anim_trigger_sel_drop_down():
    html_text = "x: " # Add keyboard selector
    html_text += """<select onchange="handleAnimTriggerDropDownSelect(this)" name="animTriggerSelector">"""
    for anim_trigger_name in flask_app.remote_control_vector.anim_trigger_names:
        html_text += """<option value=""" + anim_trigger_name + """>""" + anim_trigger_name + """</option>"""
    html_text += """</select>"""
    return html_text

def to_js_bool_string(bool_value):
    return "true" if bool_value else "false"


@flask_app.route("/")
def handle_index_page():
    return """
    <html>
        <head>
            <title>remote_control_vector.py display</title>
            <style>


                        /*///////////////////////////////////////////
                                           GLOBAL CSS
                        ///////////////////////////////////////// */
                        *{
                          margin: 0px;
                          padding: 0px;
                          box-sizing: border-box;
                          -webkit-box-sizing: border-box;
                          -moz-box-sizing: border-box;
                        }

                        body{
                          font-size: 1em;
                          line-height: 1.5;
                          font-family: serif;
                          width: 100%;
                        }

                        a,
                        li{
                          text-decoration: none;
                        }

                        img{
                          display: block;
                          max-width: 100%;
                          max-height: 100%;
                        }

                        button,
                        .fa{
                          cursor: pointer;
                        }

                        li{
                          list-style: none;
                        }

                        button,
                        input,
                        select,
                        a{
                          outline:none;
                        }

                        .fa-instagram {
                          background: #125688;
                          color: white;
                        }
                        /*///////////////////////////////////////////
                                           RECICLED CSS
                        //////////////////////////////////////////*/

                        :root{

                          --padding: 8px 20px;
                          --borderR35px : 35px;
                          --buttonPadding : 15px 10px;

                        }

                        /*///////////////////////////////////////////
                                           main wrap
                        //////////////////////////////////////////*/
                        .vector-wrap{
                          height: 100vh;
                          width: 100vw;
                          position: relative;
                          display: flex;
                          flex-direction: row;
                          background: white;
                        }
                              /* NOTE:  both wraps */
                              .inner-wrap{
                              }

                              /* NOTE: left wrap */
                              #left-wrap{
                                width: auto;
                                padding: 20px;
                                background: rgb(34, 34, 34);
                                -webkit-box-shadow: 6px 0px 13px 5px rgba(98, 98, 98, 0.58);
                                  -moz-box-shadow: 6px 0px 13px 5px rgba(98, 98, 98, 0.58);
                                  box-shadow: 6px 0px 13px 5px rgba(98, 98, 98, 0.58);
                              }
                                  .settings-list{
                                    height: 100%;
                                    padding: 5px;
                                    overflow: auto;
                                    min-width: 420px;
                                  }
                                      .list{
                                        border: solid 1px rgb(173, 255, 47);
                                        padding: 5px 10px;
                                        display: flex;
                                        flex-direction: column;
                                        color: rgb(211, 211, 211);
                                        font-size: 1rem;
                                        margin: 5px 10px;
                                      }
                                            .list h2{
                                              font-size: 1.5rem;
                                              text-align: center;
                                              color: rgb(173, 255, 47);
                                            }
                                            .list strong{
                                              font-size: 1.2rem;
                                              margin: 5px;
                                              text-align: center;
                                            }
                                            .list span{
                                              padding: 5px 0;
                                            }
                                            .list b{
                                              color: rgb(173, 255, 47);
                                            }
                                            .list input{
                                               padding: 5px 10px;
                                               border-radius: 25px;
                                               width: 100%;
                                               margin: 5px 0;
                                            }
                                            .list .button-wrap{
                                              text-align: center;
                                            }
                                                .list .button-wrap button{
                                                   padding: 5px 30px;
                                                   border-radius: 25px;
                                                   background: none;
                                                   color: white;
                                                   border: outset 2px rgb(228, 228, 228);
                                                }
                                                    .list .button-wrap button:hover{
                                                      border: outset 2px rgb(173, 255, 47);
                                                    }
                                            .list select{
                                              border: solid 2px rgb(173, 255, 47);
                                              margin: 5px 0;
                                              padding: 5px;
                                              background: none;
                                              color: white;
                                              width: 100%;
                                            }
                                                    .list select option {
                                                            color:black;
                                                    }
                              /* NOTE: right wrap */
                              #right-wrap{
                                width: 100%;
                                padding: 20px;
                                display: flex;
                                justify-content: center;
                                align-items: center;
                                flex-direction: column;
                              }
                                  .img-wrap{
                                    height: 40vh;
                                    width: 40vh;
                                  }
                                      #vectorImageId{
                                        display: block;
                                        max-width: 100%;
                                        max-height: 100%;
                                        width: 100%;
                                        height: 100%;
                                        border: none;
                                        -webkit-box-shadow: 0px 0px 13px 2px rgba(98, 98, 98, 0.62);
                                          -moz-box-shadow: 0px 0px 13px 2px rgba(98, 98, 98, 0.62);
                                          box-shadow: 0px 0px 13px 2px rgba(98, 98, 98, 0.62);
                                      }


            </style>
        </head>
        <body>
                    <div class="vector-wrap">

                    <div class="inner-wrap" id="left-wrap">
                        <ul class="settings-list">
                            <li class="list">
                              <h2>Controls</h2>
                            </li>
                            <li class="list">
                              <strong>Drive Keys</strong>
                              <span><b>( W ) =</b> Forwards</span>
                              <span><b>( A ) =</b> Left</span>
                              <span><b>( S ) =</b> Back</span>
                              <span><b>( D ) =</b> Right</span>
                            </li>
                            <li class="list">
                              <strong>Head Keys</strong>
                              <span><b>( T ) =</b> Head Up</span>
                              <span><b>( G ) =</b> Head Down</span>
                            </li>
                            <li class="list">
                              <strong>Fork Lift Keys</strong>
                              <span><b>( R ) =</b> Lift Up</span>
                              <span><b>( F ) =</b> Lift Down</span>
                            </li>
                            <li class="list">
                              <strong>Talk Keys</strong>
                              <span>
                                <input type="text" name="sayText" id="sayTextId" value=\"""" + flask_app.remote_control_vector.text_to_say + """\" onchange=handleTextInput(this)>
                              </span>
                              <span><b>( Space ) = </b> Vector Talk</span>
                            </li>
                            <li class="list">
                            </li>
                            <li class="list">
                              <strong>( Shift ) Key</strong>
                              <span>Hold <b>Shift</b> to Move Faster <b>(Driving, Head and Lift)</b></span>
                            </li>
                            <li class="list">
                              <strong>( Alt ) Key</strong>
                              <span>Hold <b>Alt</b> to Move Slower <b>(Driving, Head and Lift)</b></span>
                            </li>
                            <li class="list">
                              <strong>( Q ) Key</strong>
                              <span>Press <b> Q </b> To Toggle's Mouse Look Control</span>
                              <span class="button-wrap">
                                 <button class="active-b" id="mouseLookId" onClick=onMouseLookButtonClicked(this)>Activate</button>
                              </span>
                            </li>
                            <li class="list">
                              <strong>( P ) Key</strong>
                              <span>Press <b> P </b> To Toggle's Free Play mode</span>
                              <span class="button-wrap">
                                 <button class="active-b" id="freeplayId" onClick=onFreeplayButtonClicked(this)>Activate</button>
                              </span>
                            </li>
                            <li class="list">
                              <strong>( O ) Key</strong>
                              <span>Press <b> O </b> To Debug Annotations mode</span>
                              <span class="button-wrap">
                                 <button class="active-b" id="debugAnnotationsId" onClick=onDebugAnnotationsButtonClicked(this)>Activate</button>
                              </span>
                            </li>
                            <li class="list">
                              <strong>Play Animations</strong>
                            </li>
                            <li class="list">
                               """ + get_anim_sel_drop_downs() + """
                            </li>
                            <li class="list">
                              <strong>Animation Triggers</strong>
                            </li>
                            <li class="list">
                              """ + get_anim_trigger_sel_drop_down() + """
                            </li>
                        </ul>
                    </div>

                    <div class="inner-wrap" id="right-wrap">
                        <div id="vectorImageMicrosoftWarning"></div>
                        <div class="img-wrap">
                            <img src="vectorImage" id="vectorImageId" width=640 height=480>
                        </div>
                        <div id="DebugInfoId"></div>
                    </div>

               </div>

            <script type="text/javascript">
                var gLastClientX = -1
                var gLastClientY = -1
                var gIsMouseLookEnabled = """ + to_js_bool_string(_is_mouse_look_enabled_by_default) + """
                var gAreDebugAnnotationsEnabled = """+ str(flask_app.display_debug_annotations) + """
                var gIsFreeplayEnabled = false
                var gUserAgent = window.navigator.userAgent;
                var gIsMicrosoftBrowser = gUserAgent.indexOf('MSIE ') > 0 || gUserAgent.indexOf('Trident/') > 0 || gUserAgent.indexOf('Edge/') > 0;
                var gSkipFrame = false;

                if (gIsMicrosoftBrowser) {
                    document.getElementById("vectorImageMicrosoftWarning").style.display = "block";
                }

                function postHttpRequest(url, dataSet)
                {
                    var xhr = new XMLHttpRequest();
                    xhr.open("POST", url, true);
                    xhr.send( JSON.stringify( dataSet ) );
                }

                function updateVector()
                {
                    console.log("Updating log")
                    removeExtra()
                    if (gIsMicrosoftBrowser && !gSkipFrame) {
                        // IE doesn't support MJPEG, so we need to ping the server for more images.
                        // Though, if this happens too frequently, the controls will be unresponsive.
                        gSkipFrame = true;
                        document.getElementById("vectorImageId").src="vectorImage?" + (new Date()).getTime();
                    } else if (gSkipFrame) {
                        gSkipFrame = false;
                    }
                    var xhr = new XMLHttpRequest();
                    xhr.onreadystatechange = function() {
                        if (xhr.readyState == XMLHttpRequest.DONE) {
                            document.getElementById("DebugInfoId").innerHTML = xhr.responseText
                        }
                    }

                    xhr.open("POST", "updateVector", true);
                    xhr.send( null );
                }
                setInterval(updateVector , 60);

                function updateButtonEnabledText(button, isEnabled)
                {
                    button.firstChild.data = isEnabled ? "Enabled" : "Disabled";
                }

                function onMouseLookButtonClicked(button)
                {
                    gIsMouseLookEnabled = !gIsMouseLookEnabled;
                    updateButtonEnabledText(button, gIsMouseLookEnabled);
                    isMouseLookEnabled = gIsMouseLookEnabled
                    postHttpRequest("setMouseLookEnabled", {isMouseLookEnabled})
                }

                function updateDebugAnnotationButtonEnabledText(button, isEnabled)
                {
                    switch(gAreDebugAnnotationsEnabled)
                    {
                    case 0:
                        button.firstChild.data = "Disabled";
                        break;
                    case 1:
                        button.firstChild.data = "Enabled (vision)";
                        break;
                    case 2:
                        button.firstChild.data = "Enabled (all)";
                        break;
                    default:
                        button.firstChild.data = "ERROR";
                        break;
                    }
                }

                function onDebugAnnotationsButtonClicked(button)
                {
                    gAreDebugAnnotationsEnabled += 1;
                    if (gAreDebugAnnotationsEnabled > 2)
                    {
                        gAreDebugAnnotationsEnabled = 0
                    }
                    updateDebugAnnotationButtonEnabledText(button, gAreDebugAnnotationsEnabled)
                    areDebugAnnotationsEnabled = gAreDebugAnnotationsEnabled
                    postHttpRequest("setAreDebugAnnotationsEnabled", {areDebugAnnotationsEnabled})
                }

                function onFreeplayButtonClicked(button)
                {
                    gIsFreeplayEnabled = !gIsFreeplayEnabled;
                    updateButtonEnabledText(button, gIsFreeplayEnabled);
                    isFreeplayEnabled = gIsFreeplayEnabled
                    postHttpRequest("setFreeplayEnabled", {isFreeplayEnabled})
                }

                updateButtonEnabledText(document.getElementById("mouseLookId"), gIsMouseLookEnabled);
                updateButtonEnabledText(document.getElementById("freeplayId"), gIsFreeplayEnabled);
                updateDebugAnnotationButtonEnabledText(document.getElementById("debugAnnotationsId"), gAreDebugAnnotationsEnabled);

                function handleDropDownSelect(selectObject)
                {
                    selectedIndex = selectObject.selectedIndex
                    itemName = selectObject.name
                    postHttpRequest("dropDownSelect", {selectedIndex, itemName});
                }

                function handleAnimTriggerDropDownSelect(selectObject)
                {
                    animTriggerName = selectObject.value
                    postHttpRequest("animTriggerDropDownSelect", {animTriggerName});
                }

                function handleKeyActivity (e, actionType)
                {
                    var keyCode  = (e.keyCode ? e.keyCode : e.which);
                    var hasShift = (e.shiftKey ? 1 : 0)
                    var hasCtrl  = (e.ctrlKey  ? 1 : 0)
                    var hasAlt   = (e.altKey   ? 1 : 0)

                    if (actionType=="keyup")
                    {
                        if (keyCode == 79) // 'O'
                        {
                            // Simulate a click of the debug annotations button
                            onDebugAnnotationsButtonClicked(document.getElementById("debugAnnotationsId"))
                        }
                        else if (keyCode == 80) // 'P'
                        {
                            // Simulate a click of the freeplay button
                            onFreeplayButtonClicked(document.getElementById("freeplayId"))
                        }
                        else if (keyCode == 81) // 'Q'
                        {
                            // Simulate a click of the mouse look button
                            onMouseLookButtonClicked(document.getElementById("mouseLookId"))
                        }
                    }

                    postHttpRequest(actionType, {keyCode, hasShift, hasCtrl, hasAlt})
                }

                function handleMouseActivity (e, actionType)
                {
                    var clientX = e.clientX / document.body.clientWidth  // 0..1 (left..right)
                    var clientY = e.clientY / document.body.clientHeight // 0..1 (top..bottom)
                    var isButtonDown = e.which && (e.which != 0) ? 1 : 0
                    var deltaX = (gLastClientX >= 0) ? (clientX - gLastClientX) : 0.0
                    var deltaY = (gLastClientY >= 0) ? (clientY - gLastClientY) : 0.0
                    gLastClientX = clientX
                    gLastClientY = clientY

                    postHttpRequest(actionType, {clientX, clientY, isButtonDown, deltaX, deltaY})
                }

                function handleTextInput(textField)
                {
                    textEntered = textField.value
                    postHttpRequest("sayText", {textEntered})
                }

                document.addEventListener("keydown", function(e) { handleKeyActivity(e, "keydown") } );
                document.addEventListener("keyup",   function(e) { handleKeyActivity(e, "keyup") } );

                document.addEventListener("mousemove",   function(e) { handleMouseActivity(e, "mousemove") } );

                function stopEventPropagation(event)
                {
                    if (event.stopPropagation)
                    {
                        event.stopPropagation();
                    }
                    else
                    {
                        event.cancelBubble = true
                    }
                }

                document.getElementById("sayTextId").addEventListener("keydown", function(event) {
                    stopEventPropagation(event);
                } );
                document.getElementById("sayTextId").addEventListener("keyup", function(event) {
                    stopEventPropagation(event);
                } );

                const removeExtra = () =>{
                   let x = document.querySelectorAll(".list br");
                   for (var i = 0; i < x.length; i++) {
                         x[i].remove();
                    }
                }
            </script>

        </body>
    </html>
    """


def get_annotated_image():
    image = flask_app.remote_control_vector.vector.camera.latest_image
    if flask_app.display_debug_annotations != DebugAnnotations.DISABLED.value:
        return image.annotate_image()
    return image.raw_image


def streaming_video():
    """Video streaming generator function"""
    while True:
        if flask_app.remote_control_vector:
            image = get_annotated_image()

            img_io = io.BytesIO()
            image.save(img_io, 'PNG')
            img_io.seek(0)
            yield (b'--frame\r\n'
                   b'Content-Type: image/png\r\n\r\n' + img_io.getvalue() + b'\r\n')
        else:
            time.sleep(.1)


def serve_single_image():
    if flask_app.remote_control_vector:
        image = get_annotated_image()
        if image:
            return flask_helpers.serve_pil_image(image)

    return flask_helpers.serve_pil_image(_default_camera_image)


def is_microsoft_browser(req):
    agent = req.user_agent.string
    return 'Edge/' in agent or 'MSIE ' in agent or 'Trident/' in agent


@flask_app.route("/vectorImage")
def handle_vectorImage():
    if is_microsoft_browser(request):
        return serve_single_image()
    return flask_helpers.stream_video(streaming_video)


def handle_key_event(key_request, is_key_down):
    message = json.loads(key_request.data.decode("utf-8"))
    if flask_app.remote_control_vector:
        flask_app.remote_control_vector.handle_key(key_code=(message['keyCode']), is_shift_down=message['hasShift'],
                                                   is_alt_down=message['hasAlt'], is_key_down=is_key_down)
    return ""


@flask_app.route('/mousemove', methods=['POST'])
def handle_mousemove():
    """Called from Javascript whenever mouse moves"""
    message = json.loads(request.data.decode("utf-8"))
    if flask_app.remote_control_vector:
        flask_app.remote_control_vector.handle_mouse(mouse_x=(message['clientX']), mouse_y=message['clientY'])
    return ""


@flask_app.route('/setMouseLookEnabled', methods=['POST'])
def handle_setMouseLookEnabled():
    """Called from Javascript whenever mouse-look mode is toggled"""
    message = json.loads(request.data.decode("utf-8"))
    if flask_app.remote_control_vector:
        flask_app.remote_control_vector.set_mouse_look_enabled(is_mouse_look_enabled=message['isMouseLookEnabled'])
    return ""


@flask_app.route('/setAreDebugAnnotationsEnabled', methods=['POST'])
def handle_setAreDebugAnnotationsEnabled():
    """Called from Javascript whenever debug-annotations mode is toggled"""
    message = json.loads(request.data.decode("utf-8"))
    flask_app.display_debug_annotations = message['areDebugAnnotationsEnabled']
    if flask_app.remote_control_vector:
        if flask_app.display_debug_annotations == DebugAnnotations.ENABLED_ALL.value:
            flask_app.remote_control_vector.vector.camera.image_annotator.enable_annotator('robotState')
        else:
            flask_app.remote_control_vector.vector.camera.image_annotator.disable_annotator('robotState')
    return ""


@flask_app.route('/setFreeplayEnabled', methods=['POST'])
def handle_setFreeplayEnabled():
    """Called from Javascript whenever freeplay mode is toggled on/off"""
    message = json.loads(request.data.decode("utf-8"))
    isFreeplayEnabled = message['isFreeplayEnabled']
    if flask_app.remote_control_vector:
        connection = flask_app.remote_control_vector.vector.conn
        if isFreeplayEnabled:
            connection.release_control()
        else:
            connection.request_control()
    return ""


@flask_app.route('/keydown', methods=['POST'])
def handle_keydown():
    """Called from Javascript whenever a key is down (note: can generate repeat calls if held down)"""
    return handle_key_event(request, is_key_down=True)


@flask_app.route('/keyup', methods=['POST'])
def handle_keyup():
    """Called from Javascript whenever a key is released"""
    return handle_key_event(request, is_key_down=False)


@flask_app.route('/dropDownSelect', methods=['POST'])
def handle_dropDownSelect():
    """Called from Javascript whenever an animSelector dropdown menu is selected (i.e. modified)"""
    message = json.loads(request.data.decode("utf-8"))

    item_name_prefix = "animSelector"
    item_name = message['itemName']

    if flask_app.remote_control_vector and item_name.startswith(item_name_prefix):
        item_name_index = int(item_name[len(item_name_prefix):])
        flask_app.remote_control_vector.set_anim(item_name_index, message['selectedIndex'])

    return ""

@flask_app.route('/animTriggerDropDownSelect', methods=['POST'])
def handle_animTriggerDropDownSelect():
    """Called from Javascript whenever the animTriggerSelector dropdown menu is selected (i.e. modified)"""
    message = json.loads(request.data.decode("utf-8"))
    selected_anim_trigger_name = message['animTriggerName']
    flask_app.remote_control_vector.selected_anim_trigger_name = selected_anim_trigger_name
    return ""

@flask_app.route('/sayText', methods=['POST'])
def handle_sayText():
    """Called from Javascript whenever the saytext text field is modified"""
    message = json.loads(request.data.decode("utf-8"))
    if flask_app.remote_control_vector:
        flask_app.remote_control_vector.text_to_say = message['textEntered']
    return ""


@flask_app.route('/updateVector', methods=['POST'])
def handle_updateVector():
    if flask_app.remote_control_vector:
        flask_app.remote_control_vector.update()
        action_queue_text = ""
        i = 1
        for action in flask_app.remote_control_vector.action_queue:
            action_queue_text += str(i) + ": " + flask_app.remote_control_vector.action_to_text(action) + "<br>"
            i += 1

        return "Action Queue:<br>" + action_queue_text + "\n"
    return ""

def run():
    args = util.parse_command_args()

    with anki_vector.AsyncRobot(args.serial, enable_face_detection=True, enable_custom_object_detection=True) as robot:
        flask_app.remote_control_vector = RemoteControlVector(robot)
        flask_app.display_debug_annotations = DebugAnnotations.ENABLED_ALL.value

        robot.camera.init_camera_feed()
        robot.behavior.drive_off_charger()
        robot.camera.image_annotator.add_annotator('robotState', RobotStateDisplay)

        flask_helpers.run_flask(flask_app)


if __name__ == '__main__':
    try:
        run()
    except KeyboardInterrupt as e:
        pass
    except anki_vector.exceptions.VectorConnectionException as e:
        sys.exit("A connection error occurred: %s" % e)
