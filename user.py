#!/usr/bin/env python3

import os
import re
import json
import asyncio
import logging
from collections import ChainMap

import core
import runtime
import network
from video_collection import fetch_user_collections, gather_bvid_from_collectons

# constants

USER_BASIC_INFO_URL = "https://api.bilibili.com/x/polymer/pc-electron/v1/user/cards"
USER_BRIEF_INFO_URL = "https://api.vc.bilibili.com/account/v1/user/cards"
USER_FULL_INFO_URL = "https://api.bilibili.com/x/space/wbi/acc/info"

# static objects

logger = logging.getLogger("bili_arch.user")

# helper functions

async def get_user_full_info(sess, uid):
	info = await network.request(sess, "GET", USER_FULL_INFO_URL, wbi_sign = True, params = {"mid": uid})
	return info


async def get_user_basic_info(sess, uid_list):
	info = await network.request(sess, "GET", USER_BASIC_INFO_URL, wbi_sign = True, params = {"uids": ",".join(uid_list)})
	return info


async def get_user_brief_info(sess, uid_list):
	info = await network.request(sess, "GET", USER_BRIEF_INFO_URL, params = {"uids": ",".join(uid_list)})
	return {str(u["mid"]): u for u in info}


# methods

async def fetch_users(sess, uid_list, stall = None):
	user_map = {}
	try:
		logger.info("fetching basic user info")
		await stall()
		if runtime.credential:
			user_map = await get_user_basic_info(sess, uid_list)
		else:
			user_map = await get_user_brief_info(sess, uid_list)

	except Exception:
		logger.exception("failed to batch fetch user")
		if not runtime.credential:
			raise

	if runtime.credential:
		for uid in uid_list:
			try:
				logger.info("fetching full user info %s", uid)
				await stall()
				info = await get_user_full_info(sess, uid)
				orig_info = user_map.get(uid)
				if orig_info:
					user_map[uid] = dict(ChainMap(info, orig_info))
				else:
					user_map[uid] = info
			except Exception:
				logger.exception("failed to fetch user %s", uid)

	return user_map


async def fetch_images(img_fetch, info, path):
	img_table = runtime.find_images(info)
	logger.info("fetching %d images", len(img_table))
	for name, url in img_table.items():
		await img_fetch.schedule(path, name, url)


# entrance

async def main(args):
	if args.download:
		import video

	async with network.session() as sess, network.image_fetcher() as img_fetch:
		stall = runtime.Stall()
		user_root = args.dir or runtime.subdir("user")

		logger.info("fetching %d users", len(args.inputs))
		user_map = await fetch_users(sess, args.inputs, stall)
		bv_table = set()
		fetched_user = 0
		for uid, info in user_map.items():
			fetch_status = False
			try:
				with core.locked_path(user_root, uid) as uid_path:
					await fetch_images(img_fetch, info, uid_path)

					logger.info("fetching collections of user %s", uid)
					collection = await fetch_user_collections(sess, uid, stall)
					info["videos"] = collection

					await fetch_images(img_fetch, collection, uid_path)

					user_bv = gather_bvid_from_collectons(collection)
					logger.info("user %s, videos %d", uid, len(user_bv))
					bv_table |= user_bv

					info_path = os.path.join(uid_path, "info.json")
					logger.info("saving user %s", uid)
					with core.staged_file(info_path, "w", rotate = True) as f:
						json.dump(info, f, indent = '\t', ensure_ascii = False)

					img_table = runtime.find_images(info)
					logger.info("user %s, images %d", uid, len(img_table))
					for name, url in img_table.items():
						await img_fetch.schedule(uid_path, name, url)

				fetched_user += 1
				fetch_status = True
			except Exception:
				logger.exception("failed to fetch user %s", uid)

			runtime.report("user", fetch_status, uid)

		logger.info("finished user download %d/%d", fetched_user, len(args.inputs))

		if args.download:
			await video.batch_download(sess, bv_table, args.dir or runtime.subdir("video"), mode = args.mode, prefer = args.prefer, reject = args.reject)


if __name__ == "__main__":
	args = runtime.parse_args(("network", "auth", "dir", "bandwidth", "video_mode", "prefer"), [
		(("inputs",), {"nargs" : '+'}),
		(("--download",), {"action": "store_true"}),
	])
	asyncio.run(main(args))
