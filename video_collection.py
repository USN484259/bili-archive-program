#!/usr/bin/env python3

import logging

import core
import runtime
import network

# constants

USER_VIDEO_LIST_URL = "https://api.bilibili.com/x/space/wbi/arc/search"
USER_COLLECTIONS_URL = "https://api.bilibili.com/x/polymer/web-space/seasons_series_list"
# USER_CHANNEL_LIST_URL = "https://api.bilibili.com/x/space/channel/video"

USER_SERIES_LIST_URL = "https://api.bilibili.com/x/series/archives"
USER_SEASON_LIST_URL = "https://api.bilibili.com/x/polymer/web-space/seasons_archives_list"

# static objects

logger = logging.getLogger("bili_arch.collection")

# helper functions

async def get_video_page(sess, uid, page):
	info = await network.request(sess, "GET", USER_VIDEO_LIST_URL, wbi_sign = True, params = {"mid": uid, "pn": page})
	return info


async def get_collections(sess, uid, page):
	info = await network.request(sess, "GET", USER_COLLECTIONS_URL, wbi_sign = True, params = {"mid": uid, "page_num": page, "page_size": 20})
	return info


async def get_series_page(sess, uid, sid, page):
	info = await network.request(sess, "GET", USER_SERIES_LIST_URL, wbi_sign = True, params = {"mid": uid, "series_id": sid, "pn": page})
	return info


async def get_season_page(sess, uid, sid, page):
	info = await network.request(sess, "GET", USER_SEASON_LIST_URL, wbi_sign = True, params = {"mid": uid, "season_id": sid, "page_num": page})
	return info


# methods

async def fetch_user_videos(sess, uid, stall = None):
	video_info = None
	video_count = None
	video_list = []
	page_index = 1
	try:
		while video_count is None or len(video_list) < video_count:
			stall and await stall()
			logger.debug("fetching page %d", page_index)
			video_page = await get_video_page(sess, uid, page_index)

			if video_info is None:
				video_info = video_page.get("list").get("tlist")
				video_count = video_page.get("page").get("count")
				logger.debug("user %s, videos %d", uid, video_count)

			vlist = video_page.get("list").get("vlist")
			if not vlist:
				logger.warning("empty video page %d, stop here", page_index)
				break
			video_list += vlist
			page_index += 1
	except Exception:
		logger.exception("failed to fetch user videos")

	logger.info("fetched video list %d/%d", len(video_list), video_count or 0)
	return {"info": video_info, "count": video_count, "list": video_list}


async def fetch_user_collection_videos(sess, meta, stall = None):
	get_func = None
	sid = None
	if "series_id" in meta:
		get_func = get_series_page
		sid = meta.get("series_id")
	elif "season_id" in meta:
		get_func = get_season_page
		sid = meta.get("season_id")
	else:
		raise RuntimeError("unknown collection type")

	uid = meta.get("mid")
	video_count = meta.get("total")
	video_list = []
	page_index = 1
	try:
		while len(video_list) < video_count:
			stall and await stall()
			logger.debug("fetching page %d", page_index)
			video_page = await get_func(sess, uid, sid, page_index)
			videos = video_page.get("archives")
			if not videos:
				logger.warning("empty video page %d, stop here", page_index)
				break
			video_list += videos
			page_index += 1
	except Exception:
		logger.exception("failed to fetch collection videos")

	logger.info("fetched video collection %d/%d", len(video_list), video_count)
	return video_list


async def fetch_user_collection_list(sess, uid, stall = None):
	try:
		# TODO handle collection_list more than one page
		stall and await stall()
		collections = await get_collections(sess, uid, 1)
		season_list = collections.get("items_lists").get("seasons_list")
		series_list = collections.get("items_lists").get("series_list")

		logger.info("fetched collection list, series %d, seasons %d", len(series_list), len(season_list))
		return {
			"seasons_list": [s.get("meta") for s in season_list],
			"series_list": [s.get("meta") for s in series_list],
		}
	except Exception:
		logger.exception("failed to fetch collection list")
		return {}


async def fetch_user_collections(sess, uid, stall = None):
	result = {
		"series": [],
		"seasons": [],
	}
	result["videos"] = await fetch_user_videos(sess, uid, stall)
	collection_list = await fetch_user_collection_list(sess, uid, stall)

	for series in collection_list.get("series_list", {}):
		try:
			video_list = await fetch_user_collection_videos(sess, series, stall)
			result["series"].append({
				"info": series,
				"list": video_list,
			})
		except Exception:
			logger.exception("failed to fetch series")

	for season in collection_list.get("seasons_list", {}):
		try:
			video_list = await fetch_user_collection_videos(sess, season, stall)
			result["seasons"].append({
				"info": season,
				"list": video_list,
			})
		except Exception:
			logger.exception("failed to fetch season")

	return result


def gather_bvid_from_collectons(collection):
	bv_table = set()

	def gather_from_list(l):
		if not l:
			return
		for v in l:
			bvid = v.get("bvid")
			if bvid:
				bv_table.add(bvid)

	gather_from_list(collection.get("videos", {}).get("list"))

	for s in collection.get("series", []):
		gather_from_list(s.get("list"))

	for s in collection.get("seasons", []):
		gather_from_list(s.get("list"))

	return bv_table
