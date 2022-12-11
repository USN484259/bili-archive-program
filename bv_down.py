#!/usr/bin/env python3

import os
import json
from bilibili_api import video
import util
import aiohttp

# mode		operation
# ---------------------------------------------------
# check		download only if not complete
# update	download info, cover, better quality
# force		re-download everything

def find_best(res_list):
	result = dict(id = 0)
	for k, v in enumerate(res_list):
		if v.get("id") > result.get("id"):
			result = v
	return result

async def download(bv, path = None, credential = None, mode = None):
	if not mode:
		mode = "update"
	
	path = util.opt_path(path)

	v = video.Video(bvid = bv, credential = credential)
	info = await v.get_info()
	# print(info)
	if mode == "check" and os.path.exists(path + bv + os.path.sep + "info.json"):
		print("skipping existing " + bv, flush = True)
		return

	util.mkdir(path + bv)
	print("downloading " + bv, info.get("title", ""), flush = True)

	sess = aiohttp.ClientSession()
	try:
		await util.fetch(sess, info.get("pic"), path + bv + os.path.sep + "cover.jpg")

		for i in range(info.get("videos")):
			url = await v.get_download_url(i)
			part_path = path + bv + os.path.sep + 'P' + str(i + 1)
			util.mkdir(part_path)
			print("downloading P" + str(i + 1))
			await download_part(sess, url, part_path, mode)
	except:
		util.exception("error in downloading " + bv)
		raise
	finally:
		await sess.close()

	with open(path + bv + os.path.sep + "info.json", "w") as f:
		json.dump(info, f, indent='\t', ensure_ascii=False)


async def download_part(sess, url, path, mode):
	path = util.opt_path(path)
	# dump_table(url)
	# dump_table(url.get("accept_description"))
	# dump_table(url.get("accept_quality"))
	# dump_table(url.get("support_formats")[0])
	# print(url.get("dash"))
	best_audio = find_best(url.get("dash").get("audio"))
	best_video = find_best(url.get("dash").get("video"))
	# print(best_audio, best_video)

	audio_file = path + str(best_audio.get("id")) + ".m4a"
	video_file = path + str(best_video.get("id")) + ".m4v"

	if mode == "force" or not os.path.exists(audio_file):
		await util.fetch(sess, best_audio.get("base_url"), audio_file)
	
	if mode == "force" or not os.path.exists(video_file):
		await util.fetch(sess, best_video.get("base_url"), video_file)


async def main(args):
	credential = None
	if args.auth:
		credential = util.credential(args.auth)

	for i, bv in enumerate(args.inputs):
		try:
			await download(bv, path = args.dest, mode = args.mode, credential = credential)
		except:
			util.exception("failed to download " + bv)


if __name__ == "__main__":
	args = util.parse_args()
	print(args)
	util.sync(main(args))


