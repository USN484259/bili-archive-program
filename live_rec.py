#!/usr/bin/env python3

import os
import time
import asyncio
import re
import json
from bilibili_api import live, user
import util

schedule_pattern = re.compile(r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?[-~]+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?")


async def record(room, path, resolution = live.ScreenResolution.ORIGINAL):
	util.logv("record live into " + path + " resolution " + str(resolution))

	while True:
		start_time = time.monotonic_ns()
		try:
			play_info = await room.get_room_play_info()
			util.logt(play_info)
			if play_info.get("live_status") != 1:
				break

			info = await room.get_room_play_url(resolution)
			util.logt(info)
			url = info.get("durl")[0].get("url")
			await util.fetch(url, path, mode = "stream")
		except Exception as e:
			util.handle_exception(e)

		stop_time = time.monotonic_ns()
		diff_time = stop_time - start_time
		sleep_time = util.sec_to_ns - diff_time
		if diff_time >= 0 and sleep_time > 0:
			util.logv("restart record after " + str(sleep_time) + "ns")
			await asyncio.sleep(sleep_time / util.sec_to_ns)


def make_record_name(name_struct, title_struct, tm):
	return name_struct.get("name") + '_' + title_struct.get("title") + '_' + time.strftime("%y_%m_%d_%H_%M", tm) + ".flv"


def parse_schedule(sched):
	util.logv("parsing schedule")
	util.logt(sched)
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

	util.logt(result)
	return result


def match_schedule(schedule, tm):
	if not schedule:
		util.logt("empty schedule")
		return True

	t = tm.tm_hour * 3600 + tm.tm_min * 60 + tm.tm_sec

	for sched in schedule:
		util.logt("matching schedule", str(sched[0]) + ',' + str(sched[1]) + ';' + str(t))
		if sched[0] <= t and t <= sched[1]:
			return True
	return False


async def task_record(rid, path):
	room = live.LiveRoom(rid)
	await util.stall()
	room_info = (await room.get_room_info()).get("room_info")
	usr = user.User(room_info.get("uid"))
	user_info = await usr.get_user_info()
	rec_path = path + make_record_name(user_info, room_info, time.localtime())
	await record(room, rec_path)

async def main(args):
	path = util.opt_path(args.dest)
	util.logv(args.inputs)
	task_list = []
	for room_id in args.inputs:
		util.logv("new record task for room " + room_id)
		task = task_record(int(room_id), path)
		task_list.append(task)

	util.logv("recording " + str(len(task_list)) + "live rooms", "path " + path)
	result_list = await asyncio.gather(*task_list, return_exceptions = True)

	for index in range(len(task_list)):
		room_id = args.inputs[index]
		res = result_list[index]
		if isinstance(res, Exception):
			util.loge("exception in recoring room " + room_id, str(res))
			res = False
		else:
			res = True
		print(room_id, str(res), flush = True)


if __name__ == "__main__":
	args = util.parse_args([
		(("inputs",), {"nargs" : '+'}),
	])
	util.run(main(args))
