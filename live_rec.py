#!/usr/bin/env python3

import os
import time
import asyncio
import re
import json
import logging
from collections import deque
import bilibili_api
from bilibili_api import live, user
import util

logger = logging.getLogger("live_rec")

schedule_pattern = re.compile(r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?[-~]+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?")


async def fetch_stream(url, sink_list):
	sess = bilibili_api.get_session()
	logger.debug("streaming %s with %d sinks", url, len(sink_list))
	async with sess.stream("GET", url, headers = util.agent, timeout = util.http_timeout) as resp:
		logger.debug(resp)
		resp.raise_for_status()

		async for chunk in resp.aiter_bytes():
			for sink in sink_list:
				sink.write(chunk)


async def worker_danmaku(room, path, filename = "danmaku.json"):
	try:
		path = util.opt_path(path)
		rec_name = path + filename
		logger.debug("record live danmaku into " + rec_name)
		conn = live.LiveDanmaku(room.room_display_id)
		with open(rec_name, "a") as f:
			mutex = asyncio.Lock()
			queue = deque()
			cond = asyncio.Condition()
			async def worker_wget(path):
				try:
					while True:
						name = None
						url = None
						async with cond:
							logger.log(util.LOG_TRACE, "wget standby")
							await cond.wait()
							name, url = queue.popleft()

						logger.debug("fetch emot " + name)
						ext = os.path.splitext(url)[1]
						if not ext or ext == "":
							ext = ".png"
						emot_file = path + name + ext
						logger.log(util.LOG_TRACE, "%s\t%s", emot_file, url)
						if os.path.isfile(emot_file):
							logger.debug("skip existing emot")
						else:
							await util.fetch(url, emot_file)
				except Exception as e:
					logger.exception("exception in worker_wget")
					util.on_exception(e)
					raise


			async def on_event(info):
				logger.log(util.LOG_TRACE, info)
				cmd = info.get("type")
				data = info.get("data")
				if not data:
					data = {}
				elif not isinstance(data, dict):
					data = { "data": data }

				data["cmd"] = cmd
				data["timestamp"] = int(time.time_ns() / util.sec_to_ns)

				async with mutex:
					logger.debug(cmd)
					f.write(json.dumps(data, ensure_ascii = False) + '\n')

				for meta in data.get("info", [[]])[0]:
					if not isinstance(meta, dict):
						continue
					emot_name = meta.get("emoticon_unique")
					emot_url = meta.get("url")
					if emot_name and emot_url:
						logger.log(util.LOG_TRACE, "scheduled emot fetch " + emot_name)
						async with cond:
							queue.append((emot_name, emot_url))
							cond.notify()

			task_wget = asyncio.create_task(worker_wget(path))
			# task_wget.add_done_callback(asyncio.Task.result)
			conn.add_event_listener("ALL", on_event)

			while True:
					try:
						logger.debug("connect to live danmaku")
						await conn.connect()
					except Exception as e:
						logger.exception("exception on recording danmaku")
						util.on_exception(e)

	except Exception as e:
		logger.exception("exception in worker_danmaku")
		util.on_exception(e)
		raise

	finally:
		logger.debug("disconnect live danmaku")
		task_wget.cancel()
		await conn.disconnect()
		try:
			await task_wget
		except asyncio.CancelledError:
			pass


async def worker_record(room, path, mode, resolution = live.ScreenResolution.ORIGINAL):
	try:
		logger.debug("record live into %s, mode %s, resolution %s", path, mode, str(resolution))
		if mode == "remux" or mode == "both":
			try:
				from gst_remuxer import GstRemuxer
			except:
				logger.exception("failed in loading remuxer")
				logger.warning("no GStreamer, fallback to save mode")
				mode = "save"

		while True:
			start_time = time.time_ns()
			sink_list = []
			try:
				play_info = await room.get_room_play_info()
				logger.log(util.LOG_TRACE, play_info)
				if play_info.get("live_status") != 1:
					break

				info = await room.get_room_play_url(resolution)
				logger.log(util.LOG_TRACE, info)
				url = info.get("durl")[0].get("url")

				save_flv = (mode != "remux")
				if mode == "remux" or mode == "both":
					filename = str(start_time // util.sec_to_ns) + ".mp4"
					logger.debug("remux file " + filename)
					try:
						remuxer = GstRemuxer(os.path.join(path, filename))
						remuxer.start()
						sink_list.append(remuxer)
					except:
						logger.exception("exception in creating remuxer")
						if mode == "remux":
							logger.warning("remux failed, fallback to save mode")
							save_flv = True

				if save_flv:
					filename = str(start_time // util.sec_to_ns) + ".flv"
					logger.debug("record file " + filename)
					file_sink = open(os.path.join(path, filename), mode = "wb")
					sink_list.append(file_sink)


				await fetch_stream(url, sink_list)
				# await util.fetch(url, os.path.join(path, filename), mode = "stream")
			except Exception as e:
				logger.exception("exception on recording")
				util.on_exception(e)

			finally:
				for sink in sink_list:
					sink.close()

			stop_time = time.time_ns()
			diff_time = stop_time - start_time
			sleep_time = util.sec_to_ns - diff_time
			if diff_time >= 0 and sleep_time > 0:
				logger.debug("restart record after %d ns", sleep_time)
				await asyncio.sleep(sleep_time / util.sec_to_ns)

	except Exception as e:
		logger.exception("exception in worker_record")
		util.on_exception(e)
		raise


def parse_schedule(sched):
	logger.debug("parsing schedule")
	logger.log(util.LOG_TRACE, sched)
	result = []

	for v in sched:
		match = schedule_pattern.fullmatch(v)
		if not match:
			raise ValueError()

		head = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3) or 0)
		tail = int(match.group(4)) * 3600 + int(match.group(5)) * 60 + int(match.group(6) or 0)
		if head <= tail:
			result.append((head, tail))
		else:
			result.append((head, 86400))
			result.append((0, tail))

	logger.log(util.LOG_TRACE, result)
	return result


def match_schedule(schedule, tm):
	if not schedule:
		logger.log(util.LOG_TRACE, "empty schedule")
		return True

	t = tm.tm_hour * 3600 + tm.tm_min * 60 + tm.tm_sec

	for sched in schedule:
		logger.log(util.LOG_TRACE, "matching schedule (%d, %d) <=> %d", sched[0], sched[1], t)
		if sched[0] <= t and t <= sched[1]:
			return True
	return False


async def record(room, path, uname, title, mode = "save"):
	rec_path = os.path.join(path, uname + '_' + title + '_' + time.strftime("%y_%m_%d_%H_%M"))
	logger.info("recording liveroom %d into %s", await room.get_room_id(), rec_path)
	util.mkdir(rec_path)
	task_danmaku = asyncio.create_task(worker_danmaku(room, rec_path))
	task_danmaku.add_done_callback(asyncio.Task.result)

	try:
		await worker_record(room, rec_path, mode)
	finally:
		task_danmaku.cancel()
		try:
			await task_danmaku
		except asyncio.CancelledError:
			pass


async def main(args):
	room = live.LiveRoom(args.room)
	room_info = (await room.get_room_info()).get("room_info")
	usr = user.User(room_info.get("uid"))
	user_info = await usr.get_user_info()

	await record(room, util.opt_path(args.dest), user_info.get("name"), room_info.get("title"), args.mode)


if __name__ == "__main__":
	args = util.parse_args([
		(("room",), {"type" : int}),
		(("-m", "--mode"), {"choices": ["save", "remux", "both"], "default": "save"}),
	])
	util.run(main(args))
