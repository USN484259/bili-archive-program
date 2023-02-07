#!/usr/bin/env python3

import os
import re
import json
from bilibili_api import video
import util
from verify import verify_bv

part_pattern = re.compile(r"([PSD])(\d+)")

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


async def download_part(v, part_num, path, force):
	await util.stall()
	url = await v.get_download_url(part_num - 1)
	util.logt(url)

	result = None
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


async def fetch_danmaku(v, part_num, path, force):
	try:
		xml_file = path + "danmaku.xml"
		if force or not os.path.isfile(xml_file):
			cid = await v.get_cid(part_num - 1)
			await util.fetch("https://comment.bilibili.com/" + str(cid) + ".xml", xml_file)

		return True

	except Exception as e:
		util.handle_exception(e, "error on fetch danmaku")
		return False


async def fetch_subtitle(info, part_num, path, force):
	try:
		subtitle = info.get("subtitle")[part_num - 1].get("subtitles")
		util.logt(subtitle)
		for i, sub in enumerate(subtitle):
			subtitle_file = path + "subtitle." + sub.get("lan") + ".json"
			if force or not os.path.isfile(subtitle_file):
				await util.fetch("https:" + sub.get("subtitle_url"), subtitle_file)

		return True

	except Exception as e:
		util.handle_exception(e, "error on fetch subtitle")
		return False


async def fetch_info(v):
	util.logv("fetch video info " + v.get_bvid())
	await util.stall()
	info = await v.get_info()
	util.logt(info)

	try:
		subtitle_list = []
		for i in range(info.get("videos")):
			cid = await v.get_cid(i)
			subtitle = await v.get_subtitle(cid)
			util.logt("P" + str(i + 1), cid, subtitle)
			subtitle_list.append(subtitle)

		info["subtitle"] = subtitle_list

	except Exception as e:
		util.handle_exception("failed on fetching subtitle info " + v.get_bvid())

	return info

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
		util.logv("fetch video cover")
		info = info or await fetch_info(v)

		await util.fetch(info.get("pic"), bv_root + "cover.jpg")

	for k, r in stat.items():
		if r:
			continue

		match = part_pattern.fullmatch(k)
		if not match:
			continue
		t = match.group(1)
		p = int(match.group(2))

		part_root = bv_root + 'P' + str(p) + os.path.sep
		util.mkdir(part_root)
		if t == 'P':
			util.logv("fetch video " + k)
			await download_part(v, p, part_root, True)
		elif t == 'D':
			util.logv("fetch danmaku " + k)
			await fetch_danmaku(v, p, part_root, True)
		elif t == 'S':
			util.logv("fetch subtitle " + k)
			info = info or await fetch_info(v)

			await fetch_subtitle(info, p, part_root, True)

	stat = verify_bv(bv, path)
	for k, v in stat.items():
		if not v:
			raise Exception("failed to fix " + bv)

	util.logi("fixed " + bv)


async def do_update(bv, path, credential, force_mode):
	bv_root = path + bv + os.path.sep
	v = video.Video(bvid = bv, credential = credential)

	info = await fetch_info(v)

	util.mkdir(bv_root)
	util.logi("downloading " + bv, info.get("title", ""))

	saved_info = None
	if not force_mode:
		try:
			util.logv("checking saved info")
			with open(bv_root + "info.json", "r") as f:
				saved_info = json.load(f)
				util.logt(saved_info)
		except:
			pass

	util.logv("save video info")
	util.save_json(info, bv_root + "info.json")

	util.logv("fetch video cover")
	await util.fetch(info.get("pic"), bv_root + "cover.jpg")

	total_parts = info.get("videos")
	finished_parts = 0
	for i in range(total_parts):
		part_info = info.get("pages")[i]
		util.logi("downloading P" + str(i + 1), part_info.get("part"))

		part_root = bv_root + 'P' + str(i + 1) + os.path.sep
		util.mkdir(part_root)

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

		result = await download_part(v, i + 1, part_root, force)

		# always update danmaku
		result = await fetch_danmaku(v, i + 1, part_root, True) and result

		result = await fetch_subtitle(info, i + 1, part_root, force) and result

		if result:
			finished_parts = finished_parts + 1

	util.logi("finished " + bv, "part " + str(finished_parts) + '/' + str(total_parts))
	return finished_parts == total_parts


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


