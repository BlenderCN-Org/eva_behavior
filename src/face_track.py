#
# face_track.py -  Registery and tracking of faces
# Copyright (C) 2014,2015  Hanson Robotics
# Copyright (C) 2015 Linas Vepstas
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA

import time

from owyl import blackboard
import rospy
from pi_face_tracker.msg import FaceEvent, Faces
from blender_api_msgs.msg import Target
import tf
import random
import math

# A Face. Currently consists only of an ID number, a 3D location,
# and the time it was last seen.  Should be extended to include
# the size of the face, possibly the location of the eyes, and,
# if possible, the name of the human attached to it ...
class Face:
	def __init__(self, fid, point):
		self.faceid = fid
		self.x = point.x
		self.y = point.y
		self.z = point.z
		self.t = time.time()

# A registery (in-memory database) of all human faces that are currently
# visible, or have been recently seen.  Implements various basic look-at
# actions, including:
# *) turning to face a given face
# *) tracking a face with the eyes
# *) glancing a currrently-visible face, or a face that was recently
#    seen.
#
# Provides the new-face, lost-face data to general-behavior, by putting
# the face data into the owyl blackboard.
class FaceTrack:

	def __init__(self, owyl_bboard):

		print("Starting Face Tracker")
		self.blackboard = owyl_bboard

		# List of currently visible faces
		self.visible_faces = []
		# List of locations of currently visible faces
		self.face_locations = {}

		# List of no longer visible faces, but seen recently.
		self.recent_locations = {}
		# How long (in seconds) to keep around a recently seen, but now
		# lost face. tf does the tracking for us.
		self.RECENT_INTERVAL = 20

		# Current look-at-target
		self.look_at = 0
		self.gaze_at = 0
		self.glance_at = 0
		self.first_glance = -1
		self.glance_howlong = -1

		# How often we update the look-at target.
		self.LOOKAT_INTERVAL = 0.1
		self.last_lookat = 0

		# Last time that the list of active faces was vacuumed out.
		self.last_vacuum = 0
		self.VACUUM_INTERVAL = 1

		# Subscribed pi_vision topics and events
		self.TOPIC_FACE_EVENT = "/camera/face_event"
		self.EVENT_NEW_FACE = "new_face"
		self.EVENT_LOST_FACE = "lost_face"

		self.TOPIC_FACE_LOCATIONS = "/camera/face_locations"

		# Published blender_api topics
		self.TOPIC_FACE_TARGET = "/blender_api/set_face_target"
		self.TOPIC_GAZE_TARGET = "/blender_api/set_gaze_target"

		# Face appearance/disappearance from pi_vision
		rospy.Subscriber(self.TOPIC_FACE_EVENT, FaceEvent, self.face_event_cb)

		# Face location information from pi_vision
		rospy.Subscriber(self.TOPIC_FACE_LOCATIONS, Faces, self.face_loc_cb)

		# Where to look
		self.look_pub = rospy.Publisher(self.TOPIC_FACE_TARGET, \
			Target, queue_size=10)

		self.gaze_pub = rospy.Publisher(self.TOPIC_GAZE_TARGET, \
			Target, queue_size=10)

		# Frame in which coordinates will be returned from transformation
		self.LOCATION_FRAME = "blender"
		# Transform Listener.Allows history of RECENT_INTERVAL
		self.tf_listener = tf.TransformListener(False, rospy.Duration(self.RECENT_INTERVAL))

	# ---------------------------------------------------------------
	# Public API. Use these to get things done.

	# Turn only the eyes towards the given target face; track that face.
	def gaze_at_face(self, faceid):
		print ("gaze at: " + str(faceid))

		# Look at neutral position, 1 meter in front
		if 0 == faceid :
			trg = Target()
			trg.x = 1.0
			trg.y = 0.0
			trg.z = 0.0
			self.gaze_pub.publish(trg)

		self.last_lookat = 0
		if faceid not in self.visible_faces :
			self.gaze_at = 0
			return

		self.gaze_at = faceid

	# Turn entire head to look at the given target face. The head-turn is
	# performed only once per call; after that, the eyes will then
	# automatically track that face, but the head will not.  Call again,
	# to make the head move again.
	#
	def look_at_face(self, faceid):
		print ("look at: " + str(faceid))

		# Look at neutral position, 1 meter in front
		if 0 == faceid :
			trg = Target()
			trg.x = 1.0
			trg.y = 0.0
			trg.z = 0.0
			self.look_pub.publish(trg)

		self.last_lookat = 0
		if faceid not in self.visible_faces :
			self.look_at = 0
			return

		self.look_at = faceid

	def glance_at_face(self, faceid, howlong):
		print("glance at: " + str(faceid) + " for " + str(howlong) + " seconds")
		self.glance_at = faceid
		self.glance_howlong = howlong
		self.first_glance = -1

	def study_face(self, faceid, howlong):
		print("study: " + str(faceid) + " for " + str(howlong) + " seconds")
		self.glance_at = faceid
		self.glance_howlong = howlong
		self.first_glance = -1

	# ---------------------------------------------------------------
	# Private functions, not for use outside of this class.
	# Add a face to the Owyl blackboard.
	def add_face_to_bb(self, faceid):

		# We already know about it.
		if faceid in self.blackboard["background_face_targets"]:
			return

		# Update the blackboard.
		self.blackboard["is_interruption"] = True
		self.blackboard["new_face"] = faceid
		self.blackboard["background_face_targets"].append(faceid)

	# Remove a face from the Owyl blackboard.
	def remove_face_from_bb(self, fid):

		if fid not in self.blackboard["background_face_targets"]:
			return

		# Update the blackboard.
		self.blackboard["is_interruption"] = True
		self.blackboard["lost_face"] = fid
		self.blackboard["background_face_targets"].remove(fid)
		# If the robot lost the new face during the initial
		# interaction, reset new_face variable
		if self.blackboard["new_face"] == fid :
			self.blackboard["new_face"] = ""

	# Start tracking a face
	def add_face(self, faceid):
		if faceid in self.visible_faces:
			return

		self.visible_faces.append(faceid)

		print("New face added to visibile faces: " +
			str(self.visible_faces))

		self.add_face_to_bb(faceid)

	# Stop tracking a face
	def remove_face(self, faceid):
		self.remove_face_from_bb(faceid)
		if faceid in self.visible_faces:
			self.visible_faces.remove(faceid)

		print("Lost face; visibile faces now: " + str(self.visible_faces))



	# ----------------------------------------------------------
	# Main look-at action driver.  Should be called at least a few times
	# per second.  This publishes all of the eye-related actions that the
	# blender api robot head should be performing.
	#
	# This performs multiple actions:
	# 1) updates the list of currently visible faces
	# 2) updates the list of recently seen (but now lost) faces
	# 3) If we should be looking at one of these faces, then look
	#    at it, now.
	def do_look_at_actions(self) :
		now = time.time()

		# Should we be glancing elsewhere? If so, then do it, and
		# do it actively, i.e. track that face intently.
		if 0 < self.glance_at:
			if self.first_glance < 0:
				self.first_glance = now
			if (now - self.first_glance < self.glance_howlong):
				face = None

				# Find latest position known
				try:
					current_trg = self.face_target(self.blackboard["current_face_target"])
					gaze_trg = self.face_target(self.glance_at)
					self.glance_or_look_at(current_trg, gaze_trg)
				except:
					print("Error: no face to glance at!")
					self.glance_at = 0
					self.first_glance = -1
			else :
				# We are done with the glance. Resume normal operations.
				self.glance_at = 0
				self.first_glance = -1

		# Publish a new lookat target to the blender API
		elif (now - self.last_lookat > self.LOOKAT_INTERVAL):
			self.last_lookat = now

			# Update the eye position, if need be. Skip, if there
			# is also a pending look-at to perform.

			if 0 < self.gaze_at and self.look_at <= 0:
				# print("Gaze at id " + str(self.gaze_at))
				try:
					if not self.gaze_at in self.visible_faces:
						raise Exception("Face not visible")
					current_trg = self.face_target(self.blackboard["current_face_target"])
					gaze_trg = self.face_target(self.gaze_at)
					self.glance_or_look_at(current_trg, gaze_trg)
				except tf.LookupException as lex:
					print("Warning: TF has forgotten about face id:" +
						str(self.look_at))
					self.remove_face(self.look_at)
					self.look_at_face(0)
					return
				except Exception as ex:
					print("Error: no gaze-at target: ", ex)
					self.gaze_at_face(0)
					return

			if 0 < self.look_at:
				print("Look at id: " + str(self.look_at))
				try:
					if not self.look_at in self.visible_faces:
						raise Exception("Face not visible")
					trg = self.face_target(self.look_at)
					self.look_pub.publish(trg)
				except tf.LookupException as lex:
					print("Warning: TF has forgotten about face id: " +
						str(self.look_at))
					self.remove_face(self.look_at)
					self.look_at_face(0)
					return
				except Exception as ex:
					print("Error: no look-at target: ", ex)
					self.look_at_face(0)
					return

				# Now that we've turned to face the target, don't do it
				# again; instead, just track with the eyes.
				self.gaze_at = self.look_at
				self.look_at = -1

	# If the distance between the current face target and the glace_at target > max_glance_distance
	# Look at that face instead (so that the neck will also move instead of the eyes only)
	def glance_or_look_at(self, current_trg, gaze_trg):
		z = (current_trg.z - gaze_trg.z)
		# Avoid division by zero
		if z == 0:
			z = 1
		gaze_distance = math.sqrt(math.pow((current_trg.x - gaze_trg.x), 2) + \
					  math.pow((current_trg.y - gaze_trg.y), 2)) / z
		if gaze_distance > self.blackboard["max_glance_distance"]:
			print("Reached max_glance_distance, look at the face instead")
			self.look_pub.publish(gaze_trg)
		else:
			# For face study saccade
			if self.blackboard["face_study_nose"]:
				gaze_trg.z += self.blackboard["face_study_z_pitch_nose"]
			elif self.blackboard["face_study_mouth"]:
				gaze_trg.z += self.blackboard["face_study_z_pitch_mouth"]
			elif self.blackboard["face_study_left_ear"]:
				gaze_trg.y += self.blackboard["face_study_y_pitch_left_ear"]
			elif self.blackboard["face_study_right_ear"]:
				gaze_trg.y += self.blackboard["face_study_y_pitch_right_ear"]

			# Publish the gaze_at ROS message
			self.gaze_pub.publish(gaze_trg)

			# Reset face study saccade related flags
			self.blackboard["face_study_nose"] = False
			self.blackboard["face_study_mouth"] = False
			self.blackboard["face_study_left_ear"] = False
			self.blackboard["face_study_right_ear"] = False


	# ----------------------------------------------------------
	# pi_vision ROS callbacks

	# pi_vision ROS callback, called when a new face is detected,
	# or a face is lost.  Note: I don't think this is really needed,
	# the face_loc_cb accomplishes the same thing. So maybe should
	# remove this someday.
	def face_event_cb(self, data):
		if data.face_event == self.EVENT_NEW_FACE:
			self.add_face(data.face_id)

		elif data.face_event == self.EVENT_LOST_FACE:
			self.remove_face(data.face_id)

	# pi_vision ROS callback, called when pi_vision has new face
	# location data for us. Because this happens frequently (10x/second)
	# we also use this as the main update loop, and drive all look-at
	# actions from here.
	def face_loc_cb(self, data):
		for face in data.faces:
			fid = face.id
			loc = face.point
			# Sanity check.  Sometimes pi_vision sends us faces with
			# location (0,0,0). Discard these.
			if loc.x < 0.05:
				continue
			self.add_face(fid)

		# Now perform all the various looking-at actions
		self.do_look_at_actions()

	# Queries tf_listener to get latest available position
	# Throws TF exceptions if transform canot be returned
	def face_target(self, faceid):
		(trans, rot) = self.tf_listener.lookupTransform( \
			self.LOCATION_FRAME, 'Face' + str(faceid), rospy.Time(0))
		t = Target()
		t.x = trans[0]
		t.y = trans[1]
		t.z = trans[2] + self.blackboard["z_pitch_eyes"]
		return t


	# Picks random face from current visible faces
	# Prioritizes the real faces over virtual faces in attention regions
	@staticmethod
	def random_face_target(faces, exclude = 0):
		if len(faces) < 1:
			return 0
		# Faces with smaller (less than <1,000,000 ids are prioritized
		small_ids = [f for f in faces if (f < 1000000)and (f != exclude)]
		if len(small_ids) < 1:
			return random.choice(small_ids)
		return random.choice(faces)
