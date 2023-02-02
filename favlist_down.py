#!/usr/bin/env python3

import os
import re
from bilibili_api import favorite_list
import util
import bv_down

async def download(mid, path = None, credential = None):
	path = util.opt_path(path)

	util.logv("fetching favlist " + str(mid) + " into " + path + " auth " + ("yes" if credential else "no"))
	page_index = 1
	await util.stall()
	util.logv("fetching page " + str(page_index))
	favlist_head = await favorite_list.get_video_favorite_list_content(mid, credential = credential, page = page_index)
	util.logt(favlist_head)
	info = favlist_head.get("info")
	assert(info.get("id") == mid)

	media_list = favlist_head.get("medias")
	count = info.get("media_count")
	util.logi("favlist " + str(mid) + " name " + info.get("title") + " creator " + info.get("upper").get("name") + " count " + str(count))

	while len(media_list) < count:
		util.logv("media " + str(len(media_list)) + '/' + str(count))
		page_index = page_index + 1
		await util.stall()
		util.logv("fetching page " + str(page_index))
		favlist_part = await favorite_list.get_video_favorite_list_content(mid, credential = credential, page = page_index)
		util.logt(favlist_part)
		assert(favlist_part.get("info").get("id") == mid)
		media_list = media_list + favlist_part.get("medias")

	util.logv("finished fetching favlist " + str(mid))
	util.logt(media_list)
	favlist_head["medias"] = media_list

	info_file = path + str(mid) + ".json"
	util.logv("save favlist as " + info_file)
	await util.save_json(favlist_head, info_file)

	return media_list


async def dump_favlist(uid, filter = None, path = None, credential = None, mode = None):
	if not mode:
		mode = "fix"

	path = util.opt_path(path)

	util.logv("downloading favlist from user " + str(uid) + " filter " + str(filter) + " into " + path + " mode " + mode + " auth " + ("yes" if credential else "no"))

	await util.stall()
	user_favlist = await favorite_list.get_video_favorite_list(uid, credential = credential)
	util.logt(user_favlist)
	assert(user_favlist.get("count") == len(user_favlist.get("list")))
	util.mkdir(path + "favlist")

	bv_table = dict()
	for i, v in enumerate(user_favlist.get("list")):
		title = v.get("title")
		if filter and not re.fullmatch(filter, title):
			util.logv("skip favlist " + title)
			continue
		util.logi("downloading favlist " + title)
		favlist = await download(v.get("id"), path = path + "favlist", credential = credential)
		if mode == "skip":
			continue

		util.logv(str(len(favlist)) + " records in favlist " + title)
		for _, bv in enumerate(favlist):
			bv_table[bv.get("bvid")] = True

	util.logt(bv_table)
	if mode == "skip":
		util.logi("skip downloading videos")
		return

	util.logi("need to download " + str(len(bv_table)) + " videos")
	util.mkdir(path + "video")
	for bv, _ in bv_table.items():
		try:
			await bv_down.download(bv, path = path + "video", credential = credential, mode = mode)
		except Exception as e:
			util.handle_exception(e, "failed to download " + bv)

	util.logi("finished downloading favlist")


async def main(args):
	credential = None
	if args.auth:
		credential = util.credential(args.auth)

	parser = re.compile(r"(\d+)\:?(.*)")

	util.logv(args.inputs)
	for i, v in enumerate(args.inputs):
		match = parser.fullmatch(v)
		uid = match.group(1)
		filter = match.group(2)
		util.logv(str(uid) + ' ' + str(filter))
		try:
			await dump_favlist(uid, filter, path = args.dest, mode = args.mode, credential = credential)
		except Exception as e:
			util.handle_exception(e, "failed to download favlist " + v)

if __name__ == "__main__":
	args = util.parse_args()
	util.run(main(args))
