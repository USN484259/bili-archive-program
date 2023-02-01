#!/usr/bin/env python3

import os
import re
import util
import json
import shutil
import subprocess

bv_pattern = re.compile(r"(.*)\.m4(.?)")

ffprobe_path = shutil.which("ffprobe")
ffprobe_options = [
	"-hide_banner",
	"-show_format",
	"-show_streams",
	"-print_format", "json=compact=1"
]


def ffprobe(path):
	try:
		cmdline = [ffprobe_path]
		if util.log_level <= 3:
			cmdline = cmdline + ["-v", "8"]

		cmdline = cmdline + ffprobe_options + [path]
		util.logv("running ffprobe on " + path)
		util.logt(cmdline)
		with subprocess.Popen(cmdline, stdout = subprocess.PIPE) as proc:
			result = json.load(proc.stdout)
			util.logt(result)
			return result

	except Exception as e:
		util.handle_exception(e, "exception on ffprobe for " + path)
		return {}


def verify_bv(bv, path = None, check_media = False):
	result = {
		"info" : False,
		"cover" : False
	}
	try:
		bv_root = util.opt_path(path) + bv + os.path.sep
		util.logv("verify video " + bv, "path " + bv_root, "check_media " + str(check_media))

		with open(bv_root + "info.json") as f:
			bv_info = json.load(f)
			util.logt(bv_info)

		util.logi(bv, bv_info.get("title"))

		if check_media:
			util.logv("checking cover")
			media_info = ffprobe(bv_root + "cover.jpg")
			try:
				if media_info.get("streams")[0].get("codec_name") == "mjpeg":
					result["cover"] = True
			except:
				pass

		elif os.path.isfile(bv_root + "cover.jpg"):
			util.logv("cover exists")
			result["cover"] = True

		for i in range(bv_info.get("videos")):
			part_info = bv_info.get("pages")[i]
			part_name = 'P' + str(i + 1)
			part_duration = part_info.get("duration")
			result[part_name] = False
			util.logi(part_name, part_info.get("part"), str(part_duration) + " sec")

			part_root = bv_root + part_name + os.path.sep
			video_count = 0
			audio_count = 0

			util.logv("checking " + part_name + " in " + part_root)
			for f in os.listdir(part_root):
				util.logv("file " + f)
				if check_media:
					media_info = ffprobe(part_root + f)
					try:
						media_vc = 0
						media_ac = 0
						for i, media in enumerate(media_info.get("streams")):
							media_type = media.get("codec_type")
							media_duration = float(media.get("duration"))
							util.logv("stream " + str(media.get("index")), "type " + media_type, "duration " + str(media_duration))

							if abs(part_duration - media_duration) >= 1:
								util.logw("media duration mismatch, skipping", f)
								util.logv("duration",  '(' + str(part_duration) + '/' + str(media_duration) + ')')
								break

							if media_type == "video":
								media_vc = media_vc + 1
							elif media_type == "audio":
								media_ac = media_ac + 1
						else:
							video_count = video_count + media_vc
							audio_count = audio_count + media_ac
					except:
						pass
				else:
					match = bv_pattern.fullmatch(f)
					if not match:
						util.logv("name not match")
						continue

					file_type = match.group(2).lower()
					if file_type == 'v':
						util.logv("type: video")
						video_count = video_count + 1
					elif file_type == 'a':
						util.logv("type: audio")
						audio_count = audio_count + 1
					else:
						util.logv("unknown type " + file_type)

			util.logv("video count " + str(video_count), "audio_count " + str(audio_count))
			if video_count > 0 and audio_count > 0:
				result[part_name] = True

		util.logv("verify done " + bv)
		result["info"] = True
	except Exception as e:
		util.handle_exception(e, "exception in verifing " + bv)

	util.logv(result)
	return result

def main(args):
	if len(args.inputs) > 0:
		util.logv(str(len(args.inputs)) + " BV on cmdline")
		bv_list = args.inputs
	else:
		util.logv("scan BV in " + (args.dest or "(cwd)"))
		bv_list = []
		bv_pattern = re.compile(r"BV\w+")
		for f in os.listdir(args.dest):
			util.logt(f)
			if bv_pattern.fullmatch(f):
				bv_list.append(f)

	util.logi("BV count " + str(len(bv_list)), "mode " + str(args.mode))
	util.logt(bv_list)

	for i, bv in enumerate(bv_list):
		result = verify_bv(bv, path = args.dest, check_media = (args.mode == "ffprobe"))
		res = True
		for k, v in result.items():
			if not v:
				res = False
				break

		print(bv, res)

if __name__ == "__main__":
	args = util.parse_args()
	main(args)
