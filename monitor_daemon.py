#!/usr/bin/env python3

import os
import re
import sys
import time
import json
import logging
import asyncio
from bilibili_api.user import User
from bilibili_api.live import LiveRoom
import util
import live_rec

logger = logging.getLogger(__name__)


class content_monitor_base:
	def __init__(self):
		self.config = None
		self.task = None

	def get(self):
		return {
			"enabled":	bool(self.config),
			"running":	bool(self.task and not self.task.done())
		}

	def set(self, vargs):
		if len(vargs) != 1:
			raise ValueError()
		cfg = vargs[0].lower()
		if cfg == "true":
			self.config = True
		elif cfg == "false":
			self.config = False
		else:
			raise ValueError()

	async def cancel(self):
		self.config = None
		if self.task:
			self.task.cancel()
			# await self.task
			self.task = None

	async def check(self, usr):
		raise NotImplementedError()

	async def worker(self, usr, data):
		raise NotImplementedError()

	async def step(self, usr):
		if self.task:
			if self.task.done():
				logger.debug("task completed")
				self.task = None
			else:
				logger.debug("task running, skip")
				return

		userdata = await self.check(usr)
		if not userdata:
			return
		logger.debug("starting task")
		self.task = asyncio.create_task(self.worker(usr, userdata))
		self.task.add_done_callback(asyncio.Task.result)


class content_monitor_live(content_monitor_base):
	def __init__(self):
		super().__init__()
		self.state = "idle"
		self.rec_name = None

	def set(self, vargs):
		try:
			return super().set(vargs)
		except ValueError:
			pass

		self.config = live_rec.parse_schedule(vargs)

	def get(self):
		result = super().get()
		if isinstance(self.config, list):
			result["schedule"] = self.config

		return result

	async def check(self, usr):
		if not self.config:
			return

		logger.debug("monitoring live room")
		live_info = await usr.get_live_info()
		logger.log(util.LOG_TRACE, live_info)
		room_info = live_info.get("live_room")
		if not room_info:
			logger.debug("no live room, skip")
			return

		room_id = room_info.get("roomid")
		live_status = room_info.get("liveStatus")
		logger.debug("live room %d, status %d", room_id, live_status)
		if live_status != 1:
			self.state = "idle"
			self.rec_name = None
			return

		if self.state == "idle":
			tm = time.localtime()
			if self.config == True or live_rec.match_schedule(self.config, tm):
				self.rec_name = util.opt_path(monitor_root.get("dest")) + "live" + os.path.sep + live_rec.make_record_name(live_info, room_info, tm)
				self.state = "record"
				logger.debug("new record for room %d, %s", room_id, self.rec_name)
				return live_info
			else:
				self.state = "skip"
				logger.debug("schedule not match, skip recording room %d", room_id)
				return

		elif self.state == "record":
			logger.debug("continue recording room %d", room_id)
			return live_info

	async def worker(self, usr, live_info):
		room_id = live_info.get("live_room").get("roomid")
		room = LiveRoom(room_id)
		util.mkdir(self.rec_name)
		task_danmaku = asyncio.create_task(live_rec.record_danmaku(room, self.rec_name))
		task_danmaku.add_done_callback(asyncio.Task.result)
		try:
			logger.debug("start recording room %d", room_id)
			await live_rec.record(room, self.rec_name)
		finally:
			logger.debug("stop recording room %d", room_id)
			task_danmaku.cancel()


class content_monitor_dynamic(content_monitor_base):
	def __init__(self):
		super().__init__()
		self.last_id = None

	async def check(self, usr):
		if not self.config:
			return
		dyn_list = await usr.get_dynamics().get("cards")
		if len(dyn_list) == 0:
			return

		dyn_id = dyn_list[0].get("desc").get("dynamic_id")
		if not self.last_id:
			self.last_id = dyn_id
			return
		elif self.last_id == dyn_id:
			return dyn_list


class content_monitor_video(content_monitor_base):
	def __init__(self):
		super().__init__()
		self.last_bvid = None

	async def check(self, usr):
		if not self.config:
			return
		video_list = await usr.get_videos().get("list").get("vlist")

		if len(video_list) == 0:
			return

		bv_id = video_list[0].get("bvid")
		if not self.last_bvid:
			self.last_bvid = bv_id
			return
		elif self.last_bvid == bv_id:
			return video_list


class user_monitor:
	def __init__(self, user, name):
		self.user = user
		self.name = name

		self.content_monitor = {
			"live":		content_monitor_live(),
			"dynamic":	content_monitor_dynamic(),
			"video":	content_monitor_video()
		}

	def get(self, key = None):
		if key:
			return self.content_monitor[key].get()
		else:
			result = {
				"uid":	self.user.get_uid(),
				"name":	self.name
			}
			for c, m in self.content_monitor.items():
				result[c] = m.get()

			return result

	def set(self, key, vargs):
		return self.content_monitor[key].set(vargs)

	async def step(self):
		logger.debug("monitoring " + self.name)
		for c, m in self.content_monitor.items():
			logger.debug("checking " + c)
			await util.stall()
			await m.step(self.user)

	async def cancel(self):
		logger.debug("monitor exit " + self.name)
		for c, m in self.content_monitor.items():
			logger.debug("cancel " + c)
			await m.cancel()


async def worker_monitor():
	while True:
		try:
			for item in monitor_root.get("list"):
				await item.step()
		except Exception as e:
			logger.exception("exception on monitor step")
			util.on_exception(e)

		interval = monitor_root.get("interval", 60)
		logger.debug("monitor sleep %d sec", interval)
		await asyncio.sleep(interval)


async def on_command(cmd_list):
	monitor_list = monitor_root.get("list")

	if len(cmd_list) == 1 and cmd_list[0] == "get":
		result = []
		for item in monitor_list:
			result.append({
				"rid":	item.user.get_uid(),
				"name":	item.name
			})
		return result

	if len(cmd_list) >= 2 and cmd_list[0] in ["get", "set", "del"]:
		target = None
		for index, item in enumerate(monitor_list):
			if item.name == cmd_list[1]:
				target = (index, item)
				break
		else:
			raise Exception("user not found")

		if cmd_list[0] == "set" and len(cmd_list) >= 3:
			return target[1].set(cmd_list[2], cmd_list[3:])

		elif cmd_list[0] == "get" and len(cmd_list) <= 3:
			key = None
			if len(cmd_list) == 3:
				key = cmd_list[2]
			return target[1].get(key)

		elif cmd_list[0] == "del" and len(cmd_list) == 2:
			await target[1].cancel()
			del monitor_list[target[0]]
			return

	elif len(cmd_list) <= 3 and cmd_list[0] == "new":
		uid = int(cmd_list[1])
		usr = User(uid)
		name = None
		if len(cmd_list) == 3:
			name = cmd_list[2]
		else:
			name = (await usr.get_user_info()).get("name")

		logger.debug("add user %s, uid %d", name, uid)
		for item in monitor_list:
			if item.user.get_uid() == uid or item.name == name:
				raise Exception("user already exists")

		monitor_list.append(user_monitor(usr, name))
		return

	raise Exception("invalid command")


async def on_connected(reader, writer):
	async def write_func(data):
		writer.write(data.encode())
		writer.write('\n'.encode())
		await writer.drain()

	while not reader.at_eof():
		cmd_list = (await reader.readline()).decode().split()
		logger.info("request\t" + str(cmd_list))
		if len(cmd_list) == 0:
			continue

		try:
			result = await on_command(cmd_list)
			if result is None:
				logger.info("no response")
			else:
				logger.info("response\t" + str(result))
				await write_func(json.dumps(result, ensure_ascii = False))

		except Exception as e:
			logger.exception("exception on processing request")
			util.on_exception(e)
			await write_func(type(e).__name__ + ' ' + str(e))

	writer.close()
	await writer.wait_closed()


def load_config(f):
	result = json.load(f)
	return result


async def main(args):
	global monitor_root
	monitor_root = {
		"interval":	60,
		"dest":		"",
		"list":		[]
	}
	if args.config:
		with open(args.config, "r") as f:
			monitor_root = load_config(f)

	if args.dest:
		monitor_root["dest"] = util.opt_path(args.dest)

	monitor_list = []
	for item in monitor_root.get("list"):
		uid = item.get("uid")
		usr = User(uid)
		name = item.get("name", None) or (await usr.get_user_info()).get("name")

		logger.debug("add user %s, uid %d", name, uid)
		monitor = user_monitor(usr, name)

		for key in ["live", "dynamic", "video"]:
			cfg = item.get(key)
			logger.debug("configured %s\t%s", key, str(cfg))
			if cfg is True:
				cfg = ["true"]
			if cfg:
				monitor.set(key, cfg)

		monitor_list.append(monitor)

	monitor_root["list"] = monitor_list
	monitor_task = asyncio.create_task(worker_monitor())
	monitor_task.add_done_callback(asyncio.Task.result)

	server = await asyncio.start_unix_server(on_connected, path = args.socket)
	await server.serve_forever()


if __name__ == "__main__":
	args = util.parse_args([
		(("socket",), {}),
		(("-u", "--auth"), {}),
		(("-c", "--config"), {})
	])

	util.run(main(args))

