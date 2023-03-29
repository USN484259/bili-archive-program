#!/usr/bin/env python3

import os
from bilibili_api import user, video
import util
import bv_down


async def fetch_season(usr, season_id):
	page_index = 1
	await util.stall()
	season_info = await usr.get_channel_videos_season(season_id, pn = page_index)
	season_list = season_info.get("archives")
	season_meta = season_info.get("meta")
	assert(season_meta.get("season_id") == season_id)
	season_count = season_meta.get("total")

	util.logv("season " + str(season_id), season_meta.get("name"), "count " + str(season_count))
	util.logt(season_info)

	while (len(season_list) < season_count):
		util.logv("videos " + str(len(season_list)) + '/' + str(season_count))
		page_index += 1
		await util.stall()
		util.logv("fetching page " + str(page_index))
		season_part = await usr.get_channel_videos_season(season_id, pn = page_index)
		util.logt(season_part)
		season_list += season_part.get("archives")

	assert(len(season_info.get("archives")) == season_count)
	return season_info


async def fetch_series(usr, series_meta):
	series_id = series_meta.get("series_id")
	page_index = 1
	await util.stall()
	series_info = await usr.get_channel_videos_series(series_id, pn = page_index)
	assert("meta" not in series_info)
	series_info["meta"] = series_meta
	series_list = series_info.get("archives")
	series_count = series_meta.get("total")

	util.logv("series " + str(series_id), series_meta.get("name"), "count " + str(series_count))
	util.logt(series_info)

	while (len(series_list) < series_count):
		util.logv("videos " + str(len(series_list)) + '/' + str(series_count))
		page_index += 1
		await util.stall()
		util.logv("fetching page " + str(page_index))
		series_part = await usr.get_channel_videos_series(series_id, pn = page_index)
		util.logt(series_part)
		series_list += series_part.get("archives")

	assert(len(series_info.get("archives")) == series_count)
	return series_info


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
		page_index += 1
		await util.stall()
		util.logv("fetching page " + str(page_index))
		video_part = await usr.get_videos(pn = page_index)
		util.logt(video_part)
		video_list += video_part.get("list").get("vlist")

	util.logv("fetching channels")
	await util.stall()
	channels = await usr.get_channel_list()

	season_list = []
	for season in channels.get("items_lists").get("seasons_list"):
		util.logv("fetching season", season.get("meta").get("name"))
		season_list.append(await fetch_season(usr, season.get("meta").get("season_id")))

	series_list = []
	for series in channels.get("items_lists").get("series_list"):
		util.logv("fetching series", series.get("meta").get("name"))
		series_list.append(await fetch_series(usr, series.get("meta")))

	util.logv("finished fetching user " + str(uid))
	util.logt(video_list)

	assert("video_list" not in info)
	info["video_list"] = video_list
	assert("season_list" not in info)
	info["season_list"] = season_list
	assert("series_list" not in info)
	info["series_list"] = series_list

	util.logv("save user info")
	util.save_json(info, path + str(uid) + os.path.sep + "info.json")

	util.logv("finished " + str(uid))
	return video_list, season_list, series_list


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
			video_list, season_list, series_list = await download(uid, path = path + "user", credential = credential)
			if mode == "skip":
				continue

			util.logv(str(len(video_list)) + " videos from user " + str(uid))
			for bv in video_list:
				bv_table[bv.get("bvid")] = True

			for season in season_list:
				for bv in season.get("archives"):
					bv_table[bv.get("bvid")] = True

			for series in series_list:
				for bv in series.get("archives"):
					bv_table[bv.get("bvid")] = True

		except Exception as e:
			util.handle_exception(e, "failed to get user " + str(uid))
	
	util.logt(bv_table)
	if mode == "skip":
		util.logi("skip downloading videos")
		return
	
	util.logi("need to download " + str(len(bv_table)) + " videos")
	util.mkdir(path + "video")
	finished_count = 0
	for bv in bv_table.keys():
		res = await bv_down.download(bv, path = path + "video", credential = credential, mode = mode)
		if res:
			finished_count += 1
		print(bv, res, flush = True)

	util.logi("finished downloading users", str(finished_count) + '/' + str(len(bv_table)))


async def main(args):
	credential = None
	if args.auth:
		credential = await util.credential(args.auth)

	util.logv(args.inputs)
	await dump_user(args.inputs, path = args.dest, mode = args.mode, credential = credential)


if __name__ == "__main__":
	args = util.parse_args([
		(("inputs",), {"nargs" : '+'}),
		(("-u", "--auth"), {}),
		(("-m", "--mode"), {"choices" : ["skip", "fix", "update", "force"]}),
	])
	util.run(main(args))


