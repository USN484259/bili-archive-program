#!/usr/bin/env python3

import os
import sys
import json
import time
import signal
import asyncio
import logging
import multiprocessing

import constants
import fops
import runtime
import network
import live_rec
from utils import logger_init
from messaging import MessagingClient
from contextlib import suppress

Inotify = None
with suppress(ModuleNotFoundError):
	from simple_inotify import Inotify
	from simple_inotify.constants import *


# constants

LIVE_STATUS_URL = "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"
PROC_TERM_TIMEOUT = 2

# static objects

logger = logging.getLogger("bili_arch.monitor")
multiprocessing = multiprocessing.get_context("fork")

# helper functions

async def get_live_status(sess, uid_list):
	resp = await network.request(sess, "POST", LIVE_STATUS_URL, json = {"uids": uid_list})
	logger.trace(resp)
	return resp.get("data")


async def record_main(rid, path, rec_log, relay_path):
	with fops.locked_path(path) as rec_path:
		if rec_log:
			log_path = os.path.join(rec_path, "record.log")
			logger.debug("logger reinit to %s", log_path)
			logger_init(runtime.log_level, log_path, noprint = True)

		logger.verbose("process %d: start recording %d into %s", os.getpid(), rid, path)

		async with network.session() as sess:
			await live_rec.record(sess, rid, rec_path, relay_path = relay_path)


def exec_record(args, to_close):
	logger.debug("forked %d from %d", os.getpid(), os.getppid())
	signal.signal(signal.SIGUSR1, signal.SIG_IGN)
	signal.signal(signal.SIGUSR2, signal.SIG_IGN)
	for obj in to_close:
		logger.trace(obj)
		with suppress(Exception):
			obj.close()
	asyncio.run(record_main(*args))
	os._exit(0)


def exec_restart():
	logger.info("restarting %s", sys.argv[0])
	exec_path = sys.executable
	if exec_path:
		logger.verbose("python-path %s", exec_path)
	else:
		import shutil
		exec_path = shutil.which("python3")
		logger.notice("guessing python-path %s", exec_path)

	if sys.hexversion >= 0x030A0000:
		argv = sys.orig_argv
	else:
		argv = ["python"] + sys.argv

	logger.debug(argv)
	os.execv(exec_path, argv)


class Record:
	@staticmethod
	def validate(config, info):
		if not info:
			return

		if not isinstance(config.get("uid"), int):
			raise RuntimeError("invalid uid in config")

		if config.get("rid") != info.get("room_id"):
			logger.notice("%s rid mismatch: %s/%s", config.get("name", ""), config.get("rid"), info.get("room_id"))

		if config.get("uid") != info.get("uid"):
			logger.notice("%s uid mismatch: %s/%s", config.get("name", ""), config.get("uid"), info.get("uid"))


	def __init__(self, config, task_start_cb, task_stop_cb):
		logger.trace(config)
		self.config = config
		self.task_start_cb = task_start_cb
		self.task_stop_cb = task_stop_cb
		self.info = {}
		self.task = None
		self.path = None
		self.start_time = None
		self.stop_time = None


	def close(self):
		logger.debug("closing room %d", self.rid())
		if self.check_task():
			logger.warning("rid %d task still running, terminating", self.rid())
			try:
				self.task.terminate()
				self.task.join(PROC_TERM_TIMEOUT)
				self.task.close()
			finally:
				self.task = None


	def reload(self, config):
		logger.trace(config)
		self.validate(config, self.info)
		self.config = config


	def status(self):
		return self.info

	def rid(self):
		return int(self.info.get("room_id", self.config.get("rid")))

	def uid(self):
		return int(self.info.get("uid", self.config.get("uid")))

	def name(self):
		return self.config.get("name", self.info.get("uname"))

	def uname(self):
		return self.info.get("uname", self.config.get("name"))

	def rec_path(self):
		return self.path

	def duration(self):
		return self.start_time and ((self.stop_time or int(time.time())) - self.start_time)

	def need_record(self):
		return self.config.get("enable", True) and self.config.get("record", True)

	def enabled(self):
		return self.config.get("enable", True)

	def disable(self):
		logger.verbose("disabling room %d, %s", self.rid(), self.name())
		self.config["enable"] = False

	def check_task(self):
		if self.task is not None:
			if self.task.is_alive():
				logger.debug("recording %d, duration %d", self.rid(), self.duration())
				return True
			self.stop_time = int(time.time())
			logger.verbose("stopped %d, pid %d, duration %d", self.rid(), self.task.pid, self.duration())
			logger.trace(self.task)
			self.task.close()
			self.task = None
			try:
				self.task_stop_cb(self)
			except Exception:
				logger.exception("exception in task_stop_cb")
		return False

	def start_task(self, path, rec_log, relay_root, close_obj_list):
		assert(self.task is None)
		rid = self.rid()
		self.task = multiprocessing.Process(
			target = exec_record, args = ((
				rid, path, rec_log, (relay_root and os.path.join(relay_root, str(rid)) or None)
			), close_obj_list), daemon = False)
		self.task.start()
		logger.verbose("started %d, pid %d, path %s", rid, self.task.pid, path)
		logger.trace(self.task)
		self.path = path
		self.start_time = int(time.time())
		self.stop_time = None

	def update(self, info):
		assert(isinstance(info, dict))
		live_status = info.get("live_status", -1)
		streaming = (live_status == 1)
		running = self.check_task()

		logger.debug("updating %d, status %d", self.rid(), live_status)
		if not self.info:
			self.validate(self.config, info)
		self.info = info

		if self.need_record() and streaming and not running:
			try:
				logger.verbose("starting record %s (%d)", self.name(), self.rid())
				self.task_start_cb(self, info)
			except Exception:
				logger.exception("exception in task_start_cb")
			running = self.check_task()
		logger.debug("room %d, running %s", self.rid(), running)
		return streaming, running

# main classes

class Monitor:
	def __init__(self, args):
		self.args = args
		self.records = {}
		self.msg_client = MessagingClient(args.msg_addr)
		self.sess = network.session()
		self.observer = None
		self.started = False
		self.reload = False
		self.restart = False

		if args.auto_reload:
			if Inotify is not None:
				self.observer = Inotify(IN_NONBLOCK | IN_CLOEXEC)
				self.watch_mask = (IN_CLOSE_WRITE | IN_MOVED_TO | IN_EXCL_UNLINK | IN_ONLYDIR)
			else:
				logger.notice("inotify not available, disable auto-reload")


	async def close(self):
		logger.debug("closing monitor")
		if self.sess is not None:
			try:
				await self.sess.aclose()
			finally:
				self.sess = None

		if self.observer is not None:
			self.observer.close()
			self.observer = None

		self.msg_client.close()
		for rec in self.records.values():
			rec.close()


	async def __aenter__(self):
		self.start()
		return self

	async def __aexit__(self, *args):
		await self.close()


	def on_signal(self, signum, *args):
		if signum == signal.SIGUSR1:
			logger.info("reload scheduled")
			self.reload = True
		elif signum == signal.SIGUSR2:
			logger.info("restart scheduled")
			self.restart = True


	def reload_config(self):
		logger.verbose("loading config from %s", self.args.config)
		with open(self.args.config, "r") as f:
			user_list = json.load(f)
		logger.trace(user_list)

		existing_keys = set(self.records.keys())
		logger.trace(existing_keys)

		for config in user_list:
			uid = int(config["uid"])
			name = config.get("name", str(uid))
			if not config.get("enable", True):
				logger.debug("disabled %s", name)
				continue

			rec = self.records.get(uid)
			if rec is not None:
				rec.reload(config)
			else:
				logger.debug("creating record for %s, room %d", name, config.get("rid", -1))
				self.records[uid] = Record(config, self.on_task_start, self.on_task_stop)
			existing_keys.discard(uid)

		for uid in existing_keys:
			rec = self.records.get(uid)
			if rec is not None:
				if rec.check_task():
					rec.disable()
				else:
					rec.close()
					del self.records[uid]


	def get_status(self):
		return {uid: rec.status() for uid, rec in self.records.items()}

	def on_task_stop(self, rec):
		logger.info("stopped recording %s, room %d, duration %d", rec.name(), rec.rid(), rec.duration())
		cur_time = int(time.time())
		self.msg_client.send(constants.topic.live_monitor, json = {
			"timestamp":	cur_time,
			"rid":		rec.rid(),
			"uid":		rec.uid(),
			"uname":	rec.uname(),
			"event":	"record-stopped",
			"path":		rec.rec_path(),
			"duration":	rec.duration(),
		})

	def on_task_start(self, rec, info):
		uname = rec.uname()
		title = info.get("title", "")
		rec_name = live_rec.make_record_name(uname, title)
		live_root = self.args.dir or runtime.subdir("live")
		rec_path = os.path.join(live_root, rec_name)
		logger.info("start recording %s, room %d, %s", rec.name(), rec.rid(), rec_name)

		rec.start_task(rec_path, self.args.rec_log, self.args.relay_root, (self.sess, self.msg_client))

		cur_time = int(time.time())
		self.msg_client.send(constants.topic.live_monitor, json = {
			"timestamp":	cur_time,
			"rid":		rec.rid(),
			"uid":		rec.uid(),
			"uname":	uname,
			"event":	"record-started",
			"path":		rec_path,
			"title":	title,
		})


	async def update(self):
		uid_list = list(self.records.keys())

		logger.info("checking %d live rooms", len(uid_list))
		info_map = await get_live_status(self.sess, uid_list)
		active_count = 0
		streaming_rooms = []
		recording_rooms = []
		remove_list = {}

		for uid, rec in self.records.items():
			info = info_map.get(str(uid))
			name = rec.name()
			if info:
				(streaming, running) = rec.update(info)
			else:
				logger.warning("no stat for room %d, %s", rec.rid(), name)
				continue

			if streaming:
				streaming_rooms.append(name)

			if running:
				recording_rooms.append(name)
				if rec.enabled():
					active_count += 1

			elif not rec.enabled():
				remove_list[uid] = name

		logger.info("recording rooms %d %s", len(recording_rooms), " ".join(recording_rooms))
		if len(recording_rooms) != len(streaming_rooms):
			logger.info("streaming rooms %d %s", len(streaming_rooms), " ".join(streaming_rooms))

		for uid, name in remove_list.items():
			logger.verbose("remove monitoring of %s", name)
			self.records.pop(uid).close()

		logger.verbose("update complete, active_count %d", active_count)
		return active_count


	def handle_message(self):
		try:
			self.msg_client.wait(timeout = 0)
			sent = False
			while True:
				topic, query = self.msg_client.recv(constants.topic.live_status)
				logger.trace("%s: %s", topic, query)
				if not topic:
					return
				if not isinstance(query, dict) or query.get("action", "") != "get-live-status":
					continue
				logger.verbose("query live status")
				if sent:
					continue

				info = self.get_status()
				resp = {
					"timestamp":	int(time.time()),
					"action":	"publish-live-status",
					"live_status":	info,
				}
				logger.trace(resp)
				self.msg_client.send(topic, json = resp)
				sent = True

		except Exception:
			logger.exception("exception in handle_message")


	def handle_inotify(self):
		assert(self.observer is not None)
		try:
			events = self.observer.read()
			for ev in events:
				logger.trace(ev)
				if ev.mask & IN_IGNORED:
					logger.notice("folder %s disappear, auto-reload halted", self.watch_folder)
				if ev.mask & IN_ISDIR:
					continue
				if ev.name != self.watch_file:
					continue

				logger.debug("name %s, mask %x", ev.name, ev.mask)
				logger.verbose("file %s changed, reloading", self.watch_file)
				self.reload = True
		except Exception:
			logger.exception("exception in handle_inotify")


	def start(self):
		if self.started:
			return
		self.reload_config()
		signal.signal(signal.SIGUSR1, self.on_signal)
		signal.signal(signal.SIGUSR2, self.on_signal)
		self.msg_client.subscribe(constants.topic.live_status)
		asyncio.get_running_loop().add_reader(self.msg_client.fileno(), self.handle_message)

		if self.observer is not None:
			(watch_folder, watch_file) = os.path.split(self.args.config)
			logger.debug("watching folder %s filename %s", watch_folder, watch_file)
			wd = self.observer.add_watch(watch_folder, self.watch_mask)
			logger.debug("inotify watching %s, wd %d", watch_folder, wd)
			self.watch_folder = watch_folder
			self.watch_file = watch_file
			asyncio.get_running_loop().add_reader(self.observer.fileno(), self.handle_inotify)
			logger.info("auto-reload using inotify")

		self.started = True

	async def run(self):
		if not self.started:
			raise RuntimeError("Monitor not started")

		while True:
			if self.reload:
				try:
					logger.info("reloading config")
					self.reload_config()
				except Exception:
					logger.exception("failed to reload config")
				finally:
					self.reload = False

			with suppress(Exception):
				self.msg_client.check_connection()

			active_count = None
			try:
				active_count = await self.update()

			except Exception:
				logger.exception("exception on monitor_check")

			if self.restart and not active_count:
				try:
					exec_restart()
				except Exception:
					self.restart = False
					logger.exception("exception on restart")

			logger.verbose("sleep %d sec", self.args.interval)
			await asyncio.sleep(self.args.interval)


# entrance

async def main(args):
	async with Monitor(args) as monitor:
		await monitor.run()


if __name__ == "__main__":
	args = runtime.parse_args(("network", "auth", "dir", "prefer", "messaging"), [
		(("-i", "--interval"), {"type" : int, "default" : 30}),
		(("--rec-log",), {"action": "store_true", "default": False}),
		(("--auto-reload",), {"action": "store_true", "default": False}),
		(("--relay-root",), {}),
		(("config",), {})
	])
	logger.trace(args)
	asyncio.run(main(args))

