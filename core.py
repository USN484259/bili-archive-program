#!/usr/bin/env python3

import os
import sys
import re
import time
import shutil
import logging
import zipfile
import collections

if sys.platform != "win32":
	import fcntl
else:
	fcntl = None

# constants

from constants import *


# static objects

logger = logging.getLogger("bili_arch.core")


## file management

def touch(path):
	logger.debug("touch %s", path)
	open(path, "ab").close()


def mkdir(path):
	logger.debug("mkdir %s", path)
	os.makedirs(path, exist_ok = True)


def locked_file(filename, mode, **kwargs):
	f = open(filename, mode = mode, **kwargs)
	if not fcntl:
		return f
	if 'r' in mode and '+' not in mode:
		lock_mode = fcntl.LOCK_SH
	else:
		lock_mode = fcntl.LOCK_EX

	try:
		fcntl.flock(f, lock_mode | fcntl.LOCK_NB)
		return f
	except:
		f.close()
		raise


class staged_file:
	def __init__(self, filename, /, mode = 'r', rotate = None, **kwargs):
		self.filename = filename
		self.tmp_name = None
		self.rot_mode = bool(rotate)
		open_mode = mode
		if 'w' in mode:
			self.tmp_name = filename + default_names.tmp_ext
			logger.debug("using tmp file %s", self.tmp_name)
			touch(self.tmp_name)
			open_mode = "r+" + (('b' in mode) and 'b' or '')

		self.f = locked_file(self.tmp_name or self.filename, mode = open_mode, **kwargs)
		self.closed = False
		if 'w' in mode:
			try:
				self.f.truncate(0)
				self.f.seek(0)
			except:
				self.f.close()
				raise

	def __enter__(self):
		return self.f

	def __exit__(self, exc_type, exc_value, traceback):
		self.close(exc_type is None and exc_value is None)

	def close(self, replace = True):
		if self.closed:
			return
		try:
			if replace and self.tmp_name:
				if self.rot_mode:
					self.rotate()
				logger.debug("move %s to %s", self.tmp_name, self.filename)
				os.replace(self.tmp_name, self.filename)
		finally:
			self.f.close()
			self.closed = True

	def rotate(self):
		try:
			src_fd = locked_file(self.filename, "rb")
		except FileNotFoundError:
			return

		try:
			logger.debug("rotating %s", self.filename)
			rotate_name = self.filename + default_names.rotate_postfix
			touch(rotate_name)
			with locked_file(rotate_name, "r+b") as arch_fd:
				with zipfile.ZipFile(arch_fd, mode = "a") as archive:
					stat = os.stat(src_fd.fileno())
					file_name = os.path.split(self.filename)[1]
					file_time = time.gmtime(stat.st_mtime)
					file_info = zipfile.ZipInfo("%s-%d" % (file_name, int(stat.st_mtime)), file_time)
					with archive.open(file_info, "w") as sink:
						shutil.copyfileobj(src_fd, sink)

		except Exception as e:
			logger.error("failed to rotate file %s: %s", self.filename, str(e))
			backup_name = self.filename + default_names.backup_postfix
			logger.info("using backup file %s", backup_name)
			if os.path.isfile(backup_name):
				raise FileExistsError(backup_name + " already exists")
			logger.debug("move %s to %s",self.filename, backup_name)
			os.replace(self.filename, backup_name)
		finally:
			src_fd.close()


class locked_path(os.PathLike):
	def __init__(self, *path_list, shared = False):
		self.path = os.path.join(*path_list)
		mkdir(self.path)
		if not fcntl:
			return
		self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
		lock_mode = (shared and fcntl.LOCK_SH or fcntl.LOCK_EX)
		try:
			fcntl.flock(self.fd, lock_mode | fcntl.LOCK_NB)
		except:
			os.close(self.fd)
			raise

	def __fspath__(self):
		return self.path

	def __str__(self):
		return self.path

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_value, traceback):
		self.close()

	def close(self):
		if not fcntl:
			return
		if self.fd is not None:
			os.close(self.fd)
			self.fd = None
