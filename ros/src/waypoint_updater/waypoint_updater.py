#!/usr/bin/env python

import rospy
from copy import deepcopy
from std_msgs.msg import Bool
from geometry_msgs.msg import PoseStamped, TwistStamped
from styx_msgs.msg import Lane, TrafficLightArray, TrafficLight, Waypoint
from scipy.spatial import KDTree
import numpy as np
import math
import yaml
from speed_calculator import SpeedCalculator

'''
This node will publish waypoints from the car's current position to some `x` distance ahead.

As mentioned in the doc, you should ideally first implement a version which does not care
about traffic lights or obstacles.

Once you have created dbw_node, you will update this node to use the status of traffic lights too.

Please note that our simulator also provides the exact location of traffic lights and their
current status in `/vehicle/traffic_lights` message. You can use this message to build this node
as well as to verify your TL classifier.

TODO (for Yousuf and Aaron): Stopline location for each traffic light.
'''

LOOKAHEAD_WPS = 200  # Number of waypoints we will publish. You can change this number
STOPLINE_WPS_MARGIN = 2  # Number of waypoints to use as a safety margin when stopping at traffic lights.


class WaypointUpdater(object):
    def __init__(self):
        rospy.init_node('waypoint_updater')
        rospy.Subscriber('/current_pose', PoseStamped, self.pose_cb)
        rospy.Subscriber('/current_velocity', TwistStamped, self.velocity_cb)
        rospy.Subscriber('/base_waypoints', Lane, self.waypoints_cb)
        rospy.Subscriber('/vehicle/traffic_lights', TrafficLightArray, self.traffic_lights_cb)
        rospy.Subscriber('/vehicle/dbw_enabled', Bool, self.dbw_enabled_cb)

        self.final_waypoints_pub = rospy.Publisher('final_waypoints', Lane, queue_size=1)

        # ROS topic messages
        self.pose_msg = None
        self.velocity_msg = None
        self.max_velocity = None
        self.base_waypoints_msg = None
        self.traffic_lights_msg = None
        self.dbw_enabled_msg = None

        self.rate = rospy.Rate(50)

        self.waitUntilInit()
        self.loopForEver()

    def waitUntilInit(self):
        """Wait until all subscriptions has provided at least one msg."""
        while not rospy.is_shutdown():
            if None not in (self.pose_msg, self.velocity_msg, self.base_waypoints_msg,
                            self.traffic_lights_msg, self.dbw_enabled_msg):
                self.wp_calc = WaypointCalculator(self.base_waypoints_msg, self.load_stop_line_positions())
                break
            self.rate.sleep()

    def load_stop_line_positions(self):
        traffic_light_config_string = rospy.get_param("/traffic_light_config")
        traffic_light_config = yaml.load(traffic_light_config_string)
        return traffic_light_config['stop_line_positions']

    def loopForEver(self):
        while not rospy.is_shutdown():
            if self.dbw_enabled_msg.data:
                waypoints = self.wp_calc.calc_waypoints(self.pose_msg, self.velocity_msg, self.traffic_lights_msg)
                self.publish_waypoints(waypoints)
            else:
                self.wp_calc.reset()
            self.rate.sleep()

    def publish_waypoints(self, waypoints):
        final_waypoints_msg = Lane()
        final_waypoints_msg.header = self.base_waypoints_msg.header
        final_waypoints_msg.waypoints = waypoints
        self.final_waypoints_pub.publish(final_waypoints_msg)

    def pose_cb(self, msg):
        """Callback for /current_pose topic"""
        self.pose_msg = msg

    def velocity_cb(self, msg):
        """Callback for /current_velocity topic"""
        self.velocity_msg = msg

    def waypoints_cb(self, msg):
        """Callback for /base_waypoints topic"""
        self.base_waypoints_msg = msg

    def traffic_lights_cb(self, msg):
        """Callback for /vehicle/traffic_lights topic"""
        self.traffic_lights_msg = msg

    def traffic_cb(self, msg):
        # TODO: Callback for /traffic_waypoint message. Implement
        pass

    def dbw_enabled_cb(self, msg):
        self.dbw_enabled_msg = msg


class WaypointCalculator(object):
    def __init__(self, base_waypoints_msg, stop_line_positions):
        self.base_waypoints_msg = base_waypoints_msg
        self.previous_first_idx = None
        self.waypoints = None
        self.wp_search = WaypointSearch(self.base_waypoints_msg.waypoints)

        # Calculate the waypoints behind closest behind the stoplines (with some margin).
        self.stop_line_wp_indices = [self.wp_search.get_closest_waypoint_idx_behind(x, y) - STOPLINE_WPS_MARGIN
                                     for x, y in stop_line_positions]
        self.__set_max_velocity()

    def reset(self):
        """Reset internal state.

        Call this method when drive-by-wire is reactivated after manual control.
        """
        # Let preceding_waypoint be calculated from current vehicle state, instead of using some out-dated index.
        self.previous_first_idx = None

    def __set_max_velocity(self):
        """Set the max_velocity according to velocities in the base waypoints.

          All base waypoints should have the same velocity (in this project). This assumption is also verified to be
          correct.
        """
        self.max_velocity = get_waypoint_velocity(self.base_waypoints_msg.waypoints[0])
        assert(all(self.max_velocity == get_waypoint_velocity(waypoint)
                   for waypoint in self.base_waypoints_msg.waypoints))

    def calc_waypoints(self, pose_msg, velocity_msg, traffic_lights_msg):
        # Extract base_waypoints ahead of vehicle as a starting point for the final_waypoints.
        first_idx = self.wp_search.get_closest_waypoint_idx_ahead(pose_msg.pose.position.x,
                                                                  pose_msg.pose.position.y)
        if self.previous_first_idx is None:
            # Construct previous waypoint from the vehicles initial pose and velocity.
            self.preceding_waypoint = Waypoint()
            self.preceding_waypoint.pose.pose = pose_msg.pose
            self.preceding_waypoint.twist.twist = velocity_msg.twist
            self.preceding_waypoint.acceleration = 0.0
        elif self.previous_first_idx != first_idx:
            # Copy the preceding waypoint before updating the waypoint list.
            self.preceding_waypoint = self.waypoints[first_idx - self.previous_first_idx - 1]

        self.waypoints = deepcopy(self.base_waypoints_msg.waypoints[first_idx:first_idx + LOOKAHEAD_WPS])
        self.previous_first_idx = first_idx

        # Calculate the final_waypoints
        self.__accelerate_to_speed_limit()
        self.__adjust_for_traffic_lights(first_idx, traffic_lights_msg)
        self.__assert_speed_limit()
        return self.waypoints

    def __accelerate_to_speed_limit(self):
        """Set waypoints speed to accelerate the vehicle to max velocity."""

        # Base the acceleration on the velocity from preceding waypoint.
        current_speed = get_waypoint_velocity(self.preceding_waypoint)
        current_acceleration = self.preceding_waypoint.acceleration
        speed_calc = SpeedCalculator(target_speed=self.max_velocity, current_speed=current_speed,
                                     target_acceleration=0.0, current_accleration=current_acceleration,
                                     acceleration_limit=10.0, jerk_limit=10.0)
        distances = self.__calc_distances(self.preceding_waypoint)
        for idx in range(len(self.waypoints)):
            speed = speed_calc.get_speed_at_distance(distances[idx])
            set_waypoint_velocity(self.waypoints, idx, speed)
            acceleration = speed_calc.get_acceleration_at_distance(distances[idx])
            self.waypoints[idx].acceleration = acceleration

    def __calc_distances(self, preceding_waypoint):
        """Calculates the distances from the preceding waypoint to each of the waypoints in the path.

        Parameters
        ----------
        preceding_waypoint : The last visited waypoint behind the vehicle.
        """
        total_dist = 0
        distances = []

        def dl(a, b):
            return math.sqrt((a.x - b.x)**2 + (a.y - b.y)**2 + (a.z - b.z)**2)

        total_dist += dl(preceding_waypoint.pose.pose.position,
                         self.waypoints[0].pose.pose.position)
        distances.append(total_dist)

        for wp1, wp2 in zip(self.waypoints, self.waypoints[1:]):
            total_dist += dl(wp1.pose.pose.position, wp2.pose.pose.position)
            distances.append(total_dist)
        return distances

    def __adjust_for_traffic_lights(self, first_idx, traffic_lights_msg):
        """Adjust the speed to break for traffic lights ahead that not is at state green.

        Parameters
        ----------
        first_idx : The base_waypoint index from where waypoints have been derived.
        traffic_lights_msg : contains traffic light states
        """

        # Check if there is a stop-line along the range of waypoints
        for base_idx in range(first_idx - STOPLINE_WPS_MARGIN, first_idx + LOOKAHEAD_WPS):
            if base_idx in self.stop_line_wp_indices:
                tl_idx = self.stop_line_wp_indices.index(base_idx)
                if traffic_lights_msg.lights[tl_idx].state is not TrafficLight.GREEN:
                    final_idx = max(0, base_idx - first_idx)
                    rospy.loginfo("Stopping at base_idx=%s final_idx=%s", base_idx, final_idx)
                    self.__stop_at_waypoint(final_idx)
                    break
        else:
            rospy.loginfo("No speed adjustments for traffic lights.")

    def __stop_at_waypoint(self, stop_idx):
        """ Reduce speed to stop at stop_idx.

        Parameters
        ----------
        stop_idx : The waypoint index where the vehicle should reach a speed of zero.
        """

        # Calculate the reverse distances from the stop_idx and backwards.
        distances = [0.0] + self.__calc_distances(self.preceding_waypoint)
        distances = [distances[stop_idx] - distance for distance in distances]

        # Try more and more harsh jerk_limits until finding one that decelerate to stop in time.
        # When also unable to stop using the max permitted jerk limit of 10 m/s^3, then deceleration is skipped.
        for jerk_limit in np.arange(2.5, 10.1, 2.5):
            # Create temporary lists with speed and acceleration to be modified below.
            speeds = [get_waypoint_velocity(self.preceding_waypoint)] + \
                     [get_waypoint_velocity(wp) for wp in self.waypoints]
            accs = [self.preceding_waypoint.acceleration] + \
                   [wp.acceleration for wp in self.waypoints]
            temp_stop_idx = stop_idx + 1  # Offset due to inserting the preceding waypoint.

            # Set the speed and acceleration after the temp_stop_idx to zero.
            speeds[temp_stop_idx + 1:] = [0.0] * (len(speeds) - temp_stop_idx - 1)
            accs[temp_stop_idx + 1:] = [0.0] * (len(accs) - temp_stop_idx - 1)

            # Calculate the deceleration backwards from the temp_stop_idx,
            # until reaching a speed that is greater than what was already requested.
            speed_calc = SpeedCalculator(target_speed=self.max_velocity, current_speed=0.0,
                                         target_acceleration=0.0, current_accleration=0.0,
                                         acceleration_limit=10.0, jerk_limit=jerk_limit)

            for idx in range(temp_stop_idx, -1, -1):
                speed = speed_calc.get_speed_at_distance(distances[idx])
                acc = -speed_calc.get_acceleration_at_distance(distances[idx])
                if speed > speeds[idx] or np.isclose(speed, speeds[idx]):
                    rospy.logdebug('Success: jerk_limit %s decelerates from %s m/s (>=%s m/s) at idx %s',
                                   jerk_limit, speed, speeds[idx], idx)
                    stop_possible = True
                    break
                speeds[idx] = speed
                accs[idx] = acc

            else:
                rospy.logdebug('Failed: jerk_limit %s only decelerates from %s m/s (<%s m/s) at idx %s',
                               jerk_limit, speed, get_waypoint_velocity(self.preceding_waypoint), idx)
                stop_possible = False

            if stop_possible:
                for idx in range(len(self.waypoints)):
                    set_waypoint_velocity(self.waypoints, idx, speeds[idx + 1])
                    self.waypoints[idx].acceleration = accs[idx + 1]
                break
        else:
            rospy.loginfo('Unable to stop from %s m/s in %s m',
                          get_waypoint_velocity(self.preceding_waypoint),
                          distances[0])

    def __assert_speed_limit(self):
        """Makes sure we never exceeds max velocity"""
        for i in range(len(self.waypoints)):
            velocity = get_waypoint_velocity(self.waypoints[i])
            if velocity > self.max_velocity:
                # Note that this is not expected to ever happen. However it's perhaps not a fatal error since the
                # velocity can be limited to the max allowed value here.
                rospy.logerr('Calculated velocity %s exceeds max allowed value %s.', velocity, self.max_velocity)
                set_waypoint_velocity(self.waypoints, i, self.max_velocity)


class WaypointSearch(object):
    """Organize waypoints to make it fast to search for the closest to a position"""
    def __init__(self, waypoints):
        # Preprocess waypoints using the k-d tree algorithm
        self.waypoints_2d = [[wp.pose.pose.position.x, wp.pose.pose.position.y] for wp in waypoints]
        self.waypoints_tree = KDTree(self.waypoints_2d)

    def get_closest_waypoint_idx_ahead(self, x, y):
        closest_idx = self.get_closest_waypoint_idx(x, y)

        if not self.__is_closest_idx_ahead(closest_idx, x, y):
            closest_idx = (closest_idx + 1) % len(self.waypoints_2d)
        return closest_idx

    def get_closest_waypoint_idx_behind(self, x, y):
        closest_idx = self.get_closest_waypoint_idx(x, y)
        if self.__is_closest_idx_ahead(closest_idx, x, y):
            closest_idx = (closest_idx - 1) % len(self.waypoints_2d)
        return closest_idx

    def get_closest_waypoint_idx(self, x, y):
        return self.waypoints_tree.query([x, y], 1)[1]

    def __is_closest_idx_ahead(self, closest_idx, x, y):
        """ Check if closest_idx is ahead or behind position"""
        closest_coord = self.waypoints_2d[closest_idx]
        prev_coord = self.waypoints_2d[closest_idx - 1]

        # Equation for hyperplane through closest_coord
        cl_vect = np.array(closest_coord)
        prev_vect = np.array(prev_coord)
        pos_vect = np.array([x, y])
        val = np.dot(cl_vect - prev_vect, pos_vect - cl_vect)
        return val < 0


# Helper functions
def get_waypoint_velocity(waypoint):
    return waypoint.twist.twist.linear.x


def set_waypoint_velocity(waypoints, idx, velocity):
    waypoints[idx].twist.twist.linear.x = velocity


if __name__ == '__main__':
    try:
        WaypointUpdater()
    except rospy.ROSInterruptException:
        rospy.logerr('Could not start waypoint updater node.')
