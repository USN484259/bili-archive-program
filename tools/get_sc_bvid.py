#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()

import re
import time
import json
import csv
import asyncio
import logging
from collections import defaultdict
from contextlib import suppress

openpyxl = None
with suppress(ModuleNotFoundError):
	import openpyxl


import constants
import runtime
import network

logger = logging.getLogger("bili_arch.sc_bvid")

BV_INFO_URL = "https://api.bilibili.com/x/web-interface/view"
MEDAL_WALL_URL = "https://api.live.bilibili.com/xlive/web-ucenter/user/MedalWall"


async def get_bv_info(sess, bvid):
	resp = await network.request(sess, "GET", BV_INFO_URL, params = {"bvid": bvid})
	return resp.get("data")


async def get_medal_info(sess, uid):
	resp = await network.request(sess, "GET", MEDAL_WALL_URL, params = {"target_id": uid})
	return resp.get("data")


class csv_writer:
	def __init__(self, path):
		self.f = open(path, mode = "x", newline = "")
		try:
			self.writer = csv.writer(self.f)
		except Exception:
			self.close()

	def close(self):
		if self.f is not None:
			try:
				self.f.close()
			finally:
				self.f = None

	def __enter__(self):
		return self

	def __exit__(self, *args):
		self.close()

	def write_header(self, *header):
		self.writer.writerow(header)

	def write_row(self, *row):
		self.writer.writerow(row)


if openpyxl:
	class xlsx_writer:
		def __init__(self, path):
			self.path = path
			self.wb = openpyxl.Workbook(write_only = True)
			self.sheet = self.wb.active or self.wb.create_sheet()

		def close(self):
			try:
				if self.wb:
					self.wb.save(self.path)
					self.wb.close()
			finally:
				self.wb = None
				self.sheet = None

		def __enter__(self):
			return self

		def __exit__(self, *args):
			self.close()

		def write_header(self, *header):
			self.sheet.append(header)
			self.sheet.freeze_panes = "A2"

		def write_row(self, *row):
			self.sheet.append(row)


def make_medal_str(info):
	medal_name = info.get("medal_name")
	medal_level = info.get("level")
	if medal_name and medal_level:
		return "%s %d" % (medal_name, medal_level)
	else:
		return ""


async def main(args):
	sess = network.session()
	stall = runtime.Stall(1)
	if args.format == "xlsx":
		if not openpyxl:
			raise RuntimeError("xlsx format requires openpyxl library")
		writer = xlsx_writer(args.output)
	else:
		writer = csv_writer(args.output)

	with writer:
		writer.write_header("发送时间", "uid", "用户", "粉丝牌", "点播数", "价格", "BV链接", "视频时长", "视频标题", "SC内容")
		uid_counter = defaultdict(lambda: 0)
		while True:
			try:
				line = input().strip()
				if not line:
					continue
				obj = json.loads(line)
				if obj.get("cmd", "") == "SUPER_CHAT_MESSAGE":
					data = obj["data"]
					uid = int(data["uid"])
					uname = data["user_info"]["uname"]
					price = data["price"]
					message = data["message"].strip()
					logger.info("%d\t%s\t%s\t%s", obj["send_time"], uname, price, message)
					bv_list = constants.bvid_pattern.findall(message)
					if bv_list:
						result = ""
						title = ""
						duration = 0
						medal = ""
						for bvid in bv_list:
							try:
								await stall()
								info = await get_bv_info(sess, bvid)
								duration += info["duration"]
								result += "https://www.bilibili.com/video/%s\n" % bvid
								title += info["title"] + '\n'
								logger.info("%s\t%s\t%d\t%s", uname, bvid, info["duration"], info["title"])

							except Exception as e:
								logger.warning("cannot get video info for %s: %s", bvid, str(e))
						tm = time.localtime(obj["send_time"] // 1000)
						time_str = "%d/%02d/%02d %02d:%02d:%02d.%03d" % (tm[:6] + (obj["send_time"] % 1000,))
						duration_str = (duration >= 3600 and ("%d:" % duration // 3600) or "") + "%02d:%02d" % (duration % 3600 // 60, duration % 60)

						# first try to find medal info in SC message
						medal_info = info.get("medal_info", {})
						if (args.target_uid is None) or medal_info.get("target_id") == args.target_uid:
							medal = make_medal_str(medal_info)

						if args.target_uid and not medal:	# then try to look for medal of the user
							try:
								logger.verbose("fetching medal info for user %s %d", uname, uid)
								await stall()
								medal_info = await get_medal_info(sess, uid)
								assert(medal_info.get("name") == uname)
								for obj in medal_info.get("list"):
									info = obj.get("medal_info", {})
									if info.get("target_id") == args.target_uid:
										medal = make_medal_str(info)
										break
							except Exception as e:
								logger.warning("cannot get medal info for %s: %s", uname, str(e))

						count = uid_counter[uid] + 1
						uid_counter[uid] = count
						logger.info("%s\t%s, count %d", uname, medal, count)
						writer.write_row(time_str, str(uid), uname, medal, count, price, result.strip(), duration_str, title.strip(), message)
					elif "BV" in message:
						logger.warning("unknown BV from %s: %s", uname, message)
						continue


			except EOFError:
				break
			except Exception:
				logger.exception("")


if __name__ == "__main__":
	args = runtime.parse_args(("network", "auth"), [
		(("-o", "--output"), {"required": True}),
		(("--target-uid",), {"type": int}),
		(("-f", "--format"), {"choices" : ["csv", "xlsx"], "default": "csv"}),
	])
	asyncio.run(main(args))

