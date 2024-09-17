#!/usr/bin/env python3

import os
import sys
import time
import json
import httpx
import signal
import asyncio
import logging
import argparse
import tempfile
import webbrowser

import gi
gi.require_version('Notify', '0.7')
from gi.repository import Notify, GLib

# constants

LOG_FORMAT = "%(asctime)s\t%(levelname)s\t%(name)s\t%(message)s"

USER_AGENT = {
	"User-Agent": "Mozilla/5.0",
	"Referer": "https://www.bilibili.com/"
}

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


def load_config(config_path):
	with open(config_path, "r") as f:
		config = json.load(f)

	return [u["uid"] for u in config]


def fetch_icon(sess, url):
	icon_file = tempfile.NamedTemporaryFile()
	logger.info("fetching icon into %s", icon_file.name)
	with sess.stream("GET", url, headers = USER_AGENT) as resp:
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


def show_notification(info, icon_file, active_notifies):
	uname = info.get("uname", "")

	def on_close(notification):
		logger.info("remove notification for %s", uname)
		icon_file = active_notifies.pop(notification, None)
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
	active_notifies[notification] = icon_file


# methods

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


def fetch_status_local(sess, url):
	resp = sess.request("GET", url)
	resp.raise_for_status()
	return resp.json()
	# logger.debug(live_status)


def check_live_status(sess, rec, live_status):
	for uid, info in live_status.items():
			status = info.get("live_status")
			last_status = rec["status_rec"].get(uid, {}).get("live_status")
			logger.debug("uid %s status %d", str(uid), status)
			if status != 1:
				continue
			if last_status and last_status == 1:
				continue

			logger.info("new live room %s: %s", info.get("uname", ""), info.get("title", ""))
			icon_file = fetch_icon(sess, info.get("face"))
			show_notification(info, icon_file, rec["active_notifies"])

	rec["status_rec"] = live_status


# entrance

def main(args):
	uid_list = []

	if args.config:
		uid_list = load_config(args.config)
	elif not args.url:
		raise RuntimeError("neither url nor config specified")

	Notify.init(args.name)
	rec = {
		"status_rec": {},
		"active_notifies": {},
	}
	sess = httpx.Client(headers = USER_AGENT, timeout = min(10, args.interval / 2), follow_redirects = True)

	def sig_reload(signum, frame):
		logger.info("reset live status")
		rec["status_rec"] = {}
		if args.config:
			logger.info("reloading config")
			try:
				uid_list = load_config(args.config)
			except Exception:
				logger.exception("failed to relaod config")

	def sig_restart(signum, frame):
		for icon_file in rec["active_notifies"].values():
			icon_file.close()
		exec_restart()

	def on_timer():
		try:
			logger.info("checking live status")
			live_status = None
			if args.url:
				try:
					live_status = fetch_status_local(sess, args.url)
				except Exception:
					if not uid_list:
						raise

			if live_status is None and args.config:
				live_status = fetch_status_bili(sess, uid_list)


			check_live_status(sess, rec, live_status)
		except Exception:
			logger.exception("failed to update live status")

		return True

	signal.signal(signal.SIGUSR1, sig_reload)
	signal.signal(signal.SIGUSR2, sig_restart)
	main_loop = GLib.MainLoop()
	on_timer()
	GLib.timeout_add_seconds(args.interval, on_timer)
	try:
		main_loop.run()
	finally:
		for icon_file in rec["active_notifies"].values():
			icon_file.close()


if __name__ == "__main__":
	parser = argparse.ArgumentParser()
	parser.add_argument("-v", "--verbose", action = "store_true")
	parser.add_argument("--name", default = os.path.basename(sys.argv[0]))
	parser.add_argument("--interval", type = int, default = 30)
	parser.add_argument("-c", "--config")
	parser.add_argument("url", nargs = '?')

	args = parser.parse_args()
	logging.basicConfig(level = args.verbose and logging.DEBUG or logging.INFO, format = LOG_FORMAT, stream = sys.stderr)

	main(args)
