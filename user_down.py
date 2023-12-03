#!/usr/bin/env python3

import os
import logging
from bilibili_api import user, video
import util
import bv_down

logger = logging.getLogger("bili_arch.user_down")

async def fetch_season(usr, season_id, exclude_list = {}):
	page_index = 1
	await util.stall()
	season_info = await usr.get_channel_videos_season(season_id, pn = page_index)
	season_list = season_info.get("archives")
	season_meta = season_info.get("meta")
	assert(season_meta.get("season_id") == season_id)
	season_count = season_meta.get("total")
	season_name = season_meta.get("name")

	logger.info("season %d, %s, count %d", season_id, season_name, season_count)

	if not season_list:
		logger.warning("skip empty season")
		season_info["archives"] = []
		return season_info

	if season_name in exclude_list:
		logger.warning("excluded " + season_name)
		return

	while len(season_list) < season_count:
		logger.info("videos %d/%d", len(season_list), season_count)
		page_index += 1
		await util.stall()
		logger.debug("fetching page %d", page_index)
		season_page = await usr.get_channel_videos_season(season_id, pn = page_index)
		videos = season_page.get("archives")
		assert(len(videos) > 0)
		season_list += videos

	logger.debug("finished fetching season %d", season_id)
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

	logger.info("series %d, %s, count %d", series_id, series_name, series_count)

	if not series_list:
		logger.warning("skip empty series")
		series_info["archives"] = []
		return series_info

	if series_name in exclude_list:
		logger.warning("excluded " + series_name)
		return

	while (len(series_list) < series_count):
		logger.info("videos %d/%d", len(series_list), series_count)
		page_index += 1
		await util.stall()
		logger.debug("fetching page %d", page_index)
		series_page = await usr.get_channel_videos_series(series_id, pn = page_index)
		videos = series_page.get("archives")
		assert(len(videos) > 0)
		series_list += videos

	logger.debug("finished fetching series %d", series_id)
	assert(len(series_info.get("archives")) == series_count)
	return series_info


async def fetch_user(uid, path, credential = None):

	logger.debug("fetching user %d into %s, auth %x", uid, path, bool(credential))

	await util.stall()
	usr = user.User(uid = uid, credential = credential)
	info = await usr.get_user_info()

	with util.locked_path(path, str(uid)) as usr_root:
		logger.info("fetching user %d, %s", uid, info.get("name", ""))

		logger.info("fetching head pic")
		await util.fetch(info.get("face"), os.path.join(usr_root, "head.jpg"))

		logger.info("fetching top banner")
		await util.fetch(info.get("top_photo"), os.path.join(usr_root, "banner.jpg"))

		if info.get("pendant").get("pid") > 0:
			logger.info("fetching pendant")
			await util.fetch(info.get("pendant").get("image"), os.path.join(usr_root, "pendant.png"))

		page_index = 1
		await util.stall()
		logger.info("fetching videos")
		video_head = await usr.get_videos(pn = page_index)
		video_count = video_head.get("page").get("count")
		video_list = video_head.get("list").get("vlist")

		while len(video_list) < video_count:
			logger.info("videos %d/%d", len(video_list), video_count)
			page_index += 1
			await util.stall()
			logger.debug("fetching page %d", page_index)
			video_page = await usr.get_videos(pn = page_index)
			videos = video_page.get("list").get("vlist")
			assert(len(videos) > 0)
			video_list += videos

		logger.info("fetching channels")
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

		assert("video_list" not in info)
		info["video_list"] = video_list
		assert("season_list" not in info)
		info["season_list"] = season_list
		assert("series_list" not in info)
		info["series_list"] = series_list

		logger.info("saving user info")
		util.save_json(info, os.path.join(usr_root, "info.json"))

		logger.info("finished user %d, %s", uid, info.get("name", ""))
		return video_list, season_list, series_list


async def download(uid_list, mode = None, credential = None):
	if not mode:
		mode = "fix"

	user_root = util.subdir("user")

	logger.debug("downloading %d users into %s, mode %s, auth %x", len(uid_list), user_root, mode, bool(credential))

	fetched_user = 0
	bv_table = set()
	for uid_str in uid_list:
		fetch_status = False
		try:
			uid = int(uid_str)
			logger.info("downloading user %d", uid)
			video_list, season_list, series_list = await fetch_user(uid, path = user_root, credential = credential)
			if mode == "skip":
				continue

			logger.debug("%d videos from user %d", len(video_list), uid)
			for bv in video_list:
				bv_table.add(bv.get("bvid"))

			for season in season_list:
				for bv in season.get("archives"):
					bv_table.add(bv.get("bvid"))

			for series in series_list:
				for bv in series.get("archives"):
					bv_table.add(bv.get("bvid"))

			fetched_user += 1
			fetch_status = True

		except Exception as e:
			logger.exception("failed to fetch user %d", uid)

		util.report("user", fetch_status, uid_str)


	logger.info("fetched downloading users %d/%d", fetched_user, len(uid_list))


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


