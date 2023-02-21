#!/usr/bin/env python3

import os
import sys
import asyncio


# https://stackoverflow.com/questions/64303607/python-asyncio-how-to-read-stdin-and-write-to-stdout
async def connect_stdin_stdout():
	loop = asyncio.get_event_loop()
	reader = asyncio.StreamReader()
	protocol = asyncio.StreamReaderProtocol(reader)
	await loop.connect_read_pipe(lambda: protocol, sys.stdin)
	w_transport, w_protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout)
	writer = asyncio.StreamWriter(w_transport, w_protocol, reader, loop)
	return reader, writer


async def worker_print(socket_in, stdout):
	while not socket_in.at_eof():
		line = await socket_in.readline()
		stdout.write(line)
		await stdout.drain()


async def main(socket_path):
	socket_in, socket_out = await asyncio.open_unix_connection(path = socket_path)
	stdin, stdout = await connect_stdin_stdout()
	task = asyncio.create_task(worker_print(socket_in, stdout))
	task.add_done_callback(asyncio.Task.result)
	try:
		async for line in stdin:
			socket_out.write(line)
			await socket_out.drain()

		socket_out.write_eof()
		await socket_out.drain()
	finally:
		# task.cancel()
		await task
		socket_out.close()
		await socket_out.wait_closed()
		stdout.close()
		# await stdout.wait_closed()

if __name__ == "__main__":
	if len(sys.argv) != 2:
		print(sys.argv[0] + " socket-file", file = sys.stderr, flush = True)
	else:
		asyncio.run(main(sys.argv[1]))
