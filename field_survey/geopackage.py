"""GeoPackage 来源的只读一致快照接入。

调查员编辑期间，采集端以 SQLite 只读模式打开同一个 .gpkg 文件：不复制
正在写入的主文件来冒充完整数据库，WAL 中已提交的内容同样是来源事实。
一次接收先在同一个读取事务内识别 gpkg_contents、gpkg_geometry_columns
与 gpkg_spatial_ref_sys，再按该快照领取选定图层，元数据与记录不会跨快
照拼接。采集端只读，不写回、不修改源库。
"""

import hashlib
import json
import math
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

SUPPORTED_GEOMETRY_TYPES = (
    "POINT",
    "LINESTRING",
    "POLYGON",
    "MULTIPOINT",
    "MULTILINESTRING",
    "MULTIPOLYGON",
    "GEOMETRYCOLLECTION",
    "GEOMETRY",
)

WKB_TYPE_NAMES = {
    1: "POINT",
    2: "LINESTRING",
    3: "POLYGON",
    4: "MULTIPOINT",
    5: "MULTILINESTRING",
    6: "MULTIPOLYGON",
    7: "GEOMETRYCOLLECTION",
}

ENVELOPE_LENGTHS = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}

_WAL_MAGICS = (b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83")


class UnsupportedGeometry(Exception):
    """几何使用了本程序不支持的扩展；保留原文，不猜测位置。"""


class MalformedGeometry(Exception):
    """几何字节序列无法按 GeoPackageBinary/WKB 解析。"""


def source_facts(path):
    """读取标识源状态的文件事实：主库变更计数与 WAL 盐值、帧数。"""
    path = Path(path)
    stat = path.stat()
    facts = {"db_size": stat.st_size, "db_mtime_ns": stat.st_mtime_ns}
    with open(path, "rb") as handle:
        header = handle.read(100)
    if len(header) == 100 and header[:16] == b"SQLite format 3\x00":
        facts["db_change_counter"] = int.from_bytes(header[24:28], "big")
        facts["db_version_valid_for"] = int.from_bytes(header[92:96], "big")
    wal = path.with_name(path.name + "-wal")
    try:
        wal_stat = wal.stat()
    except FileNotFoundError:
        return facts
    facts["wal_size"] = wal_stat.st_size
    facts["wal_mtime_ns"] = wal_stat.st_mtime_ns
    with open(wal, "rb") as handle:
        wal_header = handle.read(32)
    if len(wal_header) == 32 and wal_header[:4] in _WAL_MAGICS:
        page_size = int.from_bytes(wal_header[8:12], "big")
        if page_size == 1:
            page_size = 65536
        facts["wal_checkpoint_seq"] = int.from_bytes(wal_header[12:16], "big")
        facts["wal_salt1"] = int.from_bytes(wal_header[16:20], "big")
        facts["wal_salt2"] = int.from_bytes(wal_header[20:24], "big")
        frame_size = 24 + page_size
        facts["wal_frames"] = max(0, wal_stat.st_size - 32) // frame_size
    return facts


def snapshot_token(facts):
    """由文件事实导出快照身份；任何提交、检查点都会改变它。"""
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()


def quote_ident(name):
    return '"' + str(name).replace('"', '""') + '"'


@dataclass
class LayerPlan:
    table: str
    geom_column: str
    declared_type: str
    srs_id: int
    z: int
    m: int
    reasons: list
    srs_registry: dict | None

    @property
    def ok(self):
        return not self.reasons


@dataclass
class PackageMetadata:
    application_id: int
    user_version: int
    layers: list
    srs_registry: dict


class SnapshotReader:
    """对同一 GeoPackage 的一次只读一致快照读取。

    读取事务内的所有语句看到同一份快照。快照身份由 WAL 盐值、帧数与主库
    变更计数标定；事务首尾各取一次文件事实，不一致则回滚重试，因此记录
    下来的身份就是实际读取的那份，而不是读取前或读取后的邻接状态。
    """

    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"来源包不存在: {self.path}")
        uri = "file:" + quote(str(self.path.resolve())) + "?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        self.connection.execute("PRAGMA query_only=ON")
        self.facts = None
        self.token = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def begin(self, attempts=6):
        for _ in range(attempts):
            before = source_facts(self.path)
            self.connection.execute("BEGIN")
            try:
                self.connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
            except Exception:
                self.connection.execute("ROLLBACK")
                raise
            if before == source_facts(self.path):
                self.facts = before
                self.token = snapshot_token(before)
                return self
            self.connection.execute("ROLLBACK")
        raise RuntimeError("来源写入过于频繁，无法固定读取快照")

    def close(self):
        try:
            self.connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        self.connection.close()

    def identify(self):
        """在同一快照内识别内容、几何列与空间参考，并评估图层自洽性。"""
        connection = self.connection
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for required in ("gpkg_contents", "gpkg_geometry_columns", "gpkg_spatial_ref_sys"):
            if required not in tables:
                raise ValueError("不是有效的 GeoPackage：缺少 " + required)
        srs_registry = {
            row[0]: {"organization": row[1], "organization_coordsys_id": row[2]}
            for row in connection.execute("SELECT srs_id, organization, organization_coordsys_id FROM gpkg_spatial_ref_sys")
        }
        contents = {
            row[0]: row[1]
            for row in connection.execute("SELECT table_name, srs_id FROM gpkg_contents WHERE data_type='features'")
        }
        extensions = {}
        global_extensions = []
        if "gpkg_extensions" in tables:
            for table_name, extension_name in connection.execute("SELECT table_name, extension_name FROM gpkg_extensions"):
                if table_name is None:
                    global_extensions.append(extension_name)
                else:
                    extensions.setdefault(table_name, []).append(extension_name)
        plans = []
        rows = connection.execute(
            "SELECT table_name, column_name, geometry_type_name, srs_id, z, m FROM gpkg_geometry_columns ORDER BY table_name, column_name"
        ).fetchall()
        for table_name, column_name, geometry_type, srs_id, z, m in rows:
            reasons = []
            declared_type = str(geometry_type).upper()
            if table_name not in contents:
                reasons.append("contents_missing")
                contents_srs = None
            else:
                contents_srs = contents[table_name]
            if contents_srs is not None and contents_srs != srs_id:
                reasons.append("srs_mismatch")
            if srs_id not in srs_registry:
                reasons.append("srs_unregistered")
            if declared_type not in SUPPORTED_GEOMETRY_TYPES:
                reasons.append("unsupported_geometry_type:" + declared_type)
            for extension in global_extensions + extensions.get(table_name, []):
                reasons.append("unsupported_extension:" + extension)
            plans.append(LayerPlan(
                table=table_name,
                geom_column=column_name,
                declared_type=declared_type,
                srs_id=srs_id,
                z=z,
                m=m,
                reasons=reasons,
                srs_registry=srs_registry.get(srs_id),
            ))
        return PackageMetadata(application_id, user_version, plans, srs_registry)

    def primary_key(self, table):
        for row in self.connection.execute("PRAGMA table_info(%s)" % quote_ident(table)):
            if row[5] == 1 and str(row[2]).upper() == "INTEGER":
                return row[1]
        return None

    def fetch_batch(self, table, pk_column, after_pk, limit):
        sql = "SELECT * FROM " + quote_ident(table)
        params = []
        if after_pk is not None:
            sql += " WHERE %s > ?" % quote_ident(pk_column)
            params.append(after_pk)
        sql += " ORDER BY %s LIMIT ?" % quote_ident(pk_column)
        params.append(limit)
        cursor = self.connection.execute(sql, params)
        columns = [description[0] for description in cursor.description]
        return columns, cursor.fetchall()


class _Walker:
    """按 WKB 结构走查字节序列，统计坐标并验证完整，不从包络推断位置。"""

    def __init__(self, buf, end):
        self.buf = buf
        self.end = end
        self.pos = 0
        self.coordinate_count = 0
        self.first_coordinate = None
        self.point_coordinates = None

    def need(self, count):
        if self.pos + count > self.end:
            raise MalformedGeometry("wkb_truncated")

    def uint(self, endian):
        self.need(4)
        value = struct.unpack_from(endian + "I", self.buf, self.pos)[0]
        self.pos += 4
        return value

    def point(self, endian, dims):
        self.need(8 * dims)
        values = list(struct.unpack_from(endian + "d" * dims, self.buf, self.pos))
        self.pos += 8 * dims
        self.coordinate_count += 1
        if self.first_coordinate is None:
            self.first_coordinate = values
        return values

    def geometry(self, depth=0):
        if depth > 16:
            raise MalformedGeometry("wkb_nesting_too_deep")
        self.need(5)
        order = self.buf[self.pos]
        if order == 0:
            endian = ">"
        elif order == 1:
            endian = "<"
        else:
            raise MalformedGeometry("wkb_byte_order_invalid")
        raw_type = struct.unpack_from(endian + "I", self.buf, self.pos + 1)[0]
        if raw_type & 0x20000000:
            raise UnsupportedGeometry("ewkb_srid_flag")
        if raw_type & 0xC0000000:
            raise UnsupportedGeometry("ewkb_zm_flags")
        zm, base = divmod(raw_type, 1000)
        if zm > 3 or not 1 <= base <= 7:
            raise UnsupportedGeometry("wkb_type_%d" % raw_type)
        dims = 2 + (1 if zm in (1, 3) else 0) + (1 if zm in (2, 3) else 0)
        self.pos += 5
        empty = False
        if base == 1:
            coords = self.point(endian, dims)
            empty = math.isnan(coords[0]) and math.isnan(coords[1])
            if not empty:
                self.point_coordinates = coords
        elif base == 2:
            count = self.uint(endian)
            for _ in range(count):
                self.point(endian, dims)
            empty = count == 0
        elif base == 3:
            rings = self.uint(endian)
            for _ in range(rings):
                count = self.uint(endian)
                for _ in range(count):
                    self.point(endian, dims)
            empty = rings == 0
        elif base in (4, 5, 6):
            expected = base - 3
            count = self.uint(endian)
            for _ in range(count):
                element_base, _, _ = self.geometry(depth + 1)
                if element_base != expected:
                    raise MalformedGeometry("wkb_element_type_mismatch")
            empty = count == 0
        else:
            count = self.uint(endian)
            for _ in range(count):
                self.geometry(depth + 1)
            empty = count == 0
        return base, zm, empty


def parse_wkb(buf, offset, end):
    walker = _Walker(buf, end)
    walker.pos = offset
    base, zm, empty = walker.geometry()
    summary = {
        "type": WKB_TYPE_NAMES[base],
        "dimensions": "XY" + ("Z" if zm in (1, 3) else "") + ("M" if zm in (2, 3) else ""),
        "empty": empty,
        "coordinate_count": walker.coordinate_count,
    }
    if walker.point_coordinates is not None:
        summary["coordinates"] = walker.point_coordinates
    elif walker.first_coordinate is not None:
        summary["first_coordinate"] = walker.first_coordinate
    return summary, walker.pos


def classify_geometry(blob):
    """把一条 GeoPackageBinary 分类为 ok / empty / missing / unsupported / malformed。

    空几何（空标记或空结构）、缺失几何（NULL）与不支持的扩展分别标记；
    包络只是来源声明的显示范围，记录为包络，绝不用来猜测点的位置。
    """
    if blob is None:
        return "missing", {"reason": "geom_is_null"}
    blob = bytes(blob)
    if len(blob) < 8:
        return "malformed", {"reason": "gpkg_header_short", "length": len(blob)}
    if blob[:2] != b"GP":
        return "malformed", {"reason": "bad_magic"}
    details = {"version": blob[2], "flags": blob[3]}
    flags = blob[3]
    endian = "<" if flags & 0x01 else ">"
    details["srs_id"] = struct.unpack_from(endian + "i", blob, 4)[0]
    envelope_code = (flags >> 1) & 0x07
    if envelope_code > 4:
        return "unsupported", {**details, "reason": "reserved_envelope_indicator", "indicator": envelope_code}
    empty_flag = bool(flags & 0x10)
    details["empty_flag"] = empty_flag
    offset = 8
    envelope_length = ENVELOPE_LENGTHS[envelope_code]
    if len(blob) < offset + envelope_length + 5:
        return "malformed", {**details, "reason": "truncated"}
    if envelope_length:
        details["envelope"] = list(struct.unpack_from(endian + "d" * (envelope_length // 8), blob, offset))
        offset += envelope_length
    try:
        geometry, consumed = parse_wkb(blob, offset, len(blob))
    except UnsupportedGeometry as error:
        return "unsupported", {**details, "reason": str(error)}
    except MalformedGeometry as error:
        return "malformed", {**details, "reason": str(error)}
    if consumed != len(blob):
        return "malformed", {**details, "reason": "trailing_bytes"}
    details["wkb"] = geometry
    if empty_flag or geometry["empty"]:
        return "empty", details
    return "ok", details
