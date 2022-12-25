#!/usr/bin/env python3

import os
import aiohttp
from bilibili_api import video
import util

# mode		operation
# ---------------------------------------------------
# check		download only if not complete
# update	download info, cover, better quality
# force		re-download everything

def find_best(res_list):
	result = dict(id = 0)
	for k, v in enumerate(res_list):
		util.logt("quality " + str(v.get("id")))
		if v.get("id") > result.get("id"):
			result = v
	return result

async def download(bv, path = None, credential = None, mode = None):
	if not mode:
		mode = "update"

	path = util.opt_path(path)

	util.logv("downloading " + bv + " into " + path + " mode " + mode + " auth " + ("yes" if credential else "no"))

	if mode == "check" and os.path.exists(path + bv + os.path.sep + "info.json"):
		util.logi("skip existing " + bv)
		return

	await util.stall()
	v = video.Video(bvid = bv, credential = credential)
	info = await v.get_info()
	util.logt(info)

	util.mkdir(path + bv)
	util.logi("downloading " + bv + '\t' + info.get("title", ""))

	async with aiohttp.ClientSession() as sess:
		util.logv("fetch video cover")
		await util.fetch(sess, info.get("pic"), path + bv + os.path.sep + "cover.jpg")

		for i in range(info.get("videos")):
			url = await v.get_download_url(i)
			part_path = path + bv + os.path.sep + 'P' + str(i + 1)
			util.mkdir(part_path)
			util.logi("downloading P" + str(i + 1) + '\t' + info.get("pages")[i].get("part"))
			await download_part(sess, url, part_path, mode)

	util.logv("save video info")
	await util.save_json(info, path + bv + os.path.sep + "info.json")

	util.logv("finished " + bv)


async def download_part(sess, url, path, mode):
	path = util.opt_path(path)
	util.logt(url)
	# dump_table(url)
	# dump_table(url.get("accept_description"))
	# dump_table(url.get("accept_quality"))
	# dump_table(url.get("support_formats")[0])
	if "dash" in url:
		dash = url.get("dash")
		util.logv("find best audio")
		best_audio = find_best(dash.get("audio"))
		util.logv("audio quality " + str(best_audio.get("id")))

		util.logv("find best video")
		best_video = find_best(dash.get("video"))
		util.logv("video quality " + str(best_video.get("id")))

		audio_file = path + str(best_audio.get("id")) + ".m4a"
		video_file = path + str(best_video.get("id")) + ".m4v"

		if mode == "force" or not os.path.exists(audio_file):
			await util.fetch(sess, best_audio.get("base_url"), audio_file)

		if mode == "force" or not os.path.exists(video_file):
			await util.fetch(sess, best_video.get("base_url"), video_file)

	elif "durl" in url:
		util.logw("unusual video without 'dash' instance, try 'durl'")
		durl = url.get("durl")[0].get("url")
		video_file = path + "video.flv"
		if mode == "force" or not os.path.exists(video_file):
			await util.fetch(sess, durl, video_file)

	else:
		raise Exception("unknown URL format")


async def main(args):
	credential = None
	if args.auth:
		credential = util.credential(args.auth)

	util.logv(args.inputs)
	for i, bv in enumerate(args.inputs):
		try:
			await download(bv, path = args.dest, mode = args.mode, credential = credential)
		except Exception as e:
			util.handle_exception(e, "failed to download " + bv)


if __name__ == "__main__":
	args = util.parse_args()
	util.run(main(args))


