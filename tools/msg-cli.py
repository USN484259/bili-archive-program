#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()

import selectors
from contextlib import suppress
from messaging import MessagingClient


def main(args):
	os.set_blocking(sys.stdin.fileno(), False)
	with MessagingClient(args.msg_addr, allow_dummy = True, reconnect = args.reconnect) as client, selectors.DefaultSelector() as sel:
		sel.register(client, selectors.EVENT_READ)
		sel.register(sys.stdin, selectors.EVENT_READ)
		client.subscribe()
		quit = False
		while not quit:
			results = sel.select()
			if quit:
				break
			for key, ev in results:
				if key.fileobj is client:
					if not client.wait(0):
						continue
					topic, data = client.recv()
					if topic:
						print(topic, data)
				elif key.fileobj is sys.stdin:
					line = None
					with suppress(BlockingIOError):
						line = sys.stdin.readline()
						if not line:
							quit = True

					if not line:
						continue

					split_line = line.split(maxsplit = 1)
					if len(split_line) < 2:
						print("invalid input", file = sys.stderr)
					else:
						client.send(*split_line)

if __name__ == "__main__":
	import argparse
	from utils import logger_init

	parser = argparse.ArgumentParser()
	parser.add_argument("-v", "--verbose", action = "count", default = 0)
	parser.add_argument("-o", "--topic")
	parser.add_argument("-t", "--type")
	parser.add_argument("-l", "--listen", nargs = '*')
	parser.add_argument("--reconnect", type = int)
	parser.add_argument("msg_addr")

	args = parser.parse_args()
	logger_init(args.verbose)

	main(args)
