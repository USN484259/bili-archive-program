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
from video_collection import fetch_user_collections, gather_resources_from_collectons

# constants

USER_BASIC_INFO_URL = "https://api.bilibili.com/x/polymer/pc-electron/v1/user/cards"
USER_BRIEF_INFO_URL = "https://api.vc.bilibili.com/account/v1/user/cards"
USER_FULL_INFO_URL = "https://api.bilibili.com/x/space/wbi/acc/info"

# static objects

logger = logging.getLogger("bili_arch.user")
img_pattern = re.compile(r"^.+/([^/.]+\.[^/.]+)$")

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


async def fetch_image(sess, url, img_root, stall = None):
	try:
		img_match = img_pattern.fullmatch(url)
		if not img_match:
			return
		img_name = img_match.group(1)
		logger.debug("fetching image %s", img_name)
		img_path = os.path.join(img_root, img_name)
		if os.path.isfile(img_path):
			return
		stall and await stall()
		await network.fetch(sess, url, img_path)
	except Exception:
		logger.exception("failed to fetch image")


async def recursive_save_images(sess, path, table, stall = None):
	for v in table.values():
		if type(v) is dict:
			await recursive_save_images(sess, path, v, stall)
		elif type(v) is str:
			await fetch_image(sess, v, path, stall)


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
		logger.info("fetching full user info")
		for uid in uid_list:
			try:
				logger.debug("fetching user %s", uid)
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


# entrance

async def main(args):
	if args.download:
		import video

	async with network.session() as sess:
		stall = runtime.Stall()
		user_root = args.dir or runtime.subdir("user")

		user_map = await fetch_users(sess, args.inputs, stall)
		bv_table = set()
		fetched_user = 0
		for uid, info in user_map.items():
			fetch_status = False
			try:
				collection = await fetch_user_collections(sess, uid, stall)
				info["videos"] = collection
				bv_t, img_table = gather_resources_from_collectons(collection)
				bv_table |= bv_t

				# TODO fetch dynamics
				with core.locked_path(user_root, uid) as uid_path:
					info_path = os.path.join(uid_path, "info.json")
					with core.staged_file(info_path, "w", rotate = True) as f:
						json.dump(info, f, indent = '\t', ensure_ascii = False)

					# TODO save images in seperate task
					await recursive_save_images(sess, uid_path, info, stall)

					for img_url in img_table:
						await fetch_image(sess, img_url, uid_path, stall)

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
