#!/usr/bin/env python3

import os
import re
import json
import asyncio
import logging

import core
import runtime
import network


# constants

USER_DYNAMIC_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
DYNAMIC_DETAIL_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail"

# static objects

logger = logging.getLogger("bili_arch.dynamic")

# helper functions

async def get_dynamic_list(sess, uid, offset = None):
	params = {"host_mid": uid}
	if offset:
		params["offset"] = offset
	info = await network.request(sess, "GET", USER_DYNAMIC_URL, params = params)
	return info


async def get_dynamic_detail(sess, oid):
	info = await network.request(sess, "GET", DYNAMIC_DETAIL_URL, wbi_sign = True, params = {"id": oid})
	return info


# methods

async def fetch_user_dynamics(sess, uid, stall = None):
	dynamic_list = []
	dynamic_offset = None
	try:
		while True:
			stall and await stall()
			logger.debug("fetching offset %s", str(dynamic_offset))
			dynamic_page = await get_dynamic_list(sess, uid, dynamic_offset)
			dynamic_items = dynamic_page.get("items")
			dynamic_offset = dynamic_page.get("offset")

			if not dynamic_items:
				logger.warning("empty dynamic page, stop here")
				break
			logger.debug("fetched dynamic page, count %d", len(dynamic_items))
			if not (dynamic_page.get("has_more") and dynamic_offset):
				break

			dynamic_list += dynamic_items
	except Exception:
		logger.exception("failed to fetch dynamics for user %s", uid)

	return dynamic_list


# entrance

async def main(args):
	async with network.session() as sess, network.image_fetcher() as img_fetch:
		stall = runtime.Stall()
		user_root = args.dir or runtime.subdir("user")
		fetch_detail = runtime.credential and not args.skip_detail

		fetched_dynamic = 0
		for uid in args.inputs:
			fetch_status = False
			try:
				logger.info("fetching dynamics for user %s", uid)
				dynamic_list = await fetch_user_dynamics(sess, uid, stall)
				logger.info("user %s, dynamics %d", uid, len(dynamic_list))
				dynamic_count = 0
				for dyn in dynamic_list:
					oid = dyn.get("id_str")
					if fetch_detail:
						try:
							stall and await stall()
							logger.debug("fetching dynamic detail %s", oid)
							dyn_detail = await get_dynamic_detail(sess, oid)
							dyn = dyn_detail.get("item")
						except Exception:
							logger.exception("failed to fetch dynamic detail %s", str(oid))

					try:
						with core.locked_path(user_root, uid, oid) as dyn_path:
							info_path = os.path.join(dyn_path, "info.json")
							logger.info("saving dynamic %s", oid)
							with core.staged_file(info_path, "w", rotate = True) as f:
								json.dump(dyn, f, indent = '\t', ensure_ascii = False)

							img_table = runtime.find_images(dyn)
							logger.info("dynamic %s, images %d", oid, len(img_table))
							for name, url in img_table.items():
								await img_fetch.schedule(dyn_path, name, url)
						dynamic_count += 1
					except Exception:
						logger.exception("failed to save dynamic %s", str(oid))

					fetch_status = (dynamic_count == len(dynamic_list))
					fetched_dynamic += dynamic_count
			except Exception:
				logger.exception("failed to fetch dynamic %s", str(oid))

			runtime.report("dynamic", fetch_status, uid)

		logger.info("finished dynamic download users %d, dynamics %d", len(args.inputs), fetched_dynamic)


if __name__ == "__main__":
	args = runtime.parse_args(("network", "auth", "dir", "bandwidth"), [
		(("inputs",), {"nargs" : '+'}),
		(("--skip-detail",), {"action": "store_true"})
	])
	asyncio.run(main(args))
