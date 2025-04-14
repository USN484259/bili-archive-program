#!/usr/bin/env python3

import os
import sys
import json
import signal
import asyncio
import logging
import multiprocessing

import core
import runtime
import network
import live_rec

# constants

LIVE_STATUS_URL = "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"

# static objects

logger = logging.getLogger("bili_arch.monitor")
multiprocessing = multiprocessing.get_context("fork")
scheduled_restart = False

# helper functions

async def get_live_status(sess, uid_list):
	info_map = await network.request(sess, "POST", LIVE_STATUS_URL, json = {"uids": uid_list})
	return info_map


async def record_main(rid, path, rec_log, relay_path):
	with core.locked_path(path) as rec_path:
		if rec_log:
			log_path = os.path.join(rec_path, "record.log")
			runtime.logging_init(runtime.log_level - 10, log_path, no_stderr = True)
		async with network.session() as sess:
			await live_rec.record(sess, rid, rec_path, relay_path = relay_path)


def exec_record(*args):
	signal.signal(signal.SIGUSR1, signal.SIG_IGN)
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


async def task_cleanup(record):
	task = record.get("task")
	if task is not None:
		if task.is_alive():
			name = record.get("name") or record.get("uid", "")
			logger.debug("%s recording, skip", name)
			return False
		task.close()
		record["task"] = None
	return True


async def task_start(record, rid, path, rec_log, relay_root):
	assert(record.get("task") is None)
	task = multiprocessing.Process(
		target = exec_record, args = (
			rid, path, rec_log, (relay_root and os.path.join(relay_root, str(rid)) or None)
		), daemon = False)
	task.start()
	record["task"] = task


class Config:
	def __init__(self, args):
		self.config_path = args.config
		self.live_root = args.dir or runtime.subdir("live")
		self.rec_log = args.rec_log
		self.relay_root = args.relay_root
		self.records = {}
		self.lock = asyncio.Lock()

	async def update(self):
		logger.debug("loading config from %s", self.config_path)
		with open(self.config_path, "r") as f:
			user_list = json.load(f)

		new_records = {}
		for elem in user_list:
			uid = elem["uid"]
			elem["task"] = None
			new_records[uid] = elem

		async with self.lock:
			for uid, rec in self.records.items():
				if rec["task"] is None:
					continue

				if uid not in new_records:
					rec["remove"] = True
					new_records[uid] = rec
				else:
					new_records[uid]["task"] = rec["task"]

			self.records = new_records

	async def get_status(self):
		result = {}
		async with self.lock:
			for uid, record in self.records.items():
				result[uid] = record.get("info", {})

		return result


# methods

async def monitor_check(sess, config):
	async with config.lock:
		uid_list = list(config.records.keys())

	logger.info("checking %d live rooms", len(uid_list))
	info_map = await get_live_status(sess, uid_list)
	active_rooms = []
	remove_list = {}

	async with config.lock:
		for uid, record in config.records.items():
			name = record.get("name", str(uid))

			if not await task_cleanup(record):
				active_rooms.append(name)
				continue

			if record.get("remove"):
				remove_list[uid] = name
				continue

			info = info_map.get(str(uid))
			if not info:
				logger.warning("no stat for %s(%d)", name, uid)
				continue

			rid = info.get("room_id", 0)
			if record.get("rid", rid) != rid:
				logger.warning("%s(%d) rid mismatch: %d/%d", name, uid, rid, record.get("rid", 0))

			status = info.get("live_status", -1)

			logger.debug("%s, room %d, status %d", name, rid, status)

			record["info"] = info

			if not record.get("record", True):
				continue

			if status != 1:
				continue

			uname = info.get("uname", str(uid))
			title = info.get("title", "")
			rec_name = live_rec.make_record_name(uname, title)
			rec_path = os.path.join(config.live_root, rec_name)
			logger.info("start recording %s, room %d, %s", name, rid, rec_name)

			await task_start(record, rid, rec_path, config.rec_log, config.relay_root)
			active_rooms.append(name)

		logger.info("active live rooms %d %s", len(active_rooms), " ".join(active_rooms))

		for uid, name in remove_list.items():
			logger.info("remove monitoring of %s(%d)", name, uid)
			assert(config.records[uid]["task"] is None)
			del config.records[uid]

		return len(active_rooms)


async def monitor_task(config, interval):
	global scheduled_restart
	async with network.session() as sess:
		while True:
			try:
				active_count = await monitor_check(sess, config)

			except Exception:
				logger.exception("exception on monitor_check")

			logger.info("sleep %d sec", interval)
			await asyncio.sleep(interval)

			if scheduled_restart and active_count == 0:
				try:
					exec_restart()
				except Exception:
					scheduled_restart = False
					logger.exception("exception on restart")


# entrance

async def main(args):
	config = Config(args)
	await config.update()

	def sig_reload(signum, frame):
		logger.info("reloading config")
		asyncio.create_task(config.update())

	def sig_restart(signum, frame):
		global scheduled_restart
		logger.info("restart scheduled")
		scheduled_restart = True

	async def on_connected(reader, writer):
		logger.debug("reading live status")
		info = await config.get_status()
		writer.write(json.dumps(info, indent = '\t', ensure_ascii = False).encode())
		writer.write_eof()
		await writer.drain()
		writer.close()
		await writer.wait_closed()

	signal.signal(signal.SIGUSR1, sig_reload)
	signal.signal(signal.SIGUSR2, sig_restart)

	if args.socket:
		try:
			sock = network.create_unix_socket(args.socket, mode = 0o666)
			server = await asyncio.start_unix_server(on_connected, sock = sock, start_serving = True)
		except Exception:
			logger.exception("failed to create unix socket")

	await monitor_task(config, args.interval)


if __name__ == "__main__":
	args = runtime.parse_args(("network", "auth", "dir", "prefer"), [
		(("-i", "--interval"), {"type" : int, "default" : 30}),
		(("--rec-log",), {"action": "store_true", "default": False}),
		(("--relay-root",), {}),
		(("--socket",), {}),
		(("config",), {})
	])
	asyncio.run(main(args))

