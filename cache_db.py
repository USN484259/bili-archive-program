#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()

import re
import time
import json
import logging
import sqlite3
import argparse
from stat import S_ISREG
from collections import defaultdict

import constants

# constants

## sql helper functions
def make_sql_column_def(desc, extra = None):
	result = ", ".join(f"{k} {v}" for k, v in desc.items())
	if extra:
		result += ", " + extra
	return result

def make_sql_placeholder(desc):
	return ", ".join(f":{k}" for k in desc.keys())


## table definition
## Dict are ordered on Python 3.6+

meta_table_name = "meta_table"

meta_table_desc = {
	"root":		"TEXT NOT NULL",
	"ctime":	"INTEGER NOT NULL",
}

video_table_name = "video_table"

video_table_desc = {
	"bvid":		"TEXT PRIMARY KEY",
	"mtime":	"INTEGER NOT NULL",
	"title":	"TEXT NOT NULL",
	"tags":		"TEXT NOT NULL",
	"parts":	"INTEGER NOT NULL",
	"cover":	"TEXT",
	"desc":		"TEXT",
	"duration":	"INTEGER",
	"ctime":	"INTEGER",
	"pubtime":	"INTEGER",
	"views":	"INTEGER",
	"likes":	"INTEGER",
	"size":		"INTEGER",
	"flags":	"TEXT",
}

part_table_name = "part_table"

part_table_desc = {
	"cid":		"TEXT PRIMARY KEY",
	"bvid":		"TEXT REFERENCES %s (bvid) NOT NULL" % video_table_name,
	"part":		"INTEGER NOT NULL",
	"title":	"TEXT",
	"duration":	"INTEGER",
	"size":		"INTEGER",
}

user_table_name = "user_table"

user_table_desc = {
	"uid":		"TEXT PRIMARY KEY",
	"mtime":	"INTEGER NOT NULL",
	"uname":	"TEXT NOT NULL",
	"face":		"TEXT",
}

author_table_name = "author_table"

author_table_desc = {
	"uid":		"TEXT REFERENCES %s (uid) NOT NULL" % user_table_name,
	"bvid":		"TEXT REFERENCES %s (bvid) NOT NULL" % video_table_name,
	"role":		"TEXT",
}

view_table_name = "search_view"

view_table_def = """\
SELECT v.bvid, v.mtime, v.title, v.tags, v.parts, v.cover, v.duration, \
v.ctime, v.pubtime, v.views, v.likes, v.size, v.flags, u.uid, u.uname, a.role \
FROM %s v JOIN %s a ON v.bvid == a.bvid JOIN %s u ON u.uid == a.uid \
""" % (video_table_name, author_table_name, user_table_name)


db_tables = {
	video_table_name:	make_sql_column_def(video_table_desc),
	part_table_name:	make_sql_column_def(part_table_desc),
	user_table_name:	make_sql_column_def(user_table_desc),
	author_table_name:	make_sql_column_def(author_table_desc, "PRIMARY KEY (uid, bvid)")
}

query_keys = {
	"bvid": "== ?",
	"title": r"LIKE %?%",
	"tags": r"LIKE %?%",
	"uname": r"LIKE %?%",
}

order_keys = ("mtime", "tags", "parts", "duration", "ctime", "pubtime", "views", "uid")

# static objects

logger = logging.getLogger("cache_db")

order_pattern = re.compile(r"([+-]?)(\w+)")
cid_pattern = re.compile(r"(\d+)")

def empty_func(*args):
	pass

# classes

class VideoDatabase:
	@staticmethod
	def dict_factory(cursor, row):
		# https://docs.python.org/3/library/sqlite3.html#sqlite3-howto-row-factory
		fields = [column[0] for column in cursor.description]
		return {key: value for key, value in zip(fields, row)}

	@staticmethod
	def connect(db_file):
		return sqlite3.connect("file:%s?mode=ro" % db_file, uri = True, check_same_thread = False)

	def __init__(self, db_file):
		assert(sqlite3.threadsafety == 3)
		self.database = self.connect(db_file)
		self.database.row_factory = VideoDatabase.dict_factory

	def close(self):
		if self.database:
			try:
				self.database.close()
			finally:
				self.database = None

	def __del__(self):
		self.close()

	def query(self, rules = {}):
		sql = "SELECT * FROM %s" % view_table_name

		cond_list = []
		arg_list = []
		order_key = "mtime"
		order_dir = "DESC"

		for k, v in rules.items():
			if k == "order":
				order_match = order_pattern.fullmatch(v)
				if order_match:
					dir = order_match.group(1)
					key = order_match.group(2)
				if key in order_keys:
					order_key = key
					if dir == '-':
						order = "DESC"
					else:
						order = "ASC"
					continue

			verb = query_keys.get(k)
			if not verb:
				raise KeyError("invalid key %s" % k)
			cond_list.append("%s %s" % (k, verb))
			arg_list.append(v)

		if cond_list:
			sql += " WHERE " + " AND ".join(cond_list)

		sql += " ORDER BY %s %s" % (order_key, order_dir)

		cursor = self.database.cursor()
		try:
			cursor.arraysize = 0x40
			cursor.execute(sql, *arg_list)
			return cursor.fetchall()
		finally:
			cursor.close()

	def load_info_db(self, cursor, bvid):
		cursor.execute("SELECT * FROM %s WHERE bvid = ?" % video_table_name, (bvid, ))
		bv_info = cursor.fetchone()
		if not bv_info:
			return None

		cursor.execute("SELECT * FROM %s WHERE bvid == ?" % part_table_name, (bvid, ))
		part_list = cursor.fetchall()

		cursor.execute("SELECT a.bvid, a.role, u.* FROM %s u INNER JOIN %s a ON u.uid == a.uid WHERE a.bvid == ?" % (user_table_name, author_table_name), (bvid, ))
		author_list = cursor.fetchall()

		return {
			"bv_info": bv_info,
			"authors": {int(item["uid"]): item for item in author_list},
			"parts": {int(part["cid"]): part for part in part_list},
		}


	def get_video(self, bvid):
		if not constants.bvid_pattern.fullmatch(bvid):
			raise KeyError("invalid bvid %s" % bvid)
		cursor = self.database.cursor()
		try:
			# cursor.arraysize = 0x40
			return self.load_info_db(cursor, bvid)

		finally:
			cursor.close()


class VideoDatabaseManager(VideoDatabase):
	# override
	@staticmethod
	def connect(db_file):
		return sqlite3.connect(db_file, check_same_thread = False)


	def __init__(self, video_root, db_file = None, /, update_path = False):
		super().__init__(db_file)
		self.video_root = os.path.realpath(video_root, strict = True)

		cursor = self.database.cursor()
		try:
			cursor.execute("PRAGMA journal_mode = WAL;")
			cursor.execute("PRAGMA foreign_keys = ON;")
			self.init_tables(cursor, update_path)
		finally:
			cursor.close()


	def init_tables(self, cursor, /, update_path = False):
		cursor.execute("BEGIN DEFERRED")
		try:
			cursor.execute("SELECT COUNT(*) as count FROM sqlite_schema WHERE name == '%s' AND type == 'table' " % meta_table_name)
			if cursor.fetchone()["count"] == 0:
				cursor.execute("CREATE TABLE %s (%s)" % (meta_table_name, make_sql_column_def(meta_table_desc)))
				cursor.execute("INSERT INTO %s (root, ctime) VALUES (?, ?)" % meta_table_name, (self.video_root, int(time.time())))
				cursor.execute("COMMIT")
				cursor.execute("BEGIN IMMEDIATE")
			else:
				cursor.execute("SELECT root FROM %s" % meta_table_name)
				recorded_root = cursor.fetchone()["root"]
				if recorded_root != self.video_root:
					err_msg = "video root mismatch: %s\t%s", recorded_root, self.video_root
					logger.warning(err_msg)
					if update_path:
						logger.warning("update video root in database")
						cursor.execute("UPDATE TABLE %s SET root = ?" % meta_table_name, (self.video_root, ))
					else:
						raise Exception(err_msg)

			for table, desc in reversed(db_tables.items()):
				cursor.execute("CREATE TABLE IF NOT EXISTS %s (%s) WITHOUT ROWID" % (table, desc))
			cursor.execute("CREATE VIEW IF NOT EXISTS %s AS %s" % (view_table_name, view_table_def))
			cursor.execute("COMMIT")
		except Exception:
			logger.error("exception in creating tables")
			cursor.execute("ROLLBACK")
			raise


	def load_info_json(self, bvid):
		mtime = None
		bv_info = {}
		authors = {}
		parts = {}

		bv_root = os.path.join(self.video_root, bvid)
		with open(os.path.join(bv_root, "info.json"), 'r') as f:
			raw_info = json.load(f)
			stat = os.fstat(f.fileno())
			mtime = int(stat.st_mtime)

		for k in ("bvid", "title", "desc", "duration", "ctime"):
			bv_info[k] = raw_info[k]

		bv_info["mtime"] = mtime
		bv_info["pubtime"] = raw_info["pubdate"]
		bv_info["parts"] = raw_info["videos"]
		bv_info["tags"] = raw_info["tname"]
		bv_info["cover"] = os.path.split(raw_info["pic"])[1]
		bv_info["views"] = raw_info["stat"]["view"]
		bv_info["likes"] = raw_info["stat"]["like"]
		bv_info["size"] = None
		bv_info["flags"] = None

		raw_authors = raw_info.get("staff")
		if raw_authors:
			for usr in raw_authors:
				uid_num = int(usr["mid"])
				authors[uid_num] = {
					"uid": usr["mid"],
					"mtime": mtime,
					"bvid": raw_info["bvid"],
					"uname": usr["name"],
					"role": usr["title"],
					"face": os.path.split(usr["face"])[1],
				}
		else:
			owner = raw_info["owner"]
			uid_num = int(owner["mid"])
			authors[uid_num] = {
				"uid": owner["mid"],
				"mtime": mtime,
				"bvid": raw_info["bvid"],
				"uname": owner["name"],
				"role": None,
				"face": os.path.split(owner["face"])[1],
			 }

		for part in raw_info["pages"]:
			cid_num = int(part["cid"])
			parts[cid_num] = {
				"cid": part["cid"],
				"bvid": raw_info["bvid"],
				"part": part["page"],
				"title": part["part"],
				"duration": part["duration"],
				"size": None
			}

		return {
			"bv_info": bv_info,
			"authors": authors,
			"parts": parts,
		}

	def store_bv_info(self, cursor, bv_info):
		cursor.execute("INSERT OR REPLACE INTO %s VALUES (%s)" % (video_table_name, make_sql_placeholder(video_table_desc)), bv_info)

	def store_parts(self, cursor, part_list):
		cursor.executemany("INSERT OR REPLACE INTO %s VALUES (%s)" % (part_table_name, make_sql_placeholder(part_table_desc)), part_list)

	def store_authors(self, cursor, author_list):
		for author in author_list:
			cursor.execute("SELECT COUNT(*) as count FROM %s WHERE uid == :uid AND mtime >= :mtime" % user_table_name, author)
			if cursor.fetchone()["count"] == 0:
				author = defaultdict(empty_func, author)
				cursor.execute("INSERT OR REPLACE INTO %s VALUES (%s)" % (user_table_name, make_sql_placeholder(user_table_desc)), author)

		cursor.executemany("INSERT INTO %s AS a VALUES (%s) ON CONFLICT DO UPDATE SET role = excluded.role WHERE excluded.role != a.role" % (author_table_name, make_sql_placeholder(author_table_desc)), author_list)

	def update_video(self, bvid):
		if not constants.bvid_pattern.fullmatch(bvid):
			raise ValueError("invalid video %s", bvid)

		logger.info("update video %s", bvid)
		json_info = self.load_info_json(bvid)

		cursor = self.database.cursor()
		try:
			cursor.execute("BEGIN DEFERRED")
			db_info = self.load_info_db(cursor, bvid)

			if not db_info:
				self.store_bv_info(cursor, json_info["bv_info"])
				self.store_parts(cursor, json_info["parts"].values())
				self.store_authors(cursor, json_info["authors"].values())

				cursor.execute("COMMIT")
				return True

			db_bv_info = db_info["bv_info"]
			json_bv_info = json_info["bv_info"]
			if json_bv_info["mtime"] <= db_bv_info["mtime"]:
				logger.info("skip video %s", bvid)
				# close the transaction
				cursor.execute("ROLLBACK")
				return False

			db_bv_info["mtime"] = json_bv_info["mtime"]
			json_bv_info["size"] = db_bv_info["size"]
			json_bv_info["flags"] = db_bv_info["flags"]
			if db_bv_info != json_bv_info:
				self.store_bv_info(cursor, json_bv_info)

			db_parts = db_info["parts"]
			json_parts = json_info["parts"]
			db_keys = set(db_parts.keys())
			json_keys = set(json_parts.keys())
			common_keys = json_keys & db_keys
			differ_keys = json_keys - db_keys
			part_list = [ json_parts[k] for k in differ_keys ]
			for k in common_keys:
				part = json_parts[k]
				part["size"] = db_parts[k].get("size")
				if part != db_parts[k]:
					part_list.append(part)

			if part_list:
				self.store_parts(cursor, part_list)

			self.store_authors(cursor, json_info["authors"].values())
			cursor.execute("COMMIT")
			return True
		except Exception:
			logger.error("exception in updating info %s", bvid)
			cursor.execute("ROLLBACK")
			raise
		finally:
			cursor.close()

	def store_sizes(self, cursor, bvid, size_list):
		cursor.executemany("UPDATE OR IGNORE %s SET size = :size WHERE cid == :cid AND (size IS NULL OR size != :size)" % part_table_name, size_list)
		if cursor.rowcount > 0:
			cursor.execute("UPDATE OR IGNORE %s SET size = ( SELECT SUM(size) FROM %s where bvid == ? ) WHERE bvid == ?" % (video_table_name, part_table_name), (bvid, bvid))
			return True
		else:
			return False

	def update_video_size(self, bvid):
		if not constants.bvid_pattern.fullmatch(bvid):
			raise ValueError("invalid video %s", bvid)

		cursor = self.database.cursor()
		try:
			bv_root = os.path.join(self.video_root, bvid)
			sizes = []

			logger.debug("checking video size %s", bvid)
			cursor.execute("BEGIN DEFERRED")
			with os.scandir(bv_root) as it:
				for bv_entry in it:
					if bv_entry.is_dir() and cid_pattern.fullmatch(bv_entry.name):
						cid = bv_entry.name
						logger.debug("walking cid %s", cid)

						part_path = os.path.join(bv_root, cid)
						part_size = 0
						with os.scandir(part_path) as part_it:
							for part_entry in part_it:
								if part_entry.is_file(follow_symlinks = False):
									stat = part_entry.stat(follow_symlinks = False)
									logger.debug("file %s: %d", part_entry.name, stat.st_size)
									part_size += stat.st_size
								else:
									logger.debug("skip %s", part_entry.name)
						sizes.append({ "cid": cid, "size": part_size })

			result = self.store_sizes(cursor, bvid, sizes)
			cursor.execute("COMMIT")
			return result
		except Exception:
			logger.error("exception in updating size %s", bvid)
			cursor.execute("ROLLBACK")
			raise
		finally:
			cursor.close()


	def remove_video(self, bvid, /, force = False, check_file = False):
		if not constants.bvid_pattern.fullmatch(bvid):
			raise ValueError("invalid video %s", bvid)

		if not force:
			try:
				stat = os.stat(os.path.join(self.video_root, bvid, "info.json"))
				if S_ISREG(stat.st_mode):
					if not check_file or self.load_info_json(bvid):
						logger.debug("not removing video %s", bvid)
						return False
			except OSError:
				pass

		logger.info("removing video %s", bvid)
		cursor = self.database.cursor()
		try:
			cursor.execute("BEGIN IMMEDIATE")
			for table_name in (author_table_name, part_table_name, video_table_name):
				cursor.execute("DELETE FROM %s WHERE bvid == ?" % table_name, (bvid, ))
			cursor.execute("COMMIT")
			return True
		except Exception:
			logger.error("exception in removing video %s", bvid)
			cursor.execute("ROLLBACK")
			raise
		finally:
			cursor.close()


	def add_user(self, user_info):
		raise NotImplementedError()


	def walk(self, *, callback = None):
		start_time = int(time.time())
		logger.debug("start walking %s %d", self.video_root, start_time)
		with os.scandir(self.video_root) as it:
			for entry in it:
				updated = False
				try:
					if entry.is_dir() and constants.bvid_pattern.fullmatch(entry.name):
						bvid = entry.name
						logger.debug("loading %s", bvid)
						if self.update_video(bvid):
							self.update_video_size(bvid)
							updated = True

				except Exception:
					logger.exception("failed to walk %s", entry.name)

				if updated and callable(callback):
					callback(entry.name)

		logger.debug("walking %s took %d seconds", self.video_root, int(time.time()) - start_time)


	def autoremove(self):
		cursor = self.database.cursor()
		try:
			cursor.arraysize = 0x40
			cursor.execute("BEGIN DEFERRED")
			cursor.execute("SELECT bvid FROM video_table")
			count = 0
			for row in cursor:
				bvid = row[0]
				try:
					if self.remove_video(bvid):
						count += 1
				except Exception:
					logger.exception("failed to remove %s", bvid)
			logger.info("removed %d videos", count)
		finally:
			cursor.close()


def main(args):
	database = VideoDatabaseManager(args.dir, args.database)
	database.walk()


if __name__ == "__main__":
	logging.basicConfig(level = logging.DEBUG, format = constants.LOG_FORMAT, stream = sys.stderr)

	parser = argparse.ArgumentParser()
	parser.add_argument("-d", "--dir", required = True)
	parser.add_argument("database")

	args = parser.parse_args()
	main(args)