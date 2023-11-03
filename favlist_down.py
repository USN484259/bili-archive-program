#!/usr/bin/env python3

import os
import re
import logging
from bilibili_api import favorite_list
import util
import bv_down

logger = logging.getLogger("bili_arch.favlist_down")


async def fetch_media(mid, credential = None):
	logger.debug("fetching favlist %d, auth %x", mid, bool(credential))
	page_index = 1
	await util.stall()
	favlist_head = await favorite_list.get_video_favorite_list_content(mid, credential = credential, page = page_index)
	info = favlist_head.get("info")
	assert(info.get("id") == mid)

	media_list = favlist_head.get("medias")
	count = info.get("media_count")
	logger.info("favlist %d, name %s, creator %s, count %d", mid, info.get("title"), info.get("upper").get("name"), count)

	while len(media_list) < count:
		logger.info("media %d/%d", len(media_list), count)
		page_index = page_index + 1
		await util.stall()
		logger.debug("fetching page %d", page_index)
		favlist_part = await favorite_list.get_video_favorite_list_content(mid, credential = credential, page = page_index)
		assert(favlist_part.get("info").get("id") == mid)
		media_list = media_list + favlist_part.get("medias")

	logger.debug("finished fetching favlist %d", mid)
	favlist_head["medias"] = media_list
	return favlist_head

fav_parser = re.compile(r"(\d+)(?::(.+))?")
async def fetch_favlist(fav_str, fav_root, credential = None):
	logger.debug("fetching favlist %s into %s, auth %x", fav_str, fav_root, bool(credential))

	match = fav_parser.fullmatch(fav_str)
	uid = int(match.group(1))
	name_re = match.group(2)

	await util.stall()
	user_favlist = await favorite_list.get_video_favorite_list(uid, credential = credential)
	assert(user_favlist.get("count") == len(user_favlist.get("list")))

	bv_table = set()
	for fav in user_favlist.get("list"):
		mid = fav.get("id")
		title = fav.get("title")
		if name_re and not re.fullmatch(name_re, title):
			logger.debug("skip favlist %d, %s", mid, title)
			continue

		logger.info("fetching favlist %d, %s", mid, title)
		favlist = await fetch_media(mid, credential = credential)

		media_list = favlist.get("medias")
		logger.info("favlist %s, media count %d", title, len(media_list))
		util.save_json(favlist, os.path.join(fav_root, str(mid) + ".json"))

		for v in media_list:
			bv_table.add(v.get("bvid"))

	logger.info("finished fetching %s, media count %d", fav_str, len(bv_table))
	return bv_table


async def download(fav_list, mode = None, credential = None):
	if not mode:
		mode = "fix"

	fav_root = util.subdir("favlist")

	logger.debug("downloading %d fav-groups into %s, mode %s, auth %x", len(fav_list), fav_root, mode, bool(credential))

	fetched_favlist = 0
	bv_table = set()
	for fav_str in fav_list:
		fetch_status = False
		try:
			logger.info("downloading favlist %s", fav_str)
			bv_table |= await fetch_favlist(fav_str, fav_root, credential)

			fetched_favlist += 1
			fetch_status = True
		except Exception as e:
			logger.exception("failed to fetch favlist %s", fav_str)

		util.report("favlist", fetch_status, fav_str)

	logger.info("finished downloading fav-groups %d/%d", fetched_favlist, len(fav_list))

	if mode == "skip":
		logger.info("skip video download")
		return

	bv_root = util.subdir("video")
	await bv_down.download(bv_table, bv_root, mode = mode, credential = credential)


async def main(args):
	credential = None
	if args.auth:
		credential = await util.credential(args.auth)

	await download(args.inputs, mode = args.mode, credential = credential)


if __name__ == "__main__":
	args = util.parse_args([
		(("inputs",), {"nargs" : '+'}),
		(("-u", "--auth"), {}),
		(("-m", "--mode"), {"choices" : ["skip", "fix", "update", "force"]}),
	])
	util.run(main(args))
