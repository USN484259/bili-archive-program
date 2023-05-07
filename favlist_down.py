#!/usr/bin/env python3

import os
import re
import logging
from bilibili_api import favorite_list
import util
import bv_down

logger = logging.getLogger(__name__)

async def download(mid, path = None, credential = None):
	path = util.opt_path(path)

	logger.debug("fetching favlist %d into %s auth %x", mid, path, bool(credential))
	page_index = 1
	await util.stall()
	logger.debug("fetching page %d", page_index)
	favlist_head = await favorite_list.get_video_favorite_list_content(mid, credential = credential, page = page_index)
	logger.log(util.LOG_TRACE, favlist_head)
	info = favlist_head.get("info")
	assert(info.get("id") == mid)

	media_list = favlist_head.get("medias")
	count = info.get("media_count")
	logger.info("favlist %d, name %s, creator %s, count %d", mid, info.get("title"), info.get("upper").get("name"), count)

	while len(media_list) < count:
		logger.debug("media %d/%d", len(media_list), count)
		page_index = page_index + 1
		await util.stall()
		logger.debug("fetching page %d", page_index)
		favlist_part = await favorite_list.get_video_favorite_list_content(mid, credential = credential, page = page_index)
		logger.log(util.LOG_TRACE, favlist_part)
		assert(favlist_part.get("info").get("id") == mid)
		media_list = media_list + favlist_part.get("medias")

	logger.debug("finished fetching favlist %d", mid)
	logger.log(util.LOG_TRACE, media_list)
	favlist_head["medias"] = media_list

	info_file = path + str(mid) + ".json"
	logger.debug("save favlist as " + info_file)
	util.save_json(favlist_head, info_file)

	return media_list


async def dump_favlist(uid, filter = None, path = None, credential = None, mode = None):
	if not mode:
		mode = "fix"

	path = util.opt_path(path)

	logger.debug("downloading favlist from user %d into %s, filter %s, mode %s, auth %x", uid, path, str(filter), mode, bool(credential))

	await util.stall()
	user_favlist = await favorite_list.get_video_favorite_list(uid, credential = credential)
	logger.log(util.LOG_TRACE, user_favlist)
	assert(user_favlist.get("count") == len(user_favlist.get("list")))
	util.mkdir(path + "favlist")

	bv_table = dict()
	for i, v in enumerate(user_favlist.get("list")):
		title = v.get("title")
		if filter and not re.fullmatch(filter, title):
			logger.debug("skip favlist " + title)
			continue
		logger.info("downloading favlist " + title)
		favlist = await download(v.get("id"), path = path + "favlist", credential = credential)
		if mode == "skip":
			continue

		logger.debug("%d records in favlist %s", len(favlist), title)
		for _, bv in enumerate(favlist):
			bv_table[bv.get("bvid")] = True

	logger.log(util.LOG_TRACE, bv_table)
	if mode == "skip":
		logger.info("skip downloading videos")
		return

	logger.info("need to download %d videos", len(bv_table))
	util.mkdir(path + "video")
	finished_count = 0
	for bv in bv_table.keys():
		res = await bv_down.download(bv, path = path + "video", credential = credential, mode = mode)
		if res:
			finished_count += 1
		print(bv, res, flush = True)

	logger.info("finished downloading favlist %d/%d",finished_count, len(bv_table))


async def main(args):
	credential = None
	if args.auth:
		credential = await util.credential(args.auth)

	parser = re.compile(r"(\d+)(?::(.+))?")

	logger.debug(args.inputs)
	for fav in args.inputs:
		match = parser.fullmatch(fav)
		uid = int(match.group(1))
		filter = match.group(2)
		logger.debug("%d %s", uid, str(filter))
		try:
			await dump_favlist(uid, filter, path = args.dest, mode = args.mode, credential = credential)
		except Exception as e:
			logger.exception("failed to download favlist " + fav)
			util.on_exception(e)

if __name__ == "__main__":
	args = util.parse_args([
		(("inputs",), {"nargs" : '+'}),
		(("-u", "--auth"), {}),
		(("-m", "--mode"), {"choices" : ["skip", "fix", "update", "force"]}),
	])
	util.run(main(args))
