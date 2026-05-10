#!/usr/bin/env python3

import sys
import json
import time


def main(danmaku_file):
	with open(danmaku_file, mode = "r") as f:
		lines = f.readlines()

	record = None
	for line in lines:
		try:
			info = json.loads(line)
			if info.get("cmd", "") != "SEND_GIFT":
				continue
			data = info["data"]
			if data["action"] != "投喂":
				continue
			gift_name = data["giftName"]
			if gift_name not in [ "做我的小猫", "情书" ]:
				continue
			uname = data["uname"]
			count = data["num"]
			timestamp = data["timestamp"]

			if record is not None:
				if record[2] == gift_name and record[1] == uname:
					record[3] += count
					continue
				else:
					print(record)
					record = None

			struct_time = time.localtime(timestamp)
			time_str = "%02d:%02d:%02d" % (struct_time[3], struct_time[4], struct_time[5])
			record = [time_str, uname, gift_name, count]

		except Exception as e:
			print(e, file = sys.stderr)

	if record is not None:
		print(record)


if __name__ == "__main__":
	args = sys.argv
	if len(args) > 1:
		main(args[1])

