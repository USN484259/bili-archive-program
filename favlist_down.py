#!/usr/bin/env python3

import os
import json
import re
from bilibili_api import favorite_list
import util
import bv_down

async def download(mid, path = None, credential = None):
	path = util.opt_path(path)

	page_index = 1
	favlist_info = await favorite_list.get_video_favorite_list_content(mid, credential = credential, page = page_index)
	assert(favlist_info.get("info").get("id") == mid)
	media_list = favlist_info.get("medias")
	count = favlist_info.get("info").get("media_count")
	while len(media_list) < count:
		page_index = page_index + 1
		favlist_part = await favorite_list.get_video_favorite_list_content(mid, credential = credential, page = page_index)
		assert(favlist_part.get("info").get("id") == mid)
		media_list = media_list + favlist_part.get("medias")

	favlist_info["medias"] = media_list
	with open(path + str(mid) + ".json", "w") as f:
		json.dump(favlist_info, f, indent='\t', ensure_ascii=False)
	
	return media_list


async def dump_favlist(uid, filter = None, path = None, credential = None, mode = None):
	if not mode:
		mode = "check"

	path = util.opt_path(path)
	user_favlist = await favorite_list.get_video_favorite_list(uid, credential = credential)
	assert(user_favlist.get("count") == len(user_favlist.get("list")))
	util.mkdir(path + "favlist")

	bv_table = dict()
	for i, v in enumerate(user_favlist.get("list")):
		if filter and not re.fullmatch(filter, v.get("title")):
			continue
		favlist = await download(v.get("id"), path = path + "favlist", credential = credential)
		if mode == "skip":
			continue
		for _, bv in enumerate(favlist):
			bv_table[bv.get("bvid")] = True
	
	if mode == "skip":
		return
	
	util.mkdir(path + "video")
	for bv, _ in bv_table.items():
		try:
			await bv_down.download(bv, path = path + "video", credential = credential, mode = mode)
		except:
			util.exception("failed to download " + bv)



async def main(args):
	credential = None
	if args.auth:
		credential = util.credential(args.auth)
	
	parser = re.compile(r"(\d+)\:?(.*)")

	for i, v in enumerate(args.inputs):
		match = parser.fullmatch(v)
		uid = match.group(1)
		filter = match.group(2)
		print(uid, filter)
		try:
			await dump_favlist(uid, filter, path = args.dest, mode = args.mode, credential = credential)
		except:
			util.exception("failed to fetch favlist " + v)

if __name__ == "__main__":
	args = util.parse_args()
	util.sync(main(args))
