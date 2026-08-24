#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()

import time
import json
import httpx
import socket
import signal
import asyncio
import logging
import argparse
import tempfile
import webbrowser

import gi
gi.require_version('Notify', '0.7')
from gi.repository import Notify, GLib

from utils import logger_init
from messaging import MessagingClient

# constants

import constants
LIVE_STATUS_URL = "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"

# static objects

logger = logging.getLogger("bili_arch.live_notify")

# helper functions

def exec_restart():
	logger.info("restarting %s", sys.argv[0])
	exec_path = sys.executable
	if exec_path:
		logger.info("python-path %s", exec_path)
	else:
		import shutil
		exec_path = shutil.which("python3")
		logger.warning("guessing python-path %s", exec_path)

	if sys.hexversion >= 0x030A0000:
		argv = sys.orig_argv
	else:
		argv = ["python"] + sys.argv

	logger.debug(argv)
	os.execv(exec_path, argv)


def fetch_icon(sess, url):
	icon_file = tempfile.NamedTemporaryFile()
	logger.info("fetching icon into %s", icon_file.name)
	with sess.stream("GET", url) as resp:
		logger.debug(resp)
		resp.raise_for_status()
		for chunk in resp.iter_bytes():
			icon_file.write(chunk)

	icon_file.flush()
	return icon_file


def live_time_str(start_time):
	if (not start_time) or start_time <= 0:
		return ""

	live_time = int(time.time()) - start_time
	if live_time < 0:
		return ""

	res = ""
	if live_time >= 3600:
		res = "%d:" % int(live_time / 3600)

	live_time %= 3600
	minute = int(live_time / 60)
	second = int(live_time % 60)

	res += "%02d:%02d" % (minute, second)
	return res


def on_click(notification, action, rid):
	logger.info("opening live room %d", rid)
	webbrowser.open("https://live.bilibili.com/" + str(rid), new = 1, autoraise = False)


def fetch_status_bili(sess, uid_list):
	if not uid_list:
		return {}

	resp = sess.request("POST", LIVE_STATUS_URL, json = {"uids": uid_list})
	resp.raise_for_status()
	result = resp.json()

	code = result.get("code", -32768)
	if code == 0:
		return result.get("data")

	msg = result.get("msg") or result.get("message", "")
	logger.error("response code %d, msg %s", code, msg)
	raise RuntimeError(msg)


def fetch_status_http(sess, url):
	resp = sess.request("GET", url)
	resp.raise_for_status()
	return resp.json()


# main class

class LiveStatusHandler:
	def __init__(self, args):
		self.sess = httpx.Client(headers = constants.USER_AGENT, timeout = min(10, args.interval / 2), follow_redirects = True)
		self.config_path = args.config
		self.url = args.url
		self.interval = args.interval
		self.msg_client = None
		self.live_status = {}
		self.active_notifies = {}
		self.timestamp = None
		self.uid_list = None

		if args.url or args.msg_addr:
			pass
		elif not args.config:
			raise RuntimeError("at least url, msg-addr or config should be specified")

		self.reload_config()
		if args.msg_addr:
			self.msg_client = MessagingClient(args.msg_addr, allow_dummy = False)
			self.msg_client.subscribe(constants.topic.live_status)
			self.msg_watch_id = GLib.io_add_watch(self.msg_client.fileno(), GLib.IO_IN, self.handle_message)


	def reload_config(self):
		if self.config_path:
			try:
				with open(self.config_path, "r") as f:
					config = json.load(f)

				self.uid_list = [u["uid"] for u in config]
				self.live_status = {}

			except Exception:
				logger.exception("failed to load config")


	def close(self):
		for icon_file in self.active_notifies.values():
			icon_file.close()
		self.active_notifies.clear()
		if self.msg_client is not None:
			try:
				GLib.Source.remove(self.msg_watch_id)
				self.msg_client.close()
			finally:
				self.msg_client = None

		if self.sess is not None:
			try:
				self.sess.close()
			finally:
				self.sess = None


	def run(self, main_loop):
		self.on_timer()
		GLib.timeout_add_seconds(self.interval, self.on_timer)
		main_loop.run()


	def on_timer(self):
		try:
			logger.info("checking live status")
			live_status = None
			try:
				if self.url:
					live_status = fetch_status_http(self.sess, self.url)
				elif self.msg_client:
					cur_time = time.monotonic()
					self.msg_client.send(constants.topic.live_status, json = {
						"action":	"get-live-status",
					})
					if self.timestamp is None:
						self.timestamp = 0
						return
					elif cur_time - self.timestamp < self.interval:
						return
			except Exception:
				if not self.uid_list:
					raise

			if live_status is None and self.uid_list:
				live_status = fetch_status_bili(self.sess, self.uid_list)

			self.check_live_status(live_status)
		except Exception:
			logger.exception("failed to update live status")

		return True


	def handle_message(self, *args):
		try:
			self.msg_client.wait(0)
			while True:
				topic, info = self.msg_client.recv(constants.topic.live_status)
				if not topic:
					break
				logger.debug("received %s", topic)
				if not isinstance(info, dict) or "live_status" not in info:
					continue
				self.timestamp = time.monotonic()
				self.check_live_status(info.get("live_status"))
		except Exception:
			logger.exception("exception in handle_message")
		return True


	def check_live_status(self, live_status):
		if not live_status:
			return

		for uid, info in live_status.items():
				status = info.get("live_status")
				last_status = self.live_status.get(uid, {}).get("live_status")
				logger.debug("uid %s status %d", str(uid), status)
				if status != 1:
					continue
				if last_status and last_status == 1:
					continue

				logger.info("new live room %s: %s", info.get("uname", ""), info.get("title", ""))
				icon_file = fetch_icon(self.sess, info.get("face"))
				self.show_notification(info, icon_file)

		self.live_status = live_status


	def show_notification(self, info, icon_file):
		uname = info.get("uname", "")

		def on_close(notification):
			logger.info("remove notification for %s", uname)
			icon_file = self.active_notifies.pop(notification, None)
			if icon_file:
				icon_file.close()

		logger.info("create notification for %s", uname)
		notification = Notify.Notification.new(
			uname + " 开播了",
			live_time_str(info.get("live_time")) + '\t' + info.get("title", ""),
			icon_file.name
		)

		notification.add_action(
			"default",
			"看看你的",
			on_click,
			info.get("room_id")
		)
		notification.connect("closed", on_close)
		notification.show()
		self.active_notifies[notification] = icon_file


def main(args):
	Notify.init(args.name)
	handler = LiveStatusHandler(args)
	main_loop = GLib.MainLoop()
	need_restart = False

	def sig_reload(signum, frame):
		logger.info("reset live status")
		handler.reload_config()


	def sig_restart(signum, frame):
		need_restart = True
		main_loop.quit()

	try:
		signal.signal(signal.SIGUSR1, sig_reload)
		signal.signal(signal.SIGUSR2, sig_restart)
		handler.run(main_loop)
	finally:
		handler.close()
		if need_restart:
			exec_restart()


if __name__ == "__main__":
	parser = argparse.ArgumentParser()
	parser.add_argument("-v", "--verbose", action = "count", default = 0)
	parser.add_argument("--name", default = os.path.basename(sys.argv[0]))
	parser.add_argument("--interval", type = int, default = 30)
	parser.add_argument("--url")
	parser.add_argument("--msg-addr")
	parser.add_argument("config", nargs = '?')

	args = parser.parse_args()
	logger_init(args.verbose)
	main(args)
