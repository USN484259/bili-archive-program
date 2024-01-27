#!/usr/bin/env python3

import json
import asyncio
import logging
import multiprocessing
from bilibili_api import live
import util
import live_rec

logger = logging.getLogger("bili_arch.monitor")

multiprocessing = multiprocessing.get_context("fork")

def exec_record(rid, credential, live_root, uname, title):
	room = live.LiveRoom(rid, credential)
	util.run(live_rec.record(room, credential, live_root, uname, title))


async def check(live_root, config, credential):
	await util.stall()
	info_list = await live.get_live_followers_info(need_recommend = False, credential = credential)
	rid_table = {}
	for info in info_list.get("rooms", []):
		name = info.get("uname")
		# uid = info.get("uid")
		rid = info.get("roomid")
		if rid:
			logger.debug("active room %d, %s", rid, name)
			rid_table[rid] = info

	logger.info("active live rooms: %d", len(rid_table))

	for record in config:
		name = record.get("name", "")
		rid = record.get("rid")
		if not rid:
			logger.warning("%s\tmissing rid, skip", name)
			continue

		task = record.get("task")
		if task is not None:
			if task.is_alive():
				logger.debug("%s\troom %d: task running, skip", name, rid)
				continue
			task.close()
			record["task"] = None

		if not record.get("enable", True):
			logger.debug("%s\troom %d: disabled", name, rid)
			continue

		info = rid_table.get(rid)
		if not info:
			logger.debug("%s\troom %d: inactive", name, rid)
			continue

		if not info.get("playurl"):
			logger.warning("empty play URL")

		uname = info.get("uname")
		title = info.get("title")
		logger.info("start recording %s\troom %d, user %s, title %s", name, rid, uname, title)

		await util.stall()
		task = multiprocessing.Process(
			target = exec_record, args = (
				rid,
				credential,
				live_root,
				uname,
				title
			), daemon = False)
		task.start()
		record["task"] = task


async def main(args):
	await util.wait_online()
	credential = None
	if args.auth:
		credential = await util.credential(args.auth)

	with open(args.config, "r") as f:
		config = json.load(f)

	live_root = args.dir or util.subdir("live")
	logger.info("monitoring %d rooms, record into %s" % (len(config), live_root))

	while True:
		try:
			logger.info("checking live")
			await check(live_root, config, credential)
		except Exception:
			logger.exception("exception on checking")

		logger.info("checking done, sleep %d sec", args.interval)
		await asyncio.sleep(args.interval)


if __name__ == "__main__":
	args = util.parse_args([
		(("-u", "--auth"), {"required": True}),
		(("-c", "--config"), {"required": True}),
		(("-d", "--dir"), {}),
		(("-i", "--interval"), {"type" : int, "default" : 30}),
	])
	util.run(main(args))

