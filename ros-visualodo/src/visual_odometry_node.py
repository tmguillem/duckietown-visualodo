#!/usr/bin/env python
from __future__ import division

import rospy
import time
import numpy as np
import tf.transformations
import tf2_ros as tf2

from cv_bridge import CvBridge

from sensor_msgs.msg import CameraInfo, CompressedImage, Image
from duckietown_msgs.msg import Twist2DStamped, BoolStamped, FSMState
from nav_msgs.msg import Odometry, Path
from geometry_msgs.msg import Vector3, Quaternion, TransformStamped, Pose, PoseStamped

# import duckietown_utils as dtu
from duckietown_visualodo.algo.visual_odometry import VisualOdometry, VisualOdometryParameters
from duckietown_visualodo.algo.utils import qv_multiply


class VisualOdometryNode:

    def __init__(self):
        # Set parameters
        self.node_name = "Visual Odometry Node"
        self.bridge = CvBridge()

        self.active = True
        self.FSM_mode = None

        self.v = 0.0

        # Pycharm debugging
        rospy.set_param("~veh", "maserati")
        rospy.set_param("~camera_topic", "/maserati/camera_node/image")
        rospy.set_param("~image_transport", "compressed")
        rospy.set_param("~joy_topic", "/maserati/joy_mapper_node/car_cmd")
        rospy.set_param("~camera_info_topic", "/maserati/camera_node/camera_info")
        rospy.set_param("~path", "path")
        rospy.set_param("~odometry", "odometry")
        rospy.set_param("~yaml_root", "/visualodo/visual_odometry_node/parameters/")
        from rosparam import upload_params
        from yaml import load
        f = open("../../lib-visualodo/src/duckietown_visualodo/data/default.yaml", 'r')
        yamlfile = load(f)
        f.close()
        upload_params("/visualodo/visual_odometry_node/", yamlfile)

        self.visual_odometer = VisualOdometry()

        self.images = np.array([None, None])

        # Read launch file parameters
        self.robot_name = rospy.get_param("~veh", None)
        camera_info_topic = rospy.get_param("~camera_info_topic")
        camera_topic = rospy.get_param("~camera_topic")
        joystick_command_topic = rospy.get_param("~joy_topic")
        odometry_topic = rospy.get_param("~odometry")
        path_topic = rospy.get_param("~path")
        self.parameter_file_root = rospy.get_param("~yaml_root")

        self.setup_params()

        # TODO: fix dtu library import(need to test it with docker and the other part running...)
        # if self.robot_name is None:
        #   self.robot_name = dtu.get_current_robot_name()

        self.log_info("Waiting for camera info from topic: " + camera_info_topic)
        self.visual_odometer.get_camera_info(rospy.wait_for_message(camera_info_topic, CameraInfo))
        self.log_info("Camera info received")

        # Set subscribers
        if rospy.get_param("~image_transport") == "compressed":
            rospy.Subscriber(camera_topic + "/compressed", CompressedImage, self.cb_image_c, queue_size=1)
        elif rospy.get_param("~image_transport") == "raw":
            rospy.Subscriber(camera_topic + "/raw", Image, self.cb_image_raw, queue_size=1)
        else:
            raise ValueError("Invalid value for parameter ~image_transport. Should be 'raw' or 'compressed'")
        rospy.Subscriber(joystick_command_topic, Twist2DStamped, self.cv_command, queue_size=1)

        # FSM -> to know wheter to run VO or not
        rospy.Subscriber("~switch", BoolStamped, self.cb_switch, queue_size=1)
        rospy.Subscriber("~fsm_mode", FSMState, self.cb_mode, queue_size=1)

        # Set publishers
        self.path_publisher = rospy.Publisher(path_topic, Path, queue_size=1)
        self.odom_publisher = rospy.Publisher(odometry_topic, Odometry, queue_size=1)

        # Tf broadcaster
        self.transform_broadcaster = tf2.TransformBroadcaster()

        # Transformation from world to duckiebot frame
        self.path = Path()
        self.stacked_position = Vector3(0, 0, 0)
        self.stacked_rotation = tf.transformations.quaternion_from_euler(0, 0, 0)

    def setup_params(self):
        odometry_parameters = [param for param in rospy.get_param_names() if self.parameter_file_root in param]

        for parameter_name in odometry_parameters:
            parameter_value = rospy.get_param(parameter_name)
            parameter_value = ["\'" + parameter_value + "\'" if isinstance(parameter_value, basestring)
                               else str(parameter_value)][0]
            parameter_name = parameter_name.split(self.parameter_file_root)[1]
            self.visual_odometer.set_parameter(parameter_name, parameter_value)
            self.log_info(parameter_name + "=" + parameter_value)

        self.log_info("Parameters file loaded correctly")

    def cb_image_raw(self, image_msg):
        cv_image = self.bridge.imgmsg_to_cv2(image_msg)
        self.cb_image(cv_image)

    def cb_image_c(self, image_msg):
        cv_image = self.bridge.compressed_imgmsg_to_cv2(image_msg)
        self.cb_image(cv_image)

    def cb_image(self, cv_image):

        if not self.active:
            return

        start = time.time()

        vo_transform = self.visual_odometer.get_image_and_trigger_vo(cv_image)

        try:
            t_vec = vo_transform.translation
            z_quaternion = vo_transform.transform.rotation
            current_time = vo_transform.header.stamp

            # t_vec = np.multiply(np.squeeze(t_vec), np.array([1, 0, 0]))
            t = TransformStamped()
            t.header.frame_id = "world"
            t.child_frame_id = "axis"

            # Rotate displacement vector by duckiebot rotation wrt world frame and add it to stacked displacement
            t_vec = np.squeeze(qv_multiply(self.stacked_rotation, t_vec))

            translation = Vector3(t_vec[0], t_vec[1], t_vec[2])
            self.stacked_position = Vector3(
                self.stacked_position.x + translation.x,
                self.stacked_position.y + translation.y,
                self.stacked_position.z + translation.z)

            # Add quaternion rotation to stacked rotation to obtain the rotation transformation between world and
            # duckiebot frames
            quaternion = tf.transformations.quaternion_multiply(self.stacked_rotation, z_quaternion)

            # Store quaternion and transform it to geometry message
            self.stacked_rotation = quaternion
            quaternion = Quaternion(quaternion[0], quaternion[1], quaternion[2], quaternion[3])

            # Broadcast transform
            t.transform.translation = self.stacked_position
            t.transform.rotation = quaternion
            self.transform_broadcaster.sendTransform(t)

            # Create odometry and Path msgs
            odom = Odometry()
            odom.header.stamp = current_time
            odom.header.frame_id = "world"

            self.path.header = odom.header

            # Set the position
            odom.pose.pose = Pose(self.stacked_position, quaternion)
            pose_stamped = PoseStamped()
            pose_stamped.header = odom.header
            pose_stamped.pose = odom.pose.pose
            self.path.poses.append(pose_stamped)

            # set the velocity
            odom.child_frame_id = "base_link"

            # publish the messages
            self.odom_publisher.publish(odom)
            self.path_publisher.publish(self.path)

            rospy.logwarn("TIME: Total time: %s", time.time() - start)
            rospy.logwarn("===================================================")

        except AssertionError as e:
            rospy.logwarn("Error in estimated rotation matrix")
            rospy.logerr(e)
            raise

    def cv_command(self, msg):
        # We just need to pass linear velocity
        self.visual_odometer.get_duckiebot_velocity(msg)

    def log_info(self, s):
        rospy.loginfo('[%s] %s' % (self.node_name, s))

    def onShutdown(self):
        self.log_info("Shutdown.")

    def cb_switch(self, data):
        self.active = switch_msg.data

    def cb_mode(self, data):
        self.FSM_mode = switch_msg.data



if __name__ == '__main__':
    rospy.init_node('visual_odometry_node', anonymous=False)

    visual_odometry_node = VisualOdometryNode()


    rospy.on_shutdown(visual_odometry_node.onShutdown)
    rospy.spin()
