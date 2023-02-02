#!/usr/bin/env python3

import os
from bilibili_api import user
import util
import bv_down

async def download(uid, path = None, credential = None):
	path = util.opt_path(path)

	util.logv("fetching user " + str(uid) + " into " + path + " auth " + ("yes" if credential else "no"))

	await util.stall()
	usr = user.User(uid = uid, credential = credential)
	info = await usr.get_user_info()
	util.logt(info)

	util.mkdir(path + str(uid))
	util.logi("downloading " + str(uid) + '\t' + info.get("name", ""))

	util.logv("fetching head pic")
	await util.fetch(info.get("face"), path + str(uid) + os.path.sep + "head.jpg")

	util.logv("fetching top banner")
	await util.fetch(info.get("top_photo"), path + str(uid) + os.path.sep + "banner.jpg")

	if info.get("pendant").get("pid") > 0:
		util.logv("fetching pendant")
		await util.fetch(info.get("pendant").get("image"), path + str(uid) + os.path.sep + "pendant.png")


	page_index = 1
	await util.stall()
	util.logv("fetching page " + str(page_index))
	video_head = await usr.get_videos(pn = page_index)
	util.logt(video_head)
	video_count = video_head.get("page").get("count")
	video_list = video_head.get("list").get("vlist")

	while len(video_list) < video_count:
		util.logv("videos " + str(len(video_list)) + '/' + str(video_count))
		page_index = page_index + 1
		await util.stall()
		util.logv("fetching page " + str(page_index))
		video_part = await usr.get_videos(pn = page_index)
		util.logt(video_part)
		video_list = video_list + video_part.get("list").get("vlist")
	
	util.logv("finished fetching videos " + str(uid))
	util.logt(video_list)

	assert("video_list" not in info)
	info["video_list"] = video_list

	util.logv("save user info")
	await util.save_json(info, path + str(uid) + os.path.sep + "info.json")

	util.logv("finished " + str(uid))
	return video_list


async def dump_user(uid_list, path = None, credential = None, mode = None):
	if not mode:
		mode = "fix"

	path = util.opt_path(path)
	
	util.logv("downloading " + str(len(uid_list)) + "users into " + path + " mode " + mode + " auth " + ("yes" if credential else "no"))
	util.mkdir(path + "user")

	bv_table = dict()
	for i, uid in enumerate(uid_list):
		try:
			util.logi("downloading user " + str(uid))
			video_list = await download(uid, path = path + "user", credential = credential)
			if mode == "skip":
				continue

			util.logv(str(len(video_list)) + " videos from user " + str(uid))
			for _, bv in enumerate(video_list):
				bv_table[bv.get("bvid")] = True

		except Exception as e:
			util.handle_exception(e, "failed to get user " + str(uid))

	
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

	util.logi("finished downloading users")


async def main(args):
	credential = None
	if args.auth:
		credential = util.credential(args.auth)

	util.logv(args.inputs)
	await dump_user(args.inputs, path = args.dest, mode = args.mode, credential = credential)


if __name__ == "__main__":
	args = util.parse_args()
	util.run(main(args))


