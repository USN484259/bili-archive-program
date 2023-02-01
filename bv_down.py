#!/usr/bin/env python3

import os
import re
import json
from bilibili_api import video
import util
from verify import verify_bv

part_pattern = re.compile(r"P(\d+)")

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
		util.logt("quality " + str(v.get("id")))
		if v.get("id") > result.get("id"):
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
			return
		except Exception as e:
			util.handle_exception(e, "failed to fetch media")
			util.logw("URL failed " + str(i + 1) + '/' + str(len(url_list)))

	util.loge("no valid URL (0/" + str(len(url_list)) + ')')


async def download_part(v, part_num, path, force):
	part_path = path + 'P' + str(part_num) + os.path.sep
	util.mkdir(part_path)

	await util.stall()
	url = await v.get_download_url(part_num - 1)
	util.logt(url)

	if "dash" in url:
		dash = url.get("dash")
		best_audio = find_best(dash.get("audio"))
		util.logv("audio quality " + str(best_audio.get("id")))

		best_video = find_best(dash.get("video"))
		util.logv("video quality " + str(best_video.get("id")))

		audio_file = part_path + str(best_audio.get("id")) + ".m4a"
		video_file = part_path + str(best_video.get("id")) + ".m4v"

		if force or not os.path.exists(audio_file):
			util.logv("fetch audio")
			await fetch_media(best_audio, audio_file, "base_url")
		else:
			util.logv("skip audio")

		if force or not os.path.exists(video_file):
			util.logv("fetch video")
			await fetch_media(best_video, video_file, "base_url")
		else:
			util.logv("skip video")

	elif "durl" in url:
		util.logw("unusual video without 'dash' instance, try 'durl'")
		video_file = part_path + "video.flv"
		if force or not os.path.exists(video_file):
			await fetch_media(url.get("durl")[0], video_file, "url")

	else:
		raise Exception("unknown URL format")


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
		util.logv("fetch video info")
		await util.stall()
		info = await v.get_info()
		util.save_json(info, bv_root + "info.json")

		stat = verify_bv(bv, path)
		for k, v in stat.items():
			if not v:
				raise Exception("failed to fix " + bv)

	if not stat.get("cover", False):
		util.logv("fetch video cover")
		if not info:
			await util.stall()
			info = await v.get_info()

		await util.fetch(info.get("pic"), bv_root + "cover.jpg")

	for k, t in stat.items():
		if not t:
			match = part_pattern.fullmatch(k)
			if match:
				util.logv("fetch " + k)
				await download_part(v, int(match.group(1)), bv_root, True)

	stat = verify_bv(bv, path)
	for k, v in stat.items():
		if not v:
			raise Exception("failed to fix " + bv)

	util.logi("fixed " + bv)


async def do_update(bv, path, credential, force_mode):
	bv_root = path + bv + os.path.sep
	v = video.Video(bvid = bv, credential = credential)

	await util.stall()
	info = await v.get_info()
	util.logt(info)

	util.mkdir(bv_root)
	util.logi("downloading " + bv, info.get("title", ""))

	saved_info = None
	if not force_mode:
		try:
			util.logv("loading saved info")
			with open(bv_root + "info.json", "r") as f:
				saved_info = json.load(f)
				util.logt(saved_info)
		except Exception as e:
			util.handle_exception(e, "cannot load saved info")
			pass

	util.logv("save video info")
	util.save_json(info, path + bv + os.path.sep + "info.json")

	util.logv("fetch video cover")
	await util.fetch(info.get("pic"), bv_root + "cover.jpg")

	for i in range(info.get("videos")):
		part_info = info.get("pages")[i]
		util.logi("downloading P" + str(i + 1), part_info.get("part"))

		force = force_mode
		if saved_info:
			try:
				saved_cid = saved_info.get("pages")[i].get("cid")
				cur_cid = part_info.get("cid")
				if saved_cid != cur_cid:
					util.logw("part cid mismatch, re-download")
					util.logv("saved-cid " + str(saved_cid), "current-cid " + str(cur_cid))
					force = True
			except Exception as e:
				util.handle_exception(e, "exception when checking part cid")

		await download_part(v, i + 1, bv_root, force)

	util.logi("finished " + bv)


async def download(bv, path = None, credential = None, mode = None):
	if not mode:
		mode = "fix"

	path = util.opt_path(path)

	util.logv("downloading " + bv, "path " + path, "mode " + mode, "auth " + ("yes" if credential else "no"))

	try:
		if mode != "fix" or not os.path.isfile(path + bv + os.path.sep + "info.json"):
			await do_update(bv, path, credential, mode == "force")
		else:
			await do_fix(bv, path, credential)

		return True
	except Exception as e:
		util.handle_exception(e, "failed to download " + bv)
		return False


async def main(args):
	credential = None
	if args.auth:
		credential = util.credential(args.auth)

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
	args = util.parse_args()
	util.run(main(args))


