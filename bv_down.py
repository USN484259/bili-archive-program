#!/usr/bin/env python3

import os
import re
import json
import logging
import shutil
import subprocess
from bilibili_api import video
import util
from verify import verify_bv
from interactive import is_interactive, to_interactive, save_graph

logger = logging.getLogger(__name__)

dot_bin = shutil.which("dot")
part_pattern = re.compile(r"([VSD]):(\d+)")

# mode		operation
# ---------------------------------------------------
# fix		re-download corrupted components
# update	re-download info, cover, better quality
# force		re-download everything

def find_best(res_list):
	result = {
		"id": 0
	}
	for k, v in enumerate(res_list):
		codec_id = v.get("codecid")
		logger.log(util.LOG_TRACE, "quality %d, codec %d", v.get("id"), codec_id)
		if v.get("id") < result.get("id"):
			continue

		# skip HEVC, aka H.265
		# TODO better handling for HEVC
		elif v.get("id") == result.get("id") and codec_id != 12 and codec_id <= result.get("codecid"):
			continue
		result = v
	return result


async def fetch_media(url_info, path, key_name):
	url_list = [
		url_info.get(key_name)
	] + url_info.get("backup_url")
	logger.log(util.LOG_TRACE, url_list)

	for i, url in enumerate(url_list):
		try:
			await util.fetch(url, path)
			return True
		except Exception as e:
			logger.exception("failed to fetch media")
			util.on_exception(e)
			logger.warning("URL failed %d/%d", i + 1, len(url_list))

	logger.error("no valid URL (0/%d)" + len(url_list))
	return False


async def download_part(v, cid, path, force):
	logger.debug("download part for %d, path %s, force %x", cid, path, force)
	await util.stall()
	url = await v.get_download_url(cid = cid)
	logger.log(util.LOG_TRACE, url)

	result = True
	if "dash" in url:
		dash = url.get("dash")

		best_video = find_best(dash.get("video"))
		logger.debug("video quality %d, codec %s", best_video.get("id"), best_video.get("codecs"))
		video_file = path + str(best_video.get("id")) + ".m4v"

		if force or not os.path.isfile(video_file):
			logger.debug("fetch video")
			result = await fetch_media(best_video, video_file, "base_url")
		else:
			logger.debug("skip video")

		if dash.get("audio", None):
			best_audio = find_best(dash.get("audio"))
			logger.debug("audio quality %d, codec %s", best_audio.get("id"), best_audio.get("codecs"))
			audio_file = path + str(best_audio.get("id")) + ".m4a"

			if force or not os.path.isfile(audio_file):
				logger.debug("fetch audio")
				result = await fetch_media(best_audio, audio_file, "base_url") and result
			else:
				logger.debug("skip audio")
		else:
			logger.warning("no audio")
			util.touch(path + util.noaudio_stub)


	elif "durl" in url:
		logger.warning("unusual video without 'dash' instance, try 'durl'")
		video_file = path + "video.flv"
		if force or not os.path.isfile(video_file):
			result = await fetch_media(url.get("durl")[0], video_file, "url")

	else:
		logger.error("unknown URL format")
		result = False

	return result


async def fetch_cover(info, path, force):
	logger.debug("fetch video cover for %s, path %s, force %x" , info.get("bvid"), path, force)
	try:
		url = info.get("pic")
		ext = os.path.splitext(url)[1]
		if not ext or ext == "":
			ext = ".jpg"

		cover_file = path + "cover" + ext
		if force or not os.path.isfile(cover_file):
			await util.fetch(url, cover_file)
		else:
			logger.debug("skip cover")

		return True

	except Exception as e:
		logger.exception("error on fetch cover")
		util.on_exception(e)
		return False


async def fetch_danmaku(cid, path, force):
	logger.debug("fetch danmaku for %d, path %s, force %x", cid, path, force)
	try:
		xml_file = path + "danmaku.xml"
		if force or not os.path.isfile(xml_file):
			await util.fetch("https://comment.bilibili.com/" + str(cid) + ".xml", xml_file)
		else:
			logger.debug("skip danmaku")

		return True

	except Exception as e:
		logger.exception("error on fetch danmaku")
		util.on_exception(e)
		return False


async def fetch_subtitle(info, cid, path, force):
	logger.debug("fetch subtitle for %d, path %s, force %x", cid, path, force)
	try:
		subtitle = info.get("subtitle").get(str(cid))
		logger.log(util.LOG_TRACE, subtitle)
		for i, sub in enumerate(subtitle.get("subtitles")):
			lan = sub.get("lan")
			logger.debug("subtitle lang " + lan)
			subtitle_file = path + "subtitle." + lan + ".json"
			if force or not os.path.isfile(subtitle_file):
				await util.fetch("https:" + sub.get("subtitle_url"), subtitle_file)
			else:
				logger.debug("skip subtitle " + lan)

		return True

	except Exception as e:
		logger.exception("error on fetch subtitle")
		util.on_exception(e)
		return False


async def fetch_info(v):
	logger.debug("fetch video info " + v.get_bvid())
	await util.stall()
	info = await v.get_info()
	logger.log(util.LOG_TRACE, info)
	part_list = None

	if is_interactive(info):
		logger.warning("detected interactive video")
		iv_info = await to_interactive(v)
		info["interactive"] = iv_info
		part_list = iv_info.get("nodes")
	else:
		part_list = info.get("pages")
		expect_count = info.get("videos")
		actual_count = len(part_list)
		if expect_count != actual_count:
			msg = ["part count mismatch", str(actual_count) + '/' + str(expect_count)]
			if expect_count < actual_count:
				logger.warning(*msg)
				info["videos"] = actual_count
			else:
				raise Exception(msg[0] + ':' + msg[1])

	subtitle_map = {}
	for part in part_list:
		cid = part.get("cid")
		await util.stall()
		subtitle = await v.get_subtitle(cid)
		logger.debug("subtitle for %d, count %d", cid, len(subtitle.get("subtitles")))
		logger.log(util.LOG_TRACE, subtitle)
		subtitle_map[str(cid)] = subtitle

	info["subtitle"] = subtitle_map

	return info


async def save_interactive(iv_info, bv_root, fmt = "svg"):
	dot_file = bv_root + "graph.dot"
	save_graph(iv_info, dot_file)
	if dot_bin:
		logger.debug("running dot on %s, image format %s", dot_file, fmt)
		image_file = bv_root + "graph." + fmt
		cmdline = [dot_bin, "-T" + fmt, dot_file, "-o", image_file]
		logger.log(util.LOG_TRACE, cmdline)
		subprocess.run(cmdline)
	else:
		logger.warning("dot binary not found, skip")

	theme_url = iv_info.get("theme").get("choice_image", None)
	if theme_url:
		logger.debug("fetch theme image")
		ext = os.path.splitext(theme_url)[1]
		if not ext or ext == "":
			ext = ".jpg"

		theme_file = bv_root + "theme" + ext
		# assume theme is unlikely to change
		if not os.path.isfile(theme_file):
			await util.fetch(theme_url, theme_file)
	else:
		logger.warning("missing theme image")



async def do_fix(bv, path, credential):
	bv_root = path + bv + os.path.sep
	info = None

	stat = verify_bv(bv, path)
	for k, v in stat.items():
		if not v:
			break
	else:
		logger.info("skip existing " + bv)
		return

	logger.info("fixing " + bv)
	v = video.Video(bvid = bv, credential = credential)
	if not stat.get("info", False):
		info = await fetch_info(v)
		util.save_json(info, bv_root + "info.json")

		stat = verify_bv(bv, path)
		for k, v in stat.items():
			if not v:
				raise Exception("failed to fix " + bv)

	if not stat.get("cover", False):
		info = info or await fetch_info(v)
		await fetch_cover(info, bv_root, True)

	if not stat.get("graph", True):
		info = info or await fetch_info(v)
		await save_interactive(info.get("interactive"), bv_root)

	for k, r in stat.items():
		if r:
			continue

		match = part_pattern.fullmatch(k)
		if not match:
			continue

		logger.debug("fixing " + k)
		t = match.group(1)
		cid = int(match.group(2))

		part_root = bv_root + str(cid) + os.path.sep
		util.mkdir(part_root)
		if t == 'V':
			await download_part(v, cid, part_root, True)
		elif t == 'D':
			await fetch_danmaku(cid, part_root, True)
		elif t == 'S':
			info = info or await fetch_info(v)
			await fetch_subtitle(info, cid, part_root, True)

	stat = verify_bv(bv, path)
	for k, v in stat.items():
		if not v:
			raise Exception("failed to fix " + bv)

	logger.info("fixed " + bv)


async def do_update(bv, path, credential, force):
	bv_root = path + bv + os.path.sep
	v = video.Video(bvid = bv, credential = credential)

	info = await fetch_info(v)
	logger.info("downloading %s, title %s", bv, info.get("title", ""))

	util.mkdir(bv_root)
	logger.debug("save video info")
	util.save_json(info, bv_root + "info.json")

	cover_result = await fetch_cover(info, bv_root, force)

	part_list = None
	if "interactive" in info:
		iv_info = info.get("interactive")
		part_list = iv_info.get("nodes")
		await save_interactive(iv_info, bv_root)
	else:
		part_list = info.get("pages")

	finished_parts = 0
	logger.debug("video parts %d", len(part_list))

	for part in part_list:
		cid = part.get("cid")
		logger.info("downloading %d, %s", cid, part.get("part", None) or part.get("title", ""))

		part_root = bv_root + str(cid) + os.path.sep
		util.mkdir(part_root)

		result = await download_part(v, cid, part_root, force)

		# always update danmaku
		result = await fetch_danmaku(cid, part_root, True) and result

		result = await fetch_subtitle(info, cid, part_root, force) and result

		if result:
			finished_parts += 1
		else:
			logger.error("error in downloading " + str(cid))

	logger.info("finished %s, part %d/%d", bv, finished_parts, len(part_list))
	return cover_result and (finished_parts == len(part_list))


async def download(bv, path = None, credential = None, mode = None):
	if not mode:
		mode = "fix"

	path = util.opt_path(path)


	if mode == "fix" and not os.path.isfile(path + bv + os.path.sep + "info.json"):
		mode = "update"

	logger.debug("downloading %s, path %s, mode %s, auth %x", bv, path, mode, bool(credential))

	try:
		if mode == "fix":
			await do_fix(bv, path, credential)
			return True
		else:
			return await do_update(bv, path, credential, mode == "force")


	except Exception as e:
		logger.exception("failed to download " + bv)
		util.on_exception(e)
		return False


async def main(args):
	credential = None
	if args.auth:
		credential = await util.credential(args.auth)

	if len(args.inputs) > 0:
		logger.debug("%d BV on cmdline", len(args.inputs))
		bv_list = args.inputs
	else:
		logger.debug("scan BV in " + (args.dest or "(cwd)"))
		bv_list = util.list_bv(args.dest)

	logger.info("BV count %d, mode %s", len(bv_list), args.mode)
	logger.debug(bv_list)
	for i, bv in enumerate(bv_list):
		res = await download(bv, path = args.dest, mode = args.mode, credential = credential)
		print(bv, res, flush = True)


if __name__ == "__main__":
	args = util.parse_args([
		(("inputs",), {"nargs" : '*'}),
		(("-u", "--auth"), {}),
		(("-m", "--mode"), {"choices" : ["fix", "update", "force"]}),
	])
	util.run(main(args))


