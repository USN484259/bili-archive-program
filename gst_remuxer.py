#!/usr/bin/env python3

import logging
from threading import Thread
import gi

gi.require_version("GLib", "2.0")
gi.require_version("GObject", "2.0")
gi.require_version("Gst", "1.0")

from gi.repository import Gst, GLib, GObject
logger = logging.getLogger("gst_remuxer")

class GstRemuxer(Thread):
	def __init__(self, filename):
		Thread.__init__(self, daemon = True)
		if Gst.is_initialized() or Gst.init_check(None):
			pass
		else:
			raise Exception("failed to init Gstreamer")

		self.pipeline = Gst.Pipeline.new()
		self.appsrc = Gst.ElementFactory.make("appsrc")
		self.src_queue = Gst.ElementFactory.make("queue")
		self.flv_demux = Gst.ElementFactory.make("flvdemux")
		self.multi_queue = Gst.ElementFactory.make("multiqueue")
		self.mp4_mux = Gst.ElementFactory.make("mp4mux")
		self.filesink = Gst.ElementFactory.make("filesink")

		self.pipeline.add(self.appsrc)
		self.pipeline.add(self.src_queue)
		self.pipeline.add(self.flv_demux)
		self.pipeline.add(self.multi_queue)
		self.pipeline.add(self.mp4_mux)
		self.pipeline.add(self.filesink)

		caps = Gst.caps_from_string("video/x-flv")
		self.appsrc.set_property("caps", caps)
		self.src_queue.set_property("leaky", 2)
		self.filesink.set_property("location", filename)

		self.appsrc.link(self.src_queue)
		self.src_queue.link(self.flv_demux)
		self.mp4_mux.link(self.filesink)

		self.flv_demux.connect("pad-added", self.on_demux_add_pad, self)
		self.flv_demux.connect("no-more-pads", self.on_demux_done_pads, self)
		self.multi_queue.connect("pad-added", self.on_queue_add_pad, self)
		self.play_barrier = 1


	@staticmethod
	def on_demux_add_pad(element, pad, self):
		logger.info("demux pad %s", pad.name)

		caps = pad.get_current_caps()
		struct = caps.get_structure(0)
		name = struct.get_name()
		logger.debug("demux caps %s", name)

		src_pad = pad
		if name.startswith("video/x-h264"):
			h264_parse = Gst.ElementFactory.make("h264parse")
			self.pipeline.add(h264_parse)
			peer_pad = h264_parse.get_static_pad("sink")
			pad.link(peer_pad)
			src_pad = h264_parse.get_static_pad("src")
			h264_parse.sync_state_with_parent()

		# peer_pad = self.multi_queue.request_pad_simple("sink_%u")
		peer_pad = self.multi_queue.get_request_pad("sink_%u")
		assert(peer_pad)
		res = src_pad.link(peer_pad)
		self.play_barrier += 1
		logger.info("demux pad %s, barrier %d", str(res), self.play_barrier)


	@staticmethod
	def on_demux_done_pads(element, self):
		self.play_barrier -= 1
		logger.info("demux done pads, barrier %d", self.play_barrier)
		if self.play_barrier == 0:
			self.pipeline.set_state(Gst.State.PLAYING)

	@staticmethod
	def on_queue_add_pad(element, pad, self):
		logger.debug("queue pad %s, direction %s", pad.name, str(pad.direction))
		if pad.direction != Gst.PadDirection.SRC:
			return

		typefind = Gst.ElementFactory.make("typefind")
		self.pipeline.add(typefind)
		peer_pad = typefind.get_static_pad("sink")
		pad.link(peer_pad)
		typefind.set_property("minimum", 100)
		typefind.connect("have-type", self.on_found_type, self)
		typefind.sync_state_with_parent()


	@staticmethod
	def on_found_type(element, probability, caps, self):
		logger.info("found type %s", str(caps))

		res = element.link(self.mp4_mux)
		self.play_barrier -= 1

		logger.info("mux linking %s, barrier %d", str(res), self.play_barrier)
		if self.play_barrier == 0:
			self.pipeline.set_state(Gst.State.PLAYING)


	def run(self):
		try:
			logger.info("starting pipeline")
			self.pipeline.set_state(Gst.State.PAUSED)
			bus = self.pipeline.get_bus()
			while True:
				msg = bus.timed_pop_filtered(Gst.CLOCK_TIME_NONE, Gst.MessageType.EOS | Gst.MessageType.ERROR | Gst.MessageType.WARNING | Gst.MessageType.INFO | Gst.MessageType.STATE_CHANGED | Gst.MessageType.CLOCK_LOST)
				if msg.type == Gst.MessageType.ERROR:
					logger.error(msg.parse_error())
					break
				elif msg.type == Gst.MessageType.WARNING:
					logger.warning(msg.parse_warning())
				elif msg.type == Gst.MessageType.INFO:
					logger.info(msg.parse_info())
				elif msg.type == Gst.MessageType.EOS:
					logger.info("pipeline EOS")
					break
				elif msg.type == Gst.MessageType.STATE_CHANGED:
					logger.debug(msg.parse_state_changed())
				elif msg.type == Gst.MessageType.CLOCK_LOST:
					logger.warning("pipeline clock lost")
					self.pipeline.set_state(Gst.State.PAUSED)
					self.pipeline.set_state(Gst.State.PLAYING)

		except Exception:
			logger.exception("exception in pipeline")
		finally:
			logger.info("stopping pipeline")
			self.pipeline.set_state(Gst.State.NULL)
			logger.info("pipeline stopped")


	def write(self, data):
		buf = Gst.Buffer.new_wrapped(data)
		self.appsrc.emit("push-buffer", buf)


	def close(self):
		self.appsrc.emit("end-of-stream")
		self.join(4)	# timeout
		if self.is_alive():
			logger.error("muxer thread cannot stop, dropping")


