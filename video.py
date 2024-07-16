#!/usr/bin/env python3

import os
import re
import time
import json
import asyncio
import logging
import collections

import core
import runtime
import network
import verify

# constants

DEFAULT_PREFER = "avc"
DEFAULT_REJECT = "hevc unknown"

BV_INFO_URL = "https://api.bilibili.com/x/web-interface/view"
DANMAKU_URL = "https://comment.bilibili.com/%d.xml"
PLAY_INFO_URL = "https://api.bilibili.com/x/player/playurl"

# static objects

codec_name_map = collections.defaultdict(lambda: "unknown", [
	(0, "default"),
	(7, "avc"),
	(12, "hevc"),
	(13, "av1"),
])

logger = logging.getLogger("bili_arch.video")
part_pattern = re.compile(r"([VSD]):(\d+)")

# helper functions

async def get_bv_info(sess, bvid):
	info = await network.request(sess, "GET", BV_INFO_URL, params = {"bvid": bvid})
	return info


async def get_play_info(sess, bvid, cid):
	# TODO switch to wbi interface
	params = {
		"bvid": bvid,
		"cid": cid,
		"qn": 112,
		"fnval": 16,
	}
	info = await network.request(sess, "GET", PLAY_INFO_URL, params = params)
	return info


# stub
def verify_bv(bv_root):
	return verify.verify_bv(bv_root, ignore = "S")


def is_interactive(info):
	# TODO
	return False


def save_interactive(iv_info, path):
	raise NotImplementedError("interactive video not implemented")


def save_info(info, bv_root):
	file_path = os.path.join(bv_root, "info.json")
	with core.staged_file(file_path, "w", rotate = True) as f:
		json.dump(info, f, indent = '\t', ensure_ascii = False)


def find_best(info, prefer = "", reject = ""):
	result = None
	for v in info:
		codec_name = codec_name_map[v.get("codecid")]
		if codec_name in reject:
			continue
		if result and result.get("id") >= v.get("id"):
			if result.get("id") > v.get("id"):
				continue

			result_codec_name = codec_name_map[result.get("codecid")]
			if prefer.find(codec_name) <= result.get("codec_name"):
				continue

		result = v.copy()
		result["codec_name"] = codec_name

	if result:
		logger.debug("best: id %d, codec %s(%d)", result.get("id"), result.get("codec_name"), result.get("codecid"))
	return result


def find_best_url_dash(info, *, prefer, reject):
	result = {}
	def fill_url(name, ext, dummy_name = None):
		url_info = find_best(info.get(name, []), prefer, reject)
		if not url_info:
			norej_info = find_best(info.get(name, []), prefer)
			if norej_info:
				logger.warning("bad reject: %s", reject)
				url_info = norej_info

		if url_info:
			file_name = str(url_info.get("id")) + ext
			url_list = [ url_info.get("base_url") ] + url_info.get("backup_url")
			if url_list:
				result[file_name] = url_list
				return

		if dummy_name:
			logger.warning("no %s", name)
			result[dummy_name] = []
		else:
			raise Exception("no url info for %s", name)

	fill_url("video", ".m4v")
	fill_url("audio", ".m4a", core.default_names.noaudio)
	return result


def find_best_url_durl(info, *, prefer, reject):
	return {
		"video.flv": [ info[0]["url"] ] + info[0]["backup_url"]
	}


async def fetch_interactive_info(sess, info, stall = None):
	raise NotImplementedError("interactive video not implemented")


async def fetch_info(sess, bv, stall = None):
	logger.debug("fetch video info %s", bv)
	stall and await stall()
	info = await get_bv_info(sess, bv)
	part_list = None

	if is_interactive(info):
		logger.info("detected interactive video")
		iv_info = await fetch_interactive_info(sess, info, stall)
		info["interactive"] = iv_info
		part_list = iv_info.get("nodes")
	else:
		part_list = info.get("pages")
		expect_count = info.get("videos")
		actual_count = len(part_list)
		if expect_count != actual_count:
			msg = "part count mismatch: %d/%d" % (actual_count, expect_count)
			if expect_count < actual_count:
				logger.warning(msg)
				info["videos"] = actual_count
			else:
				raise Exception(msg)

	# skip subtitle for now
	return info


async def fetch_part(sess, bvid, cid, path, force, /, stall = None, *, prefer = None, reject = None):
	logger.debug("fetch part for %d, path %s, force %x", cid, path, force)
	stall and await stall()
	play_info = await get_play_info(sess, bvid, cid)

	if prefer is None:
		prefer = DEFAULT_PREFER
	if reject is None:
		reject = DEFAULT_REJECT
	logger.debug("prefer %s, reject %s", prefer, reject)

	if "dash" in play_info:
		logger.debug("dash format")
		url_info = find_best_url_dash(play_info.get("dash"), prefer = prefer, reject = reject)
	elif "durl" in play_info:
		logger.debug("durl format")
		url_info = find_best_url_durl(play_info.get("durl"), prefer = prefer, reject = reject)
	else:
		raise Exception("unknown URL format")

	exception = None
	for name, url_list in url_info.items():
		file_path = os.path.join(path, name)
		if (not force) and os.path.isfile(file_path):
			continue

		if not url_list:
			core.touch(file_path)
			continue

		for url in url_list:
			try:
				stall and await stall()
				await network.fetch(sess, url, file_path)
				break
			except Exception:
				logger.exception("failed to fetch part %d", cid)
		else:
			exception = Exception("cannot fetch %s:%d:%s after %d attempts", bvid, cid, name, len(url_list))

	if exception is not None:
		raise exception


async def fetch_cover(sess, info, path, force, stall = None):
	logger.debug("fetch video cover for %s, path %s, force %x" , info.get("bvid"), path, force)

	url = info.get("pic")
	ext = os.path.splitext(url)[1]
	if not ext:
		ext = ".jpg"

	cover_file = os.path.join(path, "cover" + ext)
	if force or not os.path.isfile(cover_file):
		stall and await stall()
		await network.fetch(sess, url, cover_file, rotate = True)
	else:
		logger.debug("skip cover")


async def fetch_danmaku(sess, cid, path, force, stall = None):
	logger.debug("fetch danmaku for %d, path %s, force %x", cid, path, force)

	xml_file = os.path.join(path, "danmaku.xml")
	url = DANMAKU_URL % cid
	if force or not os.path.isfile(xml_file):
		stall and await stall()
		await network.fetch(sess, url, xml_file, rotate = True)
	else:
		logger.debug("skip danmaku")


async def fetch_subtitle(sess, info, cid, path, force, stall = None):
	logger.warning("subtitle not implemented, skip")
	return


async def do_fix(sess, bv, path, stall = None, **kwargs):
	with core.locked_path(path, bv) as bv_root:
		info = None
		exception = None

		stat = verify_bv(bv_root)
		for k, v in stat.items():
			if not v:
				break
		else:
			logger.info("skip existing %s", bv)
			return

		logger.info("fixing %s", bv)
		if not stat.get("info", False):
			info = await fetch_info(sess, bv, stall)
			save_info(info, bv_root)

			stat = verify_bv(bv_root)
			for k, v in stat.items():
				if not v:
					raise Exception("failed to fix " + bv)

		if not stat.get("cover", False):
			info = info or await fetch_info(sess, bv, stall)
			try:
				await fetch_cover(sess, info, bv_root, True, stall)
			except Exception as e:
				logger.exception("failed to fetch cover for %s", bv)
				exception = e

		if not stat.get("graph", True):
			info = info or await fetch_info(sess, bv, stall)
			save_interactive(info.get("interactive"), bv_root)

		for k, r in stat.items():
			if r:
				continue

			match = part_pattern.fullmatch(k)
			if not match:
				continue

			logger.debug("fixing %s", k)
			t = match.group(1)
			cid = int(match.group(2))

			with core.locked_path(bv_root, str(cid)) as part_root:
				try:
					if t == 'V':
						await fetch_part(sess, bv, cid, part_root, True, stall, **kwargs)
					elif t == 'D':
						await fetch_danmaku(sess, cid, part_root, True, stall)
					elif t == 'S':
						info = info or await fetch_info(sess, bv, stall)
						await fetch_subtitle(sess, info, cid, part_root, True, stall)
				except Exception as e:
					logger.exception("failed to fetch part %d", cid)
					exception = e

		stat = verify_bv(bv_root)
		for k, v in stat.items():
			if not v:
				break
		else:
			logger.info("fixed %s", bv)
			return

		if exception is None:
			exception = Exception("failed to fix " + bv)

		raise exception


async def do_update(sess, bv, path, force, stall = None, **kwargs):
	info = await fetch_info(sess, bv, stall)

	logger.info("downloading %s, title %s", bv, info.get("title", ""))
	exception = None

	with core.locked_path(path, bv) as bv_root:
		save_info(info, bv_root)

		try:
			await fetch_cover(sess, info, bv_root, force, stall)
		except Exception as e:
			logger.exception("failed to fetch cover for %s", bv)
			exception = e

		if "interactive" in info:
			part_list = iv_info.get("nodes")
			save_interactive(info, bv_root)
		else:
			part_list = info.get("pages")

		finished_parts = 0
		logger.debug("video parts %d", len(part_list))

		for part in part_list:
			cid = int(part.get("cid"))
			logger.info("downloading %d, %s", cid, part.get("part", None) or part.get("title", ""))
			with core.locked_path(bv_root, str(cid)) as part_root:

				try:
					await fetch_part(sess, bv, cid, part_root, force, stall, **kwargs)

					# always update danmaku
					await fetch_danmaku(sess, cid, part_root, True, stall)

					if info.get("subtitle"):
						await fetch_subtitle(sess, info, cid, part_root, force, stall)

					finished_parts += 1
				except Exception as e:
					logger.exception("failed to fetch part %d", cid)
					exception = e

	logger.info("finished %s, part %d/%d", bv, finished_parts, len(part_list))

	if exception is not None:
		raise exception


# methods

async def download(sess, bv, video_root, mode, stall = None, **kwargs):
	if mode == "fix" and not os.path.isfile(os.path.join(video_root, bv, "info.json")):
		mode = "update"

	logger.debug("downloading %s, path %s, mode %s", bv, video_root, mode)

	if mode == "fix":
		await do_fix(sess, bv, video_root, stall, **kwargs)
	else:
		await do_update(sess, bv, video_root, mode == "force", stall, **kwargs)


async def batch_download(sess, bv_list, video_root, mode, **kwargs):
	logger.info("downloading %d videos", len(bv_list))
	fetched_video = 0
	stall = runtime.Stall()
	for bv in bv_list:
		fetch_status = False
		try:
			assert(runtime.bv_pattern.fullmatch(bv))
			await download(sess, bv, video_root, mode, stall, **kwargs)
			fetched_video += 1
			fetch_status = True
		except Exception as e:
			logger.exception("failed to fetch video %s", bv)

		runtime.report("video", fetch_status, bv)

	logger.info("finish video download %d/%d", fetched_video, len(bv_list))


# entrance

async def main(args):
	if len(args.inputs) > 0:
		logger.debug("%d BV on cmdline", len(args.inputs))
		bv_list = args.inputs
	else:
		logger.debug("scan BV in %s", args.dir or "(cwd)")
		bv_list = runtime.list_bv(args.dir)

	logger.info("BV count %d, mode %s", len(bv_list), args.mode)
	logger.debug(bv_list)

	video_root = args.dir or runtime.subdir("video")
	async with network.session() as sess:
		await batch_download(sess, bv_list, video_root, mode = args.mode, prefer = args.prefer, reject = args.reject)


if __name__ == "__main__":
	args = runtime.parse_args(("network", "auth", "dir", "bandwidth", "video_mode", "prefer"), [
		(("inputs",), {"nargs" : '*'}),
	])
	asyncio.run(main(args))

