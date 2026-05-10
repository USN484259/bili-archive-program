#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()

import re
import json
import csv
import asyncio
import logging

import constants
import runtime
import network

logger = logging.getLogger("bili_arch.sc_bvid")

BV_INFO_URL = "https://api.bilibili.com/x/web-interface/view"


async def get_bv_info(sess, bvid):
	resp = await network.request(sess, "GET", BV_INFO_URL, params = {"bvid": bvid})
	return resp.get("data")



async def main(args):
	sess = network.session()
	stall = runtime.Stall(args.stall)
	with open(args.output, mode = "w", newline = "") as out:
		csv_writer = csv.writer(out)
		while True:
			try:
				line = input().strip()
				if not line:
					continue
				obj = json.loads(line)
				if obj.get("cmd", "") == "SUPER_CHAT_MESSAGE":
					data = obj["data"]
					uname = data["user_info"]["uname"]
					message = data["message"].strip()
					logger.debug("%d\t%s\t%s", obj["timestamp"], uname, message)
					bv_list = constants.bvid_pattern.findall(message)
					if bv_list:
						result = ""
						duration = 0
						for bvid in bv_list:
							try:
								await stall()
								info = await get_bv_info(sess, bvid)
								duration += info["duration"]
								result += "https://www.bilibili.com/video/%s\n" % bvid
								logger.info("%s\t%s\t%d", uname, bvid, info["duration"])

							except Exception as e:
								logger.warning("cannot get BV info: %s", str(e))

						csv_writer.writerow(
							(obj["timestamp"], uname, result.strip(), duration, message)
						)
					elif "BV" in message:
						logger.warning("unknown BV from %s: %s", uname, message)
						continue


			except EOFError:
				break
			except Exception:
				logger.exception("")


if __name__ == "__main__":
	args = runtime.parse_args(("network",), [
		(("-o", "--output"), {"required": True}),
	])
	asyncio.run(main(args))

