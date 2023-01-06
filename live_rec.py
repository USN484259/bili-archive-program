#!/usr/bin/env python3

import os
import time
import asyncio
import re
import json
from bilibili_api import live, user
import util


async def do_record(room, path, resolution):
	util.logv("record live into " + path + " resolution " + str(resolution))
	info = await room.get_room_play_url(resolution)
	util.logt(info)
	url = info.get("durl")[0].get("url")
	async with util.session() as sess:
		await util.fetch(sess, url, path, mode = "stream")


async def record(rid, path, credential = None, resolution = live.ScreenResolution.ORIGINAL):
	await util.stall()
	room = live.LiveRoom(rid, credential = credential)
	return do_record(room, path, resolution)


def parse_schedule(sched):
	# FIXME review regex & range generation
	parser = re.compile(r"(\d+)\:(\d+)(?<=\:)\d*")
	result = []
	for i, v in enumerate(sched):
		pair = []
		for i in range(2):
			match = parser.full_match(v[i])
			value = int(match.group(1)) * 3600 + int(match.group(2)) * 60
			if match.group(3):
				value = value + int(match.group(3))
			pair[i] = value

		if pair[0] <= pair[1]:
			result = result + pair
		else:
			result = result + [pair[0], 86400]
			result = result + [0, pair[1]]

	return result


def match_schedule(schedule, tm):
	if not schedule:
		return True

	t = tm.tm_hour * 3600 + tm.tm_min * 60 + tm.tm_sec

	for i, v in enumerate(schedule):
		if v[0] <= t and t <= v[1]:
			return True
	return False


async def monitor(rid, path = None, interval = 60, credential = None, schedule = None):
	# TODO more logs, defend against poor network / DoS
	path = util.opt_path(path)
	util.logi("monitoring room " + str(rid))
	await util.stall()
	room = live.LiveRoom(rid, credential = credential)
	state = "idle"
	rec_name = None
	while True:
		try:
			util.logv("checking room " + str(rid))
			await util.stall()
			play_info = await room.get_room_play_info()
			util.logt(play_info)
			live_status = play_info.get("live_status")
			if live_status == 1:
				util.logi("room " + str(rid) + " is streaming")
				live_info = await room.get_room_info()
				util.logt(live_info)
				if state == "idle":
					tm = time.localtime()
					util.logv("current time" + str(tm))
					if match_schedule(schedule, tm):
						usr = user.User(play_info.get("uid"))
						user_info = await usr.get_user_info()
						room_info = await room.get_room_info()
						rec_name = user_info.get("name") + '_' + room_info.get("room_info").get("title") + '_' + str(tm.tm_year) + '_' + str(tm.tm_mon) + '_' + str(tm.tm_mday) + '_' + str(tm.tm_hour) + '_' + str(tm.tm_min)
						state = "record"
						util.logv("schedule matched, start recording")

				if state == "record":
					util.logi("recording room " + str(rid) + '\t' + rec_name)
					await do_record(room, path + rec_name, live.ScreenResolution.ORIGINAL)

			else:
				state = "idle"
				rec_file = None
				util.logv("room " + str(rid) + " not streaming " + str(live_status))
		except Exception as e:
			util.handle_exception(e, "failed to monitor " + str(rid))

		timeout = 1 if state == "record" else interval
		util.logv("sleep for " + str(timeout) + " seconds")
		await asyncio.sleep(timeout)


async def main(args):
	credential = None
	if args.auth:
		credential = util.credential(args.auth)

	# TODO more configs & dynamic configuration
	config = None
	with open(args.inputs[0], "r") as f:
		config = json.load(f)

	interval = config.get("interval", 60)

	monitor_list = []
	for i, cfg in enumerate(config.get("list")):
		rid = cfg.get("rid")
		schedule = cfg.get("schedule", None)
		if schedule:
			schedule = parse_schedule(schedule)

		monitor_list.append(monitor(rid, args.dest, interval, credential, schedule))

	await asyncio.gather(*monitor_list)


if __name__ == "__main__":
	args = util.parse_args()
	util.run(main(args))
