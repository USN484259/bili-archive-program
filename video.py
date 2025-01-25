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
DANMAKU_URL = "https://comment.bilibili.com/%s.xml"
PLAY_INFO_URL = "https://api.bilibili.com/x/player/playurl"

# static objects

codec_name_map = collections.defaultdict(lambda: "unknown", [
	(0, "default"),
	(7, "avc"),
	(12, "hevc"),
	(13, "av1"),
])

logger = logging.getLogger("bili_arch.video")

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
def verify_bv(bv_root, ignore):
	return verify.verify_bv(bv_root, ignore = ignore + "S")


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


def find_best_url_dash(info, request, *, prefer, reject):
	result = {}
	components = ""
	def fill_url(name, ext):
		url_info = find_best(info.get(name) or [], prefer, reject)
		if not url_info:
			norej_info = find_best(info.get(name) or [], prefer)
			if norej_info:
				logger.warning("bad reject: %s", reject)
				url_info = norej_info

		if url_info:
			file_name = str(url_info.get("id")) + ext
			url_list = [ url_info.get("base_url") ] + url_info.get("backup_url", [])
			if url_list:
				result[file_name] = url_list
				return True

	if 'V' in request:
		if fill_url("video", ".m4v"):
			components += 'V'
		else:
			logger.warning("no url info for video")

	if 'A' in request:
		if fill_url("audio", ".m4a"):
			components += 'A'
		else:
			logger.warning("no url info for audio")

	return result, components


def find_best_url_durl(info, *, prefer, reject):
	return {
		"video.flv": [ info[0]["url"] ] + info[0].get("backup_url", [])
	}, "VA"


async def fetch_interactive_info(sess, info, stall = None):
	raise NotImplementedError("interactive video not implemented")


async def fetch_info(sess, bv, /, stall = None):
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
				raise RuntimeError(msg)

	# skip subtitle for now
	return info


async def fetch_part(sess, bvid, cid, path, force, /, stall = None, *, request = "VA", prefer = None, reject = None):
	logger.debug("fetch part for %s, path %s, force %x", cid, path, force)
	stall and await stall()
	play_info = await get_play_info(sess, bvid, cid)

	if prefer is None:
		prefer = DEFAULT_PREFER
	if reject is None:
		reject = DEFAULT_REJECT
	logger.debug("prefer %s, reject %s", prefer, reject)

	components = request
	if "dash" in play_info:
		logger.debug("dash format")
		url_info, components = find_best_url_dash(play_info.get("dash"), request, prefer = prefer, reject = reject)
	elif "durl" in play_info:
		logger.debug("durl format")
		url_info = find_best_url_durl(play_info.get("durl"), prefer = prefer, reject = reject)
	else:
		raise RuntimeError("unknown URL format")

	if components != request:
		logger.warning("missing components %s/%s", components, request)

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
				logger.exception("failed to fetch part %s", cid)
		else:
			exception = Exception("cannot fetch %s:%s:%s after %d attempts" % (bvid, cid, name, len(url_list)))

	if exception is not None:
		raise exception

	if 'V' in request and 'V' not in components:
		core.touch(os.path.join(path, core.default_names.novideo))
	if 'A' in request and 'A' not in components:
		core.touch(os.path.join(path, core.default_names.noaudio))


async def fetch_cover(sess, info, path, force, stall = None):
	logger.debug("fetch video cover for %s, path %s, force %x" , info.get("bvid"), path, force)

	url = info.get("pic")
	cover_name = os.path.split(url)[1]

	cover_path = os.path.join(path, cover_name)
	if force or not os.path.isfile(cover_path):
		stall and await stall()
		await network.fetch(sess, url, cover_path)
	else:
		logger.debug("skip cover")


async def fetch_danmaku(sess, cid, path, force, stall = None):
	logger.debug("fetch danmaku for %s, path %s, force %x", cid, path, force)

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


async def do_fix(sess, bv, path, /, stall = None, ignore = None, max_duration = None, **kwargs):
	with core.locked_path(path, bv) as bv_root:
		info = None
		exception = None
		fetched_info = False

		ignore = ignore or ""
		stat = verify_bv(bv_root, ignore)
		if verify.check_result(stat):
			logger.info("skip existing %s", bv)
			return

		logger.info("fixing %s", bv)
		if stat.get("info", False):
			try:
				with open(os.path.join(bv_root, "info.json"), 'r') as f:
					info = json.load(f)
			except Exception:
				logger.exception("failed to load info.json")

		while not fetched_info:
			if not info:
				info = await fetch_info(sess, bv, stall)
				save_info(info, bv_root)
				fetched_info = True

			# fetch-cover may fail due to changed cover upstream
			# re-fetching info from upstream and try again
			if not stat.get("cover", True):
				try:
					await fetch_cover(sess, info, bv_root, True, stall)
					break
				except Exception as e:
					logger.exception("failed to fetch cover for %s", bv)
					if fetched_info:
						exception = e
						break
					else:
						logger.debug("dropping info and re-fetch")
						info = None
			else:
				break

		if fetched_info:
			stat = verify_bv(bv_root, ignore)
			if not verify.check_result(stat):
				raise RuntimeError("failed to fix " + bv)


		if not stat.get("graph", True):
			save_interactive(info.get("interactive"), bv_root)

		if max_duration:
			video_duration = info.get("duration", 0)
			if video_duration > max_duration:
				raise Exception("duration exceeds limit: %d/%d" % (video_duration, max_duration))

		for cid, part_stat in stat.get("parts").items():
			with core.locked_path(bv_root, cid) as part_root:
				logger.info("%s %s", part_root, cid + str(part_stat))
				request = ""
				if not part_stat.get('V', True):
					request += 'V'
				if not part_stat.get('A', True):
					request += 'A'
				try:
					if request:
						logger.debug("fixing %s %s", cid, request)
						await fetch_part(sess, bv, cid, part_root, True, stall, request = request, **kwargs)
					if not part_stat.get('D', True):
						logger.debug("fixing %s D", cid)
						await fetch_danmaku(sess, cid, part_root, True, stall)
					if not part_stat.get('S', True):
						logger.debug("fixing %s S", cid)
						await fetch_subtitle(sess, info, cid, part_root, True, stall)
				except Exception as e:
					logger.exception("failed to fetch part %s", cid)
					exception = e

		stat = verify_bv(bv_root, ignore)
		if verify.check_result(stat):
			logger.info("fixed %s", bv)
			return

		if exception is None:
			exception = Exception("failed to fix " + bv)

		raise exception


async def do_update(sess, bv, path, force, /, stall = None, ignore = None, max_duration = None, **kwargs):
	info = await fetch_info(sess, bv, stall)

	logger.info("downloading %s, title %s", bv, info.get("title", ""))
	exception = None

	ignore = ignore or ""
	with core.locked_path(path, bv) as bv_root:
		save_info(info, bv_root)

		try:
			if 'C' not in ignore:
				await fetch_cover(sess, info, bv_root, force, stall)
		except Exception as e:
			logger.exception("failed to fetch cover for %s", bv)
			exception = e

		if 'P' in ignore:
			part_list = []
		elif "interactive" in info:
			part_list = iv_info.get("nodes")
			save_interactive(info, bv_root)
		else:
			part_list = info.get("pages")

		if max_duration:
			video_duration = info.get("duration", 0)
			if video_duration > max_duration:
				raise Exception("duration exceeds limit: %d/%d" % (video_duration, max_duration))

		finished_parts = 0
		logger.debug("video parts %d", len(part_list))

		for part in part_list:
			cid = str(part.get("cid", ""))
			logger.info("downloading %s, %s", cid, part.get("part", None) or part.get("title", ""))
			with core.locked_path(bv_root, cid) as part_root:

				try:
					request = ""
					if 'V' not in ignore:
						request += 'V'
					if 'A' not in ignore:
						request += 'A'
					await fetch_part(sess, bv, cid, part_root, force, stall, request = request, **kwargs)

					if 'D' not in ignore:
						# always update danmaku
						await fetch_danmaku(sess, cid, part_root, True, stall)

					if 'S' not in ignore and info.get("subtitle"):
						await fetch_subtitle(sess, info, cid, part_root, force, stall)

					finished_parts += 1
				except Exception as e:
					logger.exception("failed to fetch part %s", cid)
					exception = e

	logger.info("finished %s, part %d/%d", bv, finished_parts, len(part_list))

	if exception is not None:
		raise exception


# methods

async def download(sess, bv, video_root, mode, /, stall = None, **kwargs):
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
			assert(core.bvid_pattern.fullmatch(bv))
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
		await batch_download(sess, bv_list, video_root, mode = args.mode, ignore = args.ignore, prefer = args.prefer, reject = args.reject)


if __name__ == "__main__":
	args = runtime.parse_args(("network", "auth", "dir", "bandwidth", "video_mode", "video_ignore", "prefer"), [
		(("inputs",), {"nargs" : '*'}),
	])
	asyncio.run(main(args))

