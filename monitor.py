#!/usr/bin/env python3

import os
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

MONITOR_QUERY_URL = "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"

# static objects

logger = logging.getLogger("bili_arch.monitor")
multiprocessing = multiprocessing.get_context("fork")

# helper functions

async def get_live_info(sess, uid_list):
	info_map = await network.request(sess, "POST", MONITOR_QUERY_URL, json = {"uids": uid_list})
	return info_map


async def record_main(rid, path):
	async with network.session() as sess:
		with core.locked_path(path) as rec_path:
			await live_rec.record(sess, rid, rec_path)


def exec_record(*args):
	signal.signal(signal.SIGUSR1, signal.SIG_IGN)
	asyncio.run(record_main(*args))
	os._exit(0)


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


async def task_start(record, rid, path):
	assert(record.get("task") is None)
	task = multiprocessing.Process(
		target = exec_record, args = (
			rid, path
		), daemon = False)
	task.start()
	record["task"] = task


class Config:
	def __init__(self, args):
		self.config_path = args.config
		self.live_root = args.dir or runtime.subdir("live")
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
	info_map = await get_live_info(sess, uid_list)
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

			await task_start(record, rid, rec_path)
			active_rooms.append(name)

		logger.info("active live rooms %d %s", len(active_rooms), " ".join(active_rooms))

		for uid, name in remove_list.items():
			logger.info("remove monitoring of %s(%d)", name, uid)
			assert(config.records[uid]["task"] is None)
			del config.records[uid]


async def monitor_task(config, interval):
	async with network.session() as sess:
		while True:
			try:
				await monitor_check(sess, config)

			except Exception as e:
				logger.exception("exception on monitor_check")

			logger.info("sleep %d sec", interval)
			await asyncio.sleep(interval)




# entrance

async def main(args):
	config = Config(args)
	await config.update()

	def sig_handler(signum, frame):
		logger.info("reloading config")
		asyncio.create_task(config.update())

	async def on_connected(reader, writer):
		logger.debug("reading live status")
		info = await config.get_status()
		writer.write(json.dumps(info, indent = '\t', ensure_ascii = False).encode())
		writer.write_eof()
		await writer.drain()
		writer.close()
		await writer.wait_closed()

	signal.signal(signal.SIGUSR1, sig_handler)

	if args.socket:
		import socket
		try:
			logger.info("creating unix socket %s", args.socket)
			# https://stackoverflow.com/questions/11781134/change-linux-socket-file-permissions
			sock = socket.socket(socket.AF_UNIX)
			os.fchmod(sock.fileno(), 0o600)
			try:
				os.unlink(args.socket)
			except FileNotFoundError:
				pass
			sock.bind(args.socket)
			os.chmod(args.socket, 0o666)

			server = await asyncio.start_unix_server(on_connected, sock = sock, start_serving = True)
		except Exception:
			logger.exception("failed to create unix socket")

	await monitor_task(config, args.interval)


if __name__ == "__main__":
	args = runtime.parse_args(("network", "auth", "dir", "prefer"), [
		(("-c", "--config"), {"required": True}),
		(("-i", "--interval"), {"type" : int, "default" : 30}),
		(("socket",), {"nargs": '?'}),
	])
	asyncio.run(main(args))

