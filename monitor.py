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

# constants

LIVE_STATUS_URL = "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"
PROC_TERM_TIMEOUT = 2

# static objects

logger = logging.getLogger("bili_arch.monitor")
multiprocessing = multiprocessing.get_context("fork")

# helper functions

async def get_live_status(sess, uid_list):
	resp = await network.request(sess, "POST", LIVE_STATUS_URL, json = {"uids": uid_list})
	return resp.get("data")


async def record_main(rid, path, rec_log, relay_path):
	with fops.locked_path(path) as rec_path:
		if rec_log:
			log_path = os.path.join(rec_path, "record.log")
			logger_init(runtime.log_level + 1, log_path, noprint = True)
		async with network.session() as sess:
			await live_rec.record(sess, rid, rec_path, relay_path = relay_path)


def exec_record(args, to_close):
	signal.signal(signal.SIGUSR1, signal.SIG_IGN)
	signal.signal(signal.SIGUSR2, signal.SIG_IGN)
	for obj in to_close:
		with suppress(Exception):
			obj.close()
	asyncio.run(record_main(*args))
	os._exit(0)


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


class Record:
	@staticmethod
	def validate(config, info):
		if not info:
			return

		if not isinstance(config.get("uid"), int):
			raise RuntimeError("invalid uid in config")

		if config.get("rid") != info.get("room_id"):
			logger.warning("%s rid mismatch: %s/%s", config.get("name", ""), config.get("rid"), info.get("room_id"))

		if config.get("uid") != info.get("uid"):
			logger.warning("%s uid mismatch: %s/%s", config.get("name", ""), config.get("uid"), info.get("uid"))


	def __init__(self, config, task_start_cb, task_stop_cb):
		self.config = config
		self.task_start_cb = task_start_cb
		self.task_stop_cb = task_stop_cb
		self.info = {}
		self.task = None
		self.path = None
		self.start_time = None
		self.stop_time = None


	def close(self):
		if self.check_task():
			logger.warning("rid %d task still running, terminating", self.rid())
			try:
				self.task.terminate()
				self.task.join(PROC_TERM_TIMEOUT)
				self.task.close()
			finally:
				self.task = None


	def reload(self, config):
		self.validate(config, self.info)
		self.config = config


	def status(self):
		return self.info

	def rid(self):
		return self.info.get("room_id", self.config.get("rid"))

	def uid(self):
		return self.info.get("uid", self.config.get("uid"))

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
		self.config["enable"] = False

	def check_task(self):
		if self.task is not None:
			if self.task.is_alive():
				logger.debug("%s recording", self.name())
				return True
			self.task.close()
			self.stop_time = int(time.time())
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
		self.path = path
		self.start_time = int(time.time())
		self.stop_time = None

	def update(self, info):
		running = self.check_task()

		if not info:
			return
		if not self.info:
			self.validate(self.config, info)
		self.info = info
		if not self.need_record():
			return
		if self.info.get("live_status", -1) == 1 and not running:
			try:
				self.task_start_cb(self, info)
			except Exception:
				logger.exception("exception in task_start_cb")
			running = self.check_task()

		return running

# main classes

class Monitor:
	def __init__(self, args):
		self.args = args
		self.records = {}
		self.msg_client = MessagingClient(args.msg_addr)
		self.sess = network.session()
		self.reload = False
		self.restart = False


	async def close(self):
		if self.sess is not None:
			try:
				await self.sess.aclose()
			finally:
				self.sess = None
		self.msg_client.close()
		for rec in self.records.values():
			rec.close()


	async def __aenter__(self):
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
		logger.debug("loading config from %s", self.args.config)
		with open(self.args.config, "r") as f:
			user_list = json.load(f)

		existing_keys = set(self.records.keys())
		for config in user_list:
			uid = config["uid"]
			if not config.get("enable", True):
				logger.debug("disabled %s", config.get("name", str(uid)))
				continue

			rec = self.records.get(uid)
			if rec is not None:
				rec.reload(config)
			else:
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
		cur_time = int(time.time())
		self.msg_client.send(constants.topic.live_rec, json = {
			"from":		"monitor",
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
		self.msg_client.send(constants.topic.live_rec, json = {
			"from":		"monitor",
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
		hold_count = 0
		active_rooms = []
		remove_list = {}

		for uid, rec in self.records.items():
			info = info_map.get(str(uid))
			if info:
				running = rec.update(info)
			else:
				logger.warning("no stat for %s(%d)", name, uid)

			name = rec.name()
			if running:
				active_rooms.append(name)
				if rec.enabled():
					hold_count += 1

			elif not rec.enabled():
				remove_list[uid] = name

		logger.info("active live rooms %d %s", len(active_rooms), " ".join(active_rooms))

		for uid, name in remove_list.items():
			logger.info("remove monitoring of %s(%d)", name, uid)
			self.records.pop(uid).close()

		return hold_count


	def handle_message(self):
		try:
			self.msg_client.wait(timeout = 0)
			sent = False
			while True:
				topic, query = self.msg_client.recv(constants.topic.live_status)
				if not topic:
					return
				if sent:
					continue
				if not isinstance(query, dict) or query.get("action", "") != "get-live-status":
					continue
				info = self.get_status()
				resp = {
					"timestamp":	int(time.time()),
					"live-status":	info,
				}
				self.msg_client.send(topic, json = resp)
				sent = True

		except Exception:
			logger.exception("exception in handle_message")


	async def start(self):
		self.reload_config()
		signal.signal(signal.SIGUSR1, self.on_signal)
		signal.signal(signal.SIGUSR2, self.on_signal)

		if not self.msg_client.is_dummy():
			asyncio.get_running_loop().add_reader(self.msg_client.fileno(), self.handle_message)


	async def run(self):
		await self.start()
		while True:
			if self.reload:
				try:
					self.reload_config()
				except Exception:
					logger.exception("failed to reload config")
				finally:
					self.reload = False

			with suppress(Exception):
				self.msg_client.check_connection()

			try:
				active_count = await self.update()

			except Exception:
				logger.exception("exception on monitor_check")

			if self.restart and active_count == 0:
				try:
					exec_restart()
				except Exception:
					self.restart = False
					logger.exception("exception on restart")

			logger.info("sleep %d sec", self.args.interval)
			await asyncio.sleep(self.args.interval)


# entrance

async def main(args):
	async with Monitor(args) as monitor:
		await monitor.run()

	config = Config(args)
	await config.update()

	def sig_reload(signum, frame):
		global scheduled_reload
		logger.info("reload scheduled")
		scheduled_reload = True

	def sig_restart(signum, frame):
		global scheduled_restart
		logger.info("restart scheduled")
		scheduled_restart = True

	signal.signal(signal.SIGUSR1, sig_reload)
	signal.signal(signal.SIGUSR2, sig_restart)

	msg_client = MessagingClient(args.msg_addr)
	if not msg_client.is_dummy():
		asyncio.get_running_loop().add_reader(msg_client.fileno(), lambda: asyncio.create_task(handle_message(msg_client)))

	await monitor_task(config, args.interval, msg_client)


if __name__ == "__main__":
	args = runtime.parse_args(("network", "auth", "dir", "prefer", "messaging"), [
		(("-i", "--interval"), {"type" : int, "default" : 30}),
		(("--rec-log",), {"action": "store_true", "default": False}),
		(("--relay-root",), {}),
		(("config",), {})
	])
	asyncio.run(main(args))

