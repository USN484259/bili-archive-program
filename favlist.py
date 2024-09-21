#!/usr/bin/env python3

import os
import re
import json
import asyncio
import logging

import core
import runtime
import network
# import video

# constants

USER_FAVLIST_URL = "https://api.bilibili.com/x/v3/fav/folder/created/list-all"
FAVLIST_CONTENT_URL = "https://api.bilibili.com/x/v3/fav/resource/list"


# static objects

user_fav_pattern = re.compile(r"(\d+)(?::(.*))?")
logger = logging.getLogger("bili_arch.favlist")


# helper functions

async def get_user_favlist(sess, uid):
	info = await network.request(sess, "GET", USER_FAVLIST_URL, params = {"up_mid": uid})
	return info


async def get_favlist_page(sess, mid, page):
	info = await network.request(sess, "GET", FAVLIST_CONTENT_URL, params = {"media_id": mid, "ps": 20, "pn": page})
	return info


def save_favlist(favlist, path):
	mid = favlist.get("info").get("id")
	file_name = os.path.join(path, "%d.json" % mid)
	with core.staged_file(file_name, "w", rotate = True) as f:
		json.dump(favlist, f, indent = '\t', ensure_ascii = False)


# methods

async def user_favlist(sess, uid, stall = None):
	stall and await stall()
	return await get_user_favlist(sess, uid)


async def fetch_favlist(sess, mid, stall = None):
	favlist_info = None
	favlist = []
	page_index = 1
	while True:
		stall and await stall()
		logger.debug("fetching page %d", page_index)
		favlist_page = await get_favlist_page(sess, mid, page_index)
		info = favlist_page.get("info")

		if favlist_info is None:
			favlist_info = info
			logger.info("favlist %d, size %d", mid, info.get("media_count"))
		else:
			if info.get("id") != favlist_info.get("id") or info.get("media_count") != favlist_info.get("media_count"):
				raise RuntimeError("favlist %d: page %d info mismatch" % (mid, page_index))

		medias = favlist_page.get("medias")
		if not medias:
			logger.warning("empty favlist page %d, stop here", page_index)
			break
		favlist += medias
		logger.debug("favlist %d, %d/%d", mid, len(favlist), favlist_info.get("media_count"))

		if favlist_page.get("has_more"):
			page_index += 1
		else:
			break

	if favlist_info.get("media_count") != len(favlist):
		logger.error("favlist %d: size mismatch %d/%d", mid, len(favlist), favlist_info.get("media_count"))

	return {"info": favlist_info, "medias": favlist}


# entrance

async def main(args):
	if args.download:
		import video

	async with network.session() as sess:
		stall = runtime.Stall()
		mid_list = []
		for fav_str in args.inputs:
			if ':' in fav_str:
				try:
					fav_match = user_fav_pattern.fullmatch(fav_str)
					uid = int(fav_match.group(1))
					keyword = fav_match.group(2)
					user_fav = await user_favlist(sess, uid, stall)
					logger.debug("user %d, favlist %d", uid, user_fav.get("count"))
					for fav in user_fav.get("list", []):
						logger.debug("favlist %d: %s", fav.get("id"), fav.get("title"))
						if keyword in fav.get("title"):
							mid_list.append(fav.get("id"))
				except Exception:
					logger.exception("failed to get favlists of user %d", uid)
			else:
				mid_list.append(int(fav_str))

		logger.debug(mid_list)

		fetched_favlist = 0
		bv_table = set()
		for mid in mid_list:
			fetch_status = False
			try:
				logger.debug("fetching favlist %d", mid)
				favlist = await fetch_favlist(sess, mid, stall)
				media_list = favlist.get("medias")
				logger.info("favlist %d: %s, %d items", mid, favlist.get("info").get("title"), len(media_list))
				save_favlist(favlist, args.dir or runtime.subdir("favlist"))
				if args.download:
					bv_table |= media_list
				fetched_favlist += 1
				fetch_status = True
			except Exception:
				logger.exception("failed to fetch favlist %d", mid)

			runtime.report("favlist", fetch_status, mid)

		logger.info("finish favlist download %d/%d", fetched_favlist, len(mid_list))

		if args.download:
			await video.batch_download(sess, bv_table, args.dir or runtime.subdir("video"), mode = args.mode, prefer = args.prefer, reject = args.reject)


if __name__ == "__main__":
	args = runtime.parse_args(("network", "auth", "dir", "bandwidth", "video_mode", "prefer"), [
		(("inputs",), {"nargs" : '+'}),
		(("--download",), {"action": "store_true"}),
	])
	asyncio.run(main(args))
