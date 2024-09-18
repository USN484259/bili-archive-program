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


async def recursive_save_images(sess, path, table, stall = None):
	for v in table.values():
		try:
			if type(v) is dict:
				await recursive_save_images(sess, path, v, stall)
			elif type(v) is str:
				img_match = img_pattern.fullmatch(v)
				if img_match:
					img_name = img_match.group(1)
					logger.debug("fetching image %s", img_name)
					img_path = os.path.join(path, img_name)
					stall and await stall()
					await network.fetch(sess, v, img_path)
		except Exception:
			logger.exception("failed to fetch image")


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
	stall = runtime.Stall()
	user_root = args.dir or runtime.subdir("user")
	async with network.session() as sess:
		user_map = await fetch_users(sess, args.inputs, stall)

		fetched_user = 0
		for uid, info in user_map.items():
			fetch_status = False
			try:
				logger.debug(info)
				with core.locked_path(user_root, uid) as uid_path:
					info_path = os.path.join(uid_path, "info.json")
					with core.staged_file(info_path, "w", rotate = True) as f:
						json.dump(info, f, indent = '\t', ensure_ascii = False)
					await recursive_save_images(sess, uid_path, info, stall)

				# TODO fetch dynamics and videos
				fetched_user += 1
				fetch_status = True
			except Exception:
				logger.exception("failed to fetch user %s", uid)

			runtime.report("user", fetch_status, uid)

		logger.info("finished user download %d/%d", fetched_user, len(args.inputs))


if __name__ == "__main__":
	args = runtime.parse_args(("network", "auth", "dir"), [
		(("inputs",), {"nargs" : '+'}),
	])
	asyncio.run(main(args))
