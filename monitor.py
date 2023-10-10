#!/usr/bin/env python3

import json
import asyncio
import logging
import multiprocessing
from bilibili_api import live
import util
import live_rec

logger = logging.getLogger("monitor")


def exec_record(rid, credential, path, uname, title):
	util.run(live_rec.record(rid, credential, path, uname, title))


async def check(config, credential):
	await util.stall()
	info_list = await live.get_live_followers_info(need_recommend = False, credential = credential)
	rid_table = {}
	for info in info_list.get("rooms", []):
		# name = info.get("uname")
		# uid = info.get("uid")
		rid = info.get("roomid")
		if rid:
			logger.debug("active room %d", rid)
			rid_table[rid] = info

	logger.info("active live rooms: %d", len(rid_table))

	for record in config.get("list", []):
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
				config.get("path"),
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

	config = {}

	if args.config:
		with open(args.config, "r") as f:
			config = json.load(f)

	if args.dest:
		config["path"] = args.dest

	config["path"] = util.opt_path(config.get("path"))

	while True:
		try:
			logger.info("checking live")
			await check(config, credential)
		except Exception:
			logger.exception("exception on checking")

		interval = config.get("interval", 60)
		logger.info("checking done, sleep %d sec", interval)
		await asyncio.sleep(interval)


if __name__ == "__main__":
	args = util.parse_args([
		(("-u", "--auth"), {"required": True}),
		(("-c", "--config"), {"required": True}),
	])
	util.run(main(args))

