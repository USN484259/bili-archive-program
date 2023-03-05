#!/usr/bin/env python3

import os
import re
import json
import shutil
import subprocess
from bilibili_api import video
import util
from verify import verify_bv
from interactive import is_interactive, to_interactive, save_graph

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
		util.logt("quality " + str(v.get("id")), "codec " + str(v.get("codecid")))
		if v.get("id") < result.get("id"):
			continue
		elif v.get("id") == result.get("id") and v.get("codecid") <= result.get("codecid"):
			continue
		result = v
	return result


async def fetch_media(url_info, path, key_name):
	url_list = [
		url_info.get(key_name)
	] + url_info.get("backup_url")
	util.logt(url_list)

	for i, url in enumerate(url_list):
		try:
			await util.fetch(url, path)
			return True
		except Exception as e:
			util.handle_exception(e, "failed to fetch media")
			util.logw("URL failed " + str(i + 1) + '/' + str(len(url_list)))

	util.loge("no valid URL (0/" + str(len(url_list)) + ')')
	return False


async def download_part(v, cid, path, force):
	util.logv("download part for " + str(cid), "path " + path, "force " + str(force))
	await util.stall()
	url = await v.get_download_url(cid = cid)
	util.logt(url)

	result = True
	if "dash" in url:
		dash = url.get("dash")

		best_video = find_best(dash.get("video"))
		util.logv("video quality " + str(best_video.get("id")), "codec " + best_video.get("codecs"))
		video_file = path + str(best_video.get("id")) + ".m4v"

		if force or not os.path.isfile(video_file):
			util.logv("fetch video")
			result = await fetch_media(best_video, video_file, "base_url")
		else:
			util.logv("skip video")

		if dash.get("audio", None):
			best_audio = find_best(dash.get("audio"))
			util.logv("audio quality " + str(best_audio.get("id")), "codec " + best_audio.get("codecs"))
			audio_file = path + str(best_audio.get("id")) + ".m4a"

			if force or not os.path.isfile(audio_file):
				util.logv("fetch audio")
				result = await fetch_media(best_audio, audio_file, "base_url") and result
			else:
				util.logv("skip audio")
		else:
			util.logw("no audio")
			util.touch(path + util.noaudio_stub)


	elif "durl" in url:
		util.logw("unusual video without 'dash' instance, try 'durl'")
		video_file = path + "video.flv"
		if force or not os.path.isfile(video_file):
			result = await fetch_media(url.get("durl")[0], video_file, "url")

	else:
		util.loge("unknown URL format")
		result = False

	return result


async def fetch_cover(info, path, force):
	util.logv("fetch video cover for " + info.get("bvid"), "path " + path, "force " + str(force))
	try:
		url = info.get("pic")
		ext = os.path.splitext(url)[1]
		if not ext or ext == "":
			ext = ".jpg"

		cover_file = path + "cover" + ext
		if force or not os.path.isfile(cover_file):
			await util.fetch(url, cover_file)
		else:
			util.logv("skip cover")

		return True

	except Exception as e:
		util.handle_exception(e, "error on fetch cover")
		return False


async def fetch_danmaku(cid, path, force):
	util.logv("fetch danmaku for " + str(cid), "path " + path, "force " + str(force))
	try:
		xml_file = path + "danmaku.xml"
		if force or not os.path.isfile(xml_file):
			await util.fetch("https://comment.bilibili.com/" + str(cid) + ".xml", xml_file)
		else:
			util.logv("skip danmaku")

		return True

	except Exception as e:
		util.handle_exception(e, "error on fetch danmaku")
		return False


async def fetch_subtitle(info, cid, path, force):
	util.logv("fetch subtitle for " + str(cid), "path " + path, "force " + str(force))
	try:
		subtitle = info.get("subtitle").get(str(cid))
		util.logt(subtitle)
		for i, sub in enumerate(subtitle.get("subtitles")):
			lan = sub.get("lan")
			util.logv("subtitle lang " + lan)
			subtitle_file = path + "subtitle." + lan + ".json"
			if force or not os.path.isfile(subtitle_file):
				await util.fetch("https:" + sub.get("subtitle_url"), subtitle_file)
			else:
				util.logv("skip subtitle " + lan)

		return True

	except Exception as e:
		util.handle_exception(e, "error on fetch subtitle")
		return False


async def fetch_info(v):
	util.logv("fetch video info " + v.get_bvid())
	await util.stall()
	info = await v.get_info()
	util.logt(info)
	part_list = None

	if is_interactive(info):
		util.logw("detected interactive video")
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
				util.logw(*msg)
				info["videos"] = actual_count
			else:
				raise Exception(msg[0] + ':' + msg[1])

	subtitle_map = {}
	for part in part_list:
		cid = part.get("cid")
		await util.stall()
		subtitle = await v.get_subtitle(cid)
		util.logv("subtitle for " + str(cid), "count " + str(len(subtitle.get("subtitles"))))
		util.logt(subtitle)
		subtitle_map[str(cid)] = subtitle

	info["subtitle"] = subtitle_map

	return info


async def save_interactive(iv_info, bv_root, fmt = "svg"):
	dot_file = bv_root + "graph.dot"
	save_graph(iv_info, dot_file)
	if dot_bin:
		util.logv("running dot on " + dot_file,"image format " + fmt)
		image_file = bv_root + "graph." + fmt
		cmdline = [dot_bin, "-T" + fmt, dot_file, "-o", image_file]
		util.logt(cmdline)
		subprocess.run(cmdline)
	else:
		util.logw("dot binary not found, skip")

	theme_url = iv_info.get("theme").get("choice_image", None)
	if theme_url:
		util.logv("fetch theme image")
		ext = os.path.splitext(theme_url)[1]
		if not ext or ext == "":
			ext = ".jpg"

		theme_file = bv_root + "theme" + ext
		# assume theme is unlikely to change
		if not os.path.isfile(theme_file):
			await util.fetch(theme_url, theme_file)
	else:
		util.logw("missing theme image")



async def do_fix(bv, path, credential):
	bv_root = path + bv + os.path.sep
	info = None

	stat = verify_bv(bv, path)
	for k, v in stat.items():
		if not v:
			break
	else:
		util.logi("skip existing " + bv)
		return

	util.logi("fixing " + bv)
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

		util.logv("fixing " + k)
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

	util.logi("fixed " + bv)


async def do_update(bv, path, credential, force):
	bv_root = path + bv + os.path.sep
	v = video.Video(bvid = bv, credential = credential)

	info = await fetch_info(v)
	util.logi("downloading " + bv, info.get("title", ""))

	util.mkdir(bv_root)
	util.logv("save video info")
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
	util.logv("video parts " + str(len(part_list)))

	for part in part_list:
		cid = part.get("cid")
		util.logi("downloading " + str(cid), part.get("part", None) or part.get("title", ""))

		part_root = bv_root + str(cid) + os.path.sep
		util.mkdir(part_root)

		result = await download_part(v, cid, part_root, force)

		# always update danmaku
		result = await fetch_danmaku(cid, part_root, True) and result

		result = await fetch_subtitle(info, cid, part_root, force) and result

		if result:
			finished_parts += 1
		else:
			util.loge("error in downloading " + str(cid))

	util.logi("finished " + bv, "part " + str(finished_parts) + '/' + str(len(part_list)))
	return cover_result and (finished_parts == len(part_list))


async def download(bv, path = None, credential = None, mode = None):
	if not mode:
		mode = "fix"

	path = util.opt_path(path)


	if mode == "fix" and not os.path.isfile(path + bv + os.path.sep + "info.json"):
		mode = "update"

	util.logv("downloading " + bv, "path " + path, "mode " + mode, "auth " + ("yes" if credential else "no"))

	try:
		if mode == "fix":
			await do_fix(bv, path, credential)
			return True
		else:
			return await do_update(bv, path, credential, mode == "force")


	except Exception as e:
		util.handle_exception(e, "failed to download " + bv)
		return False


async def main(args):
	credential = None
	if args.auth:
		credential = await util.credential(args.auth)

	if len(args.inputs) > 0:
		util.logv(str(len(args.inputs)) + " BV on cmdline")
		bv_list = args.inputs
	else:
		util.logv("scan BV in " + (args.dest or "(cwd)"))
		bv_list = util.list_bv(args.dest)

	util.logi("BV count " + str(len(bv_list)), "mode " + str(args.mode))
	util.logv(bv_list)
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


