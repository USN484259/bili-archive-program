#!/usr/bin/env python3

import os
import sys
import time
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


LOG_FORMAT = "%(asctime)s\t%(levelname)s\t%(name)s\t%(message)s"

USER_AGENT = {
	"User-Agent": "Mozilla/5.0",
	"Referer": "https://www.bilibili.com/"
}

logger = logging.getLogger("bili_arch.live_notify")


def fetch_icon(sess, url):
	icon_file = tempfile.NamedTemporaryFile(delete=False)
	logger.debug("icon path %s", icon_file.name)
	try:
		logger.info("fetching icon into %s", icon_file.name)
		with sess.stream("GET", url, headers = USER_AGENT) as resp:
			logger.debug(resp)
			resp.raise_for_status()
			for chunk in resp.iter_bytes():
				icon_file.write(chunk)
		return icon_file.name
	finally:
		icon_file.close()


def on_click(notification, action, rid):
	logger.info("opening live room %d", rid)
	webbrowser.open("https://live.bilibili.com/" + str(rid), new = 1, autoraise = False)


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


def show_notification(info, icon_path, active_notifies):
	uname = info.get("uname", "")

	def on_close(notification):
		logger.info("remove notification for %s", uname)
		icon_path = active_notifies.pop(notification, None)
		try:
			logger.debug("removing %s", icon_path)
			os.remove(icon_path)
		except Exception:
			logger.exception("failed to remove icon file")

	logger.info("create notification for %s", uname)
	notification = Notify.Notification.new(
		uname + " 开播了",
		live_time_str(info.get("live_time")) + '\t' + info.get("title", ""),
		icon_path
	)

	notification.add_action(
		"default",
		"看看你的",
		on_click,
		info.get("room_id")
	)
	notification.connect("closed", on_close)
	notification.show()
	active_notifies[notification] = icon_path


def check_live_status(sess, rec):
	resp = sess.request("GET", rec["live_status_url"])
	resp.raise_for_status()
	live_status = resp.json()
	# logger.debug(live_status)

	for uid, info in live_status.items():
			status = info.get("live_status")
			last_status = rec["status_rec"].get(uid, {}).get("live_status")
			logger.debug("uid %s status %d", str(uid), status)
			if status != 1:
				continue
			if last_status and last_status == 1:
				continue

			logger.info("new live room %s: %s", info.get("uname", ""), info.get("title", ""))
			icon_path = fetch_icon(sess, info.get("face"))
			show_notification(info, icon_path, rec["active_notifies"])

	rec["status_rec"] = live_status


def main(args):
	Notify.init(args.name)
	rec = {
		"live_status_url": args.url,
		"status_rec": {},
		"active_notifies": {},
	}
	sess = httpx.Client(timeout = min(10, args.interval / 2), follow_redirects = True)

	def sig_handler(signum, frame):
		logger.info("reset live status")
		rec["status_rec"] = {}

	def on_timer():
		try:
			logger.info("checking live status")
			check_live_status(sess, rec)
		except Exception:
			logger.exception("failed to update live status")

		return True

	signal.signal(signal.SIGUSR1, sig_handler)
	main_loop = GLib.MainLoop()
	on_timer()
	GLib.timeout_add_seconds(args.interval, on_timer)
	main_loop.run()


if __name__ == "__main__":
	parser = argparse.ArgumentParser()
	parser.add_argument("-v", "--verbose", action = "store_true")
	parser.add_argument("--name", default = os.path.basename(sys.argv[0]))
	parser.add_argument("--interval", type = int, default = 30)
	parser.add_argument("url")

	args = parser.parse_args()
	logging.basicConfig(level = args.verbose and logging.DEBUG or logging.INFO, format = LOG_FORMAT, stream = sys.stderr)

	main(args)
