#!/usr/bin/env python3

import os
import logging
from bilibili_api import user, video
import util
import bv_down

logger = logging.getLogger(__name__)

async def fetch_season(usr, season_id, exclude_list = {}):
	page_index = 1
	await util.stall()
	season_info = await usr.get_channel_videos_season(season_id, pn = page_index)
	season_list = season_info.get("archives")
	season_meta = season_info.get("meta")
	assert(season_meta.get("season_id") == season_id)
	season_count = season_meta.get("total")
	season_name = season_meta.get("name")

	logger.debug("season %d, %s, count %d", season_id, season_name, season_count)
	logger.log(util.LOG_TRACE, season_info)

	if season_name in exclude_list:
		logger.warning("excluded " + season_name)
		return

	while (len(season_list) < season_count):
		logger.debug("videos %d/%d", len(season_list), season_count)
		page_index += 1
		await util.stall()
		logger.debug("fetching page %d", page_index)
		season_part = await usr.get_channel_videos_season(season_id, pn = page_index)
		logger.log(util.LOG_TRACE, season_part)
		season_list += season_part.get("archives")

	assert(len(season_info.get("archives")) == season_count)
	return season_info


async def fetch_series(usr, series_meta, exclude_list = {}):
	series_id = series_meta.get("series_id")
	page_index = 1
	await util.stall()
	series_info = await usr.get_channel_videos_series(series_id, pn = page_index)
	assert("meta" not in series_info)
	series_info["meta"] = series_meta
	series_list = series_info.get("archives")
	series_count = series_meta.get("total")
	series_name = series_meta.get("name")

	logger.debug("series %d, %s, count %d", series_id, series_name, series_count)
	logger.log(util.LOG_TRACE, series_info)

	if series_name in exclude_list:
		logger.warning("excluded " + series_name)
		return

	while (len(series_list) < series_count):
		logger.debug("videos %d/%d", len(series_list), series_count)
		page_index += 1
		await util.stall()
		logger.debug("fetching page %d", page_index)
		series_part = await usr.get_channel_videos_series(series_id, pn = page_index)
		logger.log(util.LOG_TRACE, series_part)
		series_list += series_part.get("archives")

	assert(len(series_info.get("archives")) == series_count)
	return series_info


async def download(uid, path = None, credential = None):
	path = util.opt_path(path)

	logger.debug("fetching user %d into %s, auth %x", uid, path, bool(credential))

	await util.stall()
	usr = user.User(uid = uid, credential = credential)
	info = await usr.get_user_info()
	logger.log(util.LOG_TRACE, info)

	util.mkdir(path + str(uid))
	logger.info("downloading %d, %s", uid, info.get("name", ""))

	logger.debug("fetching head pic")
	await util.fetch(info.get("face"), path + str(uid) + os.path.sep + "head.jpg")

	logger.debug("fetching top banner")
	await util.fetch(info.get("top_photo"), path + str(uid) + os.path.sep + "banner.jpg")

	if info.get("pendant").get("pid") > 0:
		logger.debug("fetching pendant")
		await util.fetch(info.get("pendant").get("image"), path + str(uid) + os.path.sep + "pendant.png")

	page_index = 1
	await util.stall()
	logger.debug("fetching page %d", page_index)
	video_head = await usr.get_videos(pn = page_index)
	logger.log(util.LOG_TRACE, video_head)
	video_count = video_head.get("page").get("count")
	video_list = video_head.get("list").get("vlist")

	while len(video_list) < video_count:
		logger.debug("videos %d/%d", len(video_list), video_count)
		page_index += 1
		await util.stall()
		logger.debug("fetching page %d", page_index)
		video_part = await usr.get_videos(pn = page_index)
		logger.log(util.LOG_TRACE, video_part)
		part_list = video_part.get("list").get("vlist")
		assert(len(part_list) > 0)
		video_list += part_list

	logger.debug("fetching channels")
	await util.stall()
	channels = await usr.get_channel_list()

	# TODO support custom exclude-lists
	exclude_list = ["直播回放"]

	season_list = []
	for season in channels.get("items_lists").get("seasons_list"):
		logger.debug("fetching season %s", season.get("meta").get("name"))
		season_obj = await fetch_season(usr, season.get("meta").get("season_id"), exclude_list)
		if season_obj:
			season_list.append(season_obj)

	series_list = []
	for series in channels.get("items_lists").get("series_list"):
		logger.debug("fetching series %s", series.get("meta").get("name"))
		series_obj = await fetch_series(usr, series.get("meta"), exclude_list)
		if series_obj:
			series_list.append(series_obj)

	logger.debug("finished fetching user %d", uid)
	logger.log(util.LOG_TRACE, video_list)

	assert("video_list" not in info)
	info["video_list"] = video_list
	assert("season_list" not in info)
	info["season_list"] = season_list
	assert("series_list" not in info)
	info["series_list"] = series_list

	logger.debug("save user info")
	util.save_json(info, path + str(uid) + os.path.sep + "info.json")

	logger.debug("finished %d", uid)
	return video_list, season_list, series_list


async def dump_user(uid_list, path = None, credential = None, mode = None):
	if not mode:
		mode = "fix"

	path = util.opt_path(path)
	
	logger.debug("downloading %d users into %s, mode %s, auth %x", len(uid_list), path, mode, bool(credential))
	util.mkdir(path + "user")

	bv_table = dict()
	for uid_str in uid_list:
		try:
			uid = int(uid_str)
			logger.info("downloading user %d", uid)
			video_list, season_list, series_list = await download(uid, path = path + "user", credential = credential)
			if mode == "skip":
				continue

			logger.debug("%d videos from user %d", len(video_list), uid)
			for bv in video_list:
				bv_table[bv.get("bvid")] = True

			for season in season_list:
				for bv in season.get("archives"):
					bv_table[bv.get("bvid")] = True

			for series in series_list:
				for bv in series.get("archives"):
					bv_table[bv.get("bvid")] = True

		except Exception as e:
			logger.exception("failed to get user " + str(uid))
			util.on_exception(e)
	
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

	logger.info("finished downloading users %d/%d", finished_count, len(bv_table))


async def main(args):
	credential = None
	if args.auth:
		credential = await util.credential(args.auth)

	logger.debug(args.inputs)
	await dump_user(args.inputs, path = args.dest, mode = args.mode, credential = credential)


if __name__ == "__main__":
	args = util.parse_args([
		(("inputs",), {"nargs" : '+'}),
		(("-u", "--auth"), {}),
		(("-m", "--mode"), {"choices" : ["skip", "fix", "update", "force"]}),
	])
	util.run(main(args))


